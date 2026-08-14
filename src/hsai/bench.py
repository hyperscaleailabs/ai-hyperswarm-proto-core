"""`hsai bench`: a frozen replay suite that scores the harness's decisions.

The loop's premise is that it gets measurably better. Until this module existed
there was no measurement: guards were protected only by whichever unit test
their author happened to write, and a refactor of :func:`hsai.orchestrator.
decide_path`, :func:`hsai.models.select` or the guard chain merged on faith.

A *scenario* is a frozen YAML file under ``bench/scenarios/`` declaring

1. a **transcript** - canned ``git``/``gh``/``ruff``/``pytest``/``claude``
   responses keyed by command prefix, from which :class:`TranscriptRunner`
   builds a deterministic :data:`hsai.proc.Runner`, and
2. the **decisions** the harness is expected to make when driven by it: which
   path it takes (heal/implement/improve), which tier it selects, which guards
   fire, and how the run terminates.

:func:`run_scenario` then drives the *real* :func:`hsai.orchestrator.run_once`
against that runner in a throwaway directory and scores the four decisions. It
deliberately scores **decisions only** - never prompt text, commit messages or
PR wording - so re-wording a prompt does not rot the suite, while inverting a
branch of ``decide_path`` fails it immediately.

The whole suite is offline by construction: every subprocess the loop would
spawn is answered from the transcript, and a command with no matching rule
raises :class:`TranscriptError` rather than falling through to a real shell.

Synthesis: SWE-agent/SWE-agent (every claim about the agent is a benchmark
number, and CI gates the repo on it), microsoft/JARVIS (TaskBench: evaluate the
capability rather than demo it), openai/swarm (an ``evals`` directory, and
coordination that is "lightweight, controllable, and easily testable" - the
reason scoring targets decisions rather than transcripts), and
run-llama/llama_index (a hard numeric CI gate that blocks a merge on
regression, adopted here as the SCOREBOARD baseline check).
"""
from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from . import ledger, orchestrator, review
from .config import CoreConfig
from .orchestrator import run_once
from .proc import Proc

DEFAULT_SCENARIO_DIR = "bench/scenarios"
DEFAULT_SCOREBOARD = "bench/SCOREBOARD.md"
DEFAULT_BASELINE = "bench/baseline.json"

# Every scenario replays iteration 1 of block 0, so a seeded ledger and the
# budget gate line up with what `hsai cycle` would grade.
BENCH_ITERATION = 1
BENCH_BLOCK = 0

# Terminal outcome for a run the budget gate refused to start. The other
# outcomes come from :meth:`hsai.orchestrator.IterationResult.terminal`.
HALTED = "halted"

# Guards the bench layer itself observes (the budget gate runs in `hsai cycle`,
# above `run_once`; it is graded here through :func:`hsai.ledger.grade_block`).
GUARD_BUDGET_HARD = "budget_hard"
GUARD_BUDGET_SOFT = "budget_soft"

VALID_PATHS = (orchestrator.HEAL, orchestrator.IMPLEMENT, orchestrator.IMPROVE)
VALID_OUTCOMES = (
    orchestrator.MERGED,
    orchestrator.RECOVERED,
    orchestrator.IDLE,
    orchestrator.OPEN,
    HALTED,
)
VALID_GUARDS = (
    orchestrator.GUARD_NEEDS_REFINEMENT,
    orchestrator.GUARD_IDLE_DEDUPE,
    orchestrator.GUARD_WORKFLOW_REVERT,
    orchestrator.GUARD_COMPLETENESS,
    orchestrator.GUARD_REPRO,
    orchestrator.GUARD_REVIEW_BLOCK,
    orchestrator.GUARD_REMOTE_CI,
    GUARD_BUDGET_HARD,
    GUARD_BUDGET_SOFT,
)

# --- canned model output ----------------------------------------------------
# The transcript answers `claude -p` with these envelopes. They are fixtures,
# not expectations: nothing in the suite asserts on their wording.

AGENT_OK = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": 2,
        "result": "Implemented the change and added a test.",
        "session_id": "bench",
        "usage": {"input_tokens": 1500, "output_tokens": 320},
        "messages": [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {"path": "src/hsai/widget.py"}}
                ],
            },
            {"role": "user", "content": [{"type": "tool_result", "content": "widget source"}]},
        ],
    },
    sort_keys=True,
)

AGENT_PLAIN = "widget added, tests green\n"


def _verdict_envelope(verdict: dict) -> str:
    return json.dumps(
        {
            "type": "result",
            "result": f"Checked the diff.\n\n```json\n{json.dumps(verdict, sort_keys=True)}\n```\n",
            "usage": {"input_tokens": 400, "output_tokens": 60},
        },
        sort_keys=True,
    )


REVIEW_APPROVE = _verdict_envelope(
    {
        "approve": True,
        "blocking": [],
        "advisory": ["consider naming the helper"],
        "rationale": "Every acceptance criterion is covered by code and a test.",
    }
)
REVIEW_BLOCK = _verdict_envelope(
    {
        "approve": False,
        "blocking": ["src/hsai/widget.py: criterion 2 has no test proving it"],
        "advisory": [],
        "rationale": "The diff claims a criterion it never demonstrates.",
    }
)

PAYLOADS = {
    "agent_ok": AGENT_OK,
    "agent_plain": AGENT_PLAIN,
    "review_approve": REVIEW_APPROVE,
    "review_block": REVIEW_BLOCK,
}

# Refs the base transcript resolves from the scenario at replay time.
_DYNAMIC_REFS = ("open_issues", "issue_view", "remote_rollup")

# What the branch looks like to the review gate. Fixture, never asserted on.
DIFF_PATHS = "src/hsai/widget.py\ntests/test_widget.py\n"
DIFF_TEXT = "diff --git a/src/hsai/widget.py\n+def widget(): ...\n"

_WORKTREE_ADD = "worktree_add"
_WORKTREE_REMOVE = "worktree_remove"


class ScenarioError(ValueError):
    """A scenario file is malformed; it is refused, never scored as a pass."""


class TranscriptError(RuntimeError):
    """The transcript has no answer for a command the loop issued.

    Raised instead of falling through to a real subprocess: an incomplete
    transcript must fail the scenario loudly, never leak a `gh`/`claude` call.
    """


@dataclass(frozen=True)
class Response:
    """One canned :class:`hsai.proc.Proc` a transcript rule can return."""

    code: int = 0
    stdout: str = ""
    stderr: str = ""
    ref: str = ""  # named payload (see PAYLOADS / _DYNAMIC_REFS)


@dataclass(frozen=True)
class Rule:
    """A transcript entry: which command it answers, and with what.

    ``responses`` are consumed in order and the last one repeats, which is how a
    scenario says "CI is red, then green" without inventing a call counter.
    """

    cmd: tuple[str, ...]
    exact: bool = False  # argv must equal cmd, not merely start with it
    role: str = ""  # worker | reviewer - which `claude -p` invocation
    cwd_contains: str = ""
    arg_contains: str = ""
    responses: tuple[Response, ...] = (Response(),)
    effect: str = ""  # base-transcript side effect (worktree add/remove)

    def matches(self, cmd: list[str], cwd: str | None) -> bool:
        if self.exact:
            if cmd != list(self.cmd):
                return False
        elif cmd[: len(self.cmd)] != list(self.cmd):
            return False
        if self.cwd_contains and self.cwd_contains not in (cwd or ""):
            return False
        if self.arg_contains and self.arg_contains not in cmd:
            return False
        if self.role:
            is_reviewer = any(review.PROMPT_MARKER in a for a in cmd)
            if (self.role == "reviewer") != is_reviewer:
                return False
        return True


@dataclass(frozen=True)
class Expectation:
    """The decisions a scenario declares the harness must make."""

    outcome: str
    guards: tuple[str, ...]
    path: str = ""
    tier: str = ""


@dataclass(frozen=True)
class Scenario:
    """One frozen replay: a transcript plus the decisions it should produce."""

    name: str
    description: str
    expect: Expectation
    lesson: str = ""  # knowledge-base note this scenario was seeded from
    issues: tuple[dict, ...] = ()
    remote_ci: str = "SUCCESS"
    seed_files: tuple[tuple[str, str], ...] = ()
    ledger_seed: tuple[dict, ...] = ()
    rules: tuple[Rule, ...] = ()
    source: str = ""


# --- loading ----------------------------------------------------------------

_SCENARIO_KEYS = {
    "name", "description", "lesson", "issues", "remote_ci", "seed_files",
    "ledger", "transcript", "expect",
}
_RULE_KEYS = {
    "cmd", "exact", "role", "cwd_contains", "arg_contains",
    "code", "stdout", "stderr", "stdout_ref", "responses",
}
_RESPONSE_KEYS = {"code", "stdout", "stderr", "stdout_ref"}
_EXPECT_KEYS = {"path", "tier", "guards", "outcome"}
_LEDGER_KEYS = {
    "iteration", "block", "ticket", "kind", "tier", "model",
    "wall_clock_seconds", "attempts", "outcome", "input_tokens", "output_tokens",
}


def _require_mapping(value: object, what: str) -> dict:
    if not isinstance(value, dict):
        raise ScenarioError(f"{what} must be a mapping, got {type(value).__name__}")
    return value


def _reject_unknown(data: dict, allowed: set[str], what: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise ScenarioError(
            f"{what} has unknown key(s) {unknown}; allowed: {sorted(allowed)}"
        )


def _parse_response(data: dict, what: str) -> Response:
    _require_mapping(data, what)
    _reject_unknown(data, _RESPONSE_KEYS, what)
    ref = str(data.get("stdout_ref", "") or "")
    if ref and ref not in PAYLOADS:
        raise ScenarioError(
            f"{what}: unknown stdout_ref {ref!r}; known: {sorted(PAYLOADS)}"
        )
    if ref and data.get("stdout"):
        raise ScenarioError(f"{what}: set either stdout or stdout_ref, not both")
    return Response(
        code=int(data.get("code", 0)),
        stdout=str(data.get("stdout", "") or ""),
        stderr=str(data.get("stderr", "") or ""),
        ref=ref,
    )


def _parse_rule(data: object, what: str) -> Rule:
    data = _require_mapping(data, what)
    _reject_unknown(data, _RULE_KEYS, what)
    cmd = data.get("cmd")
    if not isinstance(cmd, list) or not cmd or not all(isinstance(a, str) for a in cmd):
        raise ScenarioError(f"{what}: 'cmd' must be a non-empty list of strings")
    role = str(data.get("role", "") or "")
    if role and role not in ("worker", "reviewer"):
        raise ScenarioError(f"{what}: 'role' must be 'worker' or 'reviewer', got {role!r}")
    if role and cmd[0] != "claude":
        raise ScenarioError(f"{what}: 'role' only applies to a `claude` rule")
    raw_responses = data.get("responses")
    if raw_responses is not None:
        if not isinstance(raw_responses, list) or not raw_responses:
            raise ScenarioError(f"{what}: 'responses' must be a non-empty list")
        if any(k in data for k in ("code", "stdout", "stderr", "stdout_ref")):
            raise ScenarioError(
                f"{what}: use either 'responses' or an inline code/stdout/stderr, not both"
            )
        responses = tuple(
            _parse_response(r, f"{what} response {i}") for i, r in enumerate(raw_responses)
        )
    else:
        responses = (_parse_response(
            {k: v for k, v in data.items() if k in _RESPONSE_KEYS}, what
        ),)
    return Rule(
        cmd=tuple(cmd),
        exact=bool(data.get("exact", False)),
        role=role,
        cwd_contains=str(data.get("cwd_contains", "") or ""),
        arg_contains=str(data.get("arg_contains", "") or ""),
        responses=responses,
    )


def _parse_expect(data: object, what: str) -> Expectation:
    data = _require_mapping(data, what)
    _reject_unknown(data, _EXPECT_KEYS, what)
    outcome = str(data.get("outcome", "") or "")
    if outcome not in VALID_OUTCOMES:
        raise ScenarioError(
            f"{what}: 'outcome' must be one of {list(VALID_OUTCOMES)}, got {outcome!r}"
        )
    if "guards" not in data:
        raise ScenarioError(
            f"{what}: 'guards' is required (declare an empty list when none fire)"
        )
    guards = data.get("guards") or []
    if not isinstance(guards, list) or not all(isinstance(g, str) for g in guards):
        raise ScenarioError(f"{what}: 'guards' must be a list of strings")
    unknown = sorted(set(guards) - set(VALID_GUARDS))
    if unknown:
        raise ScenarioError(f"{what}: unknown guard(s) {unknown}; known: {list(VALID_GUARDS)}")
    path = str(data.get("path", "") or "")
    if path and path not in VALID_PATHS:
        raise ScenarioError(f"{what}: 'path' must be one of {list(VALID_PATHS)}, got {path!r}")
    if not path and outcome != HALTED:
        raise ScenarioError(f"{what}: 'path' is required unless the outcome is '{HALTED}'")
    return Expectation(
        outcome=outcome, guards=tuple(guards), path=path,
        tier=str(data.get("tier", "") or ""),
    )


def _parse_issue(data: object, what: str) -> dict:
    """Normalise a scenario issue into the shape `gh issue list --json` emits."""
    data = _require_mapping(data, what)
    if "number" not in data or "title" not in data:
        raise ScenarioError(f"{what}: an issue needs at least 'number' and 'title'")
    labels = data.get("labels") or []
    assignees = data.get("assignees") or []
    if not isinstance(labels, list) or not isinstance(assignees, list):
        raise ScenarioError(f"{what}: 'labels' and 'assignees' must be lists")
    return {
        "number": int(data["number"]),
        "title": str(data["title"]),
        "body": str(data.get("body", "") or ""),
        "labels": [lb if isinstance(lb, dict) else {"name": str(lb)} for lb in labels],
        "assignees": [a if isinstance(a, dict) else {"login": str(a)} for a in assignees],
    }


def _parse_ledger_record(data: object, what: str) -> dict:
    data = _require_mapping(data, what)
    _reject_unknown(data, _LEDGER_KEYS, what)
    record = {
        "iteration": 0, "block": BENCH_BLOCK, "ticket": None, "kind": "implement",
        "tier": "standard", "model": "sonnet", "wall_clock_seconds": 0.0,
        "attempts": 1, "outcome": "merged",
    }
    record.update(data)
    if record["tier"] not in ("light", "standard", "heavy"):
        raise ScenarioError(f"{what}: unknown tier {record['tier']!r}")
    return record


def parse_scenario(data: object, *, source: str = "") -> Scenario:
    """Turn one parsed YAML document into a :class:`Scenario`, or refuse it.

    Every structural mistake is an error: a scenario that cannot be read is
    never quietly skipped or scored as a pass, which would make the benchmark
    report a green board for a suite it did not run.
    """
    where = source or "<scenario>"
    data = _require_mapping(data, where)
    _reject_unknown(data, _SCENARIO_KEYS, where)
    name = str(data.get("name", "") or "")
    if not name:
        raise ScenarioError(f"{where}: 'name' is required")
    description = str(data.get("description", "") or "")
    if not description:
        raise ScenarioError(f"{where}: 'description' is required")
    if "expect" not in data:
        raise ScenarioError(f"{where}: 'expect' is required")
    remote_ci = str(data.get("remote_ci", "SUCCESS") or "SUCCESS")
    if remote_ci not in ("SUCCESS", "FAILURE"):
        # PENDING would make `ci.wait_remote` poll until its timeout, which a
        # frozen suite must never do.
        raise ScenarioError(f"{where}: 'remote_ci' must be SUCCESS or FAILURE")
    seed = _require_mapping(data.get("seed_files") or {}, f"{where}.seed_files")
    transcript = data.get("transcript") or []
    if not isinstance(transcript, list):
        raise ScenarioError(f"{where}: 'transcript' must be a list of rules")
    issues = data.get("issues") or []
    if not isinstance(issues, list):
        raise ScenarioError(f"{where}: 'issues' must be a list")
    seeded = data.get("ledger") or []
    if not isinstance(seeded, list):
        raise ScenarioError(f"{where}: 'ledger' must be a list of records")
    return Scenario(
        name=name,
        description=description,
        lesson=str(data.get("lesson", "") or ""),
        expect=_parse_expect(data["expect"], f"{where}.expect"),
        issues=tuple(
            _parse_issue(i, f"{where}.issues[{n}]") for n, i in enumerate(issues)
        ),
        remote_ci=remote_ci,
        seed_files=tuple((str(k), str(v)) for k, v in seed.items()),
        ledger_seed=tuple(
            _parse_ledger_record(r, f"{where}.ledger[{n}]") for n, r in enumerate(seeded)
        ),
        rules=tuple(
            _parse_rule(r, f"{where}.transcript[{n}]") for n, r in enumerate(transcript)
        ),
        source=source,
    )


def load_scenario(path: str | Path) -> Scenario:
    """Read and validate a single scenario file."""
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ScenarioError(f"{path.name}: not valid YAML - {exc}") from exc
    scenario = parse_scenario(data, source=path.name)
    if scenario.name != path.stem:
        raise ScenarioError(
            f"{path.name}: 'name' is {scenario.name!r} but the file is {path.stem!r}; "
            "they must match so a failure names the file to open"
        )
    return scenario


def load_scenarios(
    scenario_dir: str | Path, *, repo_root: str | Path | None = None
) -> list[Scenario]:
    """Load every scenario in ``scenario_dir``, sorted by name.

    When ``repo_root`` is given, every ``lesson:`` citation is checked against
    the knowledge base: the point of citing a note is that the scenario stays
    traceable to the run that taught the loop this rule, and a dangling
    citation is not traceable.
    """
    scenario_dir = Path(scenario_dir)
    if not scenario_dir.is_dir():
        raise ScenarioError(f"no scenario directory at {scenario_dir}")
    files = sorted(scenario_dir.glob("*.yaml"))
    if not files:
        raise ScenarioError(f"no scenarios (*.yaml) under {scenario_dir}")
    scenarios = [load_scenario(f) for f in files]
    if repo_root is not None:
        lessons = Path(repo_root) / "knowledge" / "lessons"
        for scenario in scenarios:
            if scenario.lesson and not (lessons / f"{scenario.lesson}.md").is_file():
                raise ScenarioError(
                    f"{scenario.source}: cites lesson {scenario.lesson!r}, "
                    f"which does not exist under {lessons}"
                )
    return scenarios


# --- replay -----------------------------------------------------------------


def _base_rules(repo_root: str) -> list[Rule]:
    """The transcript every scenario starts from: one green implement run.

    Scenario rules are matched *before* these, so a scenario declares only what
    makes it distinctive (a red build, a dirty worktree, a blocking reviewer)
    rather than re-stating the whole loop.
    """
    ok = (Response(),)
    return [
        Rule(cmd=("gh", "api", "user"), responses=(Response(stdout="hsai-bot\n"),)),
        Rule(
            cmd=("git", "rev-parse", "--show-toplevel"),
            responses=(Response(stdout=f"{repo_root}\n"),),
        ),
        Rule(cmd=("git", "worktree", "add"), effect=_WORKTREE_ADD, responses=ok),
        Rule(cmd=("git", "worktree", "remove"), effect=_WORKTREE_REMOVE, responses=ok),
        Rule(cmd=("git", "merge-base"), responses=(Response(stdout="benchbase\n"),)),
        Rule(
            cmd=("git", "diff"), arg_contains="--name-only",
            responses=(Response(stdout=DIFF_PATHS),),
        ),
        Rule(cmd=("git", "diff"), responses=(Response(stdout=DIFF_TEXT),)),
        Rule(cmd=("git", "status"), responses=ok),
        *(
            Rule(cmd=("git", verb), responses=ok)
            for verb in ("fetch", "checkout", "clean", "add", "commit", "push")
        ),
        Rule(cmd=("ruff", "check"), responses=ok),
        # Bare `pytest` is the local CI gate; `pytest <files>` is the repro
        # guard, whose pre-fix (parent) run must FAIL for the bug to be real.
        Rule(cmd=("pytest",), exact=True, responses=ok),
        Rule(
            cmd=("pytest",), cwd_contains="repro-check-",
            responses=(Response(code=1, stderr="pytest: fails on the pre-fix tree\n"),),
        ),
        Rule(cmd=("pytest",), responses=ok),
        Rule(cmd=("claude",), role="reviewer", responses=(Response(ref="review_approve"),)),
        Rule(cmd=("claude",), role="worker", responses=(Response(ref="agent_ok"),)),
        Rule(cmd=("gh", "issue", "list"), responses=(Response(ref="open_issues"),)),
        Rule(
            cmd=("gh", "issue", "create"),
            responses=(Response(stdout="https://github.com/o/r/issues/900\n"),),
        ),
        Rule(cmd=("gh", "issue", "view"), responses=(Response(ref="issue_view"),)),
        Rule(cmd=("gh", "issue", "edit"), responses=ok),
        Rule(
            cmd=("gh", "pr", "create"),
            responses=(Response(stdout="https://github.com/o/r/pull/500\n"),),
        ),
        Rule(cmd=("gh", "pr", "view"), responses=(Response(ref="remote_rollup"),)),
        Rule(cmd=("gh", "pr", "merge"), responses=ok),
        Rule(cmd=("gh", "pr", "close"), responses=ok),
    ]


class TranscriptRunner:
    """A deterministic :data:`hsai.proc.Runner` built from a scenario.

    Every call is answered from the scenario's rules (then the base transcript);
    nothing is ever executed. ``git worktree add``/``remove`` are the one place
    the runner touches disk - it materialises the throwaway worktree the real
    command would have created, seeded with the scenario's ``seed_files``, so
    guards that read files out of the worktree (the repro guard copies the new
    test onto the pre-fix tree) exercise their real code path.
    """

    def __init__(self, scenario: Scenario, *, repo_root: str) -> None:
        self.scenario = scenario
        self.repo_root = repo_root
        self.rules = [*scenario.rules, *_base_rules(repo_root)]
        self.calls: list[list[str]] = []
        self._consumed: dict[int, int] = {}

    def __call__(
        self, cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None
    ) -> Proc:
        cmd = [str(a) for a in cmd]
        self.calls.append(cmd)
        for index, rule in enumerate(self.rules):
            if rule.matches(cmd, cwd):
                response = self._next(index, rule)
                self._apply_effect(rule, cmd)
                return Proc(cmd, response.code, self._render(response, cmd), response.stderr)
        raise TranscriptError(
            f"scenario {self.scenario.name!r}: no transcript rule for {cmd[:4]!r}"
        )

    def _next(self, index: int, rule: Rule) -> Response:
        seen = self._consumed.get(index, 0)
        self._consumed[index] = seen + 1
        return rule.responses[min(seen, len(rule.responses) - 1)]

    def _render(self, response: Response, cmd: list[str]) -> str:
        if not response.ref:
            return response.stdout
        if response.ref == "open_issues":
            return json.dumps(list(self.scenario.issues))
        if response.ref == "issue_view":
            number = int(cmd[3]) if len(cmd) > 3 and cmd[3].isdigit() else 0
            match = next((i for i in self.scenario.issues if i["number"] == number), None)
            return json.dumps(
                match
                or {"number": number, "title": "", "labels": [], "assignees": [], "body": ""}
            )
        if response.ref == "remote_rollup":
            return json.dumps(
                {
                    "statusCheckRollup": [
                        {
                            "__typename": "CheckRun",
                            "status": "COMPLETED",
                            "conclusion": self.scenario.remote_ci,
                        }
                    ]
                }
            )
        return PAYLOADS[response.ref]

    def _apply_effect(self, rule: Rule, cmd: list[str]) -> None:
        if not rule.effect:
            return
        path = next((a for a in cmd if a.startswith(self.repo_root + "/")), "")
        if not path:
            return
        if rule.effect == _WORKTREE_REMOVE:
            shutil.rmtree(path, ignore_errors=True)
            return
        Path(path).mkdir(parents=True, exist_ok=True)
        if "-b" not in cmd:  # a detached pre-fix worktree starts empty
            return
        for rel, content in self.scenario.seed_files:
            target = Path(path) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")


# --- scoring ----------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    """One scored decision: what the scenario declared vs what the loop did."""

    name: str
    expected: str
    actual: str

    @property
    def ok(self) -> bool:
        return self.expected == self.actual

    def render(self) -> str:
        return f"{self.name}: expected {self.expected or '-'}, got {self.actual or '-'}"


@dataclass
class ScenarioScore:
    scenario: str
    checks: list[Check] = field(default_factory=list)
    error: str = ""
    # Every binary the replay invoked. Asserted against an allow-list by the
    # suite's own tests: the benchmark is only meaningful if it is offline.
    binaries: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.error and all(c.ok for c in self.checks)

    def failures(self) -> list[Check]:
        return [c for c in self.checks if not c.ok]

    def reason(self) -> str:
        if self.error:
            return self.error
        return "; ".join(c.render() for c in self.failures())


def _tier_of(model: str, cfg: CoreConfig) -> str:
    """Map the selected model back to its configured tier (audit, not guess)."""
    for name, tier in cfg.tiers.items():
        if tier.model == model:
            return name
    return model


def _seed_ledger(scenario: Scenario, cfg: CoreConfig, repo_dir: Path) -> None:
    for record in scenario.ledger_seed:
        ledger.append_record(
            ledger.ledger_path(cfg, repo_dir), ledger.LedgerRecord(**record)
        )


def run_scenario(scenario: Scenario, cfg: CoreConfig) -> ScenarioScore:
    """Drive the real ``run_once`` against ``scenario`` and score its decisions.

    Runs entirely inside a throwaway directory: the loop's worktrees, lessons,
    ledger and trajectories are written there and discarded, so a bench run
    leaves the checkout untouched.
    """
    score = ScenarioScore(scenario=scenario.name)
    repo_dir = Path(tempfile.mkdtemp(prefix=f"hsai-bench-{scenario.name}-"))
    try:
        _seed_ledger(scenario, cfg, repo_dir)
        # The budget gate lives in `hsai cycle`, one level above `run_once`;
        # grade it here through the same helper the cycle uses.
        _, decision = ledger.grade_block(
            ledger.ledger_path(cfg, repo_dir), BENCH_BLOCK, cfg.budget
        )
        guards: list[str] = []
        if decision.halt:
            guards.append(GUARD_BUDGET_HARD)
            path, tier, outcome = "", "", HALTED
        else:
            if decision.demote:
                guards.append(GUARD_BUDGET_SOFT)
            runner = TranscriptRunner(scenario, repo_root=str(repo_dir))
            try:
                result = run_once(
                    cfg, repo_dir=str(repo_dir), dry_run=False,
                    runner=runner, ai_runner=runner,
                    iteration=BENCH_ITERATION, block=BENCH_BLOCK,
                    demote_tier=decision.demote,
                )
            finally:
                score.binaries = tuple(sorted({c[0] for c in runner.calls if c}))
            guards.extend(result.guards)
            path, tier, outcome = result.kind, _tier_of(result.model, cfg), result.terminal()
    # A scenario that explodes is a failing scenario, named and reported, not a
    # crashed suite: the remaining scenarios still have to be scored.
    except Exception as exc:  # noqa: BLE001 - reported as this scenario's failure
        score.error = f"{type(exc).__name__}: {exc}"
        return score
    finally:
        shutil.rmtree(repo_dir, ignore_errors=True)

    expect = scenario.expect
    score.checks.append(Check("path", expect.path, path))
    if expect.tier:
        score.checks.append(Check("tier", expect.tier, tier))
    score.checks.append(
        Check("guards", ",".join(expect.guards), ",".join(guards))
    )
    score.checks.append(Check("outcome", expect.outcome, outcome))
    return score


@dataclass
class SuiteResult:
    scores: list[ScenarioScore] = field(default_factory=list)
    scenarios: list[Scenario] = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for s in self.scores if s.ok)

    @property
    def total(self) -> int:
        return len(self.scores)

    @property
    def aggregate(self) -> float:
        return round(self.passed / self.total, 4) if self.total else 0.0

    @property
    def ok(self) -> bool:
        return bool(self.scores) and self.passed == self.total

    def failures(self) -> list[ScenarioScore]:
        return [s for s in self.scores if not s.ok]

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "total": self.total,
            "aggregate": self.aggregate,
            "scenarios": [
                {
                    "name": score.scenario,
                    "ok": score.ok,
                    "error": score.error,
                    "binaries": list(score.binaries),
                    "checks": [
                        {"name": c.name, "expected": c.expected, "actual": c.actual, "ok": c.ok}
                        for c in score.checks
                    ],
                }
                for score in self.scores
            ],
        }


def run_suite(scenarios: list[Scenario], cfg: CoreConfig) -> SuiteResult:
    return SuiteResult(
        scores=[run_scenario(s, cfg) for s in scenarios], scenarios=list(scenarios)
    )


# --- reporting --------------------------------------------------------------


def render_table(result: SuiteResult) -> str:
    """The per-scenario table `hsai bench` prints (and the scoreboard embeds)."""
    rows = [
        "| scenario | path | tier | guards | outcome | score |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    by_name = {s.name: s for s in result.scenarios}
    for score in result.scores:
        scenario = by_name.get(score.scenario)
        expect = scenario.expect if scenario else Expectation(outcome="?", guards=())
        rows.append(
            f"| `{score.scenario}` | {expect.path or '-'} | {expect.tier or '-'} "
            f"| {', '.join(expect.guards) or '-'} | {expect.outcome} "
            f"| {'PASS' if score.ok else 'FAIL'} |"
        )
    return "\n".join(rows)


def render_scoreboard(result: SuiteResult, baseline: dict) -> str:
    """The committed board, regenerated by every full `hsai bench` run.

    Deliberately free of timestamps and paths: it is diffed in CI, so it must
    change only when a *decision* changes.
    """
    lines = [
        "# hsai bench scoreboard",
        "",
        "> Generated by `hsai bench`. Do not edit by hand - re-run the command.",
        "",
        "Each scenario replays a frozen transcript through the real",
        "`orchestrator.run_once` and scores four decisions: the path taken, the tier",
        "selected, which guards fired, and how the run terminated. Prompt and",
        "commit-message wording is never asserted on.",
        "",
        render_table(result),
        "",
        f"**aggregate: {result.passed}/{result.total} scenarios "
        f"({result.aggregate:.2f})**",
        "",
        f"Committed baseline: aggregate >= {float(baseline['min_aggregate_score']):.2f} "
        f"over >= {int(baseline['min_scenarios'])} scenario(s).",
        "",
    ]
    for score in result.failures():
        lines.append(f"- FAIL `{score.scenario}`: {score.reason()}")
    if result.failures():
        lines.append("")
    return "\n".join(lines)


def load_baseline(path: str | Path) -> dict:
    """Read the committed numeric gate (the merge-blocking part of the suite)."""
    path = Path(path)
    if not path.is_file():
        raise ScenarioError(f"no bench baseline at {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("min_aggregate_score", "min_scenarios"):
        if key not in data:
            raise ScenarioError(f"{path}: baseline is missing {key!r}")
    return data


def check_baseline(result: SuiteResult, baseline: dict) -> list[str]:
    """Regressions against the committed baseline, as human-readable lines.

    Scenario *count* is part of the gate on purpose: deleting a failing
    scenario would otherwise raise the aggregate score and look like progress.
    """
    problems: list[str] = []
    minimum = float(baseline["min_aggregate_score"])
    if result.aggregate < minimum:
        problems.append(
            f"aggregate score {result.aggregate:.2f} is below the committed "
            f"baseline {minimum:.2f}"
        )
    required = int(baseline["min_scenarios"])
    if result.total < required:
        problems.append(
            f"suite has {result.total} scenario(s), below the committed "
            f"baseline of {required}"
        )
    return problems
