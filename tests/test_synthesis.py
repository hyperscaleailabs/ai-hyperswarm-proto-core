import json

from hsai import ai
from hsai.config import load_config
from hsai.knowledge import KnowledgeBase, cited_practice_ids
from hsai.models import ModelChoice
from hsai.proc import Proc
from hsai.synthesis import (
    TRIED_HEADING,
    ContextPack,
    build_prompt,
    build_tried_digest,
    parse_practices,
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
    assert parse_ticket_specs('```json\n["a string, not a ticket"]\n```') == []


# --- practices: the machine-checkable half of the synthesis rationale ---------

RATIONALE = "combines gpt-researcher, MetaGPT and llama_index"
PRACTICE_OUTPUT = """PHASE 3 - CONVERGE:
```json
[{"title": "feat: practice registry", "problem": "p", "proposal": "pp",
  "acceptance_criteria": ["a", "b"], "verification_plan": ["v1", "v2"],
  "size": "L", "goal_ids": ["G1"],
  "synthesis_rationale": "combines gpt-researcher, MetaGPT and llama_index",
  "practices": [
    {"id": "MetaGPT Phase Artifacts", "source_repo": "FoundationAgents/MetaGPT",
     "artifact": "metagpt/roles/", "observation": "each role emits a named document",
     "adaptation": "render phase artifacts into every PR body"},
    {"id": "swarm-error-context", "source_repo": "openai/swarm",
     "artifact": "swarm/core.py", "adaptation": "prefix errors with phase and ticket"},
    {"id": "nope", "source_repo": "openai/swarm", "artifact": "swarm/core.py"},
    {"id": "no-artifact-given", "source_repo": "openai/swarm"}
  ]}]
```"""


def test_parse_practices_keeps_only_entries_that_can_be_checked():
    raw = [
        {"id": "Swarm Error Context", "source_repo": "openai/swarm",
         "artifact": "swarm/core.py", "observation": "o", "adaptation": "a"},
        {"id": "single", "source_repo": "openai/swarm", "artifact": "swarm/core.py"},
        {"id": "no-artifact", "source_repo": "openai/swarm"},
        {"id": "no-source", "artifact": "swarm/core.py"},
        {"id": "swarm-error-context", "source_repo": "openai/swarm",
         "artifact": "swarm/core.py"},  # duplicate id
        "not an object",
    ]
    practices = parse_practices(raw, "the rationale")
    assert [p.id for p in practices] == ["swarm-error-context"]
    assert parse_practices("not a list") == ()


def test_parse_ticket_specs_captures_per_ticket_practices():
    spec = parse_ticket_specs(PRACTICE_OUTPUT)[0]

    assert [p.id for p in spec.practices] == [
        "metagpt-phase-artifacts",  # slugified from prose
        "swarm-error-context",
    ]
    assert spec.practices[0].source_repo == "FoundationAgents/MetaGPT"
    # an entry that skipped its observation inherits the planner's own account
    assert spec.practices[1].observation == RATIONALE

    # the filed body cites them in a section the orchestrator can read back
    body = spec.render()
    assert "## Practices cited" in body
    assert cited_practice_ids(body) == ("metagpt-phase-artifacts", "swarm-error-context")
    # a ticket with no practices renders exactly as it did before the registry
    assert "Practices cited" not in parse_ticket_specs(PLAIN_TEXT_OUTPUT)[0].render()


def _practice_runner():
    calls: list[list[str]] = []

    def runner(cmd, *, cwd=None, env=None, timeout=None, input_text=None):
        calls.append(list(cmd))
        if cmd[:1] == ["claude"]:
            return Proc(cmd, 0, PRACTICE_OUTPUT, "")
        if cmd[:3] == ["gh", "issue", "create"]:
            return Proc(cmd, 0, "https://github.com/o/r/issues/321\n", "")
        return Proc(cmd, 0, "", "")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def test_synthesize_writes_the_registry_notes_the_filed_ticket_cites(tmp_path):
    cfg = _cfg()
    runner = _practice_runner()

    res = synthesize(
        cfg, cycle_index=0, root=str(tmp_path), runner=runner, ai_runner=runner
    )

    assert res.filed == [321]
    assert res.practices == ("metagpt-phase-artifacts", "swarm-error-context")

    kb = KnowledgeBase.from_config(cfg, tmp_path)
    assert kb.practice_ids() == {"metagpt-phase-artifacts", "swarm-error-context"}

    # every id the ticket cites resolves, which is exactly what the
    # orchestrator's evidence guard will demand of it later
    create = next(c for c in runner.calls if c[:3] == ["gh", "issue", "create"])
    body = create[create.index("--body") + 1]
    assert set(cited_practice_ids(body)) == kb.practice_ids()

    # and each note points back at the ticket that adopted it
    note = (kb.practices_dir / "swarm-error-context.md").read_text()
    assert "source_repo: openai/swarm" in note
    assert "- ticket #321" in note


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
