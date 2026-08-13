import json

from hsai import ai
from hsai.config import load_config
from hsai.knowledge import KnowledgeBase, extract_practice_ids
from hsai.models import ModelChoice
from hsai.proc import Proc
from hsai.synthesis import (
    TRIED_HEADING,
    ContextPack,
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


# --- practices: the planner's provenance becomes registry notes --------------

PRACTICE_OUTPUT = """PHASE 3 - CONVERGE:
```json
[{"title": "feat: evidence guard", "problem": "p", "proposal": "pp",
  "acceptance_criteria": ["a", "b"], "verification_plan": ["v1", "v2"],
  "size": "L", "goal_ids": ["G1"],
  "synthesis_rationale": "Combines crewAI (mechanical PR gates) and openai/swarm (context).",
  "practices": [
    {"source_repo": "crewAIInc/crewAI", "artifact": ".github/workflows/pr-title.yml",
     "observation": "PR metadata is checked by CI at intake, not by convention.",
     "adaptation": "Resolve every cited practice id against the registry."},
    {"id": "swarm-context-travels", "source_repo": "openai/swarm", "artifact": "",
     "observation": "Context variables travel with every handoff.",
     "adaptation": "Errors carry their phase and ticket."}
  ]}]
```"""


def test_parse_ticket_specs_captures_per_ticket_practices():
    cfg = _cfg()
    spec = parse_ticket_specs(
        PRACTICE_OUTPUT, known_repos=[r.repo for r in cfg.reference_top10]
    )[0]

    assert [p.id for p in spec.practices] == [
        "crewai-evidence-guard", "swarm-context-travels",
    ]
    crewai, swarm = spec.practices
    assert crewai.source_repo == "crewAIInc/crewAI"
    assert crewai.artifact == ".github/workflows/pr-title.yml"
    assert crewai.observation.startswith("PR metadata is checked")
    assert crewai.adaptation.startswith("Resolve every cited")
    # an artifact the planner could not point at is marked, never invented
    assert "not recorded" in swarm.artifact

    body = spec.render()
    assert "## Practices cited" in body
    for practice in spec.practices:
        assert f"`{practice.citation()}`" in body
        assert f"[[{practice.note_name()}]]" in body
    assert extract_practice_ids(body) == ("crewai-evidence-guard", "swarm-context-travels")


def test_practices_fall_back_to_the_projects_the_rationale_names():
    cfg = _cfg()
    output = """```json
[{"title": "feat: practice registry", "problem": "p", "proposal": "pp",
  "acceptance_criteria": ["a", "b"], "verification_plan": ["v1"],
  "synthesis_rationale":
    "Combines gpt-researcher (per-claim source attribution is what makes a report usable), \
MetaGPT (each phase emits an auditable artifact) and NotAPinnedProject (irrelevant aside)."}]
```"""
    spec = parse_ticket_specs(
        output, known_repos=[r.repo for r in cfg.reference_top10]
    )[0]

    # only projects pinned in the reference set count as a source
    assert [p.source_repo for p in spec.practices] == [
        "assafelovic/gpt-researcher", "FoundationAgents/MetaGPT",
    ]
    assert spec.practices[0].observation.startswith("per-claim source attribution")
    assert all("not recorded" in p.artifact for p in spec.practices)
    assert all(p.adaptation for p in spec.practices)


def test_synthesize_writes_the_registry_notes_before_filing_the_ticket(tmp_path):
    cfg = _cfg()
    registry = tmp_path / "knowledge" / "practices"
    filed_bodies: list[str] = []

    def runner(cmd, *, cwd=None, env=None, timeout=None, input_text=None):
        if cmd[:1] == ["claude"]:
            return Proc(cmd, 0, PRACTICE_OUTPUT, "")
        if cmd[:3] == ["gh", "issue", "create"]:
            # the citation must already resolve at the moment the ticket is filed
            assert (registry / "crewai-evidence-guard.md").exists()
            filed_bodies.append(cmd[cmd.index("--body") + 1])
            return Proc(cmd, 0, "https://github.com/o/r/issues/321\n", "")
        return Proc(cmd, 0, "", "")

    res = synthesize(
        cfg, cycle_index=0, root=str(tmp_path), runner=runner, ai_runner=runner
    )

    assert res.filed == [321]
    assert sorted(res.practices) == ["crewai-evidence-guard", "swarm-context-travels"]
    kb = KnowledgeBase.from_config(cfg, tmp_path)
    assert kb.practice_ids() == {"crewai-evidence-guard", "swarm-context-travels"}
    # every cited id resolves - which is exactly what the evidence guard checks
    assert set(extract_practice_ids(filed_bodies[0])) <= kb.practice_ids()
    # and the registry records which ticket adopted it
    assert {p.adopted_by for p in kb.read_practices()} == {("#321",)}
