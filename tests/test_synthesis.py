import json

from hsai.config import load_config
from hsai.practices import Practice, PracticeRegistry
from hsai.proc import Proc
from hsai.synthesis import (
    ContextPack,
    build_prompt,
    normalize_title,
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


def test_prompt_renders_do_not_repropose_section_from_registry(tmp_path):
    cfg = _cfg()
    pack = ContextPack(repos=["a/b"], sections={"a/b": "digest"})
    registry = PracticeRegistry(tmp_path)
    registry.write(
        Practice(
            title="feat: adopted practice", source_repo="crewAIInc/crewAI",
            summary="s", status="adopted", ticket=1,
        )
    )
    registry.write(
        Practice(
            title="feat: rejected practice", source_repo="FoundationAgents/MetaGPT",
            summary="s", status="rejected", ticket=2,
        )
    )
    # Still-proposed practices are undecided - they should NOT show up as
    # "do not re-propose" (nothing has ruled on them yet).
    registry.write(
        Practice(title="feat: still proposed", source_repo="a/b", summary="s", status="proposed")
    )

    prompt = build_prompt(cfg, pack, registry)
    assert "do not re-propose" in prompt.lower()
    assert "feat: adopted practice" in prompt
    assert "feat: rejected practice" in prompt
    assert "feat: still proposed" not in prompt


def test_prompt_without_registry_still_renders_section():
    cfg = _cfg()
    pack = ContextPack(repos=["a/b"], sections={"a/b": "digest"})
    prompt = build_prompt(cfg, pack)
    assert "do not re-propose" in prompt.lower()


def test_normalize_title_strips_prefix_and_punctuation():
    assert normalize_title("feat: Adaptive Budget Gate!") == "adaptive budget gate"
    assert normalize_title("Adaptive budget gate") == "adaptive budget gate"


class _SynthesisRunner:
    """Answers gh calls for a full synthesize() pass (the AI call goes through
    a separate ``ai_runner``, matching how ``synthesize()`` takes both)."""

    def __init__(self, *, existing_issues: list[dict]):
        self.existing_issues = existing_issues
        self.created: list[tuple[str, str]] = []
        self._issue_seq = 100

    def __call__(self, cmd, *, cwd=None, env=None, timeout=None, input_text=None) -> Proc:
        cmd = list(cmd)
        if cmd[:2] == ["gh", "api"]:
            return Proc(cmd, 1, "", "no data in test")
        if cmd[:3] == ["gh", "issue", "list"]:
            return Proc(cmd, 0, json.dumps(self.existing_issues), "")
        if cmd[:3] == ["gh", "issue", "create"]:
            title = cmd[cmd.index("--title") + 1]
            body = cmd[cmd.index("--body") + 1]
            self.created.append((title, body))
            self._issue_seq += 1
            return Proc(cmd, 0, f"https://github.com/o/r/issues/{self._issue_seq}\n", "")
        return Proc(cmd, 0, "", "")


def _ai_runner(output: str):
    def runner(cmd, *, cwd=None, env=None, timeout=None, input_text=None) -> Proc:
        return Proc(cmd, 0, output, "")
    return runner


def _fenced_json(specs: list[dict]) -> str:
    return "PHASE 1...PHASE 2...PHASE 3:\n```json\n" + json.dumps(specs) + "\n```"


def test_synthesize_skips_a_spec_matching_an_open_issue(tmp_path):
    cfg = _cfg()
    ai_output = _fenced_json([
        {
            "title": "feat: Adaptive Budget Gate",
            "problem": "p", "proposal": "pp",
            "acceptance_criteria": ["a", "b", "c"],
            "verification_plan": ["v1", "v2"],
            "size": "M", "goal_ids": ["G4"],
            "synthesis_rationale": "combines x+y+z",
        }
    ])
    runner = _SynthesisRunner(
        existing_issues=[
            {"number": 13, "title": "feat: adaptive budget gate", "labels": [],
             "assignees": [], "body": "", "state": "OPEN"},
        ],
    )
    res = synthesize(
        cfg, cycle_index=0, repo_dir=str(tmp_path),
        runner=runner, ai_runner=_ai_runner(ai_output),
    )
    assert res.filed == []
    assert not runner.created  # never filed to GitHub
    assert len(res.skipped) == 1
    assert res.skipped[0].matched_issue == 13
    assert "Adaptive Budget Gate" in res.skipped[0].title


def test_synthesize_files_a_novel_spec_and_writes_a_proposed_practice(tmp_path):
    cfg = _cfg()
    ai_output = _fenced_json([
        {
            "title": "feat: wholly new capability",
            "problem": "p", "proposal": "pp",
            "acceptance_criteria": ["a", "b", "c"],
            "verification_plan": ["v1", "v2"],
            "size": "M", "goal_ids": ["G4"],
            "synthesis_rationale": "combines x+y+z",
        }
    ])
    runner = _SynthesisRunner(existing_issues=[])
    res = synthesize(
        cfg, cycle_index=0, repo_dir=str(tmp_path),
        runner=runner, ai_runner=_ai_runner(ai_output),
    )
    assert res.skipped == []
    assert len(res.filed) == 1
    assert runner.created

    registry = PracticeRegistry(tmp_path)
    records = registry.read_all()
    assert len(records) == 1
    assert records[0].title == "feat: wholly new capability"
    assert records[0].status == "proposed"
    assert records[0].ticket == res.filed[0]


def test_synthesize_skips_specs_matching_a_non_proposed_practice(tmp_path):
    cfg = _cfg()
    registry = PracticeRegistry(tmp_path)
    registry.write(
        Practice(
            title="feat: already rejected idea", source_repo="a/b", summary="s",
            status="rejected", ticket=55,
        )
    )
    ai_output = _fenced_json([
        {
            "title": "feat: Already Rejected Idea",
            "problem": "p", "proposal": "pp",
            "acceptance_criteria": ["a", "b", "c"],
            "verification_plan": ["v1", "v2"],
            "size": "M", "goal_ids": ["G4"],
            "synthesis_rationale": "combines x+y+z",
        }
    ])
    runner = _SynthesisRunner(existing_issues=[])
    res = synthesize(
        cfg, cycle_index=0, repo_dir=str(tmp_path),
        runner=runner, ai_runner=_ai_runner(ai_output),
    )
    assert res.filed == []
    assert len(res.skipped) == 1
    assert res.skipped[0].matched_issue == 55
