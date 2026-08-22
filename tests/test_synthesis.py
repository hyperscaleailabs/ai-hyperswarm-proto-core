import json
import re
from pathlib import Path

from hsai import ai, github, retrieval
from hsai.config import load_config
from hsai.governance import BlockReport, render_brief
from hsai.ledger import LedgerRecord
from hsai.models import ModelChoice
from hsai.practices import ADOPTED_HEADING, build_practice
from hsai.proc import Proc
from hsai.retrieval import PRIOR_ART_HEADING, PriorArt
from hsai.synthesis import (
    DEFAULT_MEMORY_MAX_CHARS,
    DEFAULT_NOVELTY_THRESHOLD,
    MEMORY_HEADING,
    ContextPack,
    LessonMemory,
    SynthesisMemory,
    build_prompt,
    gather_prior_art,
    goal_queries,
    is_novel,
    parse_ticket_specs,
    pick_rotation,
    synthesize,
)
from hsai.tickets import NO_PRIOR_ART, TicketSpec

REPO_ROOT = Path(__file__).resolve().parents[1]


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
  "size": "L", "goal_ids": ["G4"], "synthesis_rationale": "combines x+y+z"}]
```"""
    specs = parse_ticket_specs(output)
    assert len(specs) == 1
    spec = specs[0]
    assert spec.title == "feat: adaptive budget"
    assert spec.size == "L"
    assert "size:L" in spec.all_labels()
    assert len(spec.acceptance_criteria) == 3


def test_parse_handles_garbage():
    assert parse_ticket_specs("no json here") == []
    assert parse_ticket_specs("```json\nnot json\n```") == []


def test_parse_ticket_specs_reads_practice_ids():
    output = """PHASE 3:
```json
[{"title": "feat: adaptive budget", "problem": "p", "proposal": "pp",
  "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
  "size": "L", "goal_ids": ["G4"], "synthesis_rationale": "combines x+y+z",
  "practice_ids": ["openbmb-chatdev--session-durability"]}]
```"""
    specs = parse_ticket_specs(output)
    assert specs[0].practice_ids == ("openbmb-chatdev--session-durability",)
    assert "openbmb-chatdev--session-durability" in specs[0].render()


def test_parse_ticket_specs_omitting_practice_ids_still_parses():
    """Existing (pre-practices-registry) synthesis output must not break."""
    output = """PHASE 3:
```json
[{"title": "feat: adaptive budget", "problem": "p", "proposal": "pp",
  "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
  "size": "L", "goal_ids": ["G4"], "synthesis_rationale": "combines x+y+z"}]
```"""
    specs = parse_ticket_specs(output)
    assert specs[0].practice_ids == ()
    assert "practice_ids: -" in specs[0].render()


# --- adopted-practice registry in the prompt ----------------------------------

def test_prompt_includes_adopted_practices_and_do_not_repropose_instruction():
    cfg = _cfg()
    pack = ContextPack(repos=["a/b"], sections={"a/b": "digest"})
    practice = build_practice(
        title="session durability", source_project="OpenBMB/ChatDev",
        source_artifact="harness_design", evidence="PR #104",
    )
    prompt = build_prompt(cfg, pack, practices=[practice])
    assert ADOPTED_HEADING in prompt
    assert "session durability" in prompt and "OpenBMB/ChatDev" in prompt
    assert "do not" in ADOPTED_HEADING.lower() or "not re-propose" in prompt.lower()
    assert "practice_ids" in prompt

    # the heading survives with no practices recorded yet
    empty_prompt = build_prompt(cfg, pack)
    assert ADOPTED_HEADING in empty_prompt
    assert "no practices recorded yet" in empty_prompt.lower()


def test_synthesize_feeds_the_practices_registry_to_the_model(tmp_path):
    from hsai.knowledge import KnowledgeBase
    from hsai.practices import append

    cfg = _cfg()
    kb = KnowledgeBase.from_config(cfg, tmp_path)
    append(
        tmp_path,
        build_practice(
            title="cost accounting", source_project="assafelovic/gpt-researcher",
            source_artifact="source_code", evidence="PR #47",
        ),
        cfg=cfg,
    )
    assert kb.read_practices()  # sanity: the note is visible through the KB too

    runner = _plain_text_runner()
    synthesize(cfg, cycle_index=0, root=str(tmp_path), runner=runner, ai_runner=runner)
    claude_call = next(c for c in runner.calls if c[:1] == ["claude"])
    assert "cost accounting" in claude_call[2]
    assert ADOPTED_HEADING in claude_call[2]


# --- plain-text (non-JSON) CLI output must never break synthesis -------------

PLAIN_TEXT_OUTPUT = """PHASE 1 - DIVERGE: ten candidates considered.
PHASE 2 - REFLECT: three survived critique.
PHASE 3 - PRIORITIZE:
```json
[{"title": "feat: adaptive budget", "problem": "p", "proposal": "pp",
  "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
  "size": "L", "goal_ids": ["G4"], "synthesis_rationale": "combines x+y+z"}]
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
    assert res.rejected == []


# --- SynthesisMemory: what this loop already knows about its own state -------

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


def _write_lesson(root, name, *, outcome, title, lesson="Something."):
    directory = root / "knowledge" / "lessons"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(
        f"---\ntags:\n  - lesson\n  - outcome/{outcome}\n  - kind/implement\n"
        f"created: 2026-01-01\n---\n\n# {title}\n\n## Lesson learned\n{lesson}\n"
    )


def _write_ledger(root, records):
    path = root / "knowledge" / "ledger"
    path.mkdir(parents=True, exist_ok=True)
    (path / "iterations.jsonl").write_text(
        "\n".join(r.to_json() for r in records) + "\n"
    )


def _record(**kw):
    base = dict(
        iteration=1, block=7, ticket=1, kind="implement", tier="heavy", model="opus",
        wall_clock_seconds=60.0, attempts=1, outcome="merged",
    )
    return LedgerRecord(**{**base, **kw})


def test_memory_gather_collects_tickets_lessons_and_ledger_blocks(tmp_path):
    cfg = _cfg()
    _write_lesson(tmp_path, "2026-01-01-a", outcome="pass", title="Poll remote CI")
    _write_lesson(
        tmp_path, "2026-01-02-b", outcome="fail", title="Edit the workflows",
        lesson="The harness denies every write under .github/workflows.",
    )
    _write_ledger(tmp_path, [
        _record(block=6, tier="standard", outcome="failed", failure_class="ci-red"),
        _record(block=7),
        _record(block=7, iteration=2, tier="standard", outcome="failed",
                failure_class="review-blocked"),
    ])

    memory = SynthesisMemory.gather(cfg, root=str(tmp_path), runner=_memory_runner())

    assert [i.title for i in memory.open_tickets] == [
        "feat: lesson-retrieval memory", "ci: main is red - auto-heal",
    ]
    assert memory.closed_titles == ("feat: adaptive budget throttling",)
    # read_lessons() is oldest-first; the memory flips it so the newest lesson leads,
    # and only a recorded FAILURE carries its lesson text.
    assert memory.lessons == (
        LessonMemory("fail", "implement", "Edit the workflows",
                     "The harness denies every write under .github/workflows."),
        LessonMemory("pass", "implement", "Poll remote CI"),
    )
    # Ledger blocks, newest first, with the heavy-tier spend and outcome mix folded in.
    assert [b.block for b in memory.blocks] == [7, 6]
    assert memory.blocks[0].heavy_iterations == 1
    assert memory.blocks[0].merged_iterations == 1
    assert memory.blocks[0].failure_histogram == {"review-blocked": 1}

    prov = memory.provenance()
    assert (prov.lessons, prov.failed_lessons) == (2, 1)
    assert (prov.open_tickets, prov.closed_tickets, prov.blocks) == (2, 1, 2)
    assert "2 lesson(s) (1 recorded as fail)" in prov.summary()
    assert "2 ledger block(s) summarized" in prov.summary()


def test_memory_render_lists_every_source(tmp_path):
    cfg = _cfg()
    _write_lesson(tmp_path, "2026-01-02-b", outcome="fail", title="Edit the workflows",
                  lesson="The harness denies workflow writes.")
    _write_ledger(tmp_path, [_record(block=9)])
    memory = SynthesisMemory.gather(cfg, root=str(tmp_path), runner=_memory_runner())

    text = memory.render()
    assert "#40 feat: lesson-retrieval memory" in text
    assert "feat: adaptive budget throttling" in text
    assert "**fail**/implement - Edit the workflows" in text
    assert "why it failed: The harness denies workflow writes." in text
    assert "block 9:" in text and "heavy-tier=1" in text and "merged 1/1" in text


def test_memory_render_flags_blocked_and_needs_refinement_tickets(tmp_path):
    """A refused ticket is not free ground - the planner must see why."""
    cfg = _cfg()
    issues = [
        {"number": 50, "title": "feat: vague idea",
         "labels": [{"name": "needs-refinement"}], "assignees": [], "body": ""},
        {"number": 51, "title": "feat: stuck idea",
         "labels": [{"name": "blocked"}], "assignees": [], "body": ""},
    ]
    memory = SynthesisMemory.gather(
        cfg, root=str(tmp_path), runner=_memory_runner(open_issues=issues, closed_issues=[])
    )
    text = memory.render()
    assert "#50 feat: vague idea [needs-refinement] NEEDS-REFINEMENT" in text
    assert "#51 feat: stuck idea [blocked] BLOCKED" in text


def test_memory_render_degrades_to_a_placeholder_when_empty():
    assert SynthesisMemory().render() == "_(nothing recorded yet - this is an early cycle)_"


def test_memory_render_is_capped_and_truncates_oldest_first():
    many = tuple(
        LessonMemory("pass", "implement", f"lesson number {i} about something")
        for i in range(500)
    )  # newest first, as gather() emits them
    memory = SynthesisMemory(lessons=many)

    capped = memory.render(max_chars=400)
    assert len(capped) <= 400
    # The NEWEST entries survive; the oldest are the ones dropped, and the
    # elision is stated rather than left to look like "nothing else happened".
    assert "lesson number 0 about something" in capped
    assert "lesson number 499 about something" not in capped
    assert "older entries elided" in capped

    # A generous cap does not truncate content that already fits.
    small = SynthesisMemory(lessons=(LessonMemory("pass", "implement", "one short lesson"),))
    rendered = small.render(max_chars=DEFAULT_MEMORY_MAX_CHARS)
    assert "elided" not in rendered and not rendered.endswith("...")


def test_memory_render_drops_the_least_load_bearing_section_first():
    """Open ticket titles are the primary dedupe target: they outlive closed ones."""
    memory = SynthesisMemory(
        open_tickets=(github.Issue(number=1, title="feat: still open", labels=(), assignees=()),),
        closed_titles=tuple(f"feat: closed thing {i}" for i in range(50)),
    )
    capped = memory.render(max_chars=300)
    assert len(capped) <= 300
    assert "feat: still open" in capped


def test_memory_gathering_degrades_gracefully_when_gh_is_unavailable(tmp_path):
    """`gh` missing (exit 127, empty stdout) and an empty knowledge base must
    yield an empty memory section, never raise."""
    cfg = _cfg()

    def broken_runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        return Proc(cmd, 127, "", "gh: command not found")

    memory = SynthesisMemory.gather(cfg, root=str(tmp_path), runner=broken_runner)
    assert memory.open_tickets == ()
    assert memory.closed_titles == ()
    assert memory.lessons == ()
    assert memory.blocks == ()
    assert memory.render() == "_(nothing recorded yet - this is an early cycle)_"
    assert memory.provenance().summary().startswith("0 lesson(s)")

    # And synthesis itself must not abort because memory gathering came back empty:
    # it runs to completion (the model call still happens) rather than raising.
    res = synthesize(cfg, cycle_index=0, root=str(tmp_path), runner=broken_runner,
                      ai_runner=_plain_text_runner())
    assert res.rejected == []


def test_prompt_puts_memory_section_before_the_study_digest():
    cfg = _cfg()
    pack = ContextPack(repos=["a/b"], sections={"a/b": "digest of a/b"})
    memory = SynthesisMemory(closed_titles=("feat: something already shipped",))

    prompt = build_prompt(cfg, pack, memory)
    assert MEMORY_HEADING in prompt
    assert "feat: something already shipped" in prompt
    assert "Do NOT" in prompt or "DROPPED" in prompt   # an explicit instruction, not a hint
    assert prompt.index(MEMORY_HEADING) < prompt.index("Study digest of reference projects")

    # the heading survives even with nothing to report, so the planner always
    # knows this section exists
    assert MEMORY_HEADING in build_prompt(cfg, pack)
    assert "nothing recorded yet" in build_prompt(cfg, pack)


def test_prompt_names_a_recorded_failure_and_an_open_ticket(tmp_path):
    """The planner must be shown BOTH halves of its own history, and told what
    to do with them - not just handed a digest."""
    cfg = _cfg()
    _write_lesson(tmp_path, "2026-01-02-b", outcome="fail", title="Edit the workflows",
                  lesson="The harness denies every write under .github/workflows.")
    memory = SynthesisMemory.gather(cfg, root=str(tmp_path), runner=_memory_runner())
    pack = ContextPack(repos=["a/b"], sections={"a/b": "digest of a/b"})

    prompt = build_prompt(cfg, pack, memory)

    assert "**fail**/implement - Edit the workflows" in prompt          # a recorded failure
    assert "why it failed: The harness denies every write" in prompt    # and WHY it failed
    assert "#40 feat: lesson-retrieval memory" in prompt                # a currently-open ticket
    assert "Never re-propose the" in prompt
    assert "how it DIFFERS from that failure" in prompt


def test_synthesize_feeds_memory_to_the_model_within_the_character_cap():
    """The `hsai synthesize` path itself: the captured prompt carries the memory
    section, and that section respects the configured budget."""
    cfg = _cfg()
    cfg.synthesis["memory_max_chars"] = 300
    runner = _plain_text_runner()
    synthesize(cfg, cycle_index=0, root=str(REPO_ROOT), runner=runner, ai_runner=runner)

    prompt = next(c for c in runner.calls if c[:1] == ["claude"])[2]
    assert MEMORY_HEADING in prompt
    section = prompt.split(f"{MEMORY_HEADING} -", 1)[1].split(ADOPTED_HEADING, 1)[0]
    # The digest itself (everything after the instruction paragraph's colon).
    digest = section.split(":\n", 1)[1].strip()
    assert len(digest) <= 300


# --- is_novel(): pure normalized-title overlap --------------------------------

def _spec(title: str) -> TicketSpec:
    return TicketSpec(
        title=title, problem="p", proposal="pp",
        acceptance_criteria=("a", "b", "c"), verification_plan=("v1", "v2"),
    )


def test_is_novel_rejects_an_exact_title_match():
    verdict = is_novel(
        _spec("feat: adaptive budget throttling per tier"),
        ["feat: adaptive budget throttling per tier"],
    )
    assert verdict.novel is False
    assert verdict.matched == "feat: adaptive budget throttling per tier"
    assert "exact duplicate" in verdict.reason


def test_is_novel_rejects_a_prefix_only_difference():
    """`feat:` vs `refactor:` on an otherwise identical title is still a duplicate."""
    verdict = is_novel(
        _spec("refactor: adaptive budget throttling per tier"),
        ["feat: adaptive budget throttling per tier"],
    )
    assert verdict.novel is False
    assert verdict.matched == "feat: adaptive budget throttling per tier"


def test_is_novel_rejects_a_near_duplicate_above_the_threshold():
    """Same idea, reworded - high token overlap, not an exact or prefix match."""
    verdict = is_novel(
        _spec("feat: add exponential backoff to the retry queue for flaky checks"),
        ["feat: retry queue backoff for flaky CI checks"],
    )
    assert verdict.novel is False
    assert verdict.matched == "feat: retry queue backoff for flaky CI checks"
    assert verdict.score >= DEFAULT_NOVELTY_THRESHOLD
    assert "title overlap" in verdict.reason


def test_is_novel_accepts_a_genuinely_distinct_title():
    verdict = is_novel(
        _spec("feat: cost ledger visualization dashboard"),
        ["feat: retry queue backoff for flaky CI checks"],
    )
    assert verdict.novel is True
    assert verdict.matched == ""
    assert verdict.reason == ""
    assert bool(verdict) is True


def test_is_novel_threshold_is_configurable_and_documented():
    spec = _spec("feat: add exponential backoff to the retry queue for flaky checks")
    known = ["feat: retry queue backoff for flaky CI checks"]
    # A stricter threshold than the documented default accepts the same pair.
    assert DEFAULT_NOVELTY_THRESHOLD < 1.0
    assert is_novel(spec, known, threshold=0.99).novel is True


def test_is_novel_is_pure_and_checks_every_remembered_title():
    """Tickets AND lesson titles are all `known_titles()` - one flat check."""
    memory = SynthesisMemory(
        lessons=(LessonMemory("fail", "implement", "feat: adaptive budget throttling per tier"),),
    )
    known = memory.known_titles()
    spec = _spec("feat: adaptive budget throttling per tier")

    verdict = is_novel(spec, known)
    assert verdict.novel is False
    assert verdict.matched == "feat: adaptive budget throttling per tier"
    # Pure: neither argument is mutated, and a repeat call gives the same verdict.
    assert known == memory.known_titles()
    assert is_novel(spec, known) == verdict


# --- synthesize() drops duplicates before filing ------------------------------

DUPLICATE_AND_NOVEL_OUTPUT = """PHASE 1 ... PHASE 2 ... PHASE 3:
```json
[
  {"title": "feat: lesson-retrieval memory", "problem": "p", "proposal": "pp",
   "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
   "size": "M", "goal_ids": ["G4"], "synthesis_rationale": "combines x+y+z"},
  {"title": "feat: cost ledger visualization dashboard", "problem": "p", "proposal": "pp",
   "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
   "size": "M", "goal_ids": ["G1"], "synthesis_rationale": "combines a+b+c"},
  {"title": "feat: recall-weighted worker prompts", "problem": "p", "proposal": "pp",
   "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
   "size": "M", "goal_ids": ["G3"], "synthesis_rationale": "combines d+e+f"}
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
    assert [r.title for r in res.rejected] == ["feat: lesson-retrieval memory"]
    assert "feat: lesson-retrieval memory" in res.rejected[0].reason

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


TWO_IDENTICAL_SPECS = """PHASE 3:
```json
[
  {"title": "feat: signed provenance attestation per merged pull request",
   "problem": "p", "proposal": "pp",
   "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
   "size": "M", "goal_ids": ["G2"], "synthesis_rationale": "combines a+b+c"},
  {"title": "feat: signed provenance attestation per merged pull request",
   "problem": "p", "proposal": "pp",
   "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
   "size": "M", "goal_ids": ["G2"], "synthesis_rationale": "combines d+e+f"}
]
```"""


def test_synthesize_files_one_issue_when_the_model_proposes_the_same_spec_twice(tmp_path):
    """The gate also dedupes WITHIN a batch: an empty memory is no excuse for
    filing the same ticket twice."""
    runner = _prior_art_runner(TWO_IDENTICAL_SPECS)

    res = synthesize(_cfg(), cycle_index=0, root=str(tmp_path), runner=runner, ai_runner=runner)

    created = [c for c in runner.calls if c[:3] == ["gh", "issue", "create"]]
    assert len(created) == 1
    assert len(res.filed) == 1
    assert [r.title for r in res.rejected] == [
        "feat: signed provenance attestation per merged pull request"
    ]
    assert "exact duplicate" in res.rejected[0].reason


def test_provenance_and_rejection_reasons_reach_the_block_review_brief(tmp_path):
    """One seeded `fail` lesson + one open ticket: the architect must be able to
    read what the planner was shown AND what it suppressed."""
    cfg = _cfg()
    _write_lesson(tmp_path, "2026-01-02-b", outcome="fail", title="Edit the workflows",
                  lesson="The harness denies every write under .github/workflows.")
    runner = _duplicate_fixture_runner()

    res = synthesize(cfg, cycle_index=0, root=str(tmp_path), runner=runner, ai_runner=runner)

    assert res.memory.open_tickets == 2
    assert (res.memory.lessons, res.memory.failed_lessons) == (1, 1)
    assert res.rejected

    brief = render_brief(cfg, BlockReport(
        cycle_index=1,
        synthesized=list(res.filed),
        synthesis_memory=res.memory.summary(),
        synthesis_rejections=[r.line() for r in res.rejected],
    ))
    assert "1 lesson(s) (1 recorded as fail)" in brief
    assert "2 open + 0 recently closed ticket(s)" in brief
    assert "feat: lesson-retrieval memory - " in brief       # the suppressed candidate...
    assert "exact duplicate of prior work" in brief          # ...and why it was suppressed


# --- prior art: the planner reads its own knowledge base ----------------------

def _prior_art(note_name: str, title: str, outcome: str = "pass") -> PriorArt:
    return PriorArt(note_name=note_name, title=title, outcome=outcome, score=1.0)


def test_prompt_carries_a_prior_art_section_with_note_names_and_outcomes():
    cfg = _cfg()
    pack = ContextPack(
        repos=["a/b"],
        sections={"a/b": "digest of a/b"},
        prior_art=(
            _prior_art("2026-01-01-budget", "feat: adaptive budget", outcome="fail"),
            _prior_art("2026-01-02-recall", "feat: lesson recall"),
        ),
    )
    prompt = build_prompt(cfg, pack)

    assert PRIOR_ART_HEADING in prompt
    assert "[[2026-01-01-budget]] (fail) - feat: adaptive budget" in prompt
    assert "[[2026-01-02-recall]] (pass) - feat: lesson recall" in prompt
    # The planner is told to USE it, not just shown it.
    assert "CHECK IT AGAINST THE PRIOR ART" in prompt
    assert "duplicate-risk verdict" in prompt
    assert '"prior_art"' in prompt
    assert prompt.index(PRIOR_ART_HEADING) < prompt.index("Study digest of reference projects")


def test_prompt_prior_art_section_survives_an_empty_knowledge_base():
    prompt = build_prompt(_cfg(), ContextPack(repos=["a/b"], sections={"a/b": "d"}))
    assert PRIOR_ART_HEADING in prompt
    assert "No prior art found" in prompt


def test_goal_queries_cover_every_goal_in_core_yaml():
    cfg = _cfg()
    queries = goal_queries(cfg)
    assert len(queries) == len(cfg.goals)
    assert any("knowledge base" in q.lower() for q in queries)


def test_gather_prior_art_grounds_the_cycle_in_the_real_vault():
    """The committed knowledge base must produce citations for our own goals."""
    cfg = _cfg()
    art = gather_prior_art(cfg, retrieval.load_index(REPO_ROOT, cfg))
    assert art, "the real vault must yield prior art for the core goals"
    assert all(p.note_name for p in art)
    rendered = ContextPack(repos=[], sections={}, prior_art=art).render_prior_art()
    assert rendered.startswith("- [[")


def test_synthesize_feeds_prior_art_citations_from_the_real_vault_to_the_model():
    """The captured prompt cites this repo's own notes, by name and outcome."""
    cfg = _cfg()
    runner = _plain_text_runner()
    synthesize(cfg, cycle_index=0, root=str(REPO_ROOT), runner=runner, ai_runner=runner)

    prompt = next(c for c in runner.calls if c[:1] == ["claude"])[2]
    assert PRIOR_ART_HEADING in prompt
    section = prompt.split(PRIOR_ART_HEADING, 1)[1].split("Study digest", 1)[0]
    labels = re.findall(r"\[\[[^\]]+\]\] \(([^)]+)\)", section)
    assert labels, section
    # Every citation carries what the note recorded - an outcome for a lesson,
    # otherwise what kind of note it is.
    assert all(lbl.split("/")[0] in {"pass", "fail", "whitepaper", "adr"} for lbl in labels)


# --- filed tickets cite their prior art ---------------------------------------

FAILED_IDEA = (
    "Give every worker its own persistent vector memory of past runs so it can "
    "look up similar situations before acting, backed by an embedding store "
    "refreshed on every iteration."
)

RESTATED_AND_NOVEL_OUTPUT = """PHASE 1 ... PHASE 2 ... PHASE 3:
```json
[
  {"title": "feat: agent situation store", "problem": "PROBLEM",
   "proposal": "PROPOSAL",
   "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
   "size": "L", "goal_ids": ["G4"], "synthesis_rationale": "combines x+y+z",
   "prior_art": ["2026-01-04-vector-memory", "2099-12-31-invented"]},
  {"title": "feat: signed provenance attestation per merged pull request",
   "problem": "Downstream consumers cannot verify which model produced a diff.",
   "proposal": "Publish a signed attestation naming the model, ticket and PR.",
   "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
   "size": "M", "goal_ids": ["G2"], "synthesis_rationale": "combines a+b+c",
   "prior_art": []}
]
```""".replace("PROBLEM", FAILED_IDEA).replace("PROPOSAL", FAILED_IDEA)


def _prior_art_runner(output: str):
    calls: list[list[str]] = []
    issue_numbers = iter(range(700, 800))

    def runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        calls.append(list(cmd))
        if cmd[:1] == ["claude"]:
            return Proc(cmd, 0, output, "")
        if cmd[:3] == ["gh", "issue", "create"]:
            return Proc(cmd, 0, f"https://github.com/o/r/issues/{next(issue_numbers)}\n", "")
        return Proc(cmd, 0, "", "")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def _seed_vault(root) -> None:
    """A failed lesson plus an unrelated one - the smallest honest corpus."""
    directory = root / "knowledge" / "lessons"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "2026-01-04-vector-memory.md").write_text(
        "---\ntags:\n  - lesson\n  - outcome/fail\n  - kind/implement\n"
        "created: 2026-01-04\n---\n\n"
        "# feat: per-worker persistent vector memory of past runs\n\n"
        f"## Lesson learned\n{FAILED_IDEA} It was abandoned: the store never stayed fresh.\n"
    )
    (directory / "2026-01-05-quota.md").write_text(
        "---\ntags:\n  - lesson\n  - outcome/pass\n  - kind/implement\n"
        "created: 2026-01-05\n---\n\n# feat: quota ledger\n\n## Lesson learned\nCost telemetry.\n"
    )


def _created_bodies(runner) -> list[str]:
    return [
        c[c.index("--body") + 1] for c in runner.calls if c[:3] == ["gh", "issue", "create"]
    ]


def test_every_filed_ticket_carries_a_prior_art_section(tmp_path):
    _seed_vault(tmp_path)
    runner = _prior_art_runner(RESTATED_AND_NOVEL_OUTPUT)

    res = synthesize(_cfg(), cycle_index=0, root=str(tmp_path), runner=runner, ai_runner=runner)

    assert res.filed
    bodies = _created_bodies(runner)
    assert bodies and all("## Prior art" in b for b in bodies)
    for body in bodies:
        assert "[[" in body or NO_PRIOR_ART in body


def test_filed_wikilinks_resolve_to_notes_that_exist(tmp_path):
    """An invented citation would be a dead link in the Obsidian graph."""
    _seed_vault(tmp_path)
    runner = _prior_art_runner(RESTATED_AND_NOVEL_OUTPUT)
    synthesize(_cfg(), cycle_index=0, root=str(tmp_path), runner=runner, ai_runner=runner)

    on_disk = {p.stem for p in (tmp_path / "knowledge" / "lessons").glob("*.md")}
    for body in _created_bodies(runner):
        for name in re.findall(r"\[\[([^\]]+)\]\]", body):
            assert name in on_disk
    assert "2099-12-31-invented" not in "\n".join(_created_bodies(runner))


def test_a_ticket_with_no_prior_art_says_so_explicitly(tmp_path):
    """Empty vault: "we looked and found none" must be stated, not implied."""
    runner = _prior_art_runner(RESTATED_AND_NOVEL_OUTPUT)
    synthesize(_cfg(), cycle_index=0, root=str(tmp_path), runner=runner, ai_runner=runner)

    bodies = _created_bodies(runner)
    assert bodies
    assert all(f"## Prior art\n{NO_PRIOR_ART}" in b for b in bodies)


def test_synthesis_drops_a_candidate_that_restates_a_failed_lesson(tmp_path):
    _seed_vault(tmp_path)
    runner = _prior_art_runner(RESTATED_AND_NOVEL_OUTPUT)

    res = synthesize(_cfg(), cycle_index=0, root=str(tmp_path), runner=runner, ai_runner=runner)

    titles = [
        c[c.index("--title") + 1] for c in runner.calls if c[:3] == ["gh", "issue", "create"]
    ]
    assert "feat: agent situation store" not in titles
    assert "feat: signed provenance attestation per merged pull request" in titles
    assert res.risk_dropped == 1

    # Both halves of the verdict are recorded: the flag AND the decision.
    dropped = next(f for f in res.risk_flags if f.startswith("feat: agent situation store"))
    assert "drop" in dropped
    assert "[[2026-01-04-vector-memory]]" in dropped
    kept = next(f for f in res.risk_flags if f.startswith("feat: signed provenance"))
    assert "keep" in kept
