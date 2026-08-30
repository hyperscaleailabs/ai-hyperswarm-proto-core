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
# Where the RAW stream-json payload lands, one file per branch. Distinct from
# TRAJECTORY_DIR above: that store holds the parsed, per-iteration record, this
# one holds the verbatim event stream the CLI printed. Overridable via
# `execution.trajectories.dir`.
STREAM_DIR = ".hsai/trajectories"

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


def steps_from_stream(text: str) -> list[Step]:
    """Derive the step stream from a raw ``stream-json`` payload.

    ``steps_from_output`` reads the single-envelope ``messages`` list, which a
    stream-json run does not have - its per-turn detail lives in the JSONL
    events instead. Without this the step stream would silently collapse to the
    final result the moment trajectories were enabled, taking the lesson's
    ``tools used`` row and the excerpt's failing-step pointer with it.

    Returns ``[]`` for anything that is not a multi-event stream, so the legacy
    path stays authoritative for the shape it already handles.
    """
    raw = (text or "").strip()
    if not raw.startswith("{"):
        return []
    try:
        json.loads(raw)
    except (ValueError, TypeError):
        pass
    else:
        return []  # a single JSON object is an envelope, not a stream

    steps: list[Step] = []

    def add(kind: str, body: str, name: str = "") -> None:
        steps.append(Step(index=len(steps) + 1, kind=kind, name=name,
                          text=_clip(redact(body))))

    for event in _stream_events(raw):
        etype = str(event.get("type") or "")
        message = event.get("message")
        if isinstance(message, dict):
            role = str(message.get("role") or etype or "message")
            for block in _blocks(message):
                btype = str(block.get("type", "text"))
                if btype == "tool_use":
                    add("tool_use", json.dumps(block.get("input", {}), sort_keys=True),
                        name=str(block.get("name", "")))
                elif btype == "tool_result":
                    add("tool_result", _block_text(block.get("content")))
                else:
                    add(role, _block_text(block.get("text", "")))
        elif etype == "result":
            body = event.get("result")
            if isinstance(body, str) and body.strip():
                add("result", body)
    return steps


# --- raw stream capture -----------------------------------------------------
#
# `claude -p --output-format stream-json --verbose` prints one JSON event per
# line. This half of the module keeps that stream verbatim (SWE-agent's `.traj`
# file: the run record, not the final patch, is the primary artifact) and folds
# it into a :class:`TrajectorySummary` - the per-stage intermediate results
# microsoft/JARVIS exposes at `/results`, and the complete message list
# openai/swarm's `run()` returns so a caller never has to reconstruct what
# happened. Everything here is deliberately total: an unknown event type, a
# truncated line, or a wholesale CLI format change degrades to an empty summary
# rather than raising into the iteration that produced it.

DEFAULT_MAX_BYTES = 2_000_000
# Head/tail truncation needs room for the marker line it splices in.
_TRUNCATION_RESERVE = 96
# Per-error text kept in a summary; the full text stays in the raw stream.
ERROR_CHARS = 300
# How many touched files a digest lists before it says "+N more".
DIGEST_FILES = 12

# Tool inputs name their target under one of these keys, depending on the tool.
_FILE_KEYS = ("file_path", "notebook_path", "path")


@dataclass
class TrajectorySummary:
    """What one agent run actually did, folded out of its event stream."""

    turns: int = 0
    tool_calls: dict[str, int] = field(default_factory=dict)
    files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    events: int = 0
    usage: dict[str, Any] | None = None
    result: str = ""
    session_id: str = ""

    @property
    def empty(self) -> bool:
        """True when nothing parseable was recovered - the degraded path."""
        return self.events == 0

    @property
    def total_tool_calls(self) -> int:
        return sum(self.tool_calls.values())

    def tokens(self) -> tuple[int, int] | None:
        """``(input, output)`` token counts, or ``None`` if unreported."""
        if not isinstance(self.usage, dict):
            return None
        inp, out = self.usage.get("input_tokens"), self.usage.get("output_tokens")
        if inp is None and out is None:
            return None
        return int(inp or 0), int(out or 0)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _fold_blocks(summary: TrajectorySummary, blocks: list[dict[str, Any]]) -> None:
    for block in blocks:
        btype = str(block.get("type", ""))
        if btype == "tool_use":
            name = str(block.get("name") or "tool")
            summary.tool_calls[name] = summary.tool_calls.get(name, 0) + 1
            payload = block.get("input")
            if isinstance(payload, dict):
                for key in _FILE_KEYS:
                    value = payload.get(key)
                    if isinstance(value, str) and value and value not in summary.files:
                        summary.files.append(value)
        elif btype == "tool_result" and block.get("is_error"):
            summary.errors.append(_clip(_block_text(block.get("content")), ERROR_CHARS))


def _fold_event(summary: TrajectorySummary, event: dict[str, Any]) -> int:
    """Fold one stream event into ``summary``; returns 1 if it was a turn."""
    etype = str(event.get("type") or "")
    session = event.get("session_id")
    if isinstance(session, str) and session and not summary.session_id:
        summary.session_id = session

    message = event.get("message")
    if isinstance(message, dict):
        _fold_blocks(summary, _blocks(message))
        # An assistant event reports its own usage one level down. Last-wins:
        # the terminal `result` event's cumulative usage overwrites this, and
        # if the stream was cut short before that event, the latest per-message
        # figure survives as the only cost signal there is.
        if isinstance(message.get("usage"), dict):
            summary.usage = message["usage"]

    if etype == "result":
        turns = event.get("num_turns")
        if isinstance(turns, int):
            summary.turns = turns
        usage = event.get("usage")
        if isinstance(usage, dict):
            summary.usage = usage
        text = event.get("result")
        if isinstance(text, str) and text.strip():
            summary.result = _clip(text, ERROR_CHARS)
        if event.get("is_error") or str(event.get("subtype") or "").startswith("error"):
            summary.errors.append(_clip(_block_text(event.get("result")), ERROR_CHARS))
    elif etype in ("error", "stderr"):
        summary.errors.append(_clip(_block_text(event.get("message") or event), ERROR_CHARS))

    role = message.get("role") if isinstance(message, dict) else None
    return 1 if etype == "assistant" or role == "assistant" else 0


def _stream_events(text: str) -> list[dict[str, Any]]:
    """Every JSON object in ``text``, whether it is JSONL or one pretty object.

    The legacy single-object envelope is treated as a one-event stream whose
    ``messages`` list expands into the per-turn events stream-json would have
    emitted, so both CLI shapes fold through exactly the same code path.
    """
    raw = (text or "").strip()
    if not raw:
        return []
    try:
        whole = json.loads(raw)
    except (ValueError, TypeError):
        whole = None
    if isinstance(whole, dict):
        messages = whole.get("messages")
        expanded: list[dict[str, Any]] = []
        if isinstance(messages, list):
            expanded = [
                {"type": str(m.get("role") or m.get("type") or "message"), "message": m}
                for m in messages
                if isinstance(m, dict)
            ]
        return [*expanded, whole]

    events: list[dict[str, Any]] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def parse_stream(text: str) -> TrajectorySummary:
    """Fold a ``--output-format stream-json`` payload into a summary.

    Total by construction: malformed lines are skipped, unknown event types are
    counted but otherwise ignored, and any unexpected failure degrades to an
    empty summary. A CLI format change must cost the loop its observability,
    never an iteration.
    """
    summary = TrajectorySummary()
    try:
        events = _stream_events(text)
        assistant_turns = 0
        for event in events:
            summary.events += 1
            assistant_turns += _fold_event(summary, event)
        # No `result` event (a crash mid-stream): fall back to counting the
        # assistant messages that did arrive, so turns is never silently 0.
        if not summary.turns:
            summary.turns = assistant_turns
    except Exception:  # pragma: no cover - defensive; parsing above is total
        return TrajectorySummary()
    return summary


def digest(summary: TrajectorySummary) -> str:
    """Compact markdown table - the only part of a trajectory that is committed.

    Counters and file names only: never quoted run content, so the PR body and
    the lesson gain the audit trail without becoming a mirror of the worktree.
    """
    if summary is None or summary.empty:
        return (
            "| field | value |\n| --- | --- |\n"
            "| trajectory | _(no parseable stream recorded)_ |"
        )
    tools = ", ".join(
        f"`{name}`x{summary.tool_calls[name]}" for name in sorted(summary.tool_calls)
    )
    tools_cell = f"{summary.total_tool_calls} ({tools})" if tools else "_(none)_"
    shown = summary.files[:DIGEST_FILES]
    files_cell = ", ".join(f"`{f}`" for f in shown) or "_(none)_"
    if len(summary.files) > len(shown):
        files_cell += f", +{len(summary.files) - len(shown)} more"
    toks = summary.tokens()
    tokens_cell = f"{toks[0]} in / {toks[1]} out" if toks else "unreported"
    return (
        "| field | value |\n"
        "| --- | --- |\n"
        f"| turns | {summary.turns} |\n"
        f"| tool calls | {tools_cell} |\n"
        f"| files touched | {files_cell} |\n"
        f"| tokens | {tokens_cell} |\n"
        f"| error events | {len(summary.errors)} |\n"
        f"| stream events | {summary.events} |"
    )


def stream_dir(repo_root: str | Path, directory: str = STREAM_DIR) -> Path:
    return Path(repo_root) / (directory or STREAM_DIR)


def stream_path(
    repo_root: str | Path, branch: str, directory: str = STREAM_DIR
) -> Path | None:
    """``<dir>/<branch>.jsonl``, or ``None`` if ``branch`` cannot address a file.

    Branch names carry slashes (``hsai/iter-...``) so they nest as directories;
    anything that could escape the store (absolute, empty, or containing ``..``)
    is refused rather than sanitized, since a silently rewritten path would
    store the run somewhere nobody looks for it.
    """
    name = (branch or "").strip()
    parts = name.split("/")
    if not name or ".." in parts or "" in parts or Path(name).is_absolute():
        return None
    return stream_dir(repo_root, directory) / f"{name}.jsonl"


def truncate_stream(raw: bytes, max_bytes: int) -> bytes:
    """Cap ``raw`` at ``max_bytes``, keeping the head and the tail.

    The head shows how a run started, the tail how it ended - the middle is what
    a runaway tool loop inflates, so that is what goes. The splice marker is
    itself a JSON line, so a truncated file is still valid JSONL.
    """
    if max_bytes <= 0 or len(raw) <= max_bytes:
        return raw
    budget = max_bytes - _TRUNCATION_RESERVE
    if budget <= 0:
        return raw[:max_bytes]
    head, tail = raw[: budget // 2], raw[len(raw) - (budget - budget // 2):]
    # Snap both cuts to line boundaries; a half-line either side would leave the
    # file unparseable, which defeats the point of keeping it. A half that holds
    # no boundary at all (one giant line) is dropped rather than corrupted.
    cut = head.rfind(b"\n")
    head = head[: cut + 1] if cut != -1 else b""
    cut = tail.find(b"\n")
    tail = tail[cut + 1:] if cut != -1 else b""
    dropped = len(raw) - len(head) - len(tail)
    marker = json.dumps({"type": "hsai_truncated", "dropped_bytes": dropped}) + "\n"
    out = head + marker.encode("utf-8") + tail
    return out if len(out) <= max_bytes else raw[:max_bytes]


def write_stream(
    path: str | Path,
    raw: str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    redact_text: bool = True,
) -> Path:
    """Persist one run's raw event stream, redacted and size-capped.

    Named ``write_stream`` rather than ``write`` because :func:`write` already
    persists the parsed per-iteration record; the two stores are siblings, not
    replacements.

    Redaction is deliberately blunt: a scrubbed credential can swallow the rest
    of its JSON line, so that line is unparseable when replayed later. That is
    the right trade - the summary and every committed counter are folded from
    the *pre-redaction* stdout in :func:`hsai.ai.run_agent`, so nothing numeric
    is lost, and the alternative is a secret on disk.
    """
    path = Path(path)
    text = redact(raw or "") if redact_text else (raw or "")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(truncate_stream(text.encode("utf-8"), max_bytes))
    return path


def read_stream(path: str | Path) -> str:
    """Read a stored raw stream back (invalid bytes are replaced, never raised)."""
    return Path(path).read_text(encoding="utf-8", errors="replace")


def render_summary(summary: TrajectorySummary, *, source: str = "") -> str:
    """Human-readable reconstruction - what ``hsai replay <branch>`` prints."""
    head = [f"trajectory stream: {source}"] if source else []
    if summary.empty:
        return "\n".join([*head, "(no parseable events: the stream was empty or unknown)"])
    lines = [
        *head,
        f"session: {summary.session_id or '(not reported)'}",
        f"events: {summary.events}  turns: {summary.turns}",
        "",
        f"--- tool calls ({summary.total_tool_calls}) ---",
    ]
    lines += [
        f"  {name}: {summary.tool_calls[name]}" for name in sorted(summary.tool_calls)
    ] or ["  (none recorded)"]
    lines += ["", f"--- files touched ({len(summary.files)}) ---"]
    lines += [f"  {f}" for f in summary.files] or ["  (none recorded)"]
    lines += ["", f"--- errors ({len(summary.errors)}) ---"]
    lines += [f"  {e}" for e in summary.errors] or ["  (none recorded)"]
    toks = summary.tokens()
    usage = f"{toks[0]} in / {toks[1]} out" if toks else "unreported"
    lines += ["", f"--- usage --- {usage}"]
    if summary.result:
        lines += ["", "--- result ---", summary.result]
    return "\n".join(lines)


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
    output = getattr(result, "output", "")
    # A stream-json run carries its per-turn detail in the raw JSONL, not in the
    # lifted `result` event, so try that shape first; it yields [] for the
    # single-envelope shape, which the legacy path still owns.
    steps = steps_from_stream(output) or steps_from_output(payload, output)
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
        steps=steps,
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
