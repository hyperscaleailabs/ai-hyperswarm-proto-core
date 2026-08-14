import json

from hsai import ai, github
from hsai.config import load_config
from hsai.knowledge import FieldNote, KnowledgeBase, Lesson, Observation
from hsai.models import ModelChoice
from hsai.proc import Proc
from hsai.synthesis import (
    ADOPTION_HEADING,
    DEFAULT_MEMORY_MAX_CHARS,
    DUPLICATE_JACCARD_THRESHOLD,
    MEMORY_HEADING,
    AdoptionIndex,
    ContextPack,
    MemoryPack,
    build_context_pack,
    build_prompt,
    is_duplicate,
    parse_ticket_specs,
    pick_rotation,
    screen_specs,
    synthesize,
)
from hsai.tickets import TicketSpec, parse_practice_ids


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
    assert res.refused == []
    assert res.rejected == 0


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
    assert res.refused == []


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
    assert "open ticket #40" in res.refused[0].reason

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


# --- the deepened miner, and the field notes it persists ----------------------

WORKFLOW_BODY = "name: Issue Classifier\non:\n  issues:\n    types: [opened]\n"
CONTRIBUTING_BODY = "# Contributing\nSign the CLA, then open a draft PR.\n"


def _mining_runner(*, calls: list[list[str]] | None = None):
    """A fake `gh` that answers every endpoint the deepened miner reaches."""
    seen = calls if calls is not None else []

    def runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        seen.append(list(cmd))
        target = cmd[2] if len(cmd) > 2 else ""
        if target.endswith("/readme"):
            return Proc(cmd, 0, "# llama_index\nData framework for agents.\n", "")
        if "/commits" in target:
            return Proc(cmd, 0, "feat: add router\nfix: retry embeddings\n", "")
        if target.endswith("/contents/.github/workflows"):
            return Proc(cmd, 0, "issue_classifier.yml\nclose_new_integration_prs.yml\n", "")
        if "/contents/.github/workflows/" in target:
            return Proc(cmd, 0, WORKFLOW_BODY, "")
        if "/pulls?state=closed" in target:
            return Proc(cmd, 0, "fix: broken link [bug,triage]\nfeat: new tool [feature]\n", "")
        if target.endswith("/contents/CONTRIBUTING.md"):
            return Proc(cmd, 0, CONTRIBUTING_BODY, "")
        if target.endswith("/contents/.github/ISSUE_TEMPLATE"):
            return Proc(cmd, 0, "bug_report.yml\n", "")
        return Proc(cmd, 1, "", "not found")

    runner.calls = seen  # type: ignore[attr-defined]
    return runner


def test_context_pack_fetches_workflow_bodies_prs_and_contribution_policy():
    """Names alone say a workflow exists; the body says what it does."""
    calls: list[list[str]] = []
    pack = build_context_pack(["run-llama/llama_index"], runner=_mining_runner(calls=calls))

    section = pack.sections["run-llama/llama_index"]
    assert "issue_classifier.yml" in section
    assert "name: Issue Classifier" in section          # the BODY, not just the name
    assert "fix: broken link [bug,triage]" in section   # closed PR titles WITH labels
    assert "Sign the CLA" in section                    # CONTRIBUTING.md
    assert "bug_report.yml" in section                  # issue templates
    assert "feat: add router" in section                # and the pre-existing material

    # every fetch is a `gh api` read; the miner never writes to GitHub
    assert calls and all(c[:2] == ["gh", "api"] for c in calls)


def test_context_pack_sections_stay_inside_their_character_budget():
    """Three deep sections must not swamp the heavy prompt."""
    def fat_runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        return Proc(cmd, 0, "x" * 50000, "")

    pack = build_context_pack(["a/b"], runner=fat_runner, max_section_chars=1000)
    assert len(pack.sections["a/b"]) <= 1000
    assert pack.sections["a/b"].endswith("...")


def test_build_context_pack_appends_a_dated_entry_per_pass(tmp_path):
    """Mining twice appends; it never rewrites what the first pass recorded."""
    kb = KnowledgeBase(tmp_path)
    runner = _mining_runner()

    build_context_pack(["run-llama/llama_index"], runner=runner, kb=kb)
    note = kb.reference_dir / "run-llama-llama_index.md"
    first_pass = note.read_text()
    assert "`.github/workflows/issue_classifier.yml`" in first_pass
    assert "`CONTRIBUTING.md`" in first_pass

    build_context_pack(["run-llama/llama_index"], runner=runner, kb=kb)
    second_pass = note.read_text()
    assert second_pass.startswith(first_pass)  # byte-identical prefix
    assert len(second_pass) > len(first_pass)  # a new dated entry was appended
    record = kb.read_field_notes()[0]
    assert len(record.observed_dates) == 2 * len(record.practice_ids)


def test_build_context_pack_without_a_knowledge_base_writes_nothing(tmp_path):
    """The miner stays usable as a pure read - persistence is opt-in."""
    kb = KnowledgeBase(tmp_path)
    build_context_pack(["run-llama/llama_index"], runner=_mining_runner())
    assert kb.reference_notes() == []


# --- the adoption index: what we DID about what we saw ------------------------

def _lesson(kb, title, *, outcome, practices):
    kb.write_lesson(Lesson(
        title=title, outcome=outcome, kind="implement",
        context="c", what_happened="w", lesson="l", practices=practices,
    ))


def _open_issue(number, title, body, *, labels=("self-improve",)):
    return github.Issue(number=number, title=title, labels=tuple(labels),
                        assignees=(), body=body)


def test_adoption_index_buckets_merged_failed_and_in_flight(tmp_path):
    kb = KnowledgeBase(tmp_path)
    _lesson(kb, "implement: triage gate", outcome="pass", practices=("llama--triage",))
    _lesson(kb, "implement: freeze docs", outcome="fail", practices=("crewai--freeze",))
    kb.append_field_note(FieldNote(
        repo="openai/swarm",
        observations=(Observation(
            practice="handoff protocol", artifact="`README.md`", what="w",
            observed="2026-08-14", practice_id="swarm--handoff",
        ),),
    ))

    index = AdoptionIndex.build(kb=kb, open_issues=(
        _open_issue(77, "feat: metagpt news log",
                    "## Synthesis rationale\nx\n- practice_ids: `metagpt--news`\n"),
    ))

    assert index.status("llama--triage") == "adopted"
    assert index.status("crewai--freeze") == "failed"
    assert index.status("metagpt--news") == "in-flight"
    assert index.status("swarm--handoff") == ""       # observed, never acted on
    assert index.status("nobody--knows") == ""
    assert "swarm--handoff" in index.observed
    assert "#77" in index.evidence("metagpt--news")


def test_adoption_index_never_lets_an_open_ticket_mask_a_recorded_outcome(tmp_path):
    """"We shipped it" and "we tried and it failed" both outrank "someone is on it"."""
    kb = KnowledgeBase(tmp_path)
    _lesson(kb, "implement: triage gate", outcome="pass", practices=("llama--triage",))
    _lesson(kb, "implement: freeze docs", outcome="fail", practices=("crewai--freeze",))

    index = AdoptionIndex.build(kb=kb, open_issues=(
        _open_issue(80, "feat: again", "- practice_ids: `llama--triage`, `crewai--freeze`\n"),
    ))
    assert index.status("llama--triage") == "adopted"
    assert index.status("crewai--freeze") == "failed"
    assert index.in_flight == {}


def test_adoption_index_is_empty_without_a_knowledge_base():
    index = AdoptionIndex.build()
    assert index.adopted == {} and index.failed == {} and index.in_flight == {}
    assert "_(none)_" in index.render()


def test_prompt_names_adopted_failed_and_in_flight_practice_ids(tmp_path):
    """AC: the planner is told, by id, what not to re-file."""
    cfg = _cfg()
    pack = ContextPack(repos=["a/b"], sections={"a/b": "digest"})
    index = AdoptionIndex(
        adopted={"llama--triage": "[[2026-08-01-x]] (pass)"},
        failed={"crewai--freeze": "[[2026-08-02-y]] (fail)"},
        in_flight={"metagpt--news": "#77"},
        observed=("swarm--handoff",),
    )

    prompt = build_prompt(cfg, pack, MemoryPack(), index)

    assert ADOPTION_HEADING in prompt
    assert "llama--triage" in prompt and "Already adopted" in prompt
    assert "crewai--freeze" in prompt and "Already failed" in prompt
    assert "metagpt--news" in prompt and "Currently in flight" in prompt
    assert "swarm--handoff" in prompt          # observed but not yet acted on
    assert "practice_ids" in prompt            # the emitted spec must carry them
    # memory first, then adoption, then the study material it should judge
    assert prompt.index(MEMORY_HEADING) < prompt.index(ADOPTION_HEADING)
    assert prompt.index(ADOPTION_HEADING) < prompt.index("Study digest of reference projects")
    # the heading survives an empty index, so the planner always sees the section
    assert ADOPTION_HEADING in build_prompt(cfg, pack)


def test_adoption_section_is_hard_capped():
    index = AdoptionIndex(adopted={f"repo--practice-{i}": "[[note]]" for i in range(500)})
    capped = index.render(max_chars=300)
    assert len(capped) <= 300
    assert capped.endswith("...")


# --- the dedupe gate: accept and refuse branches ------------------------------

def _pid_spec(title: str, *practice_ids: str) -> TicketSpec:
    return TicketSpec(
        title=title, problem="p", proposal="pp",
        acceptance_criteria=("a", "b", "c"), verification_plan=("v1", "v2"),
        practice_ids=practice_ids,
    )


def test_gate_accepts_a_novel_spec_with_an_unseen_practice():
    index = AdoptionIndex(adopted={"llama--triage": "[[note]] (pass)"})
    spec = _pid_spec("feat: cost ledger dashboard", "swarm--handoff")

    survivors, refused = screen_specs([spec], MemoryPack(), index)

    assert survivors == [spec]
    assert refused == []


def test_gate_refuses_an_already_adopted_practice_with_a_reason():
    index = AdoptionIndex(adopted={"llama--triage": "[[2026-08-01-x]] (pass)"})
    spec = _pid_spec("feat: automatic proposal triage", "llama--triage")

    survivors, refused = screen_specs([spec], MemoryPack(), index)

    assert survivors == []
    assert len(refused) == 1
    assert refused[0].title == "feat: automatic proposal triage"
    assert refused[0].matched == "llama--triage"
    assert "already adopted" in refused[0].reason
    assert "2026-08-01-x" in refused[0].reason  # the evidence, not just the verdict


def test_gate_refuses_a_practice_that_is_already_in_flight():
    index = AdoptionIndex(in_flight={"metagpt--news": "#77"})
    survivors, refused = screen_specs(
        [_pid_spec("feat: dated project memory", "metagpt--news")], MemoryPack(), index
    )
    assert survivors == []
    assert "already in-flight" in refused[0].reason and "#77" in refused[0].reason


def test_gate_lets_a_failed_practice_be_re_proposed():
    """A failure is an argument for a different approach, not a permanent ban."""
    index = AdoptionIndex(failed={"crewai--freeze": "[[2026-08-02-y]] (fail)"})
    spec = _pid_spec("feat: append-only snapshots, second attempt", "crewai--freeze")

    survivors, refused = screen_specs([spec], MemoryPack(), index)

    assert survivors == [spec]
    assert refused == []


def test_gate_names_where_a_duplicate_title_came_from():
    memory = MemoryPack(open_tickets=(_open_issue(40, "feat: lesson-retrieval memory", ""),))
    survivors, refused = screen_specs(
        [_pid_spec("feat: lesson-retrieval memory", "brand--new")], memory, AdoptionIndex()
    )
    assert survivors == []
    assert "open ticket #40" in refused[0].reason
    assert refused[0].matched == "feat: lesson-retrieval memory"


def test_gate_checks_practice_ids_before_titles():
    """A reworded re-proposal is caught by its id even when the title sails past."""
    index = AdoptionIndex(adopted={"llama--triage": "[[note]] (pass)"})
    spec = _pid_spec("feat: something that shares no words at all", "llama--triage")

    _, refused = screen_specs([spec], MemoryPack(), index)
    assert refused[0].matched == "llama--triage"


# --- synthesize() end to end: refuse, report, file the rest -------------------

ADOPTED_AND_NOVEL_OUTPUT = """PHASE 3:
```json
[
  {"title": "feat: automatic proposal triage", "problem": "p", "proposal": "pp",
   "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
   "size": "M", "goal_ids": ["G4"], "synthesis_rationale": "combines x+y+z",
   "practice_ids": ["llama--triage"]},
  {"title": "feat: cost ledger visualization dashboard", "problem": "p", "proposal": "pp",
   "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
   "size": "M", "goal_ids": ["G1"], "synthesis_rationale": "combines a+b+c",
   "practice_ids": ["swarm--handoff"]}
]
```"""


def test_synthesize_refuses_an_adopted_practice_and_files_the_rest(tmp_path):
    """AC + verification plan: a spec whose practice is already adopted is
    refused with a reason and reported, never filed - and the survivor lands."""
    cfg = _cfg()
    kb = KnowledgeBase(tmp_path)
    _lesson(kb, "implement: inbound triage gate", outcome="pass",
            practices=("llama--triage",))
    created: list[tuple[str, str]] = []

    def runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        if cmd[:1] == ["claude"]:
            return Proc(cmd, 0, ADOPTED_AND_NOVEL_OUTPUT, "")
        if cmd[:3] == ["gh", "issue", "create"]:
            created.append((cmd[cmd.index("--title") + 1], cmd[cmd.index("--body") + 1]))
            return Proc(cmd, 0, "https://github.com/o/r/issues/902\n", "")
        if cmd[:3] == ["gh", "issue", "list"]:
            return Proc(cmd, 0, "[]", "")
        return Proc(cmd, 0, "", "")

    res = synthesize(cfg, cycle_index=0, root=str(tmp_path), runner=runner, ai_runner=runner)

    assert res.filed == [902]
    assert [t for t, _ in created] == ["feat: cost ledger visualization dashboard"]
    assert len(res.refused) == 1
    assert res.refused[0].title == "feat: automatic proposal triage"
    assert "already adopted" in res.refused[0].reason
    assert res.ok is True

    # the filed ticket carries its practice_ids, and they parse back out
    body = created[0][1]
    assert "- practice_ids: `swarm--handoff`" in body
    assert parse_practice_ids(body) == ("swarm--handoff",)
