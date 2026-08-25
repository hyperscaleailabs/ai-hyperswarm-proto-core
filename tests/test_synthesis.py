import json
import re
from pathlib import Path

import pytest

from hsai import ai, retrieval
from hsai.config import load_config
from hsai.models import ModelChoice
from hsai.practices import (
    ADOPTED_HEADING,
    TARGETING_HEADING,
    PracticeRegistry,
    build_practice,
)
from hsai.proc import Proc
from hsai.retrieval import PRIOR_ART_HEADING, PriorArt
from hsai.synthesis import (
    DEFAULT_MEMORY_MAX_CHARS,
    DUPLICATE_JACCARD_THRESHOLD,
    MEMORY_HEADING,
    ContextPack,
    MemoryPack,
    build_prompt,
    evidence_gap,
    gather_prior_art,
    goal_queries,
    is_duplicate,
    parse_ticket_specs,
    parse_ticket_specs_detailed,
    pick_rotation,
    record_observed,
    synthesize,
)
from hsai.tickets import NO_PRIOR_ART, TicketSpec

REPO_ROOT = Path(__file__).resolve().parents[1]


def _cfg():
    return load_config()


# The rationale names three PINNED repos: anything less is refused by the
# evidence gate before it is ever filed (see synthesis.evidence_gap). Defined
# here because the gate's table-driven test needs it at collection time.
EVIDENCED_RATIONALE = (
    "langchain-ai/langchain contributes its callback layer at one choke point. "
    "OpenBMB/ChatDev contributes cheaper agents for review phases. "
    "assafelovic/gpt-researcher contributes persisted research memory."
)


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
        title="session durability", source_repo="OpenBMB/ChatDev",
        dimension="harness_design", evidence="PR #104",
    )
    prompt = build_prompt(cfg, pack, practices=[practice])
    assert ADOPTED_HEADING in prompt
    assert "session durability" in prompt and "OpenBMB/ChatDev" in prompt
    assert "do not" in ADOPTED_HEADING.lower() or "not re-propose" in prompt.lower()
    assert "practice_ids" in prompt

    # An empty registry renders NEITHER section: there is no settled ground to
    # avoid and no thin cell to aim at, so both would be pure prompt overhead.
    empty_prompt = build_prompt(cfg, pack)
    assert ADOPTED_HEADING not in empty_prompt
    assert TARGETING_HEADING not in empty_prompt


def test_synthesize_feeds_the_practices_registry_to_the_model(tmp_path):
    from hsai.knowledge import KnowledgeBase
    from hsai.practices import append

    cfg = _cfg()
    kb = KnowledgeBase.from_config(cfg, tmp_path)
    append(
        tmp_path,
        build_practice(
            title="cost accounting", source_repo="assafelovic/gpt-researcher",
            dimension="source_code", evidence="PR #47",
        ),
        cfg=cfg,
    )
    assert kb.read_practices()  # sanity: the note is visible through the KB too

    runner = _plain_text_runner()
    synthesize(cfg, cycle_index=0, root=str(tmp_path), runner=runner, ai_runner=runner)
    claude_call = next(c for c in runner.calls if c[:1] == ["claude"])
    assert "cost accounting" in claude_call[2]
    assert ADOPTED_HEADING in claude_call[2]


def test_prompt_targets_the_least_covered_areas_when_the_registry_is_non_empty():
    cfg = _cfg()
    pack = ContextPack(repos=["a/b"], sections={"a/b": "digest"})
    practice = build_practice(
        title="observability at one choke point", source_repo="langchain-ai/langchain",
        dimension="source_code", evidence="PR #94",
    )
    prompt = build_prompt(cfg, pack, practices=[practice])

    assert TARGETING_HEADING in prompt
    targeting = prompt.split(TARGETING_HEADING, 1)[1].split(PRIOR_ART_HEADING, 1)[0]
    # The one mined repo is NOT a target; untouched ones with their gaps are.
    assert "langchain-ai/langchain" not in targeting
    assert "FoundationAgents/MetaGPT" in targeting
    assert "issue_history" in targeting


def test_prompt_omits_observed_practices_from_the_already_adopted_list():
    """`observed` means "a ticket cites it", not "we ship it" - the open-ticket
    list already covers that, and listing it here would read as settled."""
    cfg = _cfg()
    pack = ContextPack(repos=["a/b"], sections={"a/b": "digest"})
    observed = build_practice(
        title="not yet proven", source_repo="microsoft/JARVIS",
        dimension="harness_design", evidence="cited by #77", status="observed", ticket=77,
    )
    prompt = build_prompt(cfg, pack, practices=[observed])

    # Both sections render (the registry is non-empty)...
    assert ADOPTED_HEADING in prompt and TARGETING_HEADING in prompt
    adopted = prompt.split(ADOPTED_HEADING, 1)[1].split(TARGETING_HEADING, 1)[0]
    # ...but the unproven claim is not presented as settled ground.
    assert "not yet proven" not in adopted
    # It still counts as coverage, so JARVIS is no longer an untouched target.
    targeting = prompt.split(TARGETING_HEADING, 1)[1].split(PRIOR_ART_HEADING, 1)[0]
    assert "microsoft/JARVIS" not in targeting


def test_pick_rotation_biases_toward_the_least_covered_repos():
    cfg = _cfg()
    top3 = [r.repo for r in cfg.reference_top10[:3]]
    assert pick_rotation(cfg, 0) == top3  # unbiased default: plain rank order

    # Mine the first three heavily; cycle 0 must now study somebody else.
    practices = [
        build_practice(
            title=f"p{i}", source_repo=repo, dimension="source_code", evidence="e",
        )
        for i, repo in enumerate(top3)
    ]
    biased = pick_rotation(cfg, 0, practices)
    assert biased != top3
    assert not set(biased) & set(top3)

    # Pure in (cycle_index, registry): same inputs, same answer, and registry
    # order cannot change it.
    assert biased == pick_rotation(cfg, 0, practices)
    assert biased == pick_rotation(cfg, 0, list(reversed(practices)))

    # Still a rotation: consecutive cycles keep covering the whole pinned set.
    seen: set[str] = set()
    for i in range(4):
        seen.update(pick_rotation(cfg, i, practices))
    assert len(seen) >= 10


# --- the evidence gate: a rationale must cite the pinned set ------------------

def _rationale_spec(rationale: str) -> TicketSpec:
    return TicketSpec(
        title="feat: something", problem="p", proposal="pp",
        acceptance_criteria=("a", "b", "c"), verification_plan=("v1", "v2"),
        synthesis_rationale=rationale,
    )


@pytest.mark.parametrize(
    "rationale, accepted",
    [
        # Three pinned repos by full slug - the contract the prompt states.
        (EVIDENCED_RATIONALE, True),
        # Bare project names are legitimate citations too.
        ("Combines MetaGPT, crewAI and llama_index.", True),
        # Exactly at the floor.
        ("From langchain-ai/langchain, OpenBMB/ChatDev and microsoft/JARVIS.", True),
        # One short of the floor.
        ("Combines MetaGPT and crewAI.", False),
        # Plausible projects that are not in the pinned set prove nothing.
        ("Combines AutoGen, BabyAGI and CAMEL.", False),
        # The classic unfalsifiable assertion.
        ("combines x+y+z", False),
        ("", False),
    ],
)
def test_evidence_gate_accepts_only_rationales_that_cite_the_pinned_set(rationale, accepted):
    cfg = _cfg()
    spec = _rationale_spec(rationale)
    gap = evidence_gap(spec, cfg)
    assert (gap == "") is accepted
    if not accepted:
        assert "needs >= 3" in gap


def test_parse_ticket_specs_drops_unevidenced_specs_and_records_why():
    cfg = _cfg()
    output = """```json
[
  {"title": "feat: evidenced", "problem": "p", "proposal": "pp",
   "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
   "synthesis_rationale": "EVIDENCED"},
  {"title": "feat: unevidenced", "problem": "p", "proposal": "pp",
   "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
   "synthesis_rationale": "combines three great projects"}
]
```""".replace("EVIDENCED", EVIDENCED_RATIONALE)

    # Without a cfg there is no pinned set to check against: parsing only.
    assert len(parse_ticket_specs(output)) == 2

    parsed = parse_ticket_specs_detailed(output, cfg)
    assert [s.title for s in parsed.specs] == ["feat: evidenced"]
    assert len(parsed.evidence_rejected) == 1
    reason = parsed.evidence_rejected[0]
    assert "feat: unevidenced" in reason and "names 0 pinned" in reason
    assert parse_ticket_specs(output, cfg) == parsed.specs


def test_synthesize_reports_evidence_rejections_instead_of_filing(tmp_path):
    """An unevidenced block files nothing and says exactly why."""
    unevidenced = PLAIN_TEXT_OUTPUT.replace(EVIDENCED_RATIONALE, "combines x+y+z")
    calls: list[list[str]] = []

    def runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        calls.append(list(cmd))
        return Proc(cmd, 0, unevidenced if cmd[:1] == ["claude"] else "", "")

    res = synthesize(_cfg(), cycle_index=0, root=str(tmp_path), runner=runner, ai_runner=runner)

    assert res.ok is False
    assert res.filed == []
    assert not any(c[:3] == ["gh", "issue", "create"] for c in calls)
    assert len(res.evidence_rejected) == 1
    assert "evidence gate" in res.error
    # Nothing unproven leaked into the registry either.
    assert not list((tmp_path / "knowledge" / "practices").glob("*.md"))


# --- observed practice notes, stamped with the ticket ------------------------

def test_record_observed_writes_one_note_per_cited_repo(tmp_path):
    cfg = _cfg()
    registry = PracticeRegistry(tmp_path, cfg)
    spec = _rationale_spec(
        "run-llama/llama_index contributes its sync-docs.yml workflow discipline. "
        "crewAIInc/crewAI contributes the commit subject feat(events): report project "
        "creation. OpenBMB/ChatDev contributes cheaper review agents."
    )

    recorded = record_observed(spec, 42, cfg, registry)

    assert {p.source_repo for p in recorded} == {
        "run-llama/llama_index", "crewAIInc/crewAI", "OpenBMB/ChatDev"
    }
    assert all(p.status == "observed" and p.ticket == 42 for p in recorded)
    # The dimension is inferred from the sentence that cited THAT repo.
    by_repo = {p.source_repo: p for p in recorded}
    assert by_repo["run-llama/llama_index"].dimension == "ci_cd"
    assert by_repo["crewAIInc/crewAI"].dimension == "commit_history"
    # ...and the evidence is that sentence, not the whole paragraph.
    assert "sync-docs.yml" in by_repo["run-llama/llama_index"].evidence
    assert "crewAIInc/crewAI" not in by_repo["run-llama/llama_index"].evidence

    assert registry.for_ticket(42) == sorted(recorded, key=lambda p: p.id)


def test_synthesize_stamps_observed_practices_with_the_filed_ticket(tmp_path):
    runner = _plain_text_runner()
    res = synthesize(_cfg(), cycle_index=0, root=str(tmp_path), runner=runner, ai_runner=runner)

    assert res.filed
    ticket = res.filed[0]
    registry = PracticeRegistry(tmp_path, _cfg())
    recorded = registry.for_ticket(ticket)
    assert len(recorded) == 3  # one per repo EVIDENCED_RATIONALE names
    assert {p.id for p in recorded} == set(res.observed)
    assert all(p.status == "observed" and p.adopted_pr is None for p in recorded)


# --- plain-text (non-JSON) CLI output must never break synthesis -------------

PLAIN_TEXT_OUTPUT = """PHASE 1 - DIVERGE: ten candidates considered.
PHASE 2 - REFLECT: three survived critique.
PHASE 3 - PRIORITIZE:
```json
[{"title": "feat: adaptive budget", "problem": "p", "proposal": "pp",
  "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
  "size": "L", "goal_ids": ["G4"], "synthesis_rationale": "RATIONALE"}]
```""".replace("RATIONALE", EVIDENCED_RATIONALE)


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


def test_synthesize_survives_output_without_a_json_envelope(tmp_path):
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
    res = synthesize(cfg, cycle_index=0, root=str(tmp_path), runner=runner, ai_runner=runner)
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


def test_synthesize_feeds_memory_to_the_model(tmp_path):
    cfg = _cfg()
    runner = _plain_text_runner()
    synthesize(cfg, cycle_index=0, root=str(tmp_path), runner=runner, ai_runner=runner)
    claude_call = next(c for c in runner.calls if c[:1] == ["claude"])
    assert MEMORY_HEADING in claude_call[2]


# --- is_duplicate(): pure normalized-title overlap ----------------------------

def _spec(title: str) -> TicketSpec:
    return TicketSpec(
        title=title, problem="p", proposal="pp",
        acceptance_criteria=("a", "b", "c"), verification_plan=("v1", "v2"),
    )


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


def test_synthesize_feeds_prior_art_citations_from_the_real_vault_to_the_model(monkeypatch):
    """The captured prompt cites this repo's own notes, by name and outcome."""
    cfg = _cfg()
    runner = _plain_text_runner()
    # This one reads the REAL vault (that is the point), so the registry write
    # is stubbed out - a prompt assertion must not leave notes in the repo.
    monkeypatch.setattr("hsai.synthesis.record_observed", lambda *a, **k: [])
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
   "size": "L", "goal_ids": ["G4"], "synthesis_rationale": "RATIONALE",
   "prior_art": ["2026-01-04-vector-memory", "2099-12-31-invented"]},
  {"title": "feat: signed provenance attestation per merged pull request",
   "problem": "Downstream consumers cannot verify which model produced a diff.",
   "proposal": "Publish a signed attestation naming the model, ticket and PR.",
   "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
   "size": "M", "goal_ids": ["G2"], "synthesis_rationale": "RATIONALE",
   "prior_art": []}
]
```""".replace("PROBLEM", FAILED_IDEA).replace("PROPOSAL", FAILED_IDEA).replace(
    "RATIONALE", EVIDENCED_RATIONALE
)


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
