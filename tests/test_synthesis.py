import json
from pathlib import Path

from hsai import practices
from hsai.config import load_config
from hsai.practices import Registry
from hsai.synthesis import (
    ContextPack,
    _ensure_citation,
    build_prompt,
    parse_ticket_specs,
    pick_rotation,
)


def _cfg():
    return load_config()


def _output(item: dict) -> str:
    return "PHASE 3:\n```json\n" + json.dumps([item]) + "\n```"


_BASE_ITEM = {
    "title": "feat: adaptive budget",
    "problem": "p",
    "proposal": "pp",
    "acceptance_criteria": ["a", "b", "c"],
    "verification_plan": ["v1", "v2"],
    "size": "L",
    "goal_ids": ["G4"],
    "synthesis_rationale": "Combines microsoft/JARVIS with openai/swarm.",
}


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


def test_prompt_lists_the_registered_practice_catalog():
    prompt = build_prompt(_cfg(), ContextPack(repos=["a/b"], sections={"a/b": "d"}),
                          "- PR-0003: routing - observed in microsoft/JARVIS")
    assert "PR-0003" in prompt
    assert '"practices"' in prompt
    assert "artifact_ref" in prompt


def test_synthesis_emits_practice_ids_and_files_missing_cards(tmp_path):
    cfg = _cfg()
    registry = Registry(tmp_path, cfg)
    output = _output(
        {
            **_BASE_ITEM,
            "practices": [
                {"id": "PR-0001"},  # not registered in this empty vault
                {
                    "title": "Gatekeeper blocks the merge",
                    "source_repo": "microsoft/semantic-kernel",
                    "artifact_kind": "ci",
                    "artifact_ref": ".github/workflows/merge-gatekeeper.yml",
                    "what": "blocks until every check concludes",
                    "why": "our invariants need a required check",
                },
                {  # unpinned: dropped rather than turned into a fake citation
                    "title": "Invented", "source_repo": "acme/nope",
                    "artifact_kind": "code", "artifact_ref": "src/x.py",
                },
            ],
        }
    )

    spec = parse_ticket_specs(output, registry=registry)[0]

    assert spec.practice_ids == ("PR-0001",)
    card = registry.by_id("PR-0001")
    assert card is not None and card.source_repo == "microsoft/semantic-kernel"
    assert (practices.practices_dir(tmp_path, cfg) / f"{card.note_name}.md").exists()
    # the citation is rendered into the ticket the loop will later read back
    assert "## Practices\n- PR-0001" in spec.render()
    assert practices.cite(spec.render()) == ("PR-0001",)


def test_parse_without_a_registry_keeps_only_known_ids():
    spec = parse_ticket_specs(_output({**_BASE_ITEM, "practices": [{"id": "PR-0003"}, {}]}))[0]
    assert spec.practice_ids == ("PR-0003",)


def test_citation_is_backfilled_from_the_synthesis_rationale():
    """A ticket that forgot its structured practices still cites real cards."""
    cfg = _cfg()
    registry = Registry(Path(__file__).resolve().parents[1], cfg)
    spec = parse_ticket_specs(_output(_BASE_ITEM))[0]
    assert spec.practice_ids == ()

    backfilled = _ensure_citation(spec, registry, cfg)

    assert backfilled.practice_ids == ("PR-0003", "PR-0008")  # JARVIS, openai/swarm
    assert practices.resolve_citation(backfilled.render(), registry.cards, cfg).repos == (
        "microsoft/JARVIS", "openai/swarm",
    )
