"""Worker trajectories: the durable record of what one iteration actually did.

Before this module a ``claude -p`` run left nothing behind but a boolean and a
truncated stderr excerpt, so the loop could not answer forensic questions about
its own behaviour - what a failed worker tried, where the quota went, whether a
retry differed from its predecessor. A :class:`Trajectory` is that record: one
JSON file per iteration under
``knowledge/trajectories/<block>/<iteration>-<branch>.json``, written at the
single invocation choke point (right after ``ai.run_agent``) and refreshed at
every terminal exit, so *every* iteration is captured - pass, fail, dry-run, or
guard-aborted.

What one record holds is deliberately the whole forensic picture of an
iteration, not just the agent call: the prompt and its hash, the model / tier /
selection strategy, each guard's verdict, the local CI step results before and
after, the remote CI conclusion, a changed-path diffstat, per-phase durations,
the failure class, and a truncated tail of the agent's stdout and stderr.

Two invariants keep it safe to commit:

- **Redacted.** Nothing reaches disk before :func:`redact_value` has scrubbed
  every string in the record - API-key-shaped values, gh tokens, ``KEY=VALUE``
  pairs whose key looks secret, and absolute home paths.
- **Bounded.** :data:`MAX_RECORD_CHARS` caps one record; a run that would
  exceed it sheds its earliest steps (a failure shows up at the *end* of a run)
  and then clips its long free-text fields, marking itself ``truncated``.
  :func:`prune` drops whole block directories beyond
  ``execution.trajectory_retention_blocks``, so the store stays bounded over
  time as well as per record.

The lesson still quotes only :meth:`Trajectory.digest` and
:meth:`Trajectory.excerpt` - a counter line and a short redacted tail - because
a lesson is prose for a human and a trajectory is evidence for a machine.
``hsai traj <id>`` reconstructs a record without spending any quota.

Synthesis: SWE-agent (persist a ``.traj`` per run and build a replay/inspector
on it - the run record, not just the final patch, is the primary artifact),
run-llama/llama_index (per-run instrumentation shipped as a feature, not bolted
on), microsoft/JARVIS (intermediate stage results must be separately
addressable, hence per-step data rather than a final blob) and openai/swarm
(the runner returns the full message list, so callers never reconstruct what
happened).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .failures import slug

#: Obsidian-adjacent, and committed: a trajectory is audit evidence (G2), so it
#: ships through the same governance PR as the ledger and the lessons. Mirrored
#: by ``knowledge.trajectories_dir`` in ``.ai-swarm/core.yaml``.
TRAJECTORY_DIR = "knowledge/trajectories"

# Per-step text is clipped so one runaway tool result cannot bloat the store.
STEP_CHARS = 2000
# Raw stdout/stderr are kept only as a tail - enough to see how a run ended.
TAIL_CHARS = 1200
# Hard ceiling on one written record. See `_fit_to_cap`.
MAX_RECORD_CHARS = 32_000
# What the committed lesson may quote: a short tail, tightly clipped.
EXCERPT_STEPS = 5
EXCERPT_CHARS = 240
# What a retry prompt may quote about the attempt before it.
PREVIOUS_ATTEMPT_CHARS = 1200

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


# Changed-path buckets. A path falls in exactly the first bucket that claims
# it, so `total` always equals the sum of the rest.
_DIFF_BUCKETS: tuple[tuple[str, Any], ...] = (
    ("workflows", lambda p: p.startswith(".github/workflows/")),
    ("tests", lambda p: p.startswith("tests/") or Path(p).name.startswith("test_")),
    ("knowledge", lambda p: p.startswith("knowledge/")),
    ("docs", lambda p: p.startswith("docs/") or p.endswith(".md")),
    ("code", lambda p: p.endswith(".py")),
)


def diffstat(paths: list[str] | tuple[str, ...]) -> dict[str, int]:
    """Bucket changed paths into a small, stable shape.

    Coarse on purpose: the question a reader asks of a failed iteration is
    "did it touch code at all, or only notes?", not "how many lines".
    """
    stat = {name: 0 for name, _ in _DIFF_BUCKETS}
    stat["other"] = 0
    for path in paths or ():
        bucket = next((name for name, match in _DIFF_BUCKETS if match(path)), "other")
        stat[bucket] += 1
    stat["total"] = len(paths or ())
    return stat


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


def _dumps(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, sort_keys=True)


def _fit_to_cap(data: dict[str, Any], cap: int) -> dict[str, Any]:
    """Shrink an already-redacted record until its JSON form fits under ``cap``.

    Sheds the *earliest* steps first: a run's failure is at its end, so the
    tail is the part worth keeping. Only if dropping every step is still not
    enough do the long free-text fields get clipped. Either way the result
    stays a complete, parseable record that says ``truncated: true``.
    """
    if len(_dumps(data)) <= cap:
        return data
    data = {**data, "truncated": True}
    steps = list(data.get("steps") or [])
    while steps and len(_dumps({**data, "steps": steps})) > cap:
        steps.pop(0)
    data["steps"] = steps
    for name in ("prompt", "stdout_tail", "stderr_tail", "error"):
        if len(_dumps(data)) <= cap:
            break
        if data.get(name):
            data[name] = _clip(str(data[name]), 400)
    return data


@dataclass
class Trajectory:
    """One iteration, start to finish - the unit the store persists."""

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
    # --- forensics ----------------------------------------------------------
    #: Each guard's verdict, keyed by guard name (`workflow`, `completeness`,
    #: `repro`). Present even when a guard passed, so silence is not ambiguous.
    guards: dict[str, str] = field(default_factory=dict)
    #: Local CI step results, before and after the agent ran.
    ci_before: dict[str, bool] = field(default_factory=dict)
    ci_after: dict[str, bool] = field(default_factory=dict)
    #: The remote check rollup's conclusion, when the iteration got that far.
    remote_ci: str = ""
    changed_paths: list[str] = field(default_factory=list)
    diffstat: dict[str, int] = field(default_factory=dict)
    #: Seconds spent per phase (`agent`, `ci_before`, `ci_after`, `remote_ci`).
    phases: dict[str, float] = field(default_factory=dict)
    stdout_tail: str = ""
    stderr_tail: str = ""
    failure_class: str = ""
    failure_reason: str = ""
    truncated: bool = False
    created: str = field(default_factory=_now)

    @property
    def identifier(self) -> str:
        """Stable id, and the file stem's head: the iteration number.

        Iterations are globally unique (a block numbers its runs
        ``block * 100 + n``), so this addresses exactly one run and is what
        ``hsai traj <iteration>`` takes.
        """
        return str(self.iteration)

    def as_record(self) -> dict[str, Any]:
        """The exact dict that gets written: redacted, then capped."""
        return _fit_to_cap(redact_value(asdict(self)), MAX_RECORD_CHARS)

    def to_json(self) -> str:
        return _dumps(self.as_record())

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
            f"failure={self.failure_class or 'none'}, "
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

    def failure_excerpt(self, limit: int = PREVIOUS_ATTEMPT_CHARS) -> str:
        """What the NEXT attempt on this ticket is shown about this one.

        Bounded and redacted like everything else that leaves the store, and
        deliberately evidence-shaped: the class, why it fired, the guard and CI
        verdicts, then a short tail. A worker that sees this should not need to
        rediscover the failure to avoid repeating it.
        """
        guards = ", ".join(f"{k}={v}" for k, v in sorted(self.guards.items())) or "none run"
        ci = ", ".join(
            f"{k}={'pass' if v else 'FAIL'}" for k, v in sorted(self.ci_after.items())
        ) or "not run"
        lines = [
            f"- failure class: `{self.failure_class or 'unknown'}`"
            + (f" - {self.failure_reason}" if self.failure_reason else ""),
            f"- iteration {self.iteration} on `{self.branch or '(unknown branch)'}`, "
            f"model `{self.model}` (tier `{self.tier}`)",
            f"- guards: {guards}",
            f"- local CI after that attempt: {ci}",
            f"- remote CI: {self.remote_ci or 'not reached'}",
            "- tail of that run:",
            self.excerpt(steps=3, limit=200),
        ]
        return _clip(redact("\n".join(lines)), limit)

    def render(self) -> str:
        """Human-readable reconstruction (what ``hsai replay`` prints)."""
        ticket = f"#{self.ticket}" if self.ticket else "(none)"
        guards = ", ".join(f"{k}={v}" for k, v in sorted(self.guards.items())) or "(none)"
        phases = ", ".join(f"{k}={v:.2f}s" for k, v in sorted(self.phases.items())) or "(none)"
        head = [
            f"trajectory {self.identifier}  [{self.kind}] ticket {ticket} block {self.block}",
            f"branch: {self.branch or '(none)'}",
            f"model: {self.model} (tier={self.tier}, strategy={self.strategy or 'n/a'})"
            f"  duration: {self.duration_seconds:.3f}s",
            f"exit: {self.exit_status}  ok={self.ok}  outcome: {self.outcome}",
            f"failure: {self.failure_class or 'none'}"
            + (f" - {self.failure_reason}" if self.failure_reason else ""),
            f"guards: {guards}",
            f"CI before: {self.ci_before or '(not run)'}  after: {self.ci_after or '(not run)'}"
            f"  remote: {self.remote_ci or '(not reached)'}",
            f"diffstat: {self.diffstat or '(none)'}",
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
        return "\n".join(head + body + tail)


def trajectory_dir(repo_root: str | Path) -> Path:
    return Path(repo_root) / TRAJECTORY_DIR


def block_dir(repo_root: str | Path, block: int) -> Path:
    return trajectory_dir(repo_root) / str(block)


def file_stem(identifier: str, branch: str = "") -> str:
    """``<iteration>-<branch>`` - the branch makes a record self-describing."""
    tail = slug(branch)
    return f"{identifier}-{tail}" if tail else str(identifier)


def path_for(
    repo_root: str | Path, identifier: str, block: int, branch: str = ""
) -> Path:
    return block_dir(repo_root, block) / f"{file_stem(identifier, branch)}.json"


def find(repo_root: str | Path, identifier: str) -> Path | None:
    """Locate one iteration's trajectory without knowing its block or branch."""
    # Ids only - a path is resolved by the caller, never fed to glob().
    if not identifier or not identifier.isdigit():
        return None
    root = trajectory_dir(repo_root)
    matches = sorted(root.glob(f"*/{identifier}.json")) + sorted(
        root.glob(f"*/{identifier}-*.json")
    )
    return matches[0] if matches else None


def write(traj: Trajectory, repo_root: str | Path) -> Path:
    """Persist (or refresh) one trajectory as a single redacted JSON file."""
    path = path_for(repo_root, traj.identifier, traj.block, traj.branch)
    path.parent.mkdir(parents=True, exist_ok=True)
    # as_record() redacts and caps: nothing reaches disk before the scrub pass.
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


def latest_for_ticket(repo_root: str | Path, ticket: int | None) -> Trajectory | None:
    """The most recent *failed* trajectory recorded against ``ticket``.

    This is what turns a retry into an informed second attempt: the previous
    attempt's record is right there on disk, so the next prompt can quote it
    without re-running anything. Returns ``None`` when the ticket has no failed
    predecessor (a first attempt, or one whose record has been pruned).
    """
    if not ticket:
        return None
    best: Trajectory | None = None
    for path in sorted(trajectory_dir(repo_root).glob("*/*.json")):
        try:
            traj = read(path)
        except (OSError, ValueError, TypeError):
            continue  # a half-written or foreign file must not break a run
        if traj.ticket != ticket or not traj.failure_class:
            continue
        if best is None or traj.iteration > best.iteration:
            best = traj
    return best


def prune(repo_root: str | Path, keep_blocks: int) -> list[int]:
    """Drop trajectory block directories older than the newest ``keep_blocks``.

    Records are worth keeping for the recent past and worth bounding beyond it;
    git history retains what the working tree drops. Returns the blocks removed
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
    result: Any = None,
    block: int = 0,
    branch: str = "",
    strategy: str = "",
    duration_seconds: float = 0.0,
    outcome: str = "ran",
) -> Trajectory:
    """Build a trajectory from an :class:`hsai.ai.AIResult` and persist it.

    ``result`` is duck-typed (``ok``/``output``/``error``/``usage``/``payload``)
    so this module stays independent of :mod:`hsai.ai`, and may be ``None`` for
    an iteration that never called a model (a dry run) - which still gets a
    record, because "we chose not to spend quota here" is itself an auditable
    fact about the block.
    """
    payload = getattr(result, "payload", None)
    output = str(getattr(result, "output", "") or "")
    error = str(getattr(result, "error", "") or "")
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
        steps=steps_from_output(payload, output) if result is not None else [],
        ok=bool(getattr(result, "ok", False)) if result is not None else True,
        exit_status=(
            "not-run" if result is None
            else "ok" if getattr(result, "ok", False) else "error"
        ),
        error=redact(_clip(error)),
        usage=getattr(result, "usage", None),
        duration_seconds=round(max(0.0, duration_seconds), 3),
        outcome=outcome,
        stdout_tail=redact(_clip(output[-TAIL_CHARS:], TAIL_CHARS)),
        stderr_tail=redact(_clip(error[-TAIL_CHARS:], TAIL_CHARS)),
    )
    write(traj, repo_root)
    return traj
