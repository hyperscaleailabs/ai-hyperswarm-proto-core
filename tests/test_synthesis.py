import dataclasses
import json
import re
from pathlib import Path

from hsai import ai, github
from hsai.config import load_config
from hsai.models import ModelChoice
from hsai.proc import Proc
from hsai.recall import PRIOR_ART_HEADING, PriorArtItem, render_prior_art
from hsai.synthesis import (
    DEFAULT_MEMORY_MAX_CHARS,
    DUPLICATE_JACCARD_THRESHOLD,
    EXACT,
    MEMORY_HEADING,
    NEAR,
    ContextPack,
    MemoryPack,
    build_prompt,
    classify_duplicate,
    is_duplicate,
    parse_ticket_specs,
    pick_rotation,
    preview,
    reproposal_justification,
    screen_candidates,
    synthesize,
)
from hsai.tickets import TicketSpec


def _cfg():
    return load_config()


def test_rotation_covers_the_set_over_cycles():
    cfg = _cfg()
    seen: set[str] = set()
    for i in range(4):
        subset = pick_rotation(cfg, i)
        assert len(subset) == 3
        seen.update(subset)
    assert len(seen) >= 10  # 4 cycles x 3 repos wraps the whole top-10


def test_prompt_demands_combination_and_reflection():
    cfg = _cfg()
    pack = ContextPack(repos=["a/b"], sections={"a/b": "digest"})
    prompt = build_prompt(cfg, pack)
    assert "PHASE 1" in prompt and "PHASE 2" in prompt and "PHASE 3" in prompt
    assert "at least 3 different reference projects" in prompt or "combine" in prompt.lower()
    assert "acceptance_criteria" in prompt


def test_parse_ticket_specs_takes_last_json_block():
    output = """PHASE 1 ... PHASE 2 ...
```json
[{"wrong": "block"}]
```
PHASE 3:
```json
[{"title": "feat: adaptive budget", "problem": "p", "proposal": "pp",
  "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
  "size": "L", "goal_ids": ["G4"], "synthesis_rationale": "combines x+y+z",
  "prior_art": "ledger block 41339 shows 1425s per merged PR"}]
```"""
    specs = parse_ticket_specs(output)
    assert len(specs) == 1
    spec = specs[0]
    assert spec.title == "feat: adaptive budget"
    assert spec.size == "L"
    assert "size:L" in spec.all_labels()
    assert len(spec.acceptance_criteria) == 3
    assert spec.prior_art.startswith("ledger block 41339")


def test_parse_ticket_specs_tolerates_a_missing_prior_art_key():
    """A missing citation is a *screening* refusal with a reason, not a parse error.

    Dropping it at parse time would report "no parseable ticket specs" - the
    same message an unusable model reply produces - and hide the real problem.
    """
    output = """```json
[{"title": "feat: x", "problem": "p", "proposal": "pp",
  "acceptance_criteria": ["a", "b"], "verification_plan": ["v1"]}]
```"""
    specs = parse_ticket_specs(output)
    assert len(specs) == 1 and specs[0].prior_art == ""


def test_parse_handles_garbage():
    assert parse_ticket_specs("no json here") == []
    assert parse_ticket_specs("```json\nnot json\n```") == []


# --- plain-text (non-JSON) CLI output must never break synthesis -------------

PLAIN_TEXT_OUTPUT = """PHASE 1 - DIVERGE: ten candidates considered.
PHASE 2 - REFLECT: three survived critique.
PHASE 3 - PRIORITIZE:
```json
[{"title": "feat: adaptive budget", "problem": "p", "proposal": "pp",
  "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
  "size": "L", "goal_ids": ["G4"], "synthesis_rationale": "combines x+y+z",
  "prior_art": "ledger block 41339 spent 1425s on one merged PR"}]
```"""


def _plain_text_runner():
    """A `claude` that prints plain text - an older binary, or a crash."""
    calls: list[list[str]] = []
    issue_numbers = iter(range(321, 400))

    def runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        calls.append(list(cmd))
        if cmd[:1] == ["claude"]:
            return Proc(cmd, 0, PLAIN_TEXT_OUTPUT, "")
        if cmd[:3] == ["gh", "issue", "create"]:
            return Proc(cmd, 0, f"https://github.com/o/r/issues/{next(issue_numbers)}\n", "")
        return Proc(cmd, 0, "", "")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def test_synthesize_survives_output_without_a_json_envelope():
    """`payload is None` is a supported state, not a failure mode."""
    cfg = _cfg()
    runner = _plain_text_runner()

    # The CLI exposed no structured envelope at all...
    result = ai.run_agent(
        "p", ModelChoice(tier="heavy", model="opus", rationale="t"), cfg, runner=runner
    )
    assert result.payload is None and result.usage is None
    assert result.text == PLAIN_TEXT_OUTPUT      # falls back to raw stdout

    # ...and synthesis still parses its ticket specs off the raw text and files them.
    res = synthesize(cfg, cycle_index=0, runner=runner, ai_runner=runner)
    assert res.ok is True
    assert res.filed == [321]
    assert res.error == ""
    assert res.rejected == 0
    assert res.rejected_titles == []


# --- MemoryPack: what this loop already knows about its own state ------------

OPEN_ISSUES = [
    {
        "number": 40, "title": "feat: lesson-retrieval memory",
        "labels": [{"name": "self-improve"}, {"name": "priority:P2"}],
        "assignees": [], "body": "",
    },
    {
        "number": 41, "title": "ci: main is red - auto-heal",
        "labels": [{"name": "ci"}], "assignees": [], "body": "",
    },
]

CLOSED_ISSUES = [
    {
        "number": 30, "title": "feat: adaptive budget throttling",
        "labels": [{"name": "self-improve"}], "assignees": [], "body": "",
        "closedAt": "2026-08-01T00:00:00Z",
    },
]


def _memory_runner(*, open_issues=None, closed_issues=None):
    """A fake `gh` that answers both `issue list --state open` and `--state closed`."""

    def runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        if cmd[:3] == ["gh", "issue", "list"]:
            state = cmd[cmd.index("--state") + 1] if "--state" in cmd else "open"
            if state == "closed":
                data = CLOSED_ISSUES if closed_issues is None else closed_issues
            else:
                data = OPEN_ISSUES if open_issues is None else open_issues
            return Proc(cmd, 0, json.dumps(data), "")
        return Proc(cmd, 0, "", "")

    return runner


def _write_lesson(root, name, *, outcome, title):
    directory = root / "knowledge" / "lessons"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(
        f"---\ntags:\n  - lesson\n  - outcome/{outcome}\n  - kind/implement\n"
        f"created: 2026-01-01\n---\n\n# {title}\n\n## Lesson learned\nSomething.\n"
    )


def test_memory_pack_gather_collects_open_closed_and_lessons(tmp_path):
    cfg = _cfg()
    _write_lesson(tmp_path, "2026-01-01-a", outcome="pass", title="Poll remote CI")
    _write_lesson(tmp_path, "2026-01-02-b", outcome="fail", title="Edit the workflows")

    memory = MemoryPack.gather(cfg, root=str(tmp_path), runner=_memory_runner())

    assert [i.title for i in memory.open_tickets] == [
        "feat: lesson-retrieval memory", "ci: main is red - auto-heal",
    ]
    assert memory.closed_titles == ("feat: adaptive budget throttling",)
    # read_lessons() is oldest-first; the pack flips it so the newest lesson leads.
    assert memory.lessons == (("fail", "Edit the workflows"), ("pass", "Poll remote CI"))


def test_memory_pack_render_lists_all_three_sources(tmp_path):
    cfg = _cfg()
    _write_lesson(tmp_path, "2026-01-02-b", outcome="fail", title="Edit the workflows")
    memory = MemoryPack.gather(cfg, root=str(tmp_path), runner=_memory_runner())

    text = memory.render()
    assert "#40 feat: lesson-retrieval memory" in text
    assert "feat: adaptive budget throttling" in text
    assert "**fail** - Edit the workflows" in text


def test_memory_pack_render_degrades_to_a_placeholder_when_empty():
    assert MemoryPack().render() == "_(nothing recorded yet - this is an early cycle)_"


def test_memory_pack_render_is_hard_capped():
    many_lessons = tuple(("pass", f"lesson number {i} about something") for i in range(500))
    memory = MemoryPack(lessons=many_lessons)

    capped = memory.render(max_chars=200)
    assert len(capped) <= 200
    assert capped.endswith("...")

    # A generous cap does not truncate content that already fits.
    small = MemoryPack(lessons=(("pass", "one short lesson"),))
    assert not small.render(max_chars=DEFAULT_MEMORY_MAX_CHARS).endswith("...")


def test_memory_pack_gathering_degrades_gracefully_when_gh_is_unavailable(tmp_path):
    """`gh` missing (exit 127, empty stdout) and an empty knowledge base must
    yield an empty memory section, never raise."""
    cfg = _cfg()

    def broken_runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        return Proc(cmd, 127, "", "gh: command not found")

    memory = MemoryPack.gather(cfg, root=str(tmp_path), runner=broken_runner)
    assert memory.open_tickets == ()
    assert memory.closed_titles == ()
    assert memory.lessons == ()
    assert memory.render() == "_(nothing recorded yet - this is an early cycle)_"

    # And synthesis itself must not abort because memory gathering came back empty:
    # it runs to completion (the model call still happens) rather than raising.
    res = synthesize(cfg, cycle_index=0, root=str(tmp_path), runner=broken_runner,
                      ai_runner=_plain_text_runner())
    assert res.rejected == 0
    assert res.rejected_titles == []


def test_prompt_puts_memory_section_before_the_study_digest():
    cfg = _cfg()
    pack = ContextPack(repos=["a/b"], sections={"a/b": "digest of a/b"})
    memory = MemoryPack(closed_titles=("feat: something already shipped",))

    prompt = build_prompt(cfg, pack, memory)
    assert MEMORY_HEADING in prompt
    assert "feat: something already shipped" in prompt
    assert "Do NOT" in prompt or "DROPPED" in prompt   # an explicit instruction, not a hint
    assert prompt.index(MEMORY_HEADING) < prompt.index("Study digest of reference projects")

    # the heading survives even with nothing to report, so the planner always
    # knows this section exists
    assert MEMORY_HEADING in build_prompt(cfg, pack)
    assert "nothing recorded yet" in build_prompt(cfg, pack)


def test_synthesize_feeds_memory_to_the_model():
    cfg = _cfg()
    runner = _plain_text_runner()
    synthesize(cfg, cycle_index=0, root=".", runner=runner, ai_runner=runner)
    claude_call = next(c for c in runner.calls if c[:1] == ["claude"])
    assert MEMORY_HEADING in claude_call[2]


# --- is_duplicate(): pure normalized-title overlap ----------------------------

def _spec(title: str, prior_art: str = "grounded in #40") -> TicketSpec:
    return TicketSpec(
        title=title, problem="p", proposal="pp",
        acceptance_criteria=("a", "b", "c"), verification_plan=("v1", "v2"),
        prior_art=prior_art,
    )


def replace_prior_art(spec: TicketSpec, prior_art: str) -> TicketSpec:
    return dataclasses.replace(spec, prior_art=prior_art)


def test_is_duplicate_exact_title_match():
    memory = MemoryPack(closed_titles=("feat: adaptive budget throttling per tier",))
    dup, matched = is_duplicate(_spec("feat: adaptive budget throttling per tier"), memory)
    assert dup is True
    assert matched == "feat: adaptive budget throttling per tier"


def test_is_duplicate_prefix_only_difference():
    """`feat:` vs `refactor:` on an otherwise identical title is still a duplicate."""
    memory = MemoryPack(closed_titles=("feat: adaptive budget throttling per tier",))
    dup, matched = is_duplicate(
        _spec("refactor: adaptive budget throttling per tier"), memory
    )
    assert dup is True
    assert matched == "feat: adaptive budget throttling per tier"


def test_is_duplicate_genuine_near_duplicate():
    """Same idea, reworded - high token overlap, not an exact or prefix match."""
    memory = MemoryPack(closed_titles=("feat: retry queue backoff for flaky CI checks",))
    dup, matched = is_duplicate(
        _spec("feat: add exponential backoff to the retry queue for flaky checks"), memory
    )
    assert dup is True
    assert matched == "feat: retry queue backoff for flaky CI checks"


def test_is_duplicate_distinct_idea_is_not_rejected():
    memory = MemoryPack(closed_titles=("feat: retry queue backoff for flaky CI checks",))
    dup, matched = is_duplicate(_spec("feat: cost ledger visualization dashboard"), memory)
    assert dup is False
    assert matched == ""


def test_is_duplicate_threshold_is_configurable_and_documented():
    memory = MemoryPack(closed_titles=("feat: retry queue backoff for flaky CI checks",))
    spec = _spec("feat: add exponential backoff to the retry queue for flaky checks")
    # A stricter threshold than the documented default rejects the same pair.
    assert DUPLICATE_JACCARD_THRESHOLD < 1.0
    dup, _ = is_duplicate(spec, memory, threshold=0.99)
    assert dup is False


def test_is_duplicate_checks_open_and_closed_tickets_and_lessons():
    memory = MemoryPack(
        open_tickets=(),
        closed_titles=(),
        lessons=(("fail", "feat: adaptive budget throttling per tier"),),
    )
    dup, matched = is_duplicate(_spec("feat: adaptive budget throttling per tier"), memory)
    assert dup is True
    assert matched == "feat: adaptive budget throttling per tier"


# --- synthesize() drops duplicates before filing ------------------------------

DUPLICATE_AND_NOVEL_OUTPUT = """PHASE 1 ... PHASE 2 ... PHASE 3:
```json
[
  {"title": "feat: lesson-retrieval memory", "problem": "p", "proposal": "pp",
   "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
   "size": "M", "goal_ids": ["G4"], "synthesis_rationale": "combines x+y+z",
   "prior_art": "motivated by #40"},
  {"title": "feat: cost ledger visualization dashboard", "problem": "p", "proposal": "pp",
   "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
   "size": "M", "goal_ids": ["G1"], "synthesis_rationale": "combines a+b+c",
   "prior_art": "the ledger records 5 iterations with 0 token counts"},
  {"title": "feat: recall-weighted worker prompts", "problem": "p", "proposal": "pp",
   "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
   "size": "M", "goal_ids": ["G3"], "synthesis_rationale": "combines d+e+f",
   "prior_art": "[[2026-01-02-b]] shows recall never fired for heal tasks"}
]
```"""


def _duplicate_fixture_runner():
    calls: list[list[str]] = []
    issue_numbers = iter(range(500, 600))

    def runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        calls.append(list(cmd))
        if cmd[:1] == ["claude"]:
            return Proc(cmd, 0, DUPLICATE_AND_NOVEL_OUTPUT, "")
        if cmd[:3] == ["gh", "issue", "create"]:
            return Proc(cmd, 0, f"https://github.com/o/r/issues/{next(issue_numbers)}\n", "")
        if cmd[:3] == ["gh", "issue", "list"]:
            state = cmd[cmd.index("--state") + 1] if "--state" in cmd else "open"
            data = OPEN_ISSUES if state == "open" else []
            return Proc(cmd, 0, json.dumps(data), "")
        return Proc(cmd, 0, "", "")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def test_synthesize_drops_duplicates_and_files_only_the_survivors():
    """One of three candidates duplicates open ticket #40 - only two get filed."""
    cfg = _cfg()
    runner = _duplicate_fixture_runner()

    res = synthesize(cfg, cycle_index=0, root=".", runner=runner, ai_runner=runner)

    assert res.ok is True
    assert len(res.filed) == 2
    assert res.rejected == 1
    assert res.rejected_titles == ["feat: lesson-retrieval memory"]

    created_titles = [
        c[c.index("--title") + 1] for c in runner.calls if c[:3] == ["gh", "issue", "create"]
    ]
    assert "feat: lesson-retrieval memory" not in created_titles
    assert "feat: cost ledger visualization dashboard" in created_titles
    assert "feat: recall-weighted worker prompts" in created_titles


def test_synthesize_never_backfills_a_thin_block():
    """Filtering below `file_top` files only the survivors - no padding."""
    cfg = _cfg()
    assert int(cfg.synthesis.get("file_top", 3)) >= 2  # the fixture must be thinner than this
    runner = _duplicate_fixture_runner()

    res = synthesize(cfg, cycle_index=0, root=".", runner=runner, ai_runner=runner)
    assert len(res.filed) < int(cfg.synthesis.get("file_top", 3))
    assert len(res.filed) == 2


def test_synthesize_reports_prior_art_coverage_of_what_it_filed():
    """Every filed ticket cites internal evidence - and the count is reported."""
    cfg = _cfg()
    runner = _duplicate_fixture_runner()

    res = synthesize(cfg, cycle_index=0, root=".", runner=runner, ai_runner=runner)
    assert res.prior_art_cited == len(res.filed) == 2

    # ...and the citation survives into the rendered issue body.
    bodies = [
        c[c.index("--body") + 1] for c in runner.calls if c[:3] == ["gh", "issue", "create"]
    ]
    assert all("## Prior art (internal evidence)" in b for b in bodies)


# --- the prior-art prompt block ----------------------------------------------

_SAMPLE_ART = render_prior_art(
    [
        PriorArtItem(
            ref="[[2026-01-02-remote-ci-merge-gate]]", source="lesson", score=9.0,
            excerpt="Local CI passed while the remote rollup was still failing.",
            detail="outcome/fail",
        ),
        PriorArtItem(
            ref="#40", source="issue", score=4.0,
            excerpt="The planner is blind to the loop's own lessons.", detail="closed",
        ),
        PriorArtItem(
            ref="`ledger:block-41339`", source="ledger", score=2.0,
            excerpt="2 iterations, heavy-tier=2, 2820s wall-clock", detail="ledger",
        ),
    ],
    2500,
    cost="Cost pressure - latest ledger block 41339: 2 iterations. Budget verdict: ok.",
)


def test_prompt_carries_a_prior_art_section_with_citable_refs():
    cfg = _cfg()
    pack = ContextPack(
        repos=["a/b"], sections={"a/b": "digest of a/b"}, prior_art=_SAMPLE_ART
    )
    prompt = build_prompt(cfg, pack)

    assert "prior art" in prompt.lower()
    assert PRIOR_ART_HEADING in prompt
    # every ref is reproduced verbatim, so the model can cite it back
    for ref in ("[[2026-01-02-remote-ci-merge-gate]]", "#40", "`ledger:block-41339`"):
        assert ref in prompt
    # the three things the planner must optimise against
    assert "outcome/fail" in prompt          # what failed
    assert "closed" in prompt                # what shipped
    assert "Cost pressure" in prompt         # what a block costs
    # internal evidence comes before external study material
    assert prompt.index(PRIOR_ART_HEADING) < prompt.index("Study digest of reference")
    # and the schema demands the citation back
    assert '"prior_art"' in prompt
    assert "what changed" in prompt


def test_prior_art_heading_survives_an_empty_retrieval():
    """The planner always knows the section exists, even on a bare checkout."""
    prompt = build_prompt(_cfg(), ContextPack(repos=[], sections={}))
    assert PRIOR_ART_HEADING in prompt
    assert "nothing retrieved" in prompt


def test_prompt_stays_under_the_configured_cap_by_clipping_the_digest():
    cfg = _cfg()
    cfg.synthesis["max_prompt_chars"] = 12000
    sections = {f"o/r{i}": "study material. " * 900 for i in range(3)}
    pack = ContextPack(repos=list(sections), sections=sections, prior_art=_SAMPLE_ART)

    prompt = build_prompt(cfg, pack)

    assert len(prompt) <= 12000
    assert "study digest truncated" in prompt
    # the digest is what gives: the sections that must not be crowded out survive
    assert PRIOR_ART_HEADING in prompt and MEMORY_HEADING in prompt
    assert "[[2026-01-02-remote-ci-merge-gate]]" in prompt


def test_a_cap_below_the_fixed_instruction_degrades_instead_of_crashing():
    """The instruction text is a floor; an absurd cap empties the digest, not the prompt."""
    cfg = _cfg()
    cfg.synthesis["max_prompt_chars"] = 50
    pack = ContextPack(repos=["a/b"], sections={"a/b": "x" * 5000})

    prompt = build_prompt(cfg, pack)
    assert "PHASE 1" in prompt
    assert "x" * 100 not in prompt  # the digest is gone, the instruction is not


def test_the_shipped_config_prompt_fits_its_own_cap():
    """A smoke test against real data: the committed vault + ledger + config."""
    cfg = _cfg()
    root = Path(__file__).resolve().parents[1]

    def offline(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        return Proc(cmd, 127, "", "gh: command not found")

    prompt = preview(cfg, cycle_index=1, root=str(root), runner=offline)

    cap = int(cfg.synthesis["max_prompt_chars"])
    assert len(prompt) <= cap
    assert PRIOR_ART_HEADING in prompt
    # the excerpts are real notes on disk, not placeholders
    on_disk = {
        p.stem
        for sub in ("knowledge/lessons", "knowledge/whitepapers", "docs/adr")
        for p in (root / sub).glob("*.md")
    }
    cited = {ref.strip("[]") for ref in re.findall(r"\[\[[^\]]+\]\]", prompt)}
    assert cited, "prior art retrieved nothing from the committed vault"
    assert cited <= on_disk
    # the committed ledger is summarised as live cost pressure
    assert "Cost pressure - latest ledger block" in prompt


# --- screening: refuse exact duplicates, demote near ones --------------------

NOVEL = _spec("feat: cost ledger visualization dashboard")
NEAR_DUP = _spec("feat: add exponential backoff to the retry queue for flaky checks")
_PRIOR = "feat: retry queue backoff for flaky CI checks"


def test_classify_separates_exact_from_near_duplicates():
    memory = MemoryPack(closed_titles=(_PRIOR,))
    assert classify_duplicate(_spec(_PRIOR), memory).kind == EXACT
    # a different conventional-commit prefix is the same idea, not a near miss
    assert classify_duplicate(_spec("refactor: " + _PRIOR.split(": ", 1)[1]), memory).kind == EXACT
    near = classify_duplicate(NEAR_DUP, memory)
    assert near.kind == NEAR and 0.6 <= near.score < 1.0 and near.title == _PRIOR
    assert not classify_duplicate(NOVEL, memory)


def test_an_exact_duplicate_is_refused_with_its_reason_recorded():
    memory = MemoryPack(closed_titles=(_PRIOR,))
    screened = screen_candidates([_spec(_PRIOR), NOVEL], memory)

    assert [s.title for s in screened.accepted] == [NOVEL.title]
    assert screened.refused_titles == [_PRIOR]
    assert screened.refusal_lines == [f"{_PRIOR}: exact duplicate of prior work: {_PRIOR!r}"]
    assert screened.demoted == []


def test_a_near_duplicate_is_demoted_rather_than_dropped():
    memory = MemoryPack(closed_titles=(_PRIOR,))
    # the near-duplicate is emitted FIRST by the planner...
    screened = screen_candidates([NEAR_DUP, NOVEL], memory)

    # ...and survives, but ranked below the genuinely new idea
    assert [s.title for s in screened.accepted] == [NOVEL.title, NEAR_DUP.title]
    assert screened.demoted == [NEAR_DUP.title]
    assert screened.refusals == []


def test_demotion_bites_when_file_top_cannot_take_everyone():
    """Demotion is only meaningful against a cap - one slot goes to the new idea."""
    memory = MemoryPack(closed_titles=(_PRIOR,))
    screened = screen_candidates([NEAR_DUP, NOVEL], memory, file_top=1)
    assert [s.title for s in screened.accepted] == [NOVEL.title]
    # still reported as demoted, not as refused: nothing was found wrong with it
    assert screened.demoted == [NEAR_DUP.title] and screened.refusals == []


def test_a_ticket_without_internal_prior_art_is_refused():
    screened = screen_candidates(
        [_spec("feat: something new", prior_art=""),
         _spec("feat: something else", prior_art="we have learned this before"),
         NOVEL],
        MemoryPack(),
    )
    assert [s.title for s in screened.accepted] == [NOVEL.title]
    assert screened.refused_titles == ["", ""]  # a schema refusal matches no prior title
    assert "empty 'prior_art'" in screened.refusal_lines[0]
    assert "cites no internal artifact" in screened.refusal_lines[1]


# --- re-proposing a previously failed idea ------------------------------------

FAILED = "feat: adaptive budget throttling per tier"
_FAILED_MEMORY = MemoryPack(lessons=(("fail", FAILED),))

JUSTIFIED = TicketSpec(
    title=FAILED, problem="p", proposal="pp",
    acceptance_criteria=("a", "b", "c"), verification_plan=("v1", "v2"),
    prior_art=(
        "Retry of [[2026-01-01-adaptive-budget-throttling-per-tier]] (outcome/fail): "
        "the demotion never fired because the verdict was graded after the run. "
        "what changed: the gate now grades spend BEFORE selecting a tier."
    ),
)


def test_a_re_proposal_citing_the_prior_failure_is_accepted():
    assert reproposal_justification(JUSTIFIED, _FAILED_MEMORY) == FAILED
    screened = screen_candidates([JUSTIFIED], _FAILED_MEMORY)
    assert [s.title for s in screened.accepted] == [FAILED]
    assert screened.refusals == []


def test_a_re_proposal_that_does_not_say_what_changed_is_refused():
    silent = replace_prior_art(
        JUSTIFIED, "Retry of [[2026-01-01-adaptive-budget-throttling-per-tier]] (outcome/fail)."
    )
    assert reproposal_justification(silent, _FAILED_MEMORY) == ""
    screened = screen_candidates([silent], _FAILED_MEMORY)
    assert screened.accepted == []
    assert screened.refused_titles == [FAILED]


def test_a_re_proposal_that_cites_the_wrong_artifact_is_refused():
    misdirected = replace_prior_art(
        JUSTIFIED,
        "[[2026-01-01-obsidian-vault-layout]] is relevant. what changed: nothing much.",
    )
    assert reproposal_justification(misdirected, _FAILED_MEMORY) == ""
    assert screen_candidates([misdirected], _FAILED_MEMORY).accepted == []


def test_a_re_proposal_of_work_that_is_still_open_is_refused_regardless():
    """The exception is for failures we finished with, not for queued work."""
    queued = MemoryPack(
        open_tickets=(github.Issue(number=7, title=FAILED, labels=(), assignees=()),),
        lessons=(("fail", FAILED),),
    )
    screened = screen_candidates([JUSTIFIED], queued)
    assert screened.accepted == []
    assert screened.refused_titles == [FAILED]
