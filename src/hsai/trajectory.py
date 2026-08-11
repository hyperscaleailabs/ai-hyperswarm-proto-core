"""Worker trajectories: the durable record of what one agent run actually did.

Two records live here, at two levels of resolution:

- :class:`Trajectory` - one JSON file per *agent run* under
  ``.hsai/traj/<block>/<iteration>.json``. Human-facing forensics: the prompt,
  the step stream, usage, and the digest/excerpt the lesson quotes.
- :class:`TrajectoryEvent` + :class:`Recorder` - an append-only JSONL *event
  stream* per iteration under ``.hsai/trajectories/<iteration>.jsonl``, one
  event per subprocess the iteration ran (agent, git, gh, ruff, pytest). It is
  machine-facing: :mod:`hsai.replay` turns one back into a ``Runner`` and
  re-drives ``orchestrator.run_once`` with zero quota and zero network.

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
langchain (record/replay cassettes turn previously-live interactions into
deterministic, zero-cost CI fixtures), FoundationAgents/MetaGPT (an SOP emits an
explicit artifact at every phase boundary, hence phase-tagged events),
microsoft/JARVIS (intermediate stage results must be separately addressable,
hence per-step data rather than a final blob) and openai/swarm (the runner
returns the full message list, so callers never reconstruct what happened).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import CoreConfig
from .proc import Proc, Runner

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


def redact(text: str) -> str:
    """Scrub credentials and absolute home paths out of ``text``."""
    out = text or ""
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(REDACTED, out)
    out = _HOME_PATTERN.sub("~", out)
    # Also catch a home dir living somewhere else entirely (`$HOME=/opt/...`).
    # Guarded: a degenerate home of "/" must not rewrite every path there is.
    home = str(Path.home())
    return out.replace(home, "~") if len(home) > 1 else out


def redact_value(value: Any) -> Any:
    """Recursively :func:`redact` every string in a JSON-ish structure.

    Applied to the whole record just before it is written, so a field nobody
    scrubbed at capture time (the prompt, most importantly) cannot leak. Only
    string *values* are rewritten - keys and numbers are left intact, so the
    usage counts stay machine-readable.
    """
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v) for v in value]
    return value


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
    return sha256_of(prompt)[:12]


# --- event stream: the deterministic, replayable record ----------------------
#
# The JSON store above answers "what did that agent do?". This one answers the
# harder question: "can I run that iteration again, exactly, for free?". One
# append-only JSONL file per iteration, one event per subprocess, recorded at
# the `Runner` choke point - which is precisely the seam `hsai.replay` needs to
# stand in for `claude`, `git` and `gh`.

# Serializes appends so parallel workers never interleave a partial line
# (the same lock-and-append discipline as `ledger.append_record`).
_EVENT_LOCK = threading.Lock()

# Env values shorter than this are too generic to substitute blindly - replacing
# a two-character value would shred every unrelated string in the record.
MIN_SECRET_CHARS = 4


def sha256_of(text: str) -> str:
    """Full content hash - the identity a replay compares prompts on."""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


@dataclass
class TrajectoryEvent:
    """One subprocess an iteration ran, recorded well enough to replay it.

    ``phase`` is the SOP step the loop was in when the call happened
    (``ci-before``, ``agent:implement``, ``repro-guard``, ``publish``, ...), so
    a trajectory reads as a sequence of phases rather than an undifferentiated
    log - and so a replay failure can name *where* it diverged.
    """

    timestamp: str
    iteration: str
    phase: str
    command: list[str] = field(default_factory=list)
    exit_code: int = 0
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0
    # Agent events only; empty for git/gh/ruff/pytest calls.
    tier: str = ""
    model: str = ""
    prompt: str = ""
    prompt_sha256: str = ""
    input_tokens: int | None = None
    output_tokens: int | None = None

    @property
    def is_agent(self) -> bool:
        return bool(self.prompt_sha256) or self.command[:1] == ["claude"]

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> TrajectoryEvent:
        """Parse one recorded line, ignoring keys a future version may add.

        A hand-curated fixture may omit ``prompt_sha256``; it is then derived
        from the recorded prompt, which is exactly what the recorder stores.
        """
        known = {f.name for f in fields(cls)}
        event = cls(**{k: v for k, v in data.items() if k in known})
        if event.prompt and not event.prompt_sha256:
            event.prompt_sha256 = sha256_of(event.prompt)
        return event


class Redactor:
    """Scrubs an event before it can reach disk.

    Three layers, most specific first: the exact *values* of the environment
    variables named in ``constraints.forbid_env`` (the credentials the loop
    already refuses to hand a worker), then the configured
    ``trajectories.redact_patterns`` deny-regexes, then the shared
    :func:`redact` pass for generic credential shapes and home paths.
    """

    def __init__(self, secrets: Iterable[str] = (), patterns: Iterable[str] = ()) -> None:
        # Longest first: a secret that contains another must be masked whole.
        self.secrets = tuple(sorted(
            {s for s in secrets if s and len(s) >= MIN_SECRET_CHARS},
            key=len, reverse=True,
        ))
        self.patterns = tuple(re.compile(p) for p in patterns if p)

    @classmethod
    def from_config(
        cls, cfg: CoreConfig, env: Mapping[str, str] | None = None
    ) -> Redactor:
        environ = os.environ if env is None else env
        return cls(
            secrets=[environ.get(name, "") for name in cfg.forbidden_env],
            patterns=cfg.trajectory_redact_patterns,
        )

    def __call__(self, text: str) -> str:
        out = text or ""
        for secret in self.secrets:
            out = out.replace(secret, REDACTED)
        for pattern in self.patterns:
            out = pattern.sub(REDACTED, out)
        return redact(out)

    def scrub(self, event: TrajectoryEvent) -> TrajectoryEvent:
        """Return a copy of ``event`` with every free-text field scrubbed.

        The prompt hash is recomputed over the *scrubbed* prompt, so the
        invariant ``prompt_sha256 == sha256_of(prompt)`` holds for everything on
        disk - a reader can verify a committed fixture without trusting it.
        """
        prompt = self(event.prompt)
        return replace(
            event,
            command=[self(arg) for arg in event.command],
            stdout=self(event.stdout),
            stderr=self(event.stderr),
            prompt=prompt,
            prompt_sha256=sha256_of(prompt) if prompt else "",
        )


class Recorder:
    """Append-only JSONL writer for one iteration's event stream."""

    def __init__(
        self,
        path: str | Path,
        iteration: str | int,
        redactor: Redactor | None = None,
        *,
        phase: str = "start",
    ) -> None:
        self.path = Path(path)
        self.iteration = str(iteration)
        self.redactor = redactor or Redactor()
        self.phase = phase

    @classmethod
    def for_iteration(
        cls, repo_root: str | Path, cfg: CoreConfig, iteration: int
    ) -> Recorder | None:
        """Open this iteration's stream, or ``None`` when recording is off.

        Creating the file up front is what lets retention prune *including* the
        run about to be recorded, so the store settles at exactly
        ``trajectories.retention_count`` files instead of one more.
        """
        if not cfg.trajectories_enabled:
            return None
        path = Path(repo_root) / cfg.trajectories_dir / f"{iteration}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        prune_events(path.parent, cfg.trajectory_retention_count)
        return cls(path, iteration, Redactor.from_config(cfg))

    def append(self, event: TrajectoryEvent) -> TrajectoryEvent:
        """Scrub, then append as one JSON line. Returns what actually landed."""
        scrubbed = self.redactor.scrub(event)
        line = scrubbed.to_json() + "\n"
        with _EVENT_LOCK:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
        return scrubbed

    def record(
        self,
        command: Sequence[str],
        *,
        exit_code: int,
        stdout: str = "",
        stderr: str = "",
        duration_s: float = 0.0,
        phase: str = "",
        tier: str = "",
        model: str = "",
        prompt: str = "",
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> TrajectoryEvent:
        return self.append(TrajectoryEvent(
            timestamp=_now(),
            iteration=self.iteration,
            phase=phase or self.phase,
            command=list(command),
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_s=round(max(0.0, duration_s), 3),
            tier=tier,
            model=model,
            prompt=prompt,
            prompt_sha256=sha256_of(prompt) if prompt else "",
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        ))

    def wrap(self, runner: Runner) -> Runner:
        """Return ``runner`` with an event appended after every call.

        One decorator at the seam covers git, gh, ruff and pytest; the agent
        call records itself in :func:`hsai.ai.run_agent`, which is the only
        place that knows the tier, model, prompt and token counts.
        """

        def recording_runner(cmd: Sequence[str], **kwargs: Any) -> Proc:
            started = time.monotonic()
            proc = runner(cmd, **kwargs)
            self.record(
                cmd,
                exit_code=proc.code,
                stdout=proc.stdout,
                stderr=proc.stderr,
                duration_s=time.monotonic() - started,
            )
            return proc

        return recording_runner


def read_events(path: str | Path) -> list[TrajectoryEvent]:
    """Parse one trajectory's events back off disk, in recorded order."""
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [TrajectoryEvent.from_dict(json.loads(line)) for line in lines if line.strip()]


def prune_events(directory: str | Path, keep: int) -> list[Path]:
    """Drop all but the newest ``keep`` trajectories (``0`` keeps everything).

    Trajectories are local forensics, not repo content: worth keeping for the
    recent past, worth bounding beyond it.
    """
    directory = Path(directory)
    if keep <= 0 or not directory.is_dir():
        return []
    files = sorted(directory.glob("*.jsonl"), key=_recency)
    dropped = files[:-keep] if len(files) > keep else []
    for path in dropped:
        path.unlink()
    return dropped


def _recency(path: Path) -> tuple[float, int]:
    """Sort key for retention: mtime, with the iteration number as tiebreak.

    Iteration numbers only increase, so the tiebreak keeps pruning deterministic
    on filesystems whose timestamps are too coarse to separate two fast runs.
    """
    stem = path.stem
    return path.stat().st_mtime, int(stem) if stem.isdigit() else 0


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
        duration_seconds=round(max(0.0, duration_seconds), 3),
        outcome=outcome,
    )
    write(traj, repo_root)
    return traj
