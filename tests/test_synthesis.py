from hsai import ai
from hsai.config import load_config
from hsai.models import ModelChoice
from hsai.practices import ADOPTED, Practice, PracticeRef, PracticeRegistry, practice_id
from hsai.proc import Proc
from hsai.synthesis import (
    ContextPack,
    build_prompt,
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


def test_prompt_tells_the_planner_what_not_to_re_propose():
    cfg = _cfg()
    pack = ContextPack(repos=["a/b"], sections={"a/b": "digest"})
    adopted = [
        Practice(
            id="crewaiinc-crewai-docs-freeze", source_repo="crewAIInc/crewAI",
            artifact="docs-freeze", summary="snapshots docs alongside each change",
            status=ADOPTED, adopted_by_pr=13,
        )
    ]
    prompt = build_prompt(cfg, pack, adopted)
    assert "do NOT re-propose" in prompt
    assert "snapshots docs alongside each change" in prompt
    assert "crewAIInc/crewAI" in prompt

    # an empty registry still says so explicitly, rather than saying nothing
    assert "nothing adopted yet" in build_prompt(cfg, pack)


def test_parse_ticket_specs_declares_the_practices_its_rationale_credits():
    cfg = _cfg()
    output = """```json
[{"title": "feat: x", "problem": "p", "proposal": "pp",
  "acceptance_criteria": ["a", "b"], "verification_plan": ["v"],
  "size": "L", "goal_ids": ["G1"],
  "synthesis_rationale": "Combines crewAIInc/crewAI's `[docs-freeze]` provenance commits with openai/swarm ergonomics and notmyrepo/invented tricks."}]
```"""
    spec = parse_ticket_specs(output, cfg)[0]

    assert {p.source_repo for p in spec.practices} == {"crewAIInc/crewAI", "openai/swarm"}
    assert "notmyrepo/invented" not in {p.source_repo for p in spec.practices}
    assert "## Practices adopted" in spec.render()

    # without a cfg there is no pinned set to match against, so nothing is claimed
    assert parse_ticket_specs(output)[0].practices == ()


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


def _plain_text_runner(output: str = PLAIN_TEXT_OUTPUT):
    """A `claude` that prints plain text - an older binary, or a crash."""
    calls: list[list[str]] = []

    def runner(cmd, *, cwd=None, env=None, timeout=None, input_text=None):
        calls.append(list(cmd))
        if cmd[:1] == ["claude"]:
            return Proc(cmd, 0, output, "")
        if cmd[:3] == ["gh", "issue", "create"]:
            return Proc(cmd, 0, "https://github.com/o/r/issues/321\n", "")
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
    res = synthesize(
        cfg, cycle_index=0, repo_root=tmp_path, runner=runner, ai_runner=runner
    )
    assert res.ok is True
    assert res.filed == [321]
    assert res.error == ""


CITING_OUTPUT = """PHASE 3:
```json
[{"title": "feat: provenance registry", "problem": "p", "proposal": "pp",
  "acceptance_criteria": ["a", "b"], "verification_plan": ["v"],
  "size": "L", "goal_ids": ["G1"],
  "synthesis_rationale": "Combines crewAIInc/crewAI's `[docs-freeze]` provenance commits with openai/swarm ergonomics."}]
```"""


def test_synthesize_queues_a_practice_note_per_cited_practice(tmp_path):
    """Filing a ticket also files the provenance it will later be judged on."""
    cfg = _cfg()
    runner = _plain_text_runner(CITING_OUTPUT)

    res = synthesize(
        cfg, cycle_index=0, repo_root=tmp_path, runner=runner, ai_runner=runner
    )
    assert res.filed == [321]

    registry = PracticeRegistry.from_config(cfg, tmp_path)
    stored = {p.source_repo: p for p in registry.read_all()}
    assert set(stored) == {"crewAIInc/crewAI", "openai/swarm"}
    assert all(p.status == "queued" for p in stored.values())
    assert all(p.adopted_by_ticket == 321 for p in stored.values())
    assert stored["crewAIInc/crewAI"].artifact == "[docs-freeze]"

    # the filed issue body carries the same declarations the registry queued
    issue_create = next(c for c in runner.calls if c[:3] == ["gh", "issue", "create"])
    body = issue_create[issue_create.index("--body") + 1]
    assert "## Practices adopted" in body
    assert "- crewAIInc/crewAI ->" in body


def test_synthesize_feeds_adopted_practices_back_into_the_prompt(tmp_path):
    cfg = _cfg()
    registry = PracticeRegistry.from_config(cfg, tmp_path)
    ref = PracticeRef("openai/swarm", "keeps handoffs explicit between agents")
    registry.mark_adopted([ref], pr=13)

    runner = _plain_text_runner(CITING_OUTPUT)
    synthesize(cfg, cycle_index=0, repo_root=tmp_path, runner=runner, ai_runner=runner)

    claude_call = next(c for c in runner.calls if c[:1] == ["claude"])
    prompt = " ".join(claude_call)
    assert "do NOT re-propose" in prompt
    assert "keeps handoffs explicit between agents" in prompt
    assert practice_id(ref.source_repo, ref.practice) in {p.id for p in registry.adopted()}
