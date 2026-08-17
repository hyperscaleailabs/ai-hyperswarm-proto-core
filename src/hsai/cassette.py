"""Deterministic subprocess cassettes: record real ``Runner`` calls, replay
them later without touching git, ``gh``, or ``claude``.

Every other regression test in this repo answers a real ``run_once`` question
against a hand-written :class:`~hsai.proc.Runner` fake (see
``tests/test_orchestrator.py``'s ``FakeRunner``) - useful, but it is a guess at
what ``gh``/``git``/``claude`` would say, not a record of what they actually
said. :class:`RecordingRunner` closes that gap: it wraps any real ``Runner``
and appends every call - argv, cwd, return code, and (redacted, truncated)
stdout/stderr - to a cassette. :class:`ReplayRunner` is the other half: it
answers each call from a cassette instead of a subprocess, so ``hsai replay``
can re-run ``run_once`` against a real recorded session for free, as many
times as the orchestrator's own logic changes.

``ReplayRunner`` is deliberately paranoid about GitHub-mutating commands
(``git push``, ``gh pr create``/``merge``, ``gh issue create``): it will only
ever answer one from an EXACT recorded entry, never fabricate a plausible
"success" for one that is missing. Structurally it never touches a real
subprocess at all - there is no wrapped ``Runner`` underneath it - so a
mutating call it does not recognise fails loudly instead of silently no-op'ing
against a live repository.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .proc import Proc, Runner
from .trajectory import redact

# Per-entry stdout/stderr cap - mirrors hsai.trajectory.STEP_CHARS in spirit:
# bounded so one runaway command cannot bloat the cassette.
ENTRY_CHARS = 4000

# Command shapes that would touch GitHub or the shared git remote for real.
# ReplayRunner refuses all of these unless an exact recorded entry exists.
MUTATING_PREFIXES: tuple[tuple[str, ...], ...] = (
    ("git", "push"),
    ("gh", "pr", "create"),
    ("gh", "pr", "merge"),
    ("gh", "issue", "create"),
)

# Env-var NAMES that are secret-shaped regardless of what their value looks
# like - a recorded `ANTHROPIC_API_KEY=1` overlay flag must still be redacted.
_SECRET_ENV_NAME = re.compile(
    r"(?i)^(ANTHROPIC[_A-Z]*|GH_TOKEN|GITHUB_TOKEN|.*_(TOKEN|KEY|SECRET|PASSWORD))$"
)


def is_mutating(cmd: list[str]) -> bool:
    return any(tuple(cmd[: len(prefix)]) == prefix for prefix in MUTATING_PREFIXES)


def _clip(text: str, limit: int = ENTRY_CHARS) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"... [+{len(text) - limit} chars]"


def _redact_env(env: dict[str, str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in (env or {}).items():
        out[key] = redact(value) if not _SECRET_ENV_NAME.match(key) else "[redacted]"
    return out


@dataclass
class CassetteEntry:
    """One recorded ``Runner`` call - argv in, ``Proc`` out."""

    argv: list[str]
    cwd: str | None
    env: dict[str, str] = field(default_factory=dict)
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class Cassette:
    """A cassette: the iteration identity it belongs to, plus its entries.

    ``iteration``/``branch``/``block`` are recorded alongside the entries so
    ``hsai replay`` can call ``run_once`` with the exact same identity the
    recording run used - required for the replay to produce argv-identical
    commands (a regenerated branch name would never match a recorded one).
    """

    iteration: int
    branch: str
    block: int = 0
    entries: list[CassetteEntry] = field(default_factory=list)


class RecordingRunner:
    """Wrap a ``Runner``; every call is captured as a redacted cassette entry."""

    def __init__(self, inner: Runner) -> None:
        self.inner = inner
        self.entries: list[CassetteEntry] = []

    def __call__(
        self, cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None
    ) -> Proc:
        proc = self.inner(
            cmd, cwd=cwd, env=env, env_remove=env_remove, timeout=timeout,
            input_text=input_text,
        )
        self.entries.append(
            CassetteEntry(
                argv=list(cmd),
                cwd=cwd,
                env=_redact_env(dict(env) if env else {}),
                returncode=proc.code,
                stdout=redact(_clip(proc.stdout)),
                stderr=redact(_clip(proc.stderr)),
            )
        )
        return proc


class ReplayError(RuntimeError):
    """Raised when a replayed command has no safe cassette answer."""


class ReplayRunner:
    """Answer every call from a cassette; never executes anything for real.

    Matching is positional per distinct argv: two recorded ``["pytest"]``
    calls (CI-before, CI-after) are answered in the order they were recorded,
    which is exactly the order ``run_once`` calls them in.
    """

    def __init__(self, entries: list[CassetteEntry]) -> None:
        self._queues: dict[tuple[str, ...], deque[CassetteEntry]] = defaultdict(deque)
        for entry in entries:
            self._queues[tuple(entry.argv)].append(entry)

    def __call__(
        self, cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None
    ) -> Proc:
        cmd = list(cmd)
        queue = self._queues.get(tuple(cmd))
        entry = queue.popleft() if queue else None
        if entry is None:
            if is_mutating(cmd):
                raise ReplayError(
                    f"refusing mutating command with no recorded cassette entry: {cmd!r}"
                )
            raise ReplayError(f"no cassette entry for command: {cmd!r}")
        return Proc(cmd, entry.returncode, entry.stdout, entry.stderr)


def save(cassette: Cassette, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "iteration": cassette.iteration,
        "branch": cassette.branch,
        "block": cassette.block,
        "entries": [asdict(e) for e in cassette.entries],
    }
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def load(path: str | Path) -> Cassette:
    data: dict[str, Any] = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = [CassetteEntry(**e) for e in data.get("entries", [])]
    return Cassette(
        iteration=int(data["iteration"]), branch=str(data["branch"]),
        block=int(data.get("block", 0)), entries=entries,
    )
