"""Per-iteration trajectories: the committed record of what one loop run did.

:mod:`hsai.trajectory` records what happened *inside* one ``claude -p`` call and
keeps it local, because it quotes the working tree. This module records the
layer above it - the iteration itself - and keeps it *committed*, because that
is the layer whose evidence kept disappearing: an iteration that a guard aborted
left one aggregate ledger row, an 800-char stderr slice in its lesson, and
nothing else. What the worker was asked, which tier was chosen and why, which
guard rejected the result, what remote CI concluded - all unrecoverable.

One iteration writes one append-only JSONL file:

    knowledge/trajectories/<branch>.jsonl

The branch name is already unique per worker (``hsai/iter-<epoch>-<n>-<hex>``),
so parallel workers never collide on a path - the same rule the lesson files
follow. Line 1 is a ``meta`` record; every line after it is one :class:`Step`.

Because these files are committed, everything is scrubbed on the way in, never
on the way out (:func:`redact`): keys named by ``constraints.forbid_env`` are
dropped outright, common credential shapes are rewritten, absolute home paths
collapse to ``~``, and every field is capped at
``knowledge.trajectory_max_chars`` with an explicit truncation marker. Redaction
runs before truncation, so a secret can never survive by being cut in half.

Read them with ``hsai trace show <path>`` (a timeline) and ``hsai trace stats``
(per-step duration and failure-rate rollup across iterations).

Synthesis: SWE-agent (the per-run ``.traj`` artifact is its most reused asset -
inspectable, replayable, convertible into demos), assafelovic/gpt-researcher
(log the sources and cost of every run, not just its answer),
FoundationAgents/MetaGPT (serialize an explicit artifact per phase) and
langchain (record every step of a run through one tracing layer).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from time import time
from typing import Any

from .config import CoreConfig

DEFAULT_DIR = "knowledge/trajectories"
DEFAULT_MAX_CHARS = 20000

REDACTED = "[redacted]"

# Step names. Fixed vocabulary so `hsai trace stats` can roll up across
# iterations without string-matching whatever a caller happened to pass.
WORKTREE_SETUP = "worktree_setup"
CI_BEFORE = "ci_before"
TICKET_CLAIM = "ticket_claim"
MODEL_SELECT = "model_select"
AGENT_RUN = "agent_run"
GUARD_WORKFLOW_REVERT = "guard_workflow_revert"
GUARD_COMPLETENESS = "guard_completeness"
GUARD_REPRO = "guard_repro"
CI_AFTER = "ci_after"
REVIEW_GATE = "review_gate"
PR_OPEN = "pr_open"
CI_REMOTE = "ci_remote"
MERGE_OR_RECOVER = "merge_or_recover"

# Credential shapes that must never reach a public commit. Deliberately broader
# than the exact formats in use: a near-miss costs a `[redacted]` in a log, a
# miss costs a leaked token. The leading lookbehind keeps ordinary prose intact -
# without it, "risk-management" reads as an `sk-` token.
_SECRET_PATTERNS = (
    re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?<![A-Za-z0-9])gh[pousr]_[A-Za-z0-9]{8,}"),
    re.compile(r"(?<![A-Za-z0-9])github_pat_[A-Za-z0-9_]{16,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
)

# `/Users/<name>` and `/home/<name>` identify the machine (and the human) the
# loop ran on, and every worker prompt is full of them.
_HOME_PATTERN = re.compile(r"/(?:Users|home)/[^/\s:\"']+")

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def _env_assignment(name: str) -> re.Pattern[str]:
    """Match ``NAME=v`` / ``NAME: v`` / ``"NAME": "v"`` - however a log printed it."""
    return re.compile(
        r"\b" + re.escape(name) + r"\b[\"']?\s*[:=]\s*[\"']?([^\s\"',;]+)"
    )


def redact(
    text: str,
    *,
    forbid_env: Sequence[str] = (),
    env: Mapping[str, str] | None = None,
) -> str:
    """Scrub credentials and machine-identifying paths out of ``text``.

    ``env`` is injected (defaulting to the real environment) so the scrub can
    also strike the *literal current value* of every forbidden variable, not
    only the shapes we thought to write a pattern for.
    """
    out = text or ""
    environ = os.environ if env is None else env
    for name in forbid_env:
        out = _env_assignment(name).sub(f"{name}={REDACTED}", out)
        value = environ.get(name, "")
        # Short values are words, not secrets; replacing them would corrupt
        # ordinary prose without protecting anything.
        if len(value) >= 8:
            out = out.replace(value, REDACTED)
    for pattern in _SECRET_PATTERNS:
        out = pattern.sub(REDACTED, out)
    return _HOME_PATTERN.sub("~", out)


def truncate(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> str:
    """Cap ``text``, stating explicitly how much was dropped.

    A silently clipped field reads like a complete one, which is exactly the
    kind of quiet lie an audit trail cannot afford.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    dropped = len(text) - max_chars
    return f"{text[:max_chars]}\n... [truncated: {dropped} of {len(text)} chars omitted]"


def scrub(
    value: Any,
    *,
    forbid_env: Sequence[str] = (),
    max_chars: int = DEFAULT_MAX_CHARS,
    env: Mapping[str, str] | None = None,
) -> Any:
    """Redact-then-truncate every string in a JSON-ish structure.

    Keys named by ``constraints.forbid_env`` are dropped at any depth: a
    variable the loop refuses to hand a worker has no business being captured
    as evidence either, whatever its value looks like.
    """
    forbidden = {n.lower() for n in forbid_env}
    if isinstance(value, str):
        return truncate(redact(value, forbid_env=forbid_env, env=env), max_chars)
    if isinstance(value, Mapping):
        return {
            k: scrub(v, forbid_env=forbid_env, max_chars=max_chars, env=env)
            for k, v in value.items()
            if str(k).lower() not in forbidden
        }
    if isinstance(value, (list, tuple)):
        return [scrub(v, forbid_env=forbid_env, max_chars=max_chars, env=env) for v in value]
    return value


def prompt_digest(prompt: str) -> str:
    """Short content hash of a prompt - enough to tell two runs apart."""
    return hashlib.sha256((prompt or "").encode("utf-8")).hexdigest()[:12]


def slug_for_branch(branch: str) -> str:
    """Flatten a branch name into a single safe filename stem."""
    return _SLUG_RE.sub("-", branch or "").strip("-") or "iteration"


def trajectory_path(root: str | Path, branch: str, *, subdir: str = DEFAULT_DIR) -> Path:
    return Path(root) / subdir / f"{slug_for_branch(branch)}.jsonl"


@dataclass
class Step:
    """One phase of an iteration, as recorded at the moment it finished."""

    name: str
    started: str = ""
    duration_s: float = 0.0
    ok: bool = True
    summary: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def render(self, index: int = 0) -> str:
        mark = "ok  " if self.ok else "FAIL"
        head = f"{index:>3}. {self.name:<24} {self.duration_s:>8.3f}s  {mark}"
        return f"{head}  {self.summary}" if self.summary else head


@dataclass
class _StepContext:
    """The mutable handle a ``with traj.step(...)`` block fills in."""

    name: str
    ok: bool = True
    summary: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


class Trajectory:
    """Append-only recorder for one iteration.

    Every collaborator is injected (``clock``, ``env``, the path itself), so
    tests drive it with a fake clock under ``tmp_path`` and never touch the
    network, a subprocess, or the real environment.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        root: str | Path | None = None,
        iteration: int = 0,
        branch: str = "",
        block: int = 0,
        forbid_env: Sequence[str] = (),
        max_chars: int = DEFAULT_MAX_CHARS,
        clock: Callable[[], float] = time,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.path = Path(path)
        self.root = Path(root) if root is not None else self.path.parent
        self.iteration = iteration
        self.branch = branch
        self.block = block
        self.forbid_env = tuple(forbid_env)
        self.max_chars = max_chars
        self.steps: list[Step] = []
        self._clock = clock
        self._env = env
        self._lock = threading.Lock()
        self._started = clock()
        self._header_written = False

    @classmethod
    def for_iteration(
        cls,
        root: str | Path,
        cfg: CoreConfig,
        *,
        branch: str,
        iteration: int = 0,
        block: int = 0,
        clock: Callable[[], float] = time,
        env: Mapping[str, str] | None = None,
    ) -> Trajectory:
        k = cfg.knowledge or {}
        return cls(
            trajectory_path(root, branch, subdir=str(k.get("trajectories_dir", DEFAULT_DIR))),
            root=root,
            iteration=iteration,
            branch=branch,
            block=block,
            forbid_env=cfg.forbidden_env,
            max_chars=int(k.get("trajectory_max_chars", DEFAULT_MAX_CHARS)),
            clock=clock,
            env=env,
        )

    @property
    def relpath(self) -> str:
        """Repo-relative path - what the lesson and the PR body point at."""
        try:
            return self.path.relative_to(self.root).as_posix()
        except ValueError:
            return self.path.as_posix()

    # --- recording ------------------------------------------------------------
    def record(
        self,
        name: str,
        *,
        ok: bool = True,
        summary: str = "",
        detail: Mapping[str, Any] | None = None,
        duration_s: float = 0.0,
    ) -> Step:
        """Append one already-finished step."""
        return self._emit(
            _StepContext(name=name, ok=ok, summary=summary, detail=dict(detail or {})),
            started=self._clock() - max(0.0, duration_s),
            duration_s=duration_s,
        )

    @contextmanager
    def step(self, name: str, *, summary: str = "") -> Iterator[_StepContext]:
        """Time a phase and record it, however it ends.

        The step is written on the way out of the block in every case - a raised
        exception is recorded as a failed step and then re-raised, because a
        crash mid-iteration is precisely the run worth having evidence for.
        """
        ctx = _StepContext(name=name, summary=summary)
        started = self._clock()
        try:
            yield ctx
        except BaseException as exc:  # recorded as a failure, then re-raised
            ctx.ok = False
            ctx.summary = ctx.summary or f"{type(exc).__name__}: {exc}"
            ctx.detail.setdefault("exception", f"{type(exc).__name__}: {exc}")
            raise
        finally:
            self._emit(ctx, started=started, duration_s=self._clock() - started)

    def _emit(self, ctx: _StepContext, *, started: float, duration_s: float) -> Step:
        step = Step(
            name=ctx.name,
            started=_stamp(started),
            duration_s=round(max(0.0, duration_s), 3),
            ok=bool(ctx.ok),
            summary=self._scrub(ctx.summary),
            detail=self._scrub(ctx.detail),
        )
        self.steps.append(step)
        self._append({"record": "step", **asdict(step)})
        return step

    def _scrub(self, value: Any) -> Any:
        return scrub(
            value, forbid_env=self.forbid_env, max_chars=self.max_chars, env=self._env
        )

    def _meta(self) -> dict[str, Any]:
        return {
            "record": "meta",
            "iteration": self.iteration,
            "branch": self.branch,
            "block": self.block,
            "created": _stamp(self._started),
        }

    def _append(self, payload: dict[str, Any]) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                if not self._header_written:
                    fh.write(json.dumps(self._meta(), sort_keys=True) + "\n")
                    self._header_written = True
                fh.write(json.dumps(payload, sort_keys=True) + "\n")

    def copy_to(self, dest_root: str | Path) -> Path | None:
        """Mirror the file into another checkout at the same relative path.

        The durable copy lives at the repo root (it must outlive the worktree,
        which a guard failure deletes); this puts the same bytes inside the
        worktree so the iteration's own PR commits its evidence.
        """
        if not self.path.is_file():
            return None
        dest = Path(dest_root) / self.relpath
        if dest.resolve() == self.path.resolve():
            return dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(self.path.read_text(encoding="utf-8"), encoding="utf-8")
        return dest


# --- reading -------------------------------------------------------------------
@dataclass
class Trace:
    """A trajectory parsed back off disk."""

    path: Path
    meta: dict[str, Any] = field(default_factory=dict)
    steps: list[Step] = field(default_factory=list)

    @property
    def iteration(self) -> int:
        return int(self.meta.get("iteration") or 0)

    @property
    def block(self) -> int:
        return int(self.meta.get("block") or 0)

    @property
    def branch(self) -> str:
        return str(self.meta.get("branch") or "")

    @property
    def duration_s(self) -> float:
        return round(sum(s.duration_s for s in self.steps), 3)

    def failures(self) -> list[Step]:
        return [s for s in self.steps if not s.ok]

    def render(self) -> str:
        """The human-readable timeline ``hsai trace show`` prints."""
        failed = self.failures()
        head = [
            f"trajectory {self.path}",
            f"iteration {self.iteration}  block {self.block}  "
            f"branch {self.branch or '(unknown)'}",
            f"created {self.meta.get('created', '(unknown)')}",
            "",
            f"{'#':>3}  {'step':<24} {'duration':>9}  status  summary",
        ]
        body = [s.render(i) for i, s in enumerate(self.steps, 1)] or ["(no steps recorded)"]
        tail = [
            "",
            f"{len(self.steps)} step(s), {len(failed)} failed, {self.duration_s:.3f}s total",
        ]
        if failed:
            tail.append("failed: " + ", ".join(s.name for s in failed))
        return "\n".join(head + body + tail)


def read(path: str | Path) -> Trace:
    """Parse one trajectory JSONL file. Malformed lines are skipped, not fatal."""
    path = Path(path)
    parsed = Trace(path=path)
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue
        kind = data.pop("record", "step")
        if kind == "meta":
            parsed.meta = data
        else:
            parsed.steps.append(
                Step(
                    name=str(data.get("name", "")),
                    started=str(data.get("started", "")),
                    duration_s=float(data.get("duration_s") or 0.0),
                    ok=bool(data.get("ok", True)),
                    summary=str(data.get("summary", "")),
                    detail=data.get("detail") or {},
                )
            )
    return parsed


def load_all(
    root: str | Path, *, block: int | None = None, subdir: str = DEFAULT_DIR
) -> list[Trace]:
    """Every stored trajectory under ``root``, oldest path first."""
    directory = Path(root) / subdir
    if not directory.is_dir():
        return []
    traces = [read(p) for p in sorted(directory.glob("*.jsonl"))]
    return [t for t in traces if block is None or t.block == block]


@dataclass
class StepStats:
    """Duration and failure rollup for one step name across iterations."""

    name: str
    runs: int = 0
    failures: int = 0
    total_s: float = 0.0
    max_s: float = 0.0

    @property
    def mean_s(self) -> float:
        return self.total_s / self.runs if self.runs else 0.0

    @property
    def failure_rate(self) -> float:
        return self.failures / self.runs if self.runs else 0.0


def aggregate(traces: Iterable[Trace]) -> list[StepStats]:
    """Roll up per-step duration and failure rate, slowest step first."""
    by_name: dict[str, StepStats] = {}
    for one in traces:
        for step in one.steps:
            stat = by_name.setdefault(step.name, StepStats(name=step.name))
            stat.runs += 1
            stat.failures += 0 if step.ok else 1
            stat.total_s += step.duration_s
            stat.max_s = max(stat.max_s, step.duration_s)
    return sorted(by_name.values(), key=lambda s: (-s.total_s, s.name))


def render_stats(traces: Sequence[Trace]) -> str:
    """The rollup ``hsai trace stats`` prints."""
    stats = aggregate(traces)
    failed_runs = sum(1 for t in traces if t.failures())
    head = [
        f"trajectories: {len(traces)}  "
        f"({failed_runs} with at least one failed step)  "
        f"steps: {sum(s.runs for s in stats)}",
        "",
        f"{'step':<24} {'runs':>5} {'fail':>5} {'fail%':>7} "
        f"{'total_s':>9} {'mean_s':>8} {'max_s':>8}",
    ]
    body = [
        f"{s.name:<24} {s.runs:>5} {s.failures:>5} {s.failure_rate * 100:>6.1f}% "
        f"{s.total_s:>9.3f} {s.mean_s:>8.3f} {s.max_s:>8.3f}"
        for s in stats
    ] or ["(no steps recorded)"]
    return "\n".join(head + body)
