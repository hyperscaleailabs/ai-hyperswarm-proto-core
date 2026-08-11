"""Deterministic replay of a recorded iteration.

A trajectory (see :mod:`hsai.trajectory`) records every subprocess an iteration
ran, in order. :func:`make_runner` turns one back into a
:class:`hsai.proc.Runner`, so ``orchestrator.run_once`` can be re-driven against
it end to end: no ``claude``, no ``git push``, no ``gh``, no quota, no network.

That buys three things the loop did not have:

- a regression corpus of real agent behaviour that costs nothing to re-run, so
  changes to prompt construction, phase artifacts or tier selection are tested
  against what actually happened rather than a hand-written fake;
- a primary source for post-mortems on blocked tickets - the run itself, not a
  PR body written after it;
- a loud failure when the harness drifts away from the recording, instead of a
  replay that quietly passes because it no longer tests the same thing.

That last point is the whole design constraint. A cassette that silently
tolerates divergence is worse than no cassette: every mismatch is an error, and
:class:`PromptDriftError` names the phase that moved so the fix is obvious.
When the worker prompt template legitimately changes, re-record the fixtures
(``scripts/record_trajectory_fixtures.py``) - the same discipline as any
record/replay cassette suite.

Synthesis: langchain's ``_test_vcr`` job (record once, replay in CI forever),
SWE-agent (the ``.traj`` file is the primary artifact, with tooling built on
it), and FoundationAgents/MetaGPT (phase-tagged steps, so a divergence report
can point at an SOP boundary).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace as replace_dataclass
from pathlib import Path
from typing import Any

from . import trajectory
from .config import CoreConfig
from .orchestrator import IterationResult, run_once
from .proc import Proc
from .trajectory import Redactor, TrajectoryEvent


class ReplayError(RuntimeError):
    """The replayed run diverged from what was recorded."""


class PromptDriftError(ReplayError):
    """A recorded agent prompt no longer matches the one the loop builds."""


def _prompt_of(cmd: Sequence[str]) -> str | None:
    """The prompt inside a ``claude -p <prompt> ...`` argv, if this is one."""
    if len(cmd) >= 3 and cmd[0] == "claude" and cmd[1] == "-p":
        return cmd[2]
    return None


def _shape(cmd: Sequence[str]) -> tuple[str, ...]:
    """The identity of a command for drift purposes: tool plus subcommand.

    Deliberately coarse. Branch names carry a timestamp and a uuid, worktree
    paths carry the repo root, and PR/issue numbers are assigned at record
    time - none of those can match on replay, and none of them signal that the
    *harness* changed. A different tool, or the same tool doing a different
    thing, does.
    """
    head = list(cmd[:1])
    for token in cmd[1:2]:
        if not token.startswith("-") and "/" not in token and not token.endswith(".py"):
            head.append(token)
    return tuple(head)


def _describe(cmd: Sequence[str]) -> str:
    return " ".join(cmd[:2]) or "(empty command)"


class ReplayRunner:
    """A ``Runner`` that answers from a recording instead of a subprocess.

    Events are consumed strictly in order - ``run_once`` is sequential, so
    position *is* the contract. ``strict`` adds two further checks that a
    tolerant replay would let slide: commands the recording does not cover, and
    events the replay never got round to consuming.
    """

    def __init__(
        self,
        events: Sequence[TrajectoryEvent],
        *,
        strict: bool = False,
        repo_root: str = "",
        redactor: Redactor | None = None,
    ) -> None:
        self.events = list(events)
        self.strict = strict
        self.repo_root = str(repo_root or "")
        self.redactor = redactor
        self.index = 0

    @property
    def iteration(self) -> int:
        """The recorded iteration number (0 when the id is not numeric)."""
        for event in self.events:
            if event.iteration.isdigit():
                return int(event.iteration)
        return 0

    def __call__(
        self,
        cmd: Sequence[str],
        *,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
        input_text: str | None = None,
    ) -> Proc:
        cmd = list(cmd)
        if self.index >= len(self.events):
            message = (
                f"trajectory exhausted after {len(self.events)} event(s); the run "
                f"asked for an extra command {_describe(cmd)!r}"
            )
            if self.strict:
                raise ReplayError(message)
            return Proc(cmd, 0, "", "")

        event = self.events[self.index]
        self.index += 1
        self._check(cmd, event)

        stdout = event.stdout
        # The recording's repo root is a path that no longer exists. Rebinding it
        # is what keeps a replay inside its sandbox instead of writing lessons
        # into whichever tree happened to record the fixture.
        if self.repo_root and cmd[:3] == ["git", "rev-parse", "--show-toplevel"]:
            stdout = f"{self.repo_root}\n"
        return Proc(cmd, event.exit_code, stdout, event.stderr)

    def finish(self) -> None:
        """Assert the replay consumed the whole recording (strict mode only)."""
        if self.strict and self.index < len(self.events):
            unconsumed = self.events[self.index]
            raise ReplayError(
                f"replay stopped early at phase {unconsumed.phase!r}: "
                f"{len(self.events) - self.index} of {len(self.events)} recorded "
                f"event(s) were never replayed, starting with "
                f"{_describe(unconsumed.command)!r}"
            )

    # --- drift detection ------------------------------------------------------
    def _check(self, cmd: list[str], event: TrajectoryEvent) -> None:
        requested = _prompt_of(cmd)
        if event.is_agent != (requested is not None) or (
            requested is None and _shape(cmd) != _shape(event.command)
        ):
            raise ReplayError(self._where(event, "command drift") + (
                f": recorded {_describe(event.command)!r}, "
                f"replay requested {_describe(cmd)!r}"
            ))
        if requested is None:
            return
        # Hash the prompt the way the recorder did - post-redaction - so a
        # scrubbed recording still compares apples to apples.
        scrubbed = self.redactor(requested) if self.redactor else requested
        got = trajectory.sha256_of(scrubbed)
        if got != event.prompt_sha256:
            raise PromptDriftError(self._where(event, "prompt drift") + (
                f": recorded prompt {event.prompt_sha256[:12] or '(none)'}, "
                f"replay built {got[:12]}. The worker prompt template changed - "
                f"re-record this trajectory before trusting the replay."
            ))

    def _where(self, event: TrajectoryEvent, what: str) -> str:
        return (
            f"{what} at phase {event.phase!r} (event {self.index} of {len(self.events)})"
        )


def load_events(path: str | Path) -> list[TrajectoryEvent]:
    """Read a trajectory, refusing an empty one rather than replaying nothing."""
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"no trajectory at {target}")
    events = trajectory.read_events(target)
    if not events:
        raise ValueError(f"trajectory {target} records no events")
    return events


def make_runner(
    path: str | Path,
    *,
    strict: bool = False,
    repo_root: str = "",
    redactor: Redactor | None = None,
) -> ReplayRunner:
    """Build a ``Runner``-compatible callable that replays ``path``."""
    return ReplayRunner(
        load_events(path), strict=strict, repo_root=repo_root, redactor=redactor
    )


def replay_iteration(
    cfg: CoreConfig,
    path: str | Path,
    *,
    repo_dir: str,
    strict: bool = False,
) -> IterationResult:
    """Re-drive ``run_once`` against a recorded trajectory inside ``repo_dir``.

    ``repo_dir`` should be a throwaway directory: the replay writes a lesson, a
    ledger record and a worktree path exactly as the real iteration did, and
    those artifacts are the evidence that the replay really executed the loop
    rather than merely parsing a file.
    """
    runner = make_runner(
        path, strict=strict, repo_root=repo_dir, redactor=Redactor.from_config(cfg)
    )
    # Recording a replay would be a hall of mirrors, and would grow the store
    # with runs that never happened.
    quiet: dict[str, Any] = {**cfg.trajectories, "enabled": False}
    result = run_once(
        replace_dataclass(cfg, trajectories=quiet),
        repo_dir=repo_dir,
        runner=runner,
        ai_runner=runner,
        iteration=runner.iteration,
    )
    runner.finish()
    result.notes.append(f"replayed {runner.index}/{len(runner.events)} recorded event(s)")
    return result
