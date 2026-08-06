"""Near-duplicate detection between a synthesized candidate and known tickets.

The gate that matters is behavioural, not numeric: a paraphrase of work this
repo already shipped must be withheld, and a genuinely new proposal must pass
through untouched. Both are asserted against a real shipped ticket (the durable
cycle journal) rendered exactly as `TicketSpec.render()` writes it, so the score
sees the same body dedupe will read back off GitHub.

Where a test needs an exact number it builds its own token sets rather than
leaning on prose, so a reworded fixture can never silently move a threshold.
"""
from __future__ import annotations

from dataclasses import replace

from hsai import dedupe
from hsai.config import load_config
from hsai.dedupe import (
    FILE,
    FLAG,
    SKIP,
    KnownTicket,
    Thresholds,
    acceptance_criteria,
    classify,
    similarity,
    tokenize,
)
from hsai.github import Issue
from hsai.tickets import TicketSpec

SHIPPED = TicketSpec(
    title="feat: durable cycle journal with idempotent --resume for interrupted blocks",
    problem="A crashed block loses its record and re-files everything on a re-run.",
    proposal="Append-only JSONL journal per block; every side-effecting step goes through once().",
    acceptance_criteria=(
        "every side-effecting cycle step appends exactly one journal record",
        "a resumed block replays recorded payloads instead of re-executing the step",
        "a terminal record closes the journal so --resume never picks it up again",
    ),
    verification_plan=("pytest tests/test_journal.py", "crash-and-resume test files nothing twice"),
    size="L",
)

# A restatement of SHIPPED: different words, same work.
PARAPHRASE = TicketSpec(
    title="feat: idempotent --resume for interrupted blocks via a durable cycle journal",
    problem="Interrupted blocks re-run completed steps because nothing durable records them.",
    proposal="Write an append-only per-block journal and replay it on resume.",
    acceptance_criteria=(
        "each side-effecting step of the cycle appends one record to the journal",
        "resuming a block replays the recorded payload rather than re-executing the step",
        "a terminal record closes the journal so it is never resumed twice",
    ),
    verification_plan=("pytest tests/test_journal.py",),
)

# Same subsystem, genuinely different problem.
OVERLAPPING = TicketSpec(
    title="feat: cycle journal compaction: resume across interrupted blocks",
    problem="Journals grow without bound and nothing compacts a finished block.",
    proposal="Compact terminal journals behind a retention window.",
    acceptance_criteria=(
        "terminal journal records older than the retention window are compacted "
        "into one summary record",
        "compaction never touches a journal that is still resumable",
        "the compacted journal still replays into the same block report",
    ),
    verification_plan=("pytest tests/test_journal.py",),
)

# Nothing to do with any of the above.
UNRELATED = TicketSpec(
    title="feat: persona-targeted article rendering with an accessibility audit",
    problem="Generated articles are never checked for reading level or alt text.",
    proposal="Score each article for reading level and require alt text on every figure.",
    acceptance_criteria=(
        "each generated article reports a reading-level score",
        "figures without alternative text fail the audit",
        "the audit result is embedded in the article frontmatter",
    ),
    verification_plan=("pytest tests/test_knowledge.py",),
)


def _known(spec: TicketSpec, number: int = 104, state: str = "closed") -> KnownTicket:
    """The known ticket as dedupe really sees it: parsed back out of an issue body."""
    return KnownTicket.from_issue(
        Issue(number=number, title=spec.title, labels=(), assignees=(), body=spec.render()),
        state=state,
    )


# --- normalization ------------------------------------------------------------
def test_tokenize_drops_commit_prefix_and_shared_vocabulary():
    tokens = tokenize("feat: durable cycle journal for the hsai loop")
    assert tokens == {"durable", "cycle", "journal"}
    assert "feat" not in tokens  # the conventional-commit prefix carries no topic
    assert "hsai" not in tokens and "loop" not in tokens  # every ticket says these


def test_acceptance_criteria_parsed_off_a_rendered_body():
    assert acceptance_criteria(SHIPPED.render()) == SHIPPED.acceptance_criteria


def test_acceptance_criteria_absent_is_not_an_error():
    assert acceptance_criteria("") == ()
    assert acceptance_criteria("## Problem\njust prose, no criteria") == ()


def test_scoring_is_deterministic():
    known = _known(SHIPPED)
    assert similarity(PARAPHRASE, known) == similarity(PARAPHRASE, known)


# --- the two bands that decide whether a candidate is filed -------------------
def test_paraphrase_of_a_shipped_ticket_scores_above_the_skip_threshold():
    score = similarity(PARAPHRASE, _known(SHIPPED))
    assert score >= Thresholds().skip, f"paraphrase scored only {score:.2f}"
    verdict = classify(PARAPHRASE, [_known(SHIPPED)])
    assert verdict.action == SKIP
    assert verdict.matched is not None and verdict.matched.number == 104
    assert "#104" in verdict.explain()


def test_unrelated_proposal_scores_below_the_file_threshold():
    score = similarity(UNRELATED, _known(SHIPPED))
    assert score < Thresholds().flag, f"unrelated proposal scored {score:.2f}"
    assert classify(UNRELATED, [_known(SHIPPED)]).action == FILE


def test_score_ranks_restatement_above_neighbour_above_stranger():
    """The ordering is the invariant; the thresholds only cut it into bands."""
    known = _known(SHIPPED)
    assert (
        similarity(UNRELATED, known)
        < similarity(OVERLAPPING, known)
        < similarity(PARAPHRASE, known)
    )


def test_mid_band_candidate_is_flagged_rather_than_skipped():
    """Between the bands: filed, but labeled so the architect makes the call.

    Token sets are explicit here so the expected score is arithmetic, not a
    property of how the prose above happens to be worded.
    """
    known = KnownTicket(number=42, title="feat: alpha beta gamma epsilon",
                        criteria=("alpha zeta",), state="open")
    spec = replace(UNRELATED, title="feat: alpha beta gamma delta",
                   acceptance_criteria=("alpha beta",))
    # titles overlap 3 of 5; criteria 1 of 3.
    score = similarity(spec, known)
    assert abs(score - (0.65 * (3 / 5) + 0.35 * (1 / 3))) < 1e-12
    assert Thresholds().flag <= score < Thresholds().skip
    verdict = classify(spec, [known])
    assert verdict.action == FLAG
    assert verdict.is_flag and not verdict.is_skip


def test_no_known_tickets_means_nothing_to_compare():
    verdict = classify(UNRELATED, [])
    assert verdict.action == FILE
    assert verdict.matched is None
    assert "no comparable ticket" in verdict.explain()


def test_best_match_wins_over_a_weaker_one():
    verdict = classify(PARAPHRASE, [_known(UNRELATED, number=7), _known(SHIPPED, number=104)])
    assert verdict.matched is not None and verdict.matched.number == 104


def test_criteria_free_ticket_falls_back_to_the_title_alone():
    """A pre-schema issue body must not dilute the score toward zero."""
    bare = KnownTicket(number=3, title=SHIPPED.title, criteria=(), state="closed")
    assert similarity(PARAPHRASE, bare) >= Thresholds().skip


# --- a flag annotates the candidate and never the match ----------------------
def test_annotate_body_links_back_and_keeps_the_candidate_intact():
    verdict = classify(PARAPHRASE, [_known(SHIPPED)])
    body = dedupe.annotate_body("## Problem\noriginal body", verdict)
    assert body.startswith("> **Possible duplicate of #104**")
    assert "## Problem\noriginal body" in body  # the candidate's own body survives
    assert "architect decides" in body
    assert "closed or edited" in body  # says out loud that nothing was auto-resolved


def test_annotate_body_is_a_noop_without_a_match():
    assert dedupe.annotate_body("body", classify(UNRELATED, [])) == "body"


def test_thresholds_are_config_driven():
    th = Thresholds.from_config(load_config())
    assert 0.0 < th.flag < th.skip <= 1.0
