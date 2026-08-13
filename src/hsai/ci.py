"""Continuous-integration gate.

"A green build" is declared ONCE, in the ``ci.steps`` manifest of
``.ai-swarm/core.yaml``. :func:`run_local` (the loop's pre-flight) and
``hsai ci`` execute that same manifest through :func:`run_steps`, and
``.github/workflows/ci.yml`` is a thin caller of ``hsai ci --scope remote``, so
local and remote cannot silently diverge; ``tests/test_ci_parity.py`` fails the
build if they ever do. ``wait_remote`` blocks until a PR's real GitHub checks
conclude - that remote result is still the source of truth for whether a change
may merge.
"""
from __future__ import annotations

import json
import re
import shlex
import time
from collections.abc import Iterable
from dataclasses import dataclass, field

import yaml

from .config import BOTH, DEFAULT_CI_JOB, LOCAL, REMOTE, CIStep, load_config
from .proc import Runner, run

# Rollup outcomes returned by wait_remote / _rollup_result.
SUCCESS = "SUCCESS"
FAILURE = "FAILURE"
PENDING = "PENDING"
TIMEOUT = "TIMEOUT"

_PASS_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}

# Used only when no manifest can be read (e.g. an ephemeral worktree without a
# core.yaml). Keeps the pre-flight honest instead of silently passing.
DEFAULT_STEPS: tuple[CIStep, ...] = (
    CIStep(id="ruff", command=("ruff", "check", "."), scope=BOTH),
    CIStep(id="pytest", command=("pytest",), scope=BOTH),
)

# PR-body evidence the SDLC contract requires (checked remotely; see the
# `sdlc-evidence` manifest step and cli.cmd_evidence_check).
PR_EVIDENCE_RULES: tuple[tuple[str, str], ...] = (
    (r"closes #[0-9]+", "PR body missing 'Closes #N' ticket link"),
    (r"##\s*model used", "PR body missing '## Model used' section"),
    (r"##\s*lesson learned", "PR body missing '## Lesson learned' section"),
)


@dataclass
class StepResult:
    """One executed manifest step, in a shape that survives ``--json``."""

    id: str
    command: tuple[str, ...]
    scope: str
    required: bool
    ok: bool
    returncode: int = 0

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "command": list(self.command),
            "scope": self.scope,
            "required": self.required,
            "ok": self.ok,
            "returncode": self.returncode,
        }


@dataclass
class CIResult:
    ok: bool
    steps: dict[str, bool] = field(default_factory=dict)
    log: str = ""
    records: list[StepResult] = field(default_factory=list)

    def summary(self) -> str:
        marks = ", ".join(f"{name}={'pass' if ok else 'FAIL'}" for name, ok in self.steps.items())
        return f"CI {'green' if self.ok else 'red'} ({marks})" if marks else (
            "CI green" if self.ok else "CI red"
        )

    def as_dict(self) -> dict:
        return {"ok": self.ok, "steps": [r.as_dict() for r in self.records]}

    def to_json(self) -> str:
        return json.dumps(self.as_dict(), indent=2)


def load_steps(cwd: str | None = None) -> tuple[CIStep, ...]:
    """Read the CI contract from the nearest core.yaml, or fall back."""
    try:
        cfg = load_config(cwd)
    except Exception:
        # No reachable/parsable manifest (an ephemeral worktree, or a config a
        # worker just broke): still lint and test rather than pass vacuously.
        # The breakage itself is reported by the config test in the suite.
        return DEFAULT_STEPS
    return cfg.ci_steps or DEFAULT_STEPS


def steps_for(
    steps: Iterable[CIStep], scope: str, *, job: str = DEFAULT_CI_JOB
) -> tuple[CIStep, ...]:
    """The manifest steps one `hsai ci` invocation runs for ``scope``/``job``."""
    return tuple(s for s in steps if s.job == job and s.in_scope(scope))


def run_steps(
    steps: Iterable[CIStep], *, cwd: str | None = None, runner: Runner = run
) -> CIResult:
    """Execute manifest steps in order; required failures make the build red."""
    ok = True
    marks: dict[str, bool] = {}
    records: list[StepResult] = []
    logs: list[str] = []

    for step in steps:
        p = runner(list(step.command), cwd=cwd)
        marks[step.id] = p.ok
        records.append(
            StepResult(
                id=step.id, command=tuple(step.command), scope=step.scope,
                required=step.required, ok=p.ok, returncode=p.code,
            )
        )
        logs.append(f"$ {step.display()}\n{p.stdout}\n{p.stderr}")
        if step.required and not p.ok:
            ok = False

    return CIResult(ok=ok, steps=marks, log="\n\n".join(logs), records=records)


def run_local(*, cwd: str | None = None, runner: Runner = run) -> CIResult:
    """Run the manifest's local steps. This defines what a 'green build' means.

    Identical code path to ``hsai ci --scope local`` - both read the same
    ``ci.steps`` manifest, so the pre-flight cannot drift from the contract.
    """
    return run_steps(steps_for(load_steps(cwd), LOCAL), cwd=cwd, runner=runner)


def missing_pr_evidence(pr_body: str) -> list[str]:
    """Which SDLC-evidence sections a PR body still lacks (empty = compliant)."""
    lowered = (pr_body or "").lower()
    return [msg for pattern, msg in PR_EVIDENCE_RULES if not re.search(pattern, lowered)]


# --- local/remote parity (see tests/test_ci_parity.py) --------------------------

# Commands that define a build's verdict. If one of these appears inline in the
# workflow it is a second definition of "green" - exactly the drift this module
# exists to prevent.
VERDICT_COMMANDS = frozenset(
    {"ruff", "pytest", "mypy", "flake8", "black", "pyright", "tox", "coverage", "grep", "pylint"}
)
_COMMAND_SEPARATORS = re.compile(r"\|\||&&|[|;]")
_DELEGATION_RE = re.compile(r"\bhsai\s+ci\b(?P<flags>[^|;&]*)")


def workflow_run_lines(workflow_yaml: str) -> list[str]:
    """Every shell line the workflow's ``run:`` steps execute."""
    data = yaml.safe_load(workflow_yaml) or {}
    lines: list[str] = []
    for job in (data.get("jobs") or {}).values():
        for step in (job or {}).get("steps", []) or []:
            script = (step or {}).get("run")
            if not script:
                continue
            lines.extend(line.strip() for line in str(script).splitlines() if line.strip())
    return lines


def _line_commands(line: str) -> list[list[str]]:
    """The argv of each command in a shell line (``a && b | c`` -> three)."""
    commands: list[list[str]] = []
    for part in _COMMAND_SEPARATORS.split(line):
        try:
            tokens = shlex.split(part)
        except ValueError:  # unbalanced quotes: fall back to a crude split
            tokens = part.split()
        if tokens:
            commands.append(tokens)
    return commands


def _command_words(line: str) -> list[str]:
    """The first word of each command in a shell line."""
    return [tokens[0] for tokens in _line_commands(line)]


def _flag_value(flags: list[str], name: str, default: str) -> str:
    for i, token in enumerate(flags[:-1]):
        if token == name:
            return flags[i + 1]
    return default


def delegated_jobs(run_lines: Iterable[str]) -> dict[str, set[str]]:
    """Map ``hsai ci`` invocations in a workflow to the job and scopes they run."""
    delegated: dict[str, set[str]] = {}
    for line in run_lines:
        for match in _DELEGATION_RE.finditer(line):
            flags = match.group("flags").split()
            job = _flag_value(flags, "--job", DEFAULT_CI_JOB)
            delegated.setdefault(job, set()).add(_flag_value(flags, "--scope", REMOTE))
    return delegated


def step_reachable(step: CIStep, run_lines: Iterable[str]) -> bool:
    """Does the workflow actually execute ``step`` - directly or via `hsai ci`?"""
    lines = list(run_lines)
    prefix = list(step.command[:2])
    if prefix:
        for line in lines:
            if any(tokens[: len(prefix)] == prefix for tokens in _line_commands(line)):
                return True
    scopes = delegated_jobs(lines).get(step.job, set())
    return any(step.in_scope(scope) for scope in scopes)


def unreachable_steps(steps: Iterable[CIStep], workflow_yaml: str) -> list[CIStep]:
    """Remote-facing manifest steps the workflow would never run."""
    lines = workflow_run_lines(workflow_yaml)
    return [s for s in steps if s.in_scope(REMOTE) and not step_reachable(s, lines)]


def bespoke_run_lines(workflow_yaml: str) -> list[str]:
    """Workflow lines that redefine 'green' inline instead of via the manifest."""
    return [
        line
        for line in workflow_run_lines(workflow_yaml)
        if any(word in VERDICT_COMMANDS for word in _command_words(line))
    ]


def _rollup_result(rollup: list[dict]) -> str:
    """Reduce a PR's statusCheckRollup array to SUCCESS / FAILURE / PENDING."""
    if not rollup:
        return PENDING
    for item in rollup:
        # CheckRun (GitHub Actions) uses status/conclusion; StatusContext uses state.
        if item.get("__typename") == "StatusContext" or "state" in item and "status" not in item:
            state = (item.get("state") or "").upper()
            if state in ("PENDING", "EXPECTED", ""):
                return PENDING
            if state not in _PASS_CONCLUSIONS:
                return FAILURE
        else:
            if (item.get("status") or "").upper() != "COMPLETED":
                return PENDING
            if (item.get("conclusion") or "").upper() not in _PASS_CONCLUSIONS:
                return FAILURE
    return SUCCESS


def poll_remote(pr_number: int, repo: str, *, runner: Runner = run) -> str:
    """One-shot: reduce a PR's current check rollup to SUCCESS/FAILURE/PENDING."""
    p = runner(
        ["gh", "pr", "view", str(pr_number), "--repo", repo, "--json", "statusCheckRollup"]
    )
    try:
        rollup = json.loads(p.stdout or "{}").get("statusCheckRollup", []) or []
    except json.JSONDecodeError:
        rollup = []
    return _rollup_result(rollup)


def wait_remote(
    pr_number: int,
    repo: str,
    *,
    timeout: float = 300,
    interval: float = 10,
    runner: Runner = run,
    sleep=time.sleep,
) -> str:
    """Block until a PR's remote checks conclude. Returns SUCCESS/FAILURE/TIMEOUT."""
    deadline = time.monotonic() + timeout
    while True:
        result = poll_remote(pr_number, repo, runner=runner)
        if result != PENDING:
            return result
        if time.monotonic() >= deadline:
            return TIMEOUT
        sleep(interval)
