"""Worker trajectories: the durable record of what one agent run actually did.

Before this module a ``claude -p`` run left nothing behind but a boolean and a
truncated stderr excerpt, so the loop could not answer forensic questions about
its own behaviour - what a failed worker tried, where the quota went, whether a
retry differed from its predecessor. A :class:`Trajectory` is that record: one
JSON file per agent run under ``.hsai/traj/<block>/<iteration>.json``, written
at the single invocation choke point (right after ``ai.run_agent``) so every
run is captured, including the ones a guard aborts moments later.

Sharding by block is what makes the store bounded: :func:`prune` drops whole
block directories beyond ``execution.trajectory_retention_blocks`` on each
cycle, so forensics stay available for the recent past without growing without
limit.

Two audiences, deliberately separated:

- **local, complete** - the trajectory file itself. It quotes repo content and
  therefore stays out of git (``.hsai/`` is ignored); ``hsai replay <id>``
  reconstructs it without spending any quota.
- **committed, redacted** - :meth:`Trajectory.excerpt`, a secrets-scrubbed tail
  of the last few steps embedded in the lesson note. The knowledge base gains
  signal without becoming a mirror of the working tree.

Synthesis: SWE-agent (persist a ``.traj`` per run and build a replay/inspector
on it - the run record, not just the final patch, is the primary artifact),
microsoft/JARVIS (intermediate stage results must be separately addressable,
hence per-step data rather than a final blob), langchain (observability as a
cross-cutting layer captured at one choke point) and openai/swarm (the runner
returns the full message list, so callers never reconstruct what happened).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TRAJECTORY_DIR = ".hsai/traj"

# Per-step text is clipped so one runaway tool result cannot bloat the store.
STEP_CHARS = 2000
# What the committed lesson may quote: a short tail, tightly clipped.
EXCERPT_STEPS = 5
EXCERPT_CHARS = 240

REDACTED = "[redacted]"

_SECRET_PATTERNS = (
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(r"(?i)\b\w*(?:api[_-]?key|token|secret|password)\w*\b\s*[:=]\s*\S+"),
)

# Absolute home paths identify the machine (and its user) the loop ran on, and
# every worker prompt is full of them. Collapse any `/Users/<name>` or
# `/home/<name>` prefix to `~` so a trajectory can be shared as-is.
_HOME_PATTERN = re.compile(r"/(?:Users|home)/[^/\s:\"']+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def redact(text: str, extra: Iterable[str] = ()) -> str:
    """Scrub credentials and absolute home paths out of ``text``.

    ``extra`` is a list of literal strings to blank as well - the live values of
    the environment variables named by ``constraints.forbid_env`` (see
    :func:`secret_env_values`). Pattern matching catches secrets that *look*
    like secrets; the literal list catches the ones that do not.
    """
    out = text or ""
    for literal in extra:
        if literal:
            out = out.replace(literal, REDACTED)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(REDACTED, out)
    out = _HOME_PATTERN.sub("~", out)
    # Also catch a home dir living somewhere else entirely (`$HOME=/opt/...`).
    # Guarded: a degenerate home of "/" must not rewrite every path there is.
    home = str(Path.home())
    return out.replace(home, "~") if len(home) > 1 else out


def redact_value(value: Any, extra: Iterable[str] = ()) -> Any:
    """Recursively :func:`redact` every string in a JSON-ish structure.

    Applied to the whole record just before it is written, so a field nobody
    scrubbed at capture time (the prompt, most importantly) cannot leak. Only
    string *values* are rewritten - keys and numbers are left intact, so the
    usage counts stay machine-readable.
    """
    extra = tuple(extra)
    if isinstance(value, str):
        return redact(value, extra)
    if isinstance(value, dict):
        return {k: redact_value(v, extra) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v, extra) for v in value]
    return value


# An environment variable whose NAME reads like a credential holds a value we
# must never serialize, whether or not `forbid_env` happens to name it.
_SECRET_ENV_NAME = re.compile(
    r"(?i)(?:^|_)(key|token|secret|password|passwd|credential|credentials|session)(?:$|_)"
)

# Below this length a value is too short to be a credential and too likely to be
# a common word ("1", "true", "on"); blanking it would corrupt the record.
_MIN_SECRET_LEN = 6


def secret_env_values(
    forbid: Iterable[str] = (), environ: Mapping[str, str] | None = None
) -> tuple[str, ...]:
    """The live values that must never reach a serialized record.

    Union of two sources: every variable named by ``constraints.forbid_env``
    (the subscription-only guard's list, which is the one the audit cares
    about) and every variable whose name reads like a credential. Only the
    values are returned - the *names* are safe to record, and an iteration
    trajectory keeps them so the scrub itself is auditable.
    """
    env = dict(os.environ if environ is None else environ)
    names = {str(n) for n in forbid} | {n for n in env if _SECRET_ENV_NAME.search(n)}
    values = [
        (env.get(n) or "").strip()
        for n in sorted(names)
        if len((env.get(n) or "").strip()) >= _MIN_SECRET_LEN
    ]
    # De-duplicated, longest first: a longer value must be blanked before a
    # shorter one that is a substring of it, or the tail would survive.
    return tuple(sorted(dict.fromkeys(values), key=len, reverse=True))


def _clip(text: str, limit: int = STEP_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit] + f"... [+{len(text) - limit} chars]"


@dataclass
class Step:
    """One addressable event in an agent run (a message, tool call, or result)."""

    index: int
    kind: str
    name: str = ""
    text: str = ""

    def render(self, limit: int = STEP_CHARS) -> str:
        head = f"{self.index:>3}. {self.kind}" + (f"({self.name})" if self.name else "")
        body = _clip(self.text, limit)
        return f"{head}: {body}" if body else head


def _blocks(message: dict[str, Any]) -> list[dict[str, Any]]:
    content = message.get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _block_text(value: Any) -> str:
    """Flatten a content value (string, block, or list of blocks) to text."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return str(value.get("text", "")) or json.dumps(value, sort_keys=True)
    if isinstance(value, list):
        return "\n".join(_block_text(v) for v in value)
    return "" if value is None else str(value)


def steps_from_output(raw: dict[str, Any] | None, output: str) -> list[Step]:
    """Derive the step stream from a parsed ``claude -p`` envelope.

    ``raw`` is ``None`` whenever the CLI did not emit JSON (an older binary, a
    crash); the whole plain-text output then becomes a single step so the
    trajectory is still a valid, replayable record.
    """
    if not isinstance(raw, dict):
        text = _clip(redact(output or ""))
        return [Step(index=1, kind="output", text=text)] if text else []

    steps: list[Step] = []

    def add(kind: str, text: str, name: str = "") -> None:
        steps.append(Step(index=len(steps) + 1, kind=kind, name=name,
                          text=_clip(redact(text))))

    messages = raw.get("messages")
    if isinstance(messages, list):
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or message.get("type") or "message")
            for block in _blocks(message):
                btype = str(block.get("type", "text"))
                if btype == "tool_use":
                    add("tool_use", json.dumps(block.get("input", {}), sort_keys=True),
                        name=str(block.get("name", "")))
                elif btype == "tool_result":
                    add("tool_result", _block_text(block.get("content")))
                else:
                    add(role, _block_text(block.get("text", "")))

    result = raw.get("result")
    if isinstance(result, str) and result.strip():
        add("result", result)
    return steps


@dataclass
class Trajectory:
    """One agent run, start to finish - the unit the store persists."""

    iteration: int
    ticket: int | None
    kind: str
    tier: str
    model: str
    prompt: str
    block: int = 0
    prompt_digest: str = ""
    session_id: str = ""
    steps: list[Step] = field(default_factory=list)
    ok: bool = True
    exit_status: str = "ok"
    error: str = ""
    usage: dict[str, Any] | None = None
    num_turns: int | None = None
    duration_seconds: float = 0.0
    outcome: str = "ran"
    created: str = field(default_factory=_now)

    @property
    def identifier(self) -> str:
        """Stable id, and the file stem: the iteration number.

        Iterations are globally unique (a block numbers its runs
        ``block * 100 + n``), so this addresses exactly one run and is what
        ``hsai traj <iteration>`` takes.
        """
        return str(self.iteration)

    def to_json(self) -> str:
        return json.dumps(redact_value(asdict(self)), indent=2, sort_keys=True)

    def tokens(self) -> tuple[int, int] | None:
        """``(input, output)`` token counts, or ``None`` if unreported."""
        if not self.usage:
            return None
        inp, out = self.usage.get("input_tokens"), self.usage.get("output_tokens")
        if inp is None and out is None:
            return None
        return int(inp or 0), int(out or 0)

    def usage_summary(self) -> str:
        if not self.usage:
            return "usage: (not reported)"
        parts = ", ".join(f"{k}={self.usage[k]}" for k in sorted(self.usage))
        return f"usage: {parts}"

    def tools_used(self) -> list[str]:
        """Distinct tool names invoked, in first-seen order."""
        seen: list[str] = []
        for step in self.steps:
            if step.kind == "tool_use" and step.name and step.name not in seen:
                seen.append(step.name)
        return seen

    def execution_trace(self) -> str:
        """Committed telemetry table for the lesson's '## Execution trace'.

        This is the fix for the other half of the defect this module exists
        for: a null token dimension. When ``usage`` never arrived (an older
        CLI, a crash mid-run, an envelope without a usage object) this says so
        explicitly - ``telemetry=unavailable`` - rather than the ledger's
        silent ``null`` or the lesson simply not mentioning it.
        """
        toks = self.tokens()
        telemetry = "ok" if toks else "unavailable"
        tokens_cell = f"{toks[0]} in / {toks[1]} out" if toks else "unavailable"
        tools = self.tools_used()
        tools_cell = ", ".join(f"`{t}`" for t in tools) if tools else "_(none recorded)_"
        turns_cell = str(self.num_turns) if self.num_turns is not None else "unavailable"
        return (
            "| field | value |\n"
            "| --- | --- |\n"
            f"| turns | {turns_cell} |\n"
            f"| tools used | {tools_cell} |\n"
            f"| tokens | {tokens_cell} |\n"
            f"| exit status | {self.exit_status} |\n"
            f"| duration | {self.duration_seconds:.1f}s |\n"
            f"| telemetry | {telemetry} |\n"
            f"| replay | `hsai traj {self.identifier}` |"
        )

    def first_failing_step(self) -> str:
        """The earliest step that looks like a failure - where to start reading."""
        markers = ("error", "failed", "failure", "traceback", "exit code 1")
        for step in self.steps:
            text = step.text.lower()
            if any(m in text for m in markers):
                return f"step {step.index} ({step.kind})"
        return "none"

    def digest(self) -> str:
        """One compact audit line: tokens, duration, exit status, first failure.

        Committed - it goes into the lesson and the PR body - so it carries
        counters and pointers only, never quoted run content.
        """
        toks = self.tokens()
        tokens = f"{toks[0]}in/{toks[1]}out" if toks else "unreported"
        return (
            f"tokens={tokens}, duration={self.duration_seconds:.1f}s, "
            f"exit={self.exit_status}, outcome={self.outcome}, "
            f"first-failing-step={self.first_failing_step()}, "
            f"replay=`hsai traj {self.identifier}`"
        )

    def excerpt(self, steps: int = EXCERPT_STEPS, limit: int = EXCERPT_CHARS) -> str:
        """A redacted tail - what the committed lesson is allowed to quote.

        Never includes the prompt or the earlier steps: the knowledge base gets
        signal about how the run ended, not a copy of the working tree.
        """
        if not self.steps:
            return "(no steps recorded)"
        tail = self.steps[-steps:]
        lines = [s.render(limit) for s in tail]
        dropped = len(self.steps) - len(tail)
        if dropped:
            lines.insert(0, f"... {dropped} earlier step(s) elided")
        return "\n".join(redact(line) for line in lines)

    def render(self) -> str:
        """Human-readable reconstruction (what ``hsai replay`` prints)."""
        ticket = f"#{self.ticket}" if self.ticket else "(none)"
        head = [
            f"trajectory {self.identifier}  [{self.kind}] ticket {ticket} block {self.block}",
            f"model: {self.model} (tier={self.tier})  duration: {self.duration_seconds:.3f}s",
            f"exit: {self.exit_status}  ok={self.ok}  outcome: {self.outcome}",
            self.usage_summary(),
            f"session: {self.session_id or '(not reported)'}",
            f"prompt digest: {self.prompt_digest or '(none)'}",
            f"created: {self.created}",
            "",
            "--- prompt ---",
            self.prompt,
            "",
            f"--- steps ({len(self.steps)}) ---",
        ]
        body = [s.render() for s in self.steps] or ["(no steps recorded)"]
        tail = ["", f"--- outcome: {self.outcome} ---"]
        if self.error:
            tail += ["--- error ---", self.error]
        return "\n".join(head + body + tail)


def trajectory_dir(repo_root: str | Path) -> Path:
    return Path(repo_root) / TRAJECTORY_DIR


def block_dir(repo_root: str | Path, block: int) -> Path:
    return trajectory_dir(repo_root) / str(block)


def path_for(repo_root: str | Path, identifier: str, block: int) -> Path:
    return block_dir(repo_root, block) / f"{identifier}.json"


def find(repo_root: str | Path, identifier: str) -> Path | None:
    """Locate one iteration's trajectory without knowing which block it is in."""
    # Ids only - a path is resolved by the caller, never fed to glob().
    if not identifier or not identifier.isdigit():
        return None
    matches = sorted(trajectory_dir(repo_root).glob(f"*/{identifier}.json"))
    return matches[0] if matches else None


def write(traj: Trajectory, repo_root: str | Path) -> Path:
    """Persist (or refresh) one trajectory as a single redacted JSON file."""
    path = path_for(repo_root, traj.identifier, traj.block)
    path.parent.mkdir(parents=True, exist_ok=True)
    # to_json() redacts: nothing reaches disk before the scrub pass.
    path.write_text(traj.to_json(), encoding="utf-8")
    return path


def read(path: str | Path) -> Trajectory:
    """Parse a trajectory back off disk."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    steps = [Step(**s) for s in data.pop("steps", []) or []]
    return Trajectory(steps=steps, **data)


def load(repo_root: str | Path, identifier: str) -> Trajectory:
    """Resolve ``identifier`` (an iteration number or a path) and read it."""
    found = find(repo_root, identifier)
    for candidate in (found, Path(identifier)):
        if candidate is not None and candidate.is_file():
            return read(candidate)
    raise FileNotFoundError(
        f"no trajectory {identifier!r} under {trajectory_dir(repo_root)}"
    )


def prune(repo_root: str | Path, keep_blocks: int) -> list[int]:
    """Drop trajectory block directories older than the newest ``keep_blocks``.

    Trajectories are local forensics, not repo content: they are worth keeping
    for the recent past and worth bounding beyond it. Returns the blocks removed
    (a non-positive ``keep_blocks`` disables pruning entirely).
    """
    root = trajectory_dir(repo_root)
    if keep_blocks <= 0 or not root.is_dir():
        return []
    blocks = sorted(
        int(d.name) for d in root.iterdir() if d.is_dir() and d.name.lstrip("-").isdigit()
    )
    dropped = blocks[:-keep_blocks] if len(blocks) > keep_blocks else []
    for block in dropped:
        target = root / str(block)
        for child in target.iterdir():
            child.unlink()
        target.rmdir()
    return dropped


def prompt_digest(prompt: str) -> str:
    """Short content hash of a prompt - enough to tell two runs apart."""
    return hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()[:12]


# --- iteration trajectories -------------------------------------------------
#
# A :class:`Trajectory` above is one *agent run*. An
# :class:`IterationTrajectory` is one *iteration of the loop*: the decisions the
# orchestrator made around that run (which path, which tier, which guards fired,
# how the ticket ended up). That is the granularity `hsai bench` replays, so it
# is a separate, independently versioned record rather than more fields bolted
# onto the run.

ITERATION_DIR = ".hsai/trajectories"

# Bumped whenever a field is removed or its meaning changes; readers reject a
# version they were not written for rather than silently misreading it. Adding
# an optional field is backwards compatible and does NOT bump this.
ITERATION_SCHEMA_VERSION = 1

# Size caps. A trajectory is forensics, not an archive: everything that can grow
# with the size of the repo or the run is bounded here.
PROMPT_EXCERPT_CHARS = 2000
MAX_PHASES = 64
# A block numbers its iterations ``block * 100 + n``, so one block is worth at
# most this many iteration trajectories. Retention is expressed in blocks
# (``execution.trajectory_retention_blocks``) for both stores; this converts.
ITERATIONS_PER_BLOCK = 100
MAX_DIFF_PATHS = 200
MAX_NOTES = 40
MAX_RECORD_CHARS = 96_000

# CI verdicts, kept as words so a record reads without a legend.
GREEN = "green"
RED = "red"
SKIPPED = "skipped"
NOT_RUN = "not-run"


def ci_verdict(ok: bool) -> str:
    return GREEN if ok else RED


@dataclass
class Phase:
    """One named stretch of an iteration and the wall-clock it consumed."""

    name: str
    seconds: float


class PhaseTimeline:
    """Accumulates :class:`Phase` entries in the order the loop runs them.

    ``clock`` is injectable so tests get a deterministic timeline instead of
    asserting on real elapsed time.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._start = clock()
        self._last = self._start
        self._phases: list[Phase] = []

    def mark(self, name: str) -> Phase:
        """Close the current phase under ``name`` and start the next one."""
        now = self._clock()
        phase = Phase(name=name, seconds=round(max(0.0, now - self._last), 3))
        self._last = now
        if len(self._phases) < MAX_PHASES:
            self._phases.append(phase)
        return phase

    @property
    def elapsed(self) -> float:
        return round(max(0.0, self._clock() - self._start), 3)

    def phases(self) -> list[Phase]:
        return list(self._phases)


@dataclass
class DiffStat:
    """What the iteration actually changed, counted rather than quoted."""

    files: int = 0
    code_files: int = 0
    knowledge_files: int = 0
    paths: list[str] = field(default_factory=list)

    @classmethod
    def from_paths(cls, paths: Iterable[str]) -> DiffStat:
        listed = sorted({str(p) for p in paths if str(p).strip()})
        knowledge = [p for p in listed if p.startswith("knowledge/")]
        return cls(
            files=len(listed),
            code_files=len(listed) - len(knowledge),
            knowledge_files=len(knowledge),
            paths=listed[:MAX_DIFF_PATHS],
        )


@dataclass
class IterationTrajectory:
    """One iteration of the loop, start to terminal outcome.

    Written once per iteration from the orchestrator's ``_record_cost`` seam,
    so a trajectory and a ledger record are emitted together or not at all -
    cost data and quality data can never disagree about what happened.
    """

    iteration: int
    kind: str
    tier: str
    model: str
    schema_version: int = ITERATION_SCHEMA_VERSION
    block: int = 0
    ticket: int | None = None
    rationale: str = ""
    strategy: str = ""
    phases: list[Phase] = field(default_factory=list)
    wall_clock_seconds: float = 0.0
    prompt_digest: str = ""
    prompt_excerpt: str = ""
    diff: DiffStat = field(default_factory=DiffStat)
    ci_local_before: str = NOT_RUN
    ci_local: str = NOT_RUN
    ci_remote: str = ""
    review: str = ""
    agent_ok: bool = True
    agent_trajectory: str = ""  # cross-reference to the per-run Trajectory
    outcome: str = ""
    attempts: int = 0
    recovered: bool = False
    pr: int | None = None
    merged: bool = False
    ledger_ref: str = ""
    redacted_env: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    created: str = field(default_factory=_now)

    @property
    def identifier(self) -> str:
        return str(self.iteration)

    def to_json(self) -> str:
        """Serialize, redacted and size-capped - the only way this reaches disk.

        Two scrub passes stack: the pattern-based one every trajectory gets, and
        a literal blank of the live values behind :attr:`redacted_env`, so a
        credential that looks like ordinary text still cannot survive.
        """
        extra = secret_env_values(self.redacted_env)
        data = redact_value(asdict(self), extra)
        text = json.dumps(data, indent=2, sort_keys=True)
        if len(text) <= MAX_RECORD_CHARS:
            return text
        # Over the cap: the prompt excerpt is the only unbounded-ish field left,
        # and it is the least load-bearing. Drop it rather than write a record
        # nobody capped.
        data["prompt_excerpt"] = f"[dropped: record exceeded {MAX_RECORD_CHARS} chars]"
        return json.dumps(data, indent=2, sort_keys=True)

    def summary(self) -> str:
        phases = ", ".join(f"{p.name}={p.seconds:g}s" for p in self.phases) or "(none)"
        ticket = f"#{self.ticket}" if self.ticket else "(none)"
        return (
            f"iteration {self.iteration} [{self.kind}] ticket {ticket} "
            f"tier={self.tier} outcome={self.outcome} attempts={self.attempts} "
            f"ci={self.ci_local}/{self.ci_remote or '-'} "
            f"diff={self.diff.code_files} code + {self.diff.knowledge_files} knowledge "
            f"in {self.wall_clock_seconds:.1f}s | phases: {phases}"
        )


def iteration_dir(repo_root: str | Path) -> Path:
    return Path(repo_root) / ITERATION_DIR


def iteration_path(repo_root: str | Path, identifier: str) -> Path:
    return iteration_dir(repo_root) / f"{identifier}.json"


def write_iteration(traj: IterationTrajectory, repo_root: str | Path) -> Path:
    """Persist one iteration trajectory as a single redacted JSON file.

    Deliberately NOT under ``knowledge/``: the Obsidian vault is a curated,
    committed artifact, and these are raw local telemetry that quotes repo
    content. ``.hsai/`` is gitignored, so they never reach a PR.
    """
    path = iteration_path(repo_root, traj.identifier)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(traj.to_json(), encoding="utf-8")
    return path


def read_iteration(path: str | Path) -> IterationTrajectory:
    """Parse an iteration trajectory, refusing a schema this code cannot read.

    The versioning rule: additive optional fields keep the version (unknown keys
    are dropped on read, so an older reader tolerates a newer writer's
    additions); removing a field or changing what one means bumps
    :data:`ITERATION_SCHEMA_VERSION`, and a mismatched version is an error
    rather than a silent misread.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    version = int(data.get("schema_version", 0))
    if version != ITERATION_SCHEMA_VERSION:
        raise ValueError(
            f"{path}: schema_version {version} is not readable by this hsai "
            f"(expected {ITERATION_SCHEMA_VERSION})"
        )
    phases = [Phase(**p) for p in data.pop("phases", None) or []]
    diff = DiffStat(**(data.pop("diff", None) or {}))
    known = {f.name for f in fields(IterationTrajectory)}
    kept = {k: v for k, v in data.items() if k in known}
    return IterationTrajectory(phases=phases, diff=diff, **kept)


def load_iteration(repo_root: str | Path, identifier: str) -> IterationTrajectory:
    """Resolve ``identifier`` (an iteration number or a path) and read it."""
    candidates = [iteration_path(repo_root, identifier), Path(identifier)]
    for candidate in candidates:
        if candidate.is_file():
            return read_iteration(candidate)
    raise FileNotFoundError(
        f"no iteration trajectory {identifier!r} under {iteration_dir(repo_root)}"
    )


def prune_iterations(repo_root: str | Path, keep: int) -> list[str]:
    """Drop all but the newest ``keep`` iteration trajectories.

    Bounds the store the same way :func:`prune` bounds the per-run one; a
    non-positive ``keep`` disables pruning. Returns the identifiers removed.
    """
    root = iteration_dir(repo_root)
    if keep <= 0 or not root.is_dir():
        return []
    stems = sorted(
        (p for p in root.glob("*.json") if p.stem.lstrip("-").isdigit()),
        key=lambda p: int(p.stem),
    )
    dropped = stems[:-keep] if len(stems) > keep else []
    for path in dropped:
        path.unlink()
    return [p.stem for p in dropped]


def record_iteration(
    repo_root: str | Path,
    *,
    iteration: int,
    kind: str,
    tier: str,
    model: str,
    outcome: str,
    block: int = 0,
    ticket: int | None = None,
    rationale: str = "",
    strategy: str = "",
    phases: Iterable[Phase] = (),
    wall_clock_seconds: float = 0.0,
    prompt: str = "",
    changed_paths: Iterable[str] = (),
    ci_local_before: str = NOT_RUN,
    ci_local: str = NOT_RUN,
    ci_remote: str = "",
    review: str = "",
    agent_ok: bool = True,
    agent_trajectory: str = "",
    attempts: int = 0,
    recovered: bool = False,
    pr: int | None = None,
    merged: bool = False,
    forbid_env: Iterable[str] = (),
    notes: Iterable[str] = (),
) -> IterationTrajectory:
    """Build one iteration trajectory and persist it."""
    traj = IterationTrajectory(
        iteration=iteration,
        kind=kind,
        tier=tier,
        model=model,
        block=block,
        ticket=ticket,
        rationale=rationale,
        strategy=strategy,
        phases=list(phases)[:MAX_PHASES],
        wall_clock_seconds=round(max(0.0, wall_clock_seconds), 3),
        prompt_digest=prompt_digest(prompt),
        prompt_excerpt=_clip(prompt, PROMPT_EXCERPT_CHARS),
        diff=DiffStat.from_paths(changed_paths),
        ci_local_before=ci_local_before,
        ci_local=ci_local,
        ci_remote=ci_remote,
        review=review,
        agent_ok=agent_ok,
        agent_trajectory=agent_trajectory,
        outcome=outcome,
        attempts=attempts,
        recovered=recovered,
        pr=pr,
        merged=merged,
        # The ledger record for this same iteration - one line, same seam.
        ledger_ref=f"block={block},iteration={iteration}",
        redacted_env=sorted({str(n) for n in forbid_env}),
        notes=[_clip(str(n), 400) for n in list(notes)[:MAX_NOTES]],
    )
    write_iteration(traj, repo_root)
    return traj


def record(
    repo_root: str | Path,
    *,
    iteration: int,
    ticket: int | None,
    kind: str,
    tier: str,
    model: str,
    prompt: str,
    result: Any,
    block: int = 0,
    duration_seconds: float = 0.0,
    outcome: str = "ran",
) -> Trajectory:
    """Build a trajectory from an :class:`hsai.ai.AIResult` and persist it.

    ``result`` is duck-typed (``ok``/``output``/``error``/``usage``/``payload``)
    so this module stays independent of :mod:`hsai.ai`.
    """
    payload = getattr(result, "payload", None)
    num_turns = payload.get("num_turns") if isinstance(payload, dict) else None
    traj = Trajectory(
        iteration=iteration,
        ticket=ticket,
        kind=kind,
        tier=tier,
        model=model,
        prompt=prompt,
        block=block,
        prompt_digest=prompt_digest(prompt),
        session_id=str(getattr(result, "session_id", "") or ""),
        steps=steps_from_output(payload, getattr(result, "output", "")),
        ok=bool(getattr(result, "ok", False)),
        exit_status="ok" if getattr(result, "ok", False) else "error",
        error=redact(_clip(getattr(result, "error", "") or "")),
        usage=getattr(result, "usage", None),
        num_turns=int(num_turns) if isinstance(num_turns, int) else None,
        duration_seconds=round(max(0.0, duration_seconds), 3),
        outcome=outcome,
    )
    write(traj, repo_root)
    return traj
