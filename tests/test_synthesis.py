import json

from hsai import ai
from hsai.config import load_config
from hsai.knowledge import KnowledgeBase
from hsai.models import ModelChoice
from hsai.proc import Proc
from hsai.synthesis import (
    ADOPTION_HEADING,
    DEFAULT_MEMORY_MAX_CHARS,
    DUPLICATE_JACCARD_THRESHOLD,
    MEMORY_HEADING,
    PER_REPO_CHARS,
    WORKFLOW_CHARS,
    AdoptionIndex,
    ContextPack,
    MemoryPack,
    build_context_pack,
    build_prompt,
    is_duplicate,
    mine_repo,
    parse_ticket_specs,
    pick_rotation,
    refuse_reason,
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

    def runner(cmd, *, cwd=None, env=None, timeout=None, input_text=None):
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

    def runner(cmd, *, cwd=None, env=None, timeout=None, input_text=None):
        if cmd[:3] == ["gh", "issue", "list"]:
            state = cmd[cmd.index("--state") + 1] if "--state" in cmd else "open"
            if state == "closed":
                data = CLOSED_ISSUES if closed_issues is None else closed_issues
            else:
                data = OPEN_ISSUES if open_issues is None else open_issues
            return Proc(cmd, 0, json.dumps(data), "")
        return Proc(cmd, 0, "", "")

    return runner


def _write_lesson(root, name, *, outcome, title, practices=()):
    directory = root / "knowledge" / "lessons"
    directory.mkdir(parents=True, exist_ok=True)
    practice_block = (
        "practices:\n" + "".join(f"  - {p}\n" for p in practices) if practices else ""
    )
    (directory / f"{name}.md").write_text(
        f"---\ntags:\n  - lesson\n  - outcome/{outcome}\n  - kind/implement\n"
        f"created: 2026-01-01\n{practice_block}---\n\n# {title}\n\n"
        "## Lesson learned\nSomething.\n"
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

    def broken_runner(cmd, *, cwd=None, env=None, timeout=None, input_text=None):
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

def _spec(title: str, practice_ids: tuple[str, ...] = ()) -> TicketSpec:
    return TicketSpec(
        title=title, problem="p", proposal="pp",
        acceptance_criteria=("a", "b", "c"), verification_plan=("v1", "v2"),
        practice_ids=practice_ids,
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

    def runner(cmd, *, cwd=None, env=None, timeout=None, input_text=None):
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


# --- the miner: artifacts deep enough to carry a practice ---------------------

WORKFLOW_BODY = "name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"


def _mining_runner(*, workflow_body=WORKFLOW_BODY):
    """A fake `gh` that answers every fetch `mine_repo` makes."""

    def runner(cmd, *, cwd=None, env=None, timeout=None, input_text=None):
        if cmd[:3] == ["gh", "pr", "list"]:
            return Proc(cmd, 0, "fix: race in planner [bug,size:S]\n", "")
        if cmd[:2] != ["gh", "api"]:
            return Proc(cmd, 0, "", "")
        path = cmd[2]
        bodies = {
            "/readme": "# crewAI\n\nRole-playing autonomous agents.\n",
            "/contents/.github/workflows": "ci.yml\n",
            "/contents/.github/workflows/ci.yml": workflow_body,
            "/contents/CONTRIBUTING.md": "## How to contribute\nRun the tests first.\n",
            "/contents/.github/ISSUE_TEMPLATE": "bug.yml\n",
            "/contents/.github/ISSUE_TEMPLATE/bug.yml": "name: Bug\ndescription: report it\n",
        }
        if "/commits?" in path:
            return Proc(cmd, 0, "feat: add memory\n[docs-freeze] snapshot the docs\n", "")
        for suffix, body in bodies.items():
            if path.endswith(suffix):
                return Proc(cmd, 0, body, "")
        return Proc(cmd, 1, "", "Not Found")

    return runner


def test_miner_reads_workflow_bodies_closed_prs_and_contributing():
    """Names alone say a project has CI; bodies say what its CI actually does."""
    artifacts = mine_repo("crewAIInc/crewAI", runner=_mining_runner())
    by_kind = {a.kind: a for a in artifacts}

    assert set(by_kind) == {
        "readme", "commits", "workflow", "prs", "contributing", "issue-template",
    }
    assert by_kind["workflow"].name == ".github/workflows/ci.yml"
    assert "runs-on: ubuntu-latest" in by_kind["workflow"].text  # the BODY, not the name
    assert "[bug,size:S]" in by_kind["prs"].text                 # labels, not just titles
    assert by_kind["contributing"].name == "CONTRIBUTING.md"
    assert by_kind["issue-template"].name == ".github/ISSUE_TEMPLATE/bug.yml"


def test_practice_ids_are_readable_and_survive_a_moved_file():
    """The id keys on the artifact's NAME, not its path, so a move is not an orphan."""
    artifacts = {a.kind: a for a in mine_repo("crewAIInc/crewAI", runner=_mining_runner())}
    assert artifacts["commits"].practice_id("crewAIInc/crewAI") == "crewaiinc-crewai-commits"
    assert (
        artifacts["workflow"].practice_id("crewAIInc/crewAI")
        == "crewaiinc-crewai-workflow-ci-yml"
    )
    assert (
        artifacts["issue-template"].practice_id("crewAIInc/crewAI")
        == "crewaiinc-crewai-issue-template-bug-yml"
    )


def test_miner_degrades_artifact_by_artifact():
    """One 404 costs its own artifact and nothing else."""

    def readme_only(cmd, *, cwd=None, env=None, timeout=None, input_text=None):
        if cmd[:2] == ["gh", "api"] and cmd[2].endswith("/readme"):
            return Proc(cmd, 0, "# just a readme\n", "")
        return Proc(cmd, 1, "", "Not Found")

    artifacts = mine_repo("a/b", runner=readme_only)
    assert [a.kind for a in artifacts] == ["readme"]


def test_mined_artifacts_are_bounded_at_the_source_and_per_repo():
    """A deeper study must not let one enormous artifact swallow the prompt."""
    runner = _mining_runner(workflow_body="x" * 50_000)

    workflow = next(a for a in mine_repo("a/b", runner=runner) if a.kind == "workflow")
    assert len(workflow.text) <= WORKFLOW_CHARS
    assert workflow.text.endswith("...")

    pack = build_context_pack(["a/b"], runner=runner)
    assert len(pack.sections["a/b"]) <= PER_REPO_CHARS


# --- mining persists: field notes accumulate across passes --------------------

def test_mining_writes_a_field_note_and_a_second_pass_only_appends(tmp_path):
    """The verification plan, executed: mine twice, diff the note.

    Pass two re-reads an UNCHANGED project and must add nothing; pass three sees
    a drifted workflow and appends a new dated entry, leaving pass one's bytes
    byte-identical. That is what makes the notes durable memory rather than a
    cache that the newest cycle overwrites.
    """
    cfg = _cfg()
    kb = KnowledgeBase(tmp_path)
    repos = ["crewAIInc/crewAI"]

    pack = build_context_pack(
        repos, runner=_mining_runner(), kb=kb,
        catalog=cfg.reference_top10, snapshot_date="2026-07-25",
    )
    note = kb.reference_dir / "crewaiinc-crewai.md"
    first = note.read_text()
    assert pack.notes["crewAIInc/crewAI"], "the first pass records what it saw"
    # the digest names each artifact's practice_id, so the planner can cite it
    assert (
        "practice_id: `crewaiinc-crewai-workflow-ci-yml`" in pack.sections["crewAIInc/crewAI"]
    )
    assert "repo: crewAIInc/crewAI" in first
    assert "stars: 56129" in first          # metadata comes from the pinned catalog
    assert "snapshot_date: 2026-07-25" in first
    assert "- artifact: `.github/workflows/ci.yml`" in first

    second = build_context_pack(
        repos, runner=_mining_runner(), kb=kb,
        catalog=cfg.reference_top10, snapshot_date="2026-07-25",
    )
    assert second.notes["crewAIInc/crewAI"] == []
    assert note.read_text() == first        # nothing rewritten, nothing duplicated

    third = build_context_pack(
        repos,
        runner=_mining_runner(workflow_body="name: CI\non: [push, schedule]\n"),
        kb=kb, catalog=cfg.reference_top10, snapshot_date="2026-07-25",
    )
    assert len(third.notes["crewAIInc/crewAI"]) == 1
    text = note.read_text()
    assert text.startswith(first)           # pass one is frozen
    assert "runs-on: ubuntu-latest" in text and "push, schedule" in text


def test_mining_without_a_knowledge_base_records_nothing(tmp_path):
    """`build_context_pack` stays usable as a pure read (tests, spot checks)."""
    pack = build_context_pack(["a/b"], runner=_mining_runner())
    assert pack.sections["a/b"]        # it still studies the project...
    assert pack.notes == {}            # ...it just has nowhere to remember it
    assert list(tmp_path.iterdir()) == []


# --- the adoption index: what we already did about what we saw ----------------

def _kb_with_field_note(tmp_path):
    kb = KnowledgeBase(tmp_path)
    build_context_pack(
        ["crewAIInc/crewAI"], runner=_mining_runner(), kb=kb,
        catalog=_cfg().reference_top10,
    )
    return kb


def _practice_ticket(number, title, practice_id):
    return {
        "number": number, "title": title,
        "labels": [{"name": "self-improve"}], "assignees": [],
        "body": f"## Synthesis rationale\nbecause\n\n"
                f"**Practice IDs** (see `knowledge/reference/`): `{practice_id}`\n",
    }


def test_adoption_index_reads_field_notes_lessons_and_open_tickets(tmp_path):
    kb = _kb_with_field_note(tmp_path)
    _write_lesson(tmp_path, "2026-01-01-a", outcome="pass", title="Merged one",
                  practices=("crewaiinc-crewai-workflow-ci-yml",))
    _write_lesson(tmp_path, "2026-01-02-b", outcome="fail", title="Lost one",
                  practices=("crewaiinc-crewai-contributing",))
    memory = MemoryPack.gather(
        _cfg(), root=str(tmp_path),
        runner=_memory_runner(open_issues=[
            _practice_ticket(77, "feat: in flight", "crewaiinc-crewai-prs"),
        ]),
    )

    index = AdoptionIndex.build(kb, memory)

    assert index.status("crewaiinc-crewai-workflow-ci-yml") == ("adopted", "[[2026-01-01-a]]")
    assert index.status("crewaiinc-crewai-contributing")[0] == "failed"
    assert index.status("crewaiinc-crewai-prs") == ("in flight", "#77")
    assert index.status("never-seen-this") == ("", "")
    # every practice the field note recorded is known, adopted or not
    known = dict(index.known)
    assert known["crewaiinc-crewai-workflow-ci-yml"] == "crewAIInc/crewAI"


def test_adoption_index_degrades_to_empty_on_a_bare_vault(tmp_path):
    index = AdoptionIndex.build(KnowledgeBase(tmp_path))
    assert index.adopted == () and index.failed == () and index.in_flight == ()
    assert "no practice has been recorded yet" in index.render()


def test_adoption_index_renders_all_three_states():
    index = AdoptionIndex(
        adopted=(("p-merged", "[[note-a]]"),),
        failed=(("p-lost", "[[note-b]]"),),
        in_flight=(("p-open", "#77"),),
        known=(("p-merged", "a/b"), ("p-unused", "a/b")),
    )
    text = index.render()
    assert "`p-merged` ([[note-a]])" in text and "ADOPTED" in text
    assert "`p-open` (#77)" in text and "IN FLIGHT" in text
    assert "`p-lost` ([[note-b]])" in text and "FAILED" in text
    assert index.render(max_chars=40).endswith("...")


def test_prompt_carries_the_adoption_index(tmp_path):
    """The acceptance criterion, on the rendered prompt text itself."""
    cfg = _cfg()
    pack = ContextPack(repos=["a/b"], sections={"a/b": "digest of a/b"})
    index = AdoptionIndex(
        adopted=(("crewaiinc-crewai-commits", "[[2026-01-01-a]]"),),
        failed=(("crewaiinc-crewai-contributing", "[[2026-01-02-b]]"),),
        in_flight=(("crewaiinc-crewai-prs", "#77"),),
    )

    prompt = build_prompt(cfg, pack, MemoryPack(), index)

    assert ADOPTION_HEADING in prompt
    assert "`crewaiinc-crewai-commits`" in prompt      # already adopted
    assert "`crewaiinc-crewai-contributing`" in prompt  # already failed
    assert "`crewaiinc-crewai-prs`" in prompt           # in flight
    assert "REFUSED" in prompt                          # an instruction, not a hint
    assert "practice_ids" in prompt                     # and the spec must carry them
    assert prompt.index(ADOPTION_HEADING) < prompt.index("Study digest of reference projects")

    # the section survives with nothing recorded, so the planner always sees it
    assert ADOPTION_HEADING in build_prompt(cfg, pack)


# --- the dedupe gate: accept, refuse, and always report -----------------------

def test_refuse_reason_accepts_a_genuinely_new_idea():
    index = AdoptionIndex(adopted=(("p-merged", "[[note-a]]"),))
    spec = _spec("feat: cost ledger dashboard", practice_ids=("p-fresh",))
    assert refuse_reason(spec, MemoryPack(), index) is None


def test_refuse_reason_rejects_an_already_adopted_practice():
    index = AdoptionIndex(adopted=(("p-merged", "[[note-a]]"),))
    spec = _spec("feat: something worded completely differently",
                 practice_ids=("p-merged",))
    refusal = refuse_reason(spec, MemoryPack(), index)
    assert refusal is not None
    assert refusal.matched == "p-merged"
    assert "already adopted" in refusal.reason and "[[note-a]]" in refusal.reason


def test_refuse_reason_rejects_a_practice_already_in_flight():
    index = AdoptionIndex(in_flight=(("p-open", "#77"),))
    refusal = refuse_reason(_spec("feat: brand new words", practice_ids=("p-open",)),
                            MemoryPack(), index)
    assert refusal is not None and "already in flight (#77)" in refusal.reason


def test_refuse_reason_allows_retrying_a_failed_practice():
    """A practice we tried and lost may deserve a better design, not silence."""
    index = AdoptionIndex(failed=(("p-lost", "[[note-b]]"),))
    assert refuse_reason(_spec("feat: retry it properly", practice_ids=("p-lost",)),
                         MemoryPack(), index) is None


def test_refuse_reason_falls_back_to_title_similarity():
    memory = MemoryPack(closed_titles=("feat: retry queue backoff for flaky CI checks",))
    refusal = refuse_reason(
        _spec("feat: add exponential backoff to the retry queue for flaky checks"),
        memory, AdoptionIndex(),
    )
    assert refusal is not None
    assert "near-duplicate" in refusal.reason
    assert refusal.matched == "feat: retry queue backoff for flaky CI checks"


ADOPTED_PRACTICE_OUTPUT = """PHASE 3:
```json
[
  {"title": "feat: re-file the practice we already shipped", "problem": "p",
   "proposal": "pp", "acceptance_criteria": ["a", "b", "c"],
   "verification_plan": ["v1", "v2"], "size": "M", "goal_ids": ["G1"],
   "synthesis_rationale": "combines x+y+z",
   "practice_ids": ["crewaiinc-crewai-workflow-ci-yml"]},
  {"title": "feat: genuinely novel telemetry heatmap", "problem": "p",
   "proposal": "pp", "acceptance_criteria": ["a", "b", "c"],
   "verification_plan": ["v1", "v2"], "size": "M", "goal_ids": ["G4"],
   "synthesis_rationale": "combines a+b+c",
   "practice_ids": ["crewaiinc-crewai-commits"]}
]
```"""


def _adoption_fixture_runner():
    calls: list[list[str]] = []

    def runner(cmd, *, cwd=None, env=None, timeout=None, input_text=None):
        calls.append(list(cmd))
        if cmd[:1] == ["claude"]:
            return Proc(cmd, 0, ADOPTED_PRACTICE_OUTPUT, "")
        if cmd[:3] == ["gh", "issue", "create"]:
            return Proc(cmd, 0, "https://github.com/o/r/issues/700\n", "")
        if cmd[:3] == ["gh", "issue", "list"]:
            return Proc(cmd, 0, "[]", "")
        return _mining_runner()(cmd)

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def test_synthesize_refuses_an_already_adopted_practice_and_files_the_rest(tmp_path):
    """`hsai synthesize --index 0` against a spec whose practice already merged."""
    cfg = _cfg()
    kb = _kb_with_field_note(tmp_path)
    assert kb.reference_notes() == ["crewaiinc-crewai"]
    _write_lesson(tmp_path, "2026-01-01-a", outcome="pass", title="Already shipped it",
                  practices=("crewaiinc-crewai-workflow-ci-yml",))
    runner = _adoption_fixture_runner()

    res = synthesize(cfg, cycle_index=0, root=str(tmp_path), runner=runner, ai_runner=runner)

    assert res.filed == [700]
    assert res.ok is True
    assert len(res.refused) == 1
    refusal = res.refused[0]
    assert refusal.title == "feat: re-file the practice we already shipped"
    assert "already adopted" in refusal.reason
    assert res.rejected == 1 and res.rejected_titles == ["crewaiinc-crewai-workflow-ci-yml"]

    created = [
        c[c.index("--title") + 1] for c in runner.calls if c[:3] == ["gh", "issue", "create"]
    ]
    assert created == ["feat: genuinely novel telemetry heatmap"]
    # the surviving ticket carries its practice ids into the filed body
    body = next(c[c.index("--body") + 1] for c in runner.calls if c[:3] == ["gh", "issue", "create"])
    assert "`crewaiinc-crewai-commits`" in body


def test_synthesize_feeds_the_adoption_index_to_the_model(tmp_path):
    cfg = _cfg()
    _kb_with_field_note(tmp_path)
    _write_lesson(tmp_path, "2026-01-01-a", outcome="pass", title="Already shipped it",
                  practices=("crewaiinc-crewai-workflow-ci-yml",))
    runner = _adoption_fixture_runner()

    synthesize(cfg, cycle_index=0, root=str(tmp_path), runner=runner, ai_runner=runner)

    prompt = next(c for c in runner.calls if c[:1] == ["claude"])[2]
    assert ADOPTION_HEADING in prompt
    assert "`crewaiinc-crewai-workflow-ci-yml`" in prompt
