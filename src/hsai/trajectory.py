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
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TRAJECTORY_DIR = ".hsai/traj"

# Per-step text is clipped so one runaway tool result cannot bloat the store.
STEP_CHARS = 2000
# Raw agent stdout/stderr kept verbatim (post-redaction) on the record.
STREAM_CHARS = 4000
# Hard ceiling on one serialized record. A single pathological run must not be
# able to fill the store on its own; `capped_json` sheds steps, then streams,
# until the record fits and says so in `truncated`.
MAX_RECORD_CHARS = 256_000
# What the committed lesson may quote: a short tail, tightly clipped.
EXCERPT_STEPS = 5
EXCERPT_CHARS = 240
# What a retry prompt may quote from its predecessor - bounded hard, because it
# is prepended to a worker prompt and competes with the ticket for attention.
REMEDIATION_CHARS = 1200

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
    branch: str = ""
    strategy: str = ""
    prompt_digest: str = ""
    session_id: str = ""
    steps: list[Step] = field(default_factory=list)
    ok: bool = True
    exit_status: str = "ok"
    error: str = ""
    usage: dict[str, Any] | None = None
    duration_seconds: float = 0.0
    outcome: str = "ran"
    # --- iteration context, filled in as the iteration progresses -------------
    # Each guard's verdict by name ("workflow_revert", "completeness", "repro"),
    # so a post-mortem can tell "the guard passed" from "the guard never ran".
    guards: dict[str, str] = field(default_factory=dict)
    # Local ruff/pytest results, keyed "before"/"after" the agent ran.
    local_ci: dict[str, dict[str, bool]] = field(default_factory=dict)
    remote_ci: str = ""
    changed_paths: list[str] = field(default_factory=list)
    # Wall clock per named phase (agent, ci_before, ci_after, remote_ci, total).
    phase_seconds: dict[str, float] = field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""
    failure_class: str = ""
    retry_action: str = ""
    truncated: str = ""
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

    def capped_json(self, cap: int = MAX_RECORD_CHARS) -> str:
        """Serialize, shedding content until the record fits within ``cap``.

        Sheds in order of forensic value, cheapest first: middle steps (the
        opening moves and the failing tail are what a post-mortem actually
        reads), then the raw streams and the prompt. Whatever was dropped is
        recorded in ``truncated`` so a reader is never silently misled into
        thinking they have the whole run.
        """
        data = redact_value(asdict(self))
        text = json.dumps(data, indent=2, sort_keys=True)
        if len(text) <= cap:
            return text

        steps: list[dict[str, Any]] = list(data.get("steps") or [])
        dropped = 0
        while len(text) > cap and len(steps) > 2:
            # Drop a batch sized by the overshoot so this converges in a few
            # passes rather than one re-serialization per step.
            per_step = max(1, len(text) // max(1, len(steps)))
            batch = max(1, min(len(steps) - 2, (len(text) - cap) // per_step + 1))
            start = max(1, len(steps) // 2 - batch // 2)
            del steps[start:start + batch]
            dropped += batch
            data["steps"] = steps
            data["truncated"] = f"{dropped} step(s) dropped to fit the {cap}-char cap"
            text = json.dumps(data, indent=2, sort_keys=True)

        if len(text) > cap:
            for name in ("stdout", "stderr", "prompt"):
                data[name] = _clip(str(data.get(name) or ""), 500)
            note = f"streams and prompt clipped to fit the {cap}-char cap"
            data["truncated"] = f"{data['truncated']}; {note}" if dropped else note
            text = json.dumps(data, indent=2, sort_keys=True)
        return text

    def diffstat(self) -> dict[str, int]:
        """Changed paths bucketed by top-level area - the shape of the diff.

        Counts, not contents: enough for the brief to see "this run only touched
        knowledge/" without quoting a line of it.
        """
        stat: dict[str, int] = {}
        for path in self.changed_paths:
            head = path.split("/", 1)[0] if "/" in path else path
            stat[head] = stat.get(head, 0) + 1
        return stat

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

    def remediation(self, limit: int = REMEDIATION_CHARS) -> str:
        """What the *next* attempt on this ticket is told about this one.

        Facts first (class, guard verdicts, which CI step was red), then a short
        redacted tail. Bounded hard: this is prepended to a worker prompt, so an
        unbounded excerpt would crowd out the ticket it is meant to support.
        """
        lines = [
            f"- failure class: `{self.failure_class or 'unknown'}`"
            + (f" (retry policy: `{self.retry_action}`)" if self.retry_action else ""),
            f"- iteration {self.iteration} on `{self.model}` ({self.tier}), "
            f"exit={self.exit_status}, outcome={self.outcome}",
        ]
        red = [
            f"{phase}:{step}"
            for phase, steps in sorted(self.local_ci.items())
            for step, ok in sorted(steps.items())
            if not ok
        ]
        if red:
            lines.append(f"- local CI red: {', '.join(red)}")
        if self.remote_ci:
            lines.append(f"- remote CI: {self.remote_ci}")
        verdicts = [f"{k}={v}" for k, v in sorted(self.guards.items()) if v]
        if verdicts:
            lines.append(f"- guards: {', '.join(verdicts)}")
        if self.changed_paths:
            stat = ", ".join(f"{k}={v}" for k, v in sorted(self.diffstat().items()))
            lines.append(f"- files touched: {stat}")
        if self.error:
            lines.append(f"- error: {_clip(self.error, 300)}")
        lines.append("- last steps:\n" + self.excerpt())
        return _clip(redact("\n".join(lines)), limit)

    def render(self) -> str:
        """Human-readable reconstruction (what ``hsai replay`` prints)."""
        ticket = f"#{self.ticket}" if self.ticket else "(none)"
        guards = ", ".join(f"{k}={v}" for k, v in sorted(self.guards.items())) or "(none)"
        phases = ", ".join(
            f"{k}={v:.1f}s" for k, v in sorted(self.phase_seconds.items())
        ) or "(none)"
        head = [
            f"trajectory {self.identifier}  [{self.kind}] ticket {ticket} block {self.block}",
            f"branch: {self.branch or '(none)'}",
            f"model: {self.model} (tier={self.tier}, strategy={self.strategy or '-'})"
            f"  duration: {self.duration_seconds:.3f}s",
            f"exit: {self.exit_status}  ok={self.ok}  outcome: {self.outcome}",
            f"failure: {self.failure_class or '(none)'}"
            f"  retry action: {self.retry_action or '(none)'}",
            f"guards: {guards}",
            f"local CI: {self.local_ci or '(not run)'}  remote CI: {self.remote_ci or '-'}",
            f"changed paths ({len(self.changed_paths)}): {self.diffstat() or '(none)'}",
            f"phases: {phases}",
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
        if self.truncated:
            tail += ["--- truncated ---", self.truncated]
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
    # capped_json() redacts *and* bounds: nothing reaches disk unscrubbed, and
    # no single run can outgrow the cap.
    path.write_text(traj.capped_json(), encoding="utf-8")
    return path


def read(path: str | Path) -> Trajectory:
    """Parse a trajectory back off disk.

    Tolerant of records written by an older hsai: unknown keys are dropped and
    absent ones fall back to their defaults, so the store survives a schema
    change instead of making `hsai traj` throw on last week's block.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    steps = [Step(**s) for s in data.pop("steps", []) or []]
    known = {f.name for f in fields(Trajectory)}
    return Trajectory(steps=steps, **{k: v for k, v in data.items() if k in known})


def last_failure_for_ticket(repo_root: str | Path, ticket: int | None) -> Trajectory | None:
    """The most recent *failed* run recorded against ``ticket``, if any.

    This is what makes a retry better informed than its predecessor: the next
    attempt's prompt quotes it (see ``orchestrator._task_prompt``). Reads only -
    a scan of the bounded local store, never a model call.
    """
    if not ticket:
        return None
    root = trajectory_dir(repo_root)
    if not root.is_dir():
        return None
    best: Trajectory | None = None
    for path in root.glob("*/*.json"):
        try:
            traj = read(path)
        except (OSError, ValueError, TypeError):
            continue  # a partially written record must not break the next run
        if traj.ticket != ticket or traj.outcome == "merged":
            continue
        if best is None or traj.iteration > best.iteration:
            best = traj
    return best


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
    branch: str = "",
    strategy: str = "",
    duration_seconds: float = 0.0,
    outcome: str = "ran",
) -> Trajectory:
    """Build a trajectory from an :class:`hsai.ai.AIResult` and persist it.

    ``result`` is duck-typed (``ok``/``output``/``error``/``usage``/``payload``)
    so this module stays independent of :mod:`hsai.ai`. ``result=None`` records
    an iteration that never ran a model (a ``--dry-run`` rehearsal): the record
    still exists, with an empty step stream and ``exit_status="dry-run"``, so
    "every iteration leaves exactly one trajectory" holds without pretending a
    model was called.
    """
    dry = result is None
    ok = True if dry else bool(getattr(result, "ok", False))
    traj = Trajectory(
        iteration=iteration,
        ticket=ticket,
        kind=kind,
        tier=tier,
        model=model,
        prompt=prompt,
        block=block,
        branch=branch,
        strategy=strategy,
        prompt_digest=prompt_digest(prompt),
        session_id=str(getattr(result, "session_id", "") or ""),
        steps=[] if dry else steps_from_output(
            getattr(result, "payload", None), getattr(result, "output", "")
        ),
        ok=ok,
        exit_status="dry-run" if dry else ("ok" if ok else "error"),
        error="" if dry else redact(_clip(getattr(result, "error", "") or "")),
        usage=None if dry else getattr(result, "usage", None),
        duration_seconds=round(max(0.0, duration_seconds), 3),
        outcome=outcome,
        # The raw streams, redacted and clipped: `parse_tokens` reads the
        # envelope, but a post-mortem often needs the bytes the CLI printed.
        stdout="" if dry else redact(_clip(getattr(result, "output", "") or "", STREAM_CHARS)),
        stderr="" if dry else redact(_clip(getattr(result, "error", "") or "", STREAM_CHARS)),
    )
    write(traj, repo_root)
    return traj
