import json

from hsai import ai
from hsai.config import load_config
from hsai.dedupe import DUPLICATE_LABEL
from hsai.knowledge import KnowledgeBase, Lesson
from hsai.models import ModelChoice
from hsai.proc import Proc
from hsai.synthesis import (
    MEMORY_HEADING,
    Backlog,
    ContextPack,
    MemoryLesson,
    RepoMemory,
    build_prompt,
    build_repo_memory,
    extract_ticket_specs,
    known_tickets,
    load_backlog,
    parse_ticket_specs,
    pick_rotation,
    strip_reasoning,
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


# --- repo memory --------------------------------------------------------------
def _issue(number: int, title: str, labels=("self-improve",), body: str = ""):
    return {
        "number": number, "title": title, "body": body,
        "labels": [{"name": lb} for lb in labels], "assignees": [],
    }


def _memory_runner(open_issues, closed_issues):
    """A `gh` that answers the two issue-list calls repo memory makes."""
    def runner(cmd, *, cwd=None, env=None, timeout=None, input_text=None):
        cmd = list(cmd)
        if cmd[:3] == ["gh", "issue", "list"]:
            state = cmd[cmd.index("--state") + 1]
            return Proc(cmd, 0, json.dumps(open_issues if state == "open" else closed_issues), "")
        return Proc(cmd, 0, "", "")
    return runner


def _seed_lesson(tmp_path, cfg):
    kb = KnowledgeBase.from_config(cfg, tmp_path)
    kb.write_lesson(Lesson(
        title="feat: quota ledger", outcome="pass", kind="implement",
        context="c", what_happened="w",
        lesson="Measure quota per merged PR, not per iteration.\nSecond line ignored.",
        references=("openai/swarm", "SWE-agent/SWE-agent"),
    ))
    return kb


def test_repo_memory_indexes_lessons_and_both_ticket_states(tmp_path):
    cfg = _cfg()
    _seed_lesson(tmp_path, cfg)
    runner = _memory_runner(
        [_issue(11, "feat: open work")],
        [_issue(9, "feat: shipped work"), _issue(8, "chore: not a proposal", labels=("hsai",))],
    )
    memory = build_repo_memory(cfg, backlog=load_backlog(cfg, runner=runner), repo_root=tmp_path)
    assert memory.lessons[0].title == "feat: quota ledger"
    assert memory.lessons[0].headline == "Measure quota per merged PR, not per iteration."
    assert "openai/swarm" in memory.lessons[0].references
    assert memory.open_tickets == ["#11 feat: open work"]
    # Only self-improve/skill work is prior *proposal* ground; chores are noise.
    assert memory.closed_tickets == ["#9 feat: shipped work"]


def test_review_briefs_are_kept_out_of_memory_and_dedupe():
    cfg = _cfg()
    runner = _memory_runner(
        [_issue(20, "review: block 41340", labels=("review",)), _issue(21, "feat: real work")],
        [],
    )
    backlog = load_backlog(cfg, runner=runner)
    assert [i.number for i in backlog.open_issues] == [21]
    assert [k.number for k in known_tickets(backlog)] == [21]


def test_prompt_carries_the_repo_memory_section_and_stays_in_budget():
    cfg = _cfg()
    pack = ContextPack(repos=["a/b"], sections={"a/b": "digest " * 20000})
    memory = RepoMemory(
        lessons=[MemoryLesson("feat: quota ledger", "measure per merged PR", ("openai/swarm",))],
        open_tickets=[f"#{n} feat: open item {n}" for n in range(40)],
        closed_tickets=[f"#{n} feat: shipped item {n}" for n in range(400)],
    )
    prompt = build_prompt(cfg, pack, memory)
    budget = int(cfg.synthesis["prompt_char_budget"])
    assert len(prompt) <= budget
    assert MEMORY_HEADING in prompt
    assert "feat: quota ledger" in prompt
    assert "#0 feat: open item 0" in prompt
    # The output schema is the one thing trimming must never eat.
    assert "acceptance_criteria" in prompt and "The JSON block must be the LAST" in prompt
    assert "elided for prompt budget" in prompt  # truncation is stated, never silent


def test_empty_memory_says_so_rather_than_pretending():
    cfg = _cfg()
    prompt = build_prompt(cfg, ContextPack(repos=[], sections={}), RepoMemory())
    assert MEMORY_HEADING in prompt
    assert "no history" in prompt


# --- extraction: every wrapper a reasoning model can emit ---------------------
PLAN = [{
    "title": "feat: adaptive budget", "problem": "p", "proposal": "pp",
    "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
    "size": "L", "goal_ids": ["G4"], "synthesis_rationale": "combines x+y+z",
}]
PLAN_JSON = json.dumps(PLAN)

FENCED = f"PHASE 3 - CONVERGE:\n```json\n{PLAN_JSON}\n```"
THINK_TAGS = (
    "<think>\nThe user wants tickets. Let me draft [{\"title\": \"draft\"}] first.\n</think>\n"
    + FENCED
)
DANGLING_CLOSER = f"I considered [{{\"title\": \"scratch\"}}] internally.\n</think>\n{FENCED}"
PREAMBLE = (
    "Here is my reasoning in prose. I looked at three projects and weighed "
    "feasibility against novelty theater, then converged.\n\n" + FENCED
)
NESTED_FENCE = f"""````markdown
Full answer below.

```python
def helper(): return [1, 2]
```

```json
{PLAN_JSON}
```
````"""
BARE_ARRAY = f"PHASE 3 - CONVERGE. The tickets are:\n\n{PLAN_JSON}\n\nThat is all."
EARLIER_DRAFT = f"""Draft:
```json
[{{"title": "feat: draft only", "problem": "x"}}]
```
Final:
{FENCED}"""


def test_every_wrapper_yields_the_same_ticket_specs():
    """Think tags, prose, nested fences and a bare array must all parse alike."""
    variants = {
        "fenced": FENCED,
        "think-tags": THINK_TAGS,
        "dangling-closer": DANGLING_CLOSER,
        "preamble": PREAMBLE,
        "nested-fence": NESTED_FENCE,
        "bare-array": BARE_ARRAY,
        "earlier-draft": EARLIER_DRAFT,
    }
    for name, output in variants.items():
        specs = parse_ticket_specs(output)
        assert len(specs) == 1, f"{name} yielded {len(specs)} specs"
        spec = specs[0]
        assert spec.title == "feat: adaptive budget", name
        assert spec.size == "L" and "size:L" in spec.all_labels(), name
        assert len(spec.acceptance_criteria) == 3, name
        assert spec.goal_ids == ("G4",), name


def test_strip_reasoning_removes_scratchpads_only():
    assert strip_reasoning("<think>hidden</think>answer") == "answer"
    assert strip_reasoning("<thinking>a</thinking>\nb") == "b"
    assert strip_reasoning("scratch\n</think>\nanswer") == "answer"
    assert strip_reasoning("plain answer") == "plain answer"


def test_markdown_checkboxes_are_not_mistaken_for_a_plan():
    """Prose brackets must not shadow the real array."""
    output = "- [ ] a criterion\n- [x] another\n\n" + FENCED
    assert len(parse_ticket_specs(output)) == 1


# --- extraction failures name the failure mode -------------------------------
def test_failure_modes_are_named_not_generic():
    empty = extract_ticket_specs("no json here at all")
    assert empty.specs == [] and "no JSON array" in empty.error

    malformed = extract_ticket_specs("```json\n[{not: valid json,}]\n```")
    assert malformed.specs == []
    assert "none was valid JSON" in malformed.error and malformed.candidates == 1

    wrong_shape = extract_ticket_specs('```json\n[{"title": "t", "body": "no schema"}]\n```')
    assert wrong_shape.specs == []
    assert "required ticket keys" in wrong_shape.error

    for extraction in (empty, malformed, wrong_shape):
        assert "no parseable ticket specs" not in extraction.error


def test_elements_missing_keys_are_dropped_not_fatal():
    output = json.dumps([{"title": "junk"}, *PLAN])
    extraction = extract_ticket_specs(output)
    assert len(extraction.specs) == 1 and extraction.dropped == 1


# --- end to end ---------------------------------------------------------------
class _SynthRunner:
    """A `claude` plus `gh` stand-in that records every issue write."""

    def __init__(self, model_output: str, open_issues=(), closed_issues=()):
        self.model_output = model_output
        self.open_issues = list(open_issues)
        self.closed_issues = list(closed_issues)
        self.calls: list[list[str]] = []
        self.created: list[dict] = []
        self._next = 500

    def __call__(self, cmd, *, cwd=None, env=None, timeout=None, input_text=None):
        cmd = list(cmd)
        self.calls.append(cmd)
        if cmd[:1] == ["claude"]:
            return Proc(cmd, 0, self.model_output, "")
        if cmd[:3] == ["gh", "issue", "list"]:
            state = cmd[cmd.index("--state") + 1]
            issues = self.open_issues if state == "open" else self.closed_issues
            return Proc(cmd, 0, json.dumps(issues), "")
        if cmd[:3] == ["gh", "issue", "create"]:
            self._next += 1
            self.created.append({
                "title": cmd[cmd.index("--title") + 1],
                "body": cmd[cmd.index("--body") + 1],
                "labels": [cmd[i + 1] for i, a in enumerate(cmd) if a == "--label"],
            })
            return Proc(cmd, 0, f"https://github.com/o/r/issues/{self._next}\n", "")
        return Proc(cmd, 0, "", "")

    def wrote(self, *prefix) -> list[list[str]]:
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]


def test_synthesize_files_a_new_proposal_unchanged(tmp_path):
    cfg = _cfg()
    runner = _SynthRunner(FENCED)
    res = synthesize(cfg, cycle_index=0, repo_root=tmp_path, runner=runner, ai_runner=runner)
    assert res.ok and res.filed == [501] and res.candidates == 1
    assert res.skipped == [] and res.flagged == []
    assert DUPLICATE_LABEL not in runner.created[0]["labels"]
    assert "Possible duplicate" not in runner.created[0]["body"]


DUPLICATE_PLAN = json.dumps([{
    "title": "feat: adaptive quota budget gate that halts new work per block",
    "problem": "p", "proposal": "pp",
    "acceptance_criteria": ["quota spend is graded before each iteration",
                            "a hard breach halts new work for the block",
                            "an in-flight PR still merges"],
    "verification_plan": ["pytest tests/test_cycle.py"],
    "size": "L", "goal_ids": ["G4"], "synthesis_rationale": "x+y+z",
}])
OPEN_MATCH = {
    "number": 77,
    "title": "feat: adaptive quota budget gate halting new work per block",
    "body": (
        "## Acceptance criteria\n"
        "- [ ] quota spend is graded before each iteration\n"
        "- [ ] a hard breach halts new work for the block\n"
        "- [ ] an in-flight PR still merges\n\n## Verification plan\n- [ ] pytest\n"
    ),
    "labels": [{"name": "self-improve"}], "assignees": [],
}


def test_candidate_matching_an_open_ticket_is_skipped_and_reported(tmp_path):
    cfg = _cfg()
    runner = _SynthRunner(f"```json\n{DUPLICATE_PLAN}\n```", open_issues=[OPEN_MATCH])
    res = synthesize(cfg, cycle_index=3, repo_root=tmp_path, runner=runner, ai_runner=runner)

    assert res.filed == [] and res.filed_count == 0
    assert res.skipped_count == 1
    assert res.skipped[0].matched_issue == 77 and res.skipped[0].matched_state == "open"
    assert "#77" in res.skipped[0].describe()
    # Nothing was filed - and nothing that already exists was touched.
    assert runner.wrote("gh", "issue", "create") == []
    assert runner.wrote("gh", "issue", "close") == []
    assert runner.wrote("gh", "issue", "edit") == []
    # A wholly-duplicate batch is a real outcome, not a broken heavy call.
    assert res.ok is True and "nothing new to file" in res.error


def test_mid_similarity_candidate_is_filed_with_a_label_and_a_backlink(tmp_path):
    """Same headline, different acceptance criteria: filed, but flagged."""
    cfg = _cfg()
    partial = dict(OPEN_MATCH, number=88)
    partial["body"] = (
        "## Acceptance criteria\n- [ ] the ledger totals quota spend per block\n"
        "- [ ] a soft breach biases selection toward a cheaper tier\n\n"
        "## Verification plan\n- [ ] pytest\n"
    )
    runner = _SynthRunner(f"```json\n{DUPLICATE_PLAN}\n```", open_issues=[partial])
    res = synthesize(cfg, cycle_index=4, repo_root=tmp_path, runner=runner, ai_runner=runner)

    assert res.filed == [501] and res.flagged == [501] and res.skipped == []
    created = runner.created[0]
    assert DUPLICATE_LABEL in created["labels"]
    assert "#88" in created["body"] and created["body"].startswith("> **Possible duplicate")
    assert "## Problem" in created["body"]  # the ticket schema survives the annotation
    assert runner.wrote("gh", "issue", "close") == []
    assert runner.wrote("gh", "issue", "edit") == []


def test_unparseable_output_is_persisted_and_the_reason_named(tmp_path):
    cfg = _cfg()
    runner = _SynthRunner("PHASE 3: I could not decide, so here is prose instead.")
    res = synthesize(cfg, cycle_index=12, repo_root=tmp_path, runner=runner, ai_runner=runner)

    assert res.ok is False and res.filed == []
    saved = tmp_path / ".hsai" / "synthesis" / "12.txt"
    assert saved.exists() and "could not decide" in saved.read_text()
    assert res.raw_path == str(saved) and str(saved) in res.error
    assert "no JSON array" in res.error
    assert "no parseable ticket specs" not in res.error
    assert runner.wrote("gh", "issue", "create") == []


def test_dry_run_renders_the_prompt_and_writes_nothing(tmp_path):
    cfg = _cfg()
    runner = _SynthRunner(FENCED, open_issues=[OPEN_MATCH])
    res = synthesize(
        cfg, cycle_index=0, repo_root=tmp_path, runner=runner, ai_runner=runner, dry_run=True
    )
    assert res.ok is True and res.filed == []
    assert MEMORY_HEADING in res.prompt and "#77" in res.prompt
    assert runner.wrote("claude") == []  # no quota spent
    assert runner.wrote("gh", "issue", "create") == []


def test_backlog_is_fetched_once_per_pass(tmp_path):
    """Repo memory and dedupe share one fetch; two calls means two round trips."""
    cfg = _cfg()
    runner = _SynthRunner(FENCED)
    synthesize(cfg, cycle_index=0, repo_root=tmp_path, runner=runner, ai_runner=runner)
    assert len(runner.wrote("gh", "issue", "list")) == 2  # one open, one closed


# --- plain-text (non-JSON) CLI output must never break synthesis -------------
PLAIN_TEXT_OUTPUT = f"""PHASE 1 - DIVERGE: ten candidates considered.
PHASE 2 - REFLECT: three survived critique.
PHASE 3 - PRIORITIZE:
```json
{PLAN_JSON}
```"""


def test_synthesize_survives_output_without_a_json_envelope(tmp_path):
    """`payload is None` is a supported state, not a failure mode."""
    cfg = _cfg()
    runner = _SynthRunner(PLAIN_TEXT_OUTPUT)

    # The CLI exposed no structured envelope at all...
    result = ai.run_agent(
        "p", ModelChoice(tier="heavy", model="opus", rationale="t"), cfg, runner=runner
    )
    assert result.payload is None and result.usage is None
    assert result.text == PLAIN_TEXT_OUTPUT      # falls back to raw stdout

    # ...and synthesis still parses its ticket specs off the raw text and files them.
    res = synthesize(cfg, cycle_index=0, repo_root=tmp_path, runner=runner, ai_runner=runner)
    assert res.ok is True
    assert res.filed == [501]
    assert res.error == ""


def test_synthesize_reads_through_the_json_envelope(tmp_path):
    """With `--output-format json` the plan lives inside the envelope's result."""
    cfg = _cfg()
    envelope = json.dumps({"session_id": "s1", "result": PLAIN_TEXT_OUTPUT, "usage": {}})
    runner = _SynthRunner(envelope)
    res = synthesize(cfg, cycle_index=0, repo_root=tmp_path, runner=runner, ai_runner=runner)
    assert res.filed == [501]


def test_backlog_dataclass_defaults_are_empty():
    assert Backlog().open_issues == [] and Backlog().closed_issues == []
