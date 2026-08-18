"""Durable per-block cycle journal: an append-only record that makes steps idempotent.

Before this module ``run_cycle`` held the whole :class:`~hsai.governance.BlockReport`
in memory. A crash, a laptop sleep, or the budget gate's hard halt lost the
block's record entirely, and re-running the cycle re-synthesized tickets,
re-wrote the whitepaper, re-generated persona articles and re-opened a review
issue - burning quota and polluting the backlog with duplicates. There was no
way to answer "what did block N actually complete" once the process died.

A journal is one append-only JSONL file per block under
``.hsai/cycles/<cycle_index>/journal.jsonl``. Every side-effecting cycle step
appends exactly one :class:`JournalRecord` after it completes, carrying the
payload the step produced. :func:`once` is the whole contract:

    payload = once(jr, "whitepaper", "block", write_the_whitepaper)

On a first run the callable executes and its payload is journaled; on a resumed
run the recorded payload is returned verbatim and the callable never runs. That
single rule gives the cycle at-most-once GitHub writes and lets a resumed block
rebuild the same brief an uninterrupted one would have produced.

Two statuses close a journal: ``halted`` (the budget gate hard-breached and
stopped new work) and ``complete`` (the block finished). Either makes the
journal *terminal*, which is how ``hsai cycle --resume`` picks a block to resume
without ever restarting a block that already finished or was deliberately
halted.

The store lives under ``.hsai/`` (gitignored) for the same reason trajectories
do: it is local operational forensics, not repo content. What reaches the review
brief is one summary line via ``BlockReport.notes``.

Synthesis: OpenBMB/ChatDev's session durability (state survives the transport
dying - reconnect and replay rather than restart), assafelovic/gpt-researcher's
reconciliation discipline (fold partial, interrupted work back in without
duplicating it), and run-llama/llama_index's structured telemetry direction
(every run step emits a durable, inspectable record).
"""
from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CYCLES_DIR = ".hsai/cycles"
JOURNAL_FILE = "journal.jsonl"
# A dry run journals into its own file so a rehearsal can never satisfy - or
# poison - a later live run of the same block.
DRY_RUN_JOURNAL_FILE = "journal.dry-run.jsonl"

# Record statuses. DONE is one completed step; the other two close the journal.
DONE = "done"
HALTED = "halted"
COMPLETE = "complete"
TERMINAL_STATUSES = (HALTED, COMPLETE)

# Steps the cycle journals. Not enforced - a new step must not need a schema
# change to become durable - but named so the file is self-describing.
STEPS = (
    "practices_baseline", "janitor", "synthesis", "budget", "budget_halt", "iteration", "prune",
    "sync", "practices_adopted", "whitepaper", "articles", "direction", "governance_ticket",
    "governance_pr", "review_issue", "block",
)

# Serializes appends so a step running under a worker thread cannot interleave
# a partial line with another (same discipline as the quota ledger).
_JOURNAL_LOCK = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class JournalRecord:
    """One completed cycle step - the unit the journal appends."""

    step: str
    key: str
    status: str = DONE
    payload: Any = None
    created: str = field(default_factory=_now)

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def cycles_dir(repo_root: str | Path) -> Path:
    return Path(repo_root) / CYCLES_DIR


def journal_path(repo_root: str | Path, cycle_index: int, *, dry_run: bool = False) -> Path:
    name = DRY_RUN_JOURNAL_FILE if dry_run else JOURNAL_FILE
    return cycles_dir(repo_root) / str(cycle_index) / name


def append_record(path: str | Path, record: JournalRecord) -> Path:
    """Append one record as a single JSON line (append-only, never rewrites)."""
    path = Path(path)
    line = record.to_json() + "\n"
    with _JOURNAL_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    return path


def read_records(path: str | Path) -> list[JournalRecord]:
    """Parse every record back off disk (empty list if the journal is absent).

    A trailing partial line - the process died mid-write - is dropped rather
    than raising: a torn record means that step did not complete, so re-running
    it is exactly the right behaviour.
    """
    path = Path(path)
    if not path.exists():
        return []
    records: list[JournalRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except ValueError:
            continue
        records.append(JournalRecord(**data))
    return records


@dataclass
class Journal:
    """One block's step records, plus what this run replayed rather than ran."""

    path: Path
    cycle_index: int
    records: list[JournalRecord] = field(default_factory=list)
    replayed: list[str] = field(default_factory=list)
    prior: int = 0  # records already on disk when this run opened the journal

    @property
    def resumed(self) -> bool:
        """True when this run found work already recorded for the block."""
        return self.prior > 0

    def find(self, step: str, key: str) -> JournalRecord | None:
        for record in self.records:
            if record.step == step and record.key == key:
                return record
        return None

    def terminal(self) -> JournalRecord | None:
        """The record that closed this journal (halted or complete), if any."""
        return next((r for r in self.records if r.terminal), None)

    def append(self, record: JournalRecord) -> JournalRecord:
        append_record(self.path, record)
        self.records.append(record)
        return record

    def summary(self) -> str:
        """The one line the review brief carries about a resumed block."""
        shown = ", ".join(self.replayed[:6])
        more = f", +{len(self.replayed) - 6} more" if len(self.replayed) > 6 else ""
        return (
            f"resume: replayed {len(self.replayed)} recorded step(s) from block "
            f"{self.cycle_index}'s journal ({shown}{more}) - no step was re-executed"
        )


def open_journal(
    repo_root: str | Path, cycle_index: int, *, dry_run: bool = False
) -> Journal:
    """Open (or start) the journal for one block."""
    path = journal_path(repo_root, cycle_index, dry_run=dry_run)
    records = read_records(path)
    return Journal(path=path, cycle_index=cycle_index, records=records, prior=len(records))


def once(
    jr: Journal | None,
    step: str,
    key: str,
    fn: Callable[[], Any],
    *,
    status: str = DONE,
) -> Any:
    """Run ``fn`` once per ``(step, key)`` for a block, replaying it thereafter.

    The payload is journaled *after* ``fn`` returns, so a crash mid-step leaves
    no record and the step is retried on resume: at-least-once for the effect,
    at-most-once for everything that completed. ``jr=None`` disables journaling
    entirely and just calls ``fn`` - the pre-journal behaviour.
    """
    if jr is None:
        return fn()
    recorded = jr.find(step, key)
    if recorded is not None:
        jr.replayed.append(f"{step}:{key}" if key else step)
        return recorded.payload
    payload = fn()
    jr.append(JournalRecord(step=step, key=key, status=status, payload=payload))
    return payload


def resumable_indices(repo_root: str | Path, *, dry_run: bool = False) -> list[int]:
    """Block indices whose journal exists and was never closed, oldest first."""
    root = cycles_dir(repo_root)
    if not root.is_dir():
        return []
    name = DRY_RUN_JOURNAL_FILE if dry_run else JOURNAL_FILE
    found: list[int] = []
    for child in root.iterdir():
        if not child.is_dir() or not child.name.lstrip("-").isdigit():
            continue
        records = read_records(child / name)
        if records and not any(r.terminal for r in records):
            found.append(int(child.name))
    return sorted(found)


def latest_resumable(repo_root: str | Path, *, dry_run: bool = False) -> int | None:
    """The most recent unfinished block, or ``None`` when there is nothing to resume."""
    indices = resumable_indices(repo_root, dry_run=dry_run)
    return indices[-1] if indices else None
