from hsai.config import load_config
from hsai.synthesis import ContextPack, build_prompt, parse_ticket_specs, pick_rotation


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
