import json

from hsai import ai, memory
from hsai.config import load_config
from hsai.models import ModelChoice
from hsai.proc import Proc
from hsai.synthesis import (
    ADOPTED_HEADING,
    TRIED_HEADING,
    ContextPack,
    build_adopted_digest,
    build_context_pack,
    build_prompt,
    build_tried_digest,
    parse_ticket_specs,
    pick_rotation,
    synthesize,
)


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


# --- "already tried here": the planner reads our own history -----------------

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


def _issue_runner(issues=None):
    def runner(cmd, *, cwd=None, env=None, timeout=None, input_text=None):
        if cmd[:3] == ["gh", "issue", "list"]:
            return Proc(cmd, 0, json.dumps(OPEN_ISSUES if issues is None else issues), "")
        return Proc(cmd, 0, "", "")

    return runner


def _write_lesson(root, name, *, outcome, title):
    directory = root / "knowledge" / "lessons"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(
        f"---\ntags:\n  - lesson\n  - outcome/{outcome}\n  - kind/implement\n"
        f"created: 2026-01-01\n---\n\n# {title}\n\n## Lesson learned\nSomething.\n"
    )


def test_tried_digest_lists_lesson_outcomes_and_open_synthesis_tickets(tmp_path):
    cfg = _cfg()
    _write_lesson(tmp_path, "2026-01-01-a", outcome="pass", title="Poll remote CI")
    _write_lesson(tmp_path, "2026-01-02-b", outcome="fail", title="Edit the workflows")

    digest = build_tried_digest(cfg, root=str(tmp_path), runner=_issue_runner())

    assert "**fail** - Edit the workflows" in digest
    assert "**pass** - Poll remote CI" in digest
    # failures lead: they are the ones worth not repeating
    assert digest.index("Edit the workflows") < digest.index("Poll remote CI")
    # only self-improve tickets count as "already proposed"
    assert "feat: lesson-retrieval memory" in digest
    assert "ci: main is red - auto-heal" not in digest


def test_tried_digest_degrades_to_a_placeholder_when_there_is_no_history(tmp_path):
    digest = build_tried_digest(_cfg(), root=str(tmp_path), runner=_issue_runner([]))
    assert digest == "_(nothing recorded yet - this is an early cycle)_"


def test_prompt_tells_the_planner_what_this_repo_already_tried(tmp_path):
    cfg = _cfg()
    pack = ContextPack(repos=["a/b"], sections={"a/b": "digest"})
    _write_lesson(tmp_path, "2026-01-02-b", outcome="fail", title="Edit the workflows")
    digest = build_tried_digest(cfg, root=str(tmp_path), runner=_issue_runner())

    prompt = build_prompt(cfg, pack, digest)
    assert TRIED_HEADING in prompt
    assert digest in prompt
    assert "Do NOT" in prompt          # an explicit instruction, not a hint
    # the heading survives even with nothing to report, so the planner always
    # knows this section exists
    assert TRIED_HEADING in build_prompt(cfg, pack)
    assert "nothing recorded yet" in build_prompt(cfg, pack)


def test_synthesize_feeds_the_tried_digest_to_the_model():
    cfg = _cfg()
    runner = _plain_text_runner()
    synthesize(cfg, cycle_index=0, root=".", runner=runner, ai_runner=runner)
    claude_call = next(c for c in runner.calls if c[:1] == ["claude"])
    assert TRIED_HEADING in claude_call[2]


# --- "already adopted": the provenance registry feeds the planner ------------


def _adopt(root, **kwargs):
    memory.append_practice(
        memory.practices_path(_cfg(), root), memory.PracticeRecord(**kwargs)
    )


def test_prompt_lists_the_practices_this_repo_already_adopted(tmp_path):
    cfg = _cfg()
    pack = ContextPack(repos=["a/b"], sections={"a/b": "digest"})
    _adopt(tmp_path, ticket=1, pr=2, title="feat: quota ledger",
           reference_repos=("assafelovic/gpt-researcher",))

    adopted = build_adopted_digest(cfg, root=str(tmp_path))
    prompt = build_prompt(cfg, pack, "", adopted)

    assert ADOPTED_HEADING in prompt
    assert "feat: quota ledger" in prompt
    assert "`assafelovic/gpt-researcher`" in prompt   # the citation, not just the title
    assert "Do NOT re-propose any of them" in prompt  # an instruction, not a hint

    # the heading survives with nothing adopted, so the planner always knows the
    # section exists rather than inferring its absence means "nothing shipped"
    empty = build_prompt(cfg, pack)
    assert ADOPTED_HEADING in empty
    assert "no practices recorded in the provenance registry yet" in empty


def test_synthesize_feeds_the_adopted_digest_to_the_model(tmp_path):
    cfg = _cfg()
    _adopt(tmp_path, ticket=1, pr=2, title="feat: quota ledger",
           reference_repos=("assafelovic/gpt-researcher",))
    runner = _plain_text_runner()

    synthesize(cfg, cycle_index=0, root=str(tmp_path), runner=runner, ai_runner=runner)

    claude_call = next(c for c in runner.calls if c[:1] == ["claude"])
    assert ADOPTED_HEADING in claude_call[2]
    assert "feat: quota ledger" in claude_call[2]


# --- the context pack must match what core.yaml promises to learn from -------


def _pack_runner(*, fail: tuple[str, ...] = ()):
    """A `gh api` stand-in; any path containing an entry of `fail` returns red."""

    def runner(cmd, *, cwd=None, env=None, timeout=None, input_text=None):
        path = cmd[2] if len(cmd) > 2 else ""
        if any(f in path for f in fail):
            return Proc(cmd, 1, "", "gh: HTTP 404")
        if path.endswith("/readme"):
            return Proc(cmd, 0, "# a/b\nA reference project.", "")
        if "/commits" in path:
            return Proc(cmd, 0, "feat: add a thing\nfix: repair a thing", "")
        if path.endswith("/contents/.github/workflows"):
            return Proc(cmd, 0, "label-issues.yml\nci.yml", "")
        if "/contents/.github/workflows/" in path:
            return Proc(cmd, 0, "name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest", "")
        if "issues?state=closed" in path:
            return Proc(cmd, 0, "- Bug: the thing broke  [bug, resolved]", "")
        return Proc(cmd, 0, "", "")

    return runner


def test_context_pack_fetches_closed_issues_and_one_workflow_body():
    digest = build_context_pack(["a/b"], runner=_pack_runner()).sections["a/b"]

    assert "README (truncated):" in digest
    assert "Recent commit subjects:" in digest
    assert "CI workflows:" in digest
    # a filename teaches nothing reusable - the CONTENTS do
    assert "Workflow `ci.yml` (truncated):" in digest   # deterministic pick: sorted first
    assert "runs-on: ubuntu-latest" in digest
    # issue_history is declared in core.yaml's learn_from and was never fetched
    assert "Recently closed issues (title [labels]):" in digest
    assert "Bug: the thing broke" in digest


def test_context_pack_degrades_to_the_pre_existing_sections_when_the_new_calls_fail():
    runner = _pack_runner(fail=("issues?state=closed", "/contents/.github/workflows/"))

    digest = build_context_pack(["a/b"], runner=runner).sections["a/b"]

    assert "README (truncated):" in digest
    assert "Recent commit subjects:" in digest
    assert "CI workflows:" in digest              # the filename listing still lands
    assert "Workflow `ci.yml`" not in digest
    assert "Recently closed issues" not in digest


def test_context_pack_survives_every_gh_call_failing():
    def dead(cmd, *, cwd=None, env=None, timeout=None, input_text=None):
        return Proc(cmd, 1, "", "gh: API rate limit exceeded")

    pack = build_context_pack(["a/b", "c/d"], runner=dead)

    assert pack.repos == ["a/b", "c/d"]
    assert pack.sections == {"a/b": "(no data fetched)", "c/d": "(no data fetched)"}
    assert "### a/b" in pack.render()
