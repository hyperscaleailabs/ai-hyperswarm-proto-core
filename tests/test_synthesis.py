import json

from hsai import ai
from hsai.config import load_config
from hsai.models import ModelChoice
from hsai.proc import Proc
from hsai.synthesis import (
    TRIED_HEADING,
    ContextPack,
    MemoryPack,
    TicketRef,
    build_memory_pack,
    build_prompt,
    is_duplicate,
    parse_ticket_specs,
    pick_rotation,
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

    def runner(cmd, *, cwd=None, env=None, timeout=None, input_text=None):
        calls.append(list(cmd))
        if cmd[:1] == ["claude"]:
            return Proc(cmd, 0, PLAIN_TEXT_OUTPUT, "")
        if cmd[:3] == ["gh", "issue", "create"]:
            return Proc(cmd, 0, "https://github.com/o/r/issues/321\n", "")
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
    assert res.duplicates_rejected == 0


# --- MemoryPack: "already tried" from open tickets, closed tickets, lessons --

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
        "number": 30, "title": "feat: adversarial cross-model PR review gate",
        "labels": [{"name": "self-improve"}], "closedAt": "2026-01-05T00:00:00Z",
    },
    {
        "number": 20, "title": "chore: governance artifacts for block 5",
        "labels": [], "closedAt": "2026-01-01T00:00:00Z",
    },
]


def _memory_runner(*, open_issues=None, closed_issues=None):
    def runner(cmd, *, cwd=None, env=None, timeout=None, input_text=None):
        cmd = list(cmd)
        if cmd[:4] == ["gh", "issue", "list", "--repo"] and "closed" in cmd:
            return Proc(cmd, 0, json.dumps(CLOSED_ISSUES if closed_issues is None else closed_issues), "")
        if cmd[:3] == ["gh", "issue", "list"]:
            return Proc(cmd, 0, json.dumps(OPEN_ISSUES if open_issues is None else open_issues), "")
        return Proc(cmd, 0, "", "")

    return runner


def _write_lesson(root, name, *, outcome, title):
    directory = root / "knowledge" / "lessons"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(
        f"---\ntags:\n  - lesson\n  - outcome/{outcome}\n  - kind/implement\n"
        f"created: 2026-01-01\n---\n\n# {title}\n\n## Lesson learned\nSomething.\n"
    )


def test_memory_pack_lists_open_closed_and_lesson_outcomes(tmp_path):
    cfg = _cfg()
    _write_lesson(tmp_path, "2026-01-01-a", outcome="pass", title="Poll remote CI")
    _write_lesson(tmp_path, "2026-01-02-b", outcome="fail", title="Edit the workflows")

    memory = build_memory_pack(cfg, root=str(tmp_path), runner=_memory_runner())
    rendered = memory.render()

    assert "**fail** - Edit the workflows" in rendered
    assert "**pass** - Poll remote CI" in rendered
    # failures lead: they are the ones worth not repeating
    assert rendered.index("Edit the workflows") < rendered.index("Poll remote CI")
    assert "#40 feat: lesson-retrieval memory" in rendered
    assert "#41 ci: main is red - auto-heal" in rendered
    assert "#30 feat: adversarial cross-model PR review gate" in rendered
    assert "#20 chore: governance artifacts for block 5" in rendered


def test_memory_pack_degrades_to_a_placeholder_when_there_is_no_history(tmp_path):
    memory = build_memory_pack(
        _cfg(), root=str(tmp_path), runner=_memory_runner(open_issues=[], closed_issues=[]),
    )
    assert memory.render() == "_(nothing recorded yet - this is an early cycle)_"


def test_memory_gathering_degrades_gracefully_when_gh_is_unavailable(tmp_path):
    """`gh` missing entirely (FileNotFoundError -> code 127, empty stdout) and an
    empty/nonexistent knowledge base must degrade to an empty section, not raise."""

    def broken_runner(cmd, *, cwd=None, env=None, timeout=None, input_text=None):
        return Proc(list(cmd), 127, "", "gh: command not found")

    memory = build_memory_pack(_cfg(), root=str(tmp_path), runner=broken_runner)
    assert memory.open_tickets == ()
    assert memory.closed_tickets == ()
    assert memory.lesson_lines == ()
    assert memory.render() == "_(nothing recorded yet - this is an early cycle)_"

    # And synthesis as a whole must not abort because of it.
    plain = _plain_text_runner()

    def combined(cmd, *, cwd=None, env=None, timeout=None, input_text=None):
        cmd = list(cmd)
        if cmd[:1] == ["claude"] or cmd[:3] == ["gh", "issue", "create"]:
            return plain(cmd, cwd=cwd, env=env, timeout=timeout, input_text=input_text)
        return broken_runner(cmd, cwd=cwd, env=env, timeout=timeout, input_text=input_text)

    res = synthesize(_cfg(), cycle_index=0, root=str(tmp_path), runner=combined, ai_runner=combined)
    assert res.ok is True
    assert res.filed == [321]


def test_memory_render_is_hard_capped_in_length():
    open_refs = tuple(TicketRef(n, f"feat: ticket number {n} about something long enough") for n in range(500))
    memory = MemoryPack(open_tickets=open_refs)
    rendered = memory.render(max_chars=3000)
    assert len(rendered) <= 3000
    # sanity: without the cap this would be far longer
    assert len(memory.render(max_chars=10_000_000)) > 3000


def test_prompt_puts_memory_section_before_the_reference_digest():
    cfg = _cfg()
    pack = ContextPack(repos=["a/b"], sections={"a/b": "STUDY_DIGEST_MARKER"})
    memory = MemoryPack(
        open_tickets=(TicketRef(1, "feat: something already queued"),),
        lesson_lines=("**fail** - Edit the workflows",),
    )
    prompt = build_prompt(cfg, pack, memory)
    assert TRIED_HEADING in prompt
    assert "feat: something already queued" in prompt
    assert "Edit the workflows" in prompt
    assert "Do NOT" in prompt
    assert prompt.index(TRIED_HEADING) < prompt.index("STUDY_DIGEST_MARKER")

    # the heading survives even with nothing to report
    empty_prompt = build_prompt(cfg, pack)
    assert TRIED_HEADING in empty_prompt
    assert "nothing recorded yet" in empty_prompt


def test_synthesize_feeds_the_memory_pack_to_the_model():
    cfg = _cfg()
    runner = _plain_text_runner()
    synthesize(cfg, cycle_index=0, root=".", runner=runner, ai_runner=runner)
    claude_call = next(c for c in runner.calls if c[:1] == ["claude"])
    assert TRIED_HEADING in claude_call[2]


# --- is_duplicate(): pure, fixture-driven -------------------------------------

def _spec(title: str) -> TicketSpec:
    return TicketSpec(
        title=title, problem="p", proposal="pp",
        acceptance_criteria=("a",), verification_plan=("v",),
    )


def test_is_duplicate_exact_title_match():
    memory = MemoryPack(open_tickets=(TicketRef(1, "feat: adaptive retry budget for heavy tier"),))
    matched = is_duplicate(_spec("feat: adaptive retry budget for heavy tier"), memory)
    assert matched == "feat: adaptive retry budget for heavy tier"


def test_is_duplicate_prefix_only_difference_is_rejected():
    """feat: vs refactor: on an otherwise-identical title is the same idea."""
    memory = MemoryPack(open_tickets=(TicketRef(1, "feat: adaptive retry budget for heavy tier"),))
    matched = is_duplicate(_spec("refactor: adaptive retry budget for heavy tier"), memory)
    assert matched == "feat: adaptive retry budget for heavy tier"


def test_is_duplicate_genuine_near_duplicate_is_rejected():
    memory = MemoryPack(
        closed_tickets=(TicketRef(9, "feat: retry budget backoff for the heavy tier"),),
    )
    matched = is_duplicate(
        _spec("feat: add exponential backoff to heavy-tier retry budget"), memory,
    )
    assert matched == "feat: retry budget backoff for the heavy tier"


def test_is_duplicate_distinct_idea_is_not_rejected():
    memory = MemoryPack(
        open_tickets=(TicketRef(1, "feat: adaptive retry budget for heavy tier"),),
        lesson_lines=("**fail** - Edit the CI workflow matrix",),
    )
    matched = is_duplicate(
        _spec("skill: obsidian MOC reindex on every whitepaper write"), memory,
    )
    assert matched is None


# --- synthesize(): drops duplicates before filing, reports the count ---------

DUP_AND_NOVEL_OUTPUT = """PHASE 3:
```json
[
  {"title": "feat: adaptive retry budget for heavy tier", "problem": "p", "proposal": "pp",
   "acceptance_criteria": ["a", "b"], "verification_plan": ["v1"],
   "size": "M", "goal_ids": ["G4"], "synthesis_rationale": "combines x+y+z"},
  {"title": "skill: obsidian MOC reindex on every whitepaper write", "problem": "p",
   "proposal": "pp", "acceptance_criteria": ["a", "b"], "verification_plan": ["v1"],
   "size": "M", "goal_ids": ["G3"], "synthesis_rationale": "combines x+y+z"},
  {"title": "feat: recall index sorted by relevance score", "problem": "p", "proposal": "pp",
   "acceptance_criteria": ["a", "b"], "verification_plan": ["v1"],
   "size": "M", "goal_ids": ["G1"], "synthesis_rationale": "combines x+y+z"}
]
```"""


def _dup_runner():
    filed_titles: list[str] = []

    def runner(cmd, *, cwd=None, env=None, timeout=None, input_text=None):
        cmd = list(cmd)
        if cmd[:1] == ["claude"]:
            return Proc(cmd, 0, DUP_AND_NOVEL_OUTPUT, "")
        if cmd[:3] == ["gh", "issue", "create"]:
            title = cmd[cmd.index("--title") + 1]
            filed_titles.append(title)
            return Proc(cmd, 0, f"https://github.com/o/r/issues/{100 + len(filed_titles)}\n", "")
        if cmd[:4] == ["gh", "issue", "list", "--repo"] and "closed" in cmd:
            return Proc(cmd, 0, "[]", "")
        if cmd[:3] == ["gh", "issue", "list"]:
            return Proc(
                cmd, 0,
                json.dumps([{
                    "number": 5, "title": "feat: adaptive retry budget for heavy tier",
                    "labels": [{"name": "self-improve"}], "assignees": [], "body": "",
                }]),
                "",
            )
        return Proc(cmd, 0, "", "")

    runner.filed_titles = filed_titles  # type: ignore[attr-defined]
    return runner


def test_synthesize_drops_duplicates_and_files_only_novel_specs(tmp_path):
    cfg = _cfg()
    runner = _dup_runner()

    # Isolated tmp_path: no real repo lessons in scope, so the only prior
    # title in play is the one open issue the fake runner returns.
    res = synthesize(cfg, cycle_index=0, root=str(tmp_path), runner=runner, ai_runner=runner)

    assert len(res.filed) == 2
    assert len(runner.filed_titles) == 2
    assert "feat: adaptive retry budget for heavy tier" not in runner.filed_titles
    assert res.duplicates_rejected == 1
    assert res.rejected_titles == ("feat: adaptive retry budget for heavy tier",)


def test_synthesize_files_only_survivors_when_short_of_file_top(tmp_path):
    """3 candidates, 1 duplicate -> 2 filed, and the shortfall is explained
    rather than silently padded back up to file_top."""
    cfg = _cfg()
    assert int(cfg.synthesis.get("file_top", 3)) == 3
    runner = _dup_runner()

    res = synthesize(cfg, cycle_index=0, root=str(tmp_path), runner=runner, ai_runner=runner)

    assert len(res.filed) == 2
    assert "only 2/3 candidate(s) were novel" in res.error
    assert "1 rejected as duplicates" in res.error
