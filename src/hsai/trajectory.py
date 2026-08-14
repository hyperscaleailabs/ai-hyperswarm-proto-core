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
import re
from dataclasses import asdict, dataclass, field
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
