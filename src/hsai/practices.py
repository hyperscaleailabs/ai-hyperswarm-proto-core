"""Reference-practice provenance ledger: what was mined, from where, and its fate.

G1 asks every improvement to trace back to something observed in the field, but
until this module existed that citation was decorative: `build_pr_body` and
every lesson cited `cfg.reference_top10[:3]` - the same three repo names on
every PR - and the self-improve path (`_improvement_idea`) returned one
hardcoded chore title regardless of what the reference set actually contained.

A :class:`Practice` is one observed-or-adopted unit of evidence: a repo, a
concrete artifact (file path, workflow name, or commit subject) observed in
it, and one sentence of observation. The store at
``knowledge/reference/practices.jsonl`` is append-only, like
:mod:`hsai.ledger` - but unlike the ledger, a practice's ``status`` changes
over its life (``observed`` -> ``in-flight`` -> ``adopted``/``rejected``), so a
status change is recorded as a NEW line carrying the same ``id``. The file
never rewrites history; :func:`load` folds duplicate ids down to the latest
line (last write wins) to materialize current state, exactly the way an
event-sourced log is normally read.

:func:`next_unadopted` is what turns this from a passive log into the engine
behind the self-improve path (see `orchestrator._improvement_idea`): it picks
the highest-value ``observed`` practice, favouring goal fit and repos with
fewer adoptions so far, so two consecutive self-improve iterations pick two
different practices instead of refiling the same stub ticket nine times.
"""
from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .config import CoreConfig

# Practice lifecycle. A practice starts `OBSERVED` (mined but not yet worked),
# moves to `IN_FLIGHT` the moment a ticket claims it, and ends at `ADOPTED`
# (merged) or `REJECTED` (the architect decided against it).
OBSERVED = "observed"
IN_FLIGHT = "in-flight"
ADOPTED = "adopted"
REJECTED = "rejected"

CATEGORIES = ("ci", "orchestration", "testing", "safety", "docs", "economics")

DEFAULT_PRACTICES_FILE = "knowledge/reference/practices.jsonl"

# Serializes appends so concurrent local workers never interleave a partial
# line - the same discipline the quota ledger uses.
_LOCK = threading.Lock()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class Practice:
    """One observed practice from the reference set - the unit the store holds."""

    id: str
    repo: str
    artifact: str
    category: str
    observation: str
    goal_ids: tuple[str, ...] = ()
    status: str = OBSERVED
    ticket: int | None = None
    pr: int | None = None
    lesson: str = ""
    first_seen: str = field(default_factory=_today)

    def to_json(self) -> str:
        d = asdict(self)
        d["goal_ids"] = list(self.goal_ids)
        return json.dumps(d, sort_keys=True)

    def citation(self) -> str:
        """Repo + artifact + id, for a lesson's or PR's reference-set evidence."""
        return f"{self.id} ({self.repo}: {self.artifact})"


def practices_path(cfg: CoreConfig, repo_root: str | Path) -> Path:
    """Resolve the append-only practices JSONL under the repo's knowledge base."""
    rel = cfg.knowledge.get("practices_file", DEFAULT_PRACTICES_FILE)
    return Path(repo_root) / rel


def append(path: str | Path, practice: Practice) -> Path:
    """Append one record as a single JSON line (append-only, never rewrites)."""
    path = Path(path)
    line = practice.to_json() + "\n"
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    return path


def load(path: str | Path) -> list[Practice]:
    """Materialize current state: one :class:`Practice` per id, latest status.

    Duplicate ids fold to their last-written line (event-sourced update), and
    the result preserves first-seen order. An absent store reads as empty, so
    a fresh checkout with no practices yet mined never errors.
    """
    path = Path(path)
    if not path.exists():
        return []
    order: list[str] = []
    latest: dict[str, Practice] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        raw = json.loads(line)
        raw["goal_ids"] = tuple(raw.get("goal_ids", ()))
        practice = Practice(**raw)
        if practice.id not in latest:
            order.append(practice.id)
        latest[practice.id] = practice
    return [latest[i] for i in order]


def mark(path: str | Path, practice_id: str, status: str, **updates: object) -> Practice:
    """Append a status-change record for ``practice_id``.

    The store stays append-only - this never edits the original line, it adds
    a new one with the same id and the updated status (plus any other field
    overrides, e.g. ``ticket=``/``pr=``/``lesson=``), which :func:`load` then
    folds to the current state. Raises if ``practice_id`` was never observed.
    """
    current = {p.id: p for p in load(path)}
    if practice_id not in current:
        raise KeyError(f"unknown practice id: {practice_id!r}")
    fields = asdict(current[practice_id])
    fields["goal_ids"] = current[practice_id].goal_ids
    fields["status"] = status
    fields.update(updates)
    updated = Practice(**fields)
    append(path, updated)
    return updated


def next_unadopted(cfg: CoreConfig, practices: list[Practice]) -> Practice | None:
    """Pick the highest-value practice to work next, or ``None`` if exhausted.

    Never returns a practice already ``adopted``, ``rejected``, or
    ``in-flight`` - only ``observed`` candidates are eligible. Among those,
    prefer (in order): better fit with the configured goals, repos with fewer
    adopted practices so far (so mining spreads across the reference set
    instead of piling onto whichever repo was mined first), then the practice
    observed longest ago (oldest evidence gets acted on first), then id for a
    fully deterministic tie-break.
    """
    candidates = [p for p in practices if p.status == OBSERVED]
    if not candidates:
        return None
    goal_ids = set(cfg.goal_ids())
    adopted_counts: dict[str, int] = {}
    for p in practices:
        if p.status == ADOPTED:
            adopted_counts[p.repo] = adopted_counts.get(p.repo, 0) + 1

    def score(p: Practice) -> tuple:
        goal_fit = len(set(p.goal_ids) & goal_ids)
        return (-goal_fit, adopted_counts.get(p.repo, 0), p.first_seen, p.id)

    return sorted(candidates, key=score)[0]


def coverage_report(cfg: CoreConfig, practices: list[Practice]) -> list[dict]:
    """Per-repo observed/adopted counts, ordered by reference-set rank.

    ``observed`` counts every distinct practice ever logged for that repo
    (any status); ``adopted`` is the subset that actually merged. A repo with
    ``observed=0`` has never been mined at all - the gap `next_unadopted` is
    meant to close over time.
    """
    counts: dict[str, dict[str, int]] = {}
    for p in practices:
        c = counts.setdefault(p.repo, {"observed": 0, "adopted": 0, "in_flight": 0})
        c["observed"] += 1
        if p.status == ADOPTED:
            c["adopted"] += 1
        elif p.status == IN_FLIGHT:
            c["in_flight"] += 1
    rows: list[dict] = []
    for r in cfg.reference_top10:
        c = counts.get(r.repo, {"observed": 0, "adopted": 0, "in_flight": 0})
        rows.append({"rank": r.rank, "repo": r.repo, **c})
    return rows
