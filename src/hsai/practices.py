"""Reference-practice registry: what we saw upstream, and what became of it.

G1 asserts that "every improvement should trace back to something observed in
the field", but nothing on disk made that claim checkable: the synthesis
context pack fetched a README / commit log / CI inventory per reference project
and discarded all of it the moment the model answered.

This module is the durable counterpart:

- **Registry** (``knowledge/reference/practices.yaml``) - one row per practice
  observed upstream: the source repo, the specific artifact that carried it (a
  commit subject, a workflow file, a README section), a one-line description,
  and a lifecycle status. ``proposed`` when a ticket is filed for it,
  ``adopted`` once that ticket's PR merges (ticket, PR and lesson note
  recorded), ``rejected`` once the ticket exhausts its attempts, ``superseded``
  when a later practice replaces it.
- **Journal** (``knowledge/reference/adoptions.jsonl``) - append-only status
  transitions. Parallel workers never rewrite the registry (they would collide
  on a shared derived file); they append one line, exactly like the quota
  ledger, and the serialized ``hsai reindex`` / cycle step folds the journal
  into the registry.

Synthesis: run-llama/llama_index (index an external corpus once into durable
retrievable artifacts instead of re-reading raw sources on every query),
assafelovic/gpt-researcher (a research pass must terminate in a durable,
source-cited report, with planning and synthesis as distinct states),
FoundationAgents/MetaGPT (each SOP stage emits a defined, auditable work
product), SWE-agent/SWE-agent (every run is inspectable and linked back to the
issue that produced it - here extended past the ticket to the upstream
artifact).
"""
from __future__ import annotations

import json
import re
import threading
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .config import CoreConfig

# Lifecycle of a practice.
PROPOSED = "proposed"
ADOPTED = "adopted"
REJECTED = "rejected"
SUPERSEDED = "superseded"
STATUSES = (PROPOSED, ADOPTED, REJECTED, SUPERSEDED)

# Statuses a re-proposal must never walk back to ``proposed``.
_SETTLED = (ADOPTED, REJECTED, SUPERSEDED)

DEFAULT_REFERENCE_DIR = "knowledge/reference"
REGISTRY_FILE = "practices.yaml"
JOURNAL_FILE = "adoptions.jsonl"

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s+")

_HEADER = (
    "# Reference-practice registry - which upstream practice became which change.\n"
    "# Rebuilt from knowledge/reference/adoptions.jsonl by `hsai reindex`; never\n"
    "# written by a parallel worker. See src/hsai/practices.py.\n"
)

# Serializes journal appends so concurrent workers never interleave a line.
_JOURNAL_LOCK = threading.Lock()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slug(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-") or "untitled"


def make_id(source_repo: str, description: str) -> str:
    """A stable id: source repo + a slug of the one-line description.

    Stable means re-deriving the same practice from the same ticket yields the
    same row, which is what keeps ``upsert`` idempotent across rotations.
    """
    return f"{slug(source_repo)}--{slug(description)[:60].strip('-')}"


@dataclass
class Practice:
    """One practice observed in a reference project, tracked to its outcome."""

    id: str
    source_repo: str
    source_artifact: str  # commit subject / workflow file / README section
    description: str  # one line: what we take from it
    status: str = PROPOSED
    ticket: int | None = None
    pr: int | None = None
    lesson: str = ""  # knowledge-base note name of the lesson it produced
    reason: str = ""  # why it was rejected / superseded
    created: str = field(default_factory=_today)
    updated: str = field(default_factory=_today)

    @classmethod
    def new(cls, *, source_repo: str, source_artifact: str, description: str, **kwargs):
        """Build a practice with its id derived from repo + description."""
        return cls(
            id=make_id(source_repo, description),
            source_repo=source_repo,
            source_artifact=source_artifact,
            description=description,
            **kwargs,
        )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Practice:
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def render_line(self) -> str:
        """One registry row, rendered for a note or a prompt."""
        trail = []
        if self.ticket:
            trail.append(f"ticket #{self.ticket}")
        if self.pr:
            trail.append(f"PR #{self.pr}")
        if self.lesson:
            trail.append(f"[[{self.lesson}]]")
        if self.reason:
            trail.append(self.reason)
        suffix = f" ({', '.join(trail)})" if trail else ""
        return f"`{self.source_repo}` - {self.description} - _{self.source_artifact}_{suffix}"


# --- paths --------------------------------------------------------------------
def reference_dir(cfg: CoreConfig, repo_root: str | Path) -> Path:
    rel = cfg.knowledge.get("reference_dir", DEFAULT_REFERENCE_DIR)
    return Path(repo_root) / rel


def registry_path(cfg: CoreConfig, repo_root: str | Path) -> Path:
    return reference_dir(cfg, repo_root) / REGISTRY_FILE


def journal_path(cfg: CoreConfig, repo_root: str | Path) -> Path:
    return reference_dir(cfg, repo_root) / JOURNAL_FILE


# --- registry -----------------------------------------------------------------
def load(path: str | Path) -> list[Practice]:
    """Read the registry off disk (empty list when it does not exist yet)."""
    path = Path(path)
    if not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    return [Practice.from_dict(item) for item in raw if isinstance(item, dict)]


def save(path: str | Path, practices: Iterable[Practice]) -> Path:
    """Write the registry, sorted by id so the file diffs cleanly."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [p.to_dict() for p in sorted(practices, key=lambda p: p.id)]
    body = yaml.safe_dump(payload, sort_keys=False, allow_unicode=True, width=1000)
    path.write_text(_HEADER + body, encoding="utf-8")
    return path


def _merge(existing: Practice, incoming: Practice) -> Practice:
    """Fold ``incoming`` onto ``existing``: empty fields never erase known ones,
    and a re-proposal never walks a settled practice back to ``proposed``."""
    merged = replace(existing)
    for f in fields(Practice):
        if f.name in ("id", "created", "updated"):
            continue
        value = getattr(incoming, f.name)
        if value is None or value == "":
            continue
        setattr(merged, f.name, value)
    if incoming.status == PROPOSED and existing.status in _SETTLED:
        merged.status = existing.status
    merged.updated = incoming.updated or _today()
    return merged


def upsert(practices: Sequence[Practice], practice: Practice) -> list[Practice]:
    """Insert ``practice``, or fold it onto the existing row with the same id."""
    out = list(practices)
    for i, existing in enumerate(out):
        if existing.id == practice.id:
            out[i] = _merge(existing, practice)
            return out
    out.append(practice)
    return out


def by_status(practices: Iterable[Practice], status: str) -> list[Practice]:
    return [p for p in practices if p.status == status]


def by_repo(practices: Iterable[Practice], repo: str) -> list[Practice]:
    return [p for p in practices if p.source_repo == repo]


def find_by_ticket(practices: Iterable[Practice], ticket: int | None) -> Practice | None:
    if not ticket:
        return None
    return next((p for p in practices if p.ticket == ticket), None)


# --- journal (append-only, safe for parallel workers) -------------------------
@dataclass
class Transition:
    """One status change, journalled by a worker for the serialized step to apply."""

    practice_id: str
    status: str
    ticket: int | None = None
    pr: int | None = None
    lesson: str = ""
    reason: str = ""
    created: str = field(default_factory=_now)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def record_transition(path: str | Path, transition: Transition) -> Path:
    """Append one transition as a single JSON line (append-only, never rewrites)."""
    path = Path(path)
    line = transition.to_json() + "\n"
    with _JOURNAL_LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line)
    return path


def read_transitions(path: str | Path) -> list[Transition]:
    path = Path(path)
    if not path.exists():
        return []
    out: list[Transition] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(Transition(**json.loads(line)))
    return out


def apply_transitions(cfg: CoreConfig, repo_root: str | Path) -> list[Practice]:
    """Fold the journal into the registry. Serialized paths only (reindex/cycle).

    Idempotent: replaying the whole journal always yields the same registry, so
    the journal stays append-only and remains the audit trail.
    """
    reg_path = registry_path(cfg, repo_root)
    registry = load(reg_path)
    if not registry:
        return []
    index = {p.id: p for p in registry}
    changed: dict[str, Practice] = {}
    for t in read_transitions(journal_path(cfg, repo_root)):
        p = index.get(t.practice_id)
        if p is None or t.status not in STATUSES:
            continue
        p.status = t.status
        p.ticket = t.ticket or p.ticket
        p.pr = t.pr or p.pr
        p.lesson = t.lesson or p.lesson
        p.reason = t.reason or p.reason
        p.updated = t.created[:10] or _today()
        changed[p.id] = p
    if changed:
        save(reg_path, registry)
    return list(changed.values())


# --- deriving a practice from a synthesized ticket -----------------------------
def _first_known_repo(text: str, known_repos: Sequence[str]) -> str:
    """The reference repo named earliest in ``text`` (matching is exact)."""
    hits = [(text.find(r), r) for r in known_repos if r and r in text]
    return min(hits)[1] if hits else ""


def _artifact_sentence(text: str, repo: str) -> str:
    """The sentence of the rationale that names ``repo`` - the cited artifact."""
    for sentence in _SENTENCE_RE.split(text.strip()):
        if repo and repo in sentence:
            return " ".join(sentence.split())
    return " ".join(text.split())


def practice_from_ticket(
    *,
    title: str,
    rationale: str,
    known_repos: Sequence[str],
    studied: Sequence[str] = (),
    ticket: int | None = None,
) -> Practice:
    """Derive the ``proposed`` practice a synthesized ticket stands for.

    The source repo is the reference project the ticket's synthesis rationale
    names first; the artifact is the sentence that cites it. That keeps the
    provenance pointing at a concrete upstream thing rather than a repo name.
    """
    repo = _first_known_repo(rationale, known_repos) or (studied[0] if studied else "unknown")
    return Practice.new(
        source_repo=repo,
        source_artifact=(_artifact_sentence(rationale, repo) or "synthesis rationale")[:400],
        description=title,
        status=PROPOSED,
        ticket=ticket,
    )


# --- rendering ----------------------------------------------------------------
def render_for_prompt(practices: Sequence[Practice]) -> str:
    """The memory the synthesizer gets: what is done, and what already failed.

    Both sections are rendered whenever the registry has any row at all - an
    explicitly empty bucket still tells the planner the ledger exists. An empty
    registry renders nothing, so a fresh repo adds no noise to the prompt.
    """
    if not practices:
        return ""
    adopted = "\n".join(f"- {p.render_line()}" for p in by_status(practices, ADOPTED))
    rejected = "\n".join(
        f"- `{p.source_repo}` - {p.description} - rejected: "
        f"{p.reason or 'no reason recorded'}"
        for p in by_status(practices, REJECTED)
    )
    return (
        "Already adopted - do NOT re-propose these; this loop has already "
        f"shipped them:\n{adopted or '- _(none yet)_'}\n\n"
        "Previously rejected - do NOT re-propose unless you have a materially "
        f"different angle:\n{rejected or '- _(none yet)_'}"
    )


def render_for_note(practices: Sequence[Practice], repo: str) -> str:
    """The 'Practices adopted from this project' body of a reference note."""
    rows = [p for p in by_repo(practices, repo) if p.status != REJECTED]
    if not rows:
        return "_No practice has been taken from this project yet._"
    return "\n".join(f"- **{p.status}** - {p.render_line()}" for p in rows)
