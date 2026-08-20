"""The reference-set observatory: cached, diffed study digests per project.

Synthesis used to re-fetch each reference project's README, recent commit
subjects and workflow inventory on every cycle, hand them to the heavy model,
and throw them away. Nothing persisted, so the planner could never tell what
had CHANGED in a project since it was last studied - only what was present -
and every rotation re-derived the same observations (the recurring "refresh
reference-set snapshot and extract one practice" chore is the visible symptom).
G1 says every improvement traces back to a field observation; without a durable
record of the observation, that trace stopped at the ticket.

This module is that record. Three pieces, all dependency-free:

1. **A digest cache.** :func:`fetch_digest` collects one :class:`Digest` per
   project - default branch, head sha, README hash + capped excerpt, the
   ``(sha, subject)`` commit stream, and the workflow-file inventory - and
   :func:`observe` stores it as JSON under ``knowledge/reference/``. Every
   stored field is capped so the cache stays a reviewable artifact rather than
   a mirror of the upstream repo.
2. **A delta.** :func:`diff_digest` answers "what is new since we last looked":
   commit subjects newer than the stored head, workflow files added or removed,
   and whether the README changed. A first-ever observation is marked
   ``baseline`` instead of reporting its whole commit window as new - "we have
   never looked here" is a different statement from "30 things changed".
3. **An adopted-practice index.** :func:`adopted_index` scans the knowledge
   base's lessons for citations of each reference project, so the planner can
   be shown - per project - what this repo has ALREADY taken from it.

:mod:`hsai.synthesis` renders all three into the context pack,
:mod:`hsai.knowledge` composes them into one Obsidian dossier per project, and
:mod:`hsai.governance` surfaces :func:`staleness_line` in DIRECTION.md so an
unobserved project is a visible state rather than a silent one.

Nothing here imports :mod:`hsai.knowledge`: the lesson records
:func:`adopted_index` reads are passed in, which keeps the dossier renderer
(which lives with the other note renderers) free to import this module.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .config import CoreConfig
from .proc import Runner, run

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .knowledge import LessonRecord

REFERENCE_DIR_DEFAULT = "knowledge/reference"
SCHEMA_VERSION = 1

DEFAULT_COMMITS = 30
DEFAULT_README_CHARS = 4000
DEFAULT_SUBJECT_CHARS = 200
DEFAULT_STALE_DAYS = 14
DEFAULT_DELTA_COMMITS = 12

# Headings the synthesis prompt renders per project. Named here (not inlined in
# the f-string) because the tests assert on them: they are the contract between
# what the observatory records and what the planner is told.
DELTA_HEADING = "Changes since last cycle"
DIGEST_HEADING = "Study digest (baseline)"
# Deliberately distinct from hsai.practices.ADOPTED_HEADING: that one covers the
# whole registry, this one is scoped to the single project being rendered.
ADOPTED_FROM_PROJECT_HEADING = (
    "Practices already adopted from this project (do NOT re-propose)"
)

NO_DATA = "(no data fetched)"
NOTHING_ADOPTED = "_(nothing adopted from this project yet)_"

_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _safe(text: str) -> str:
    return _UNSAFE_RE.sub("-", text).strip("-") or "unknown"


def _parse_iso(stamp: str) -> datetime | None:
    """Parse a stored ``fetched_at`` back into an aware datetime ("" -> None)."""
    try:
        parsed = datetime.fromisoformat(stamp)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class ObservatoryConfig:
    """The ``observatory`` block of core.yaml, with documented defaults."""

    dir: str = REFERENCE_DIR_DEFAULT
    commits: int = DEFAULT_COMMITS
    readme_chars: int = DEFAULT_README_CHARS
    subject_chars: int = DEFAULT_SUBJECT_CHARS
    stale_days: int = DEFAULT_STALE_DAYS
    delta_commits: int = DEFAULT_DELTA_COMMITS

    @classmethod
    def from_core(cls, cfg: CoreConfig | None) -> ObservatoryConfig:
        raw = (cfg.observatory if cfg else None) or {}
        d = cls()
        return cls(
            dir=str(raw.get("dir", d.dir)),
            commits=int(raw.get("commits", d.commits)),
            readme_chars=int(raw.get("readme_chars", d.readme_chars)),
            subject_chars=int(raw.get("subject_chars", d.subject_chars)),
            stale_days=int(raw.get("stale_days", d.stale_days)),
            delta_commits=int(raw.get("delta_commits", d.delta_commits)),
        )


# --- the digest ---------------------------------------------------------------

@dataclass(frozen=True)
class Digest:
    """One observation of a reference project, as stored on disk.

    ``commits`` is newest-first and holds ``(sha, subject)`` pairs: the sha is
    what makes :func:`diff_digest` exact (a reworded-but-identical subject is
    not a new commit), the subject is what the planner reads.
    """

    repo: str
    fetched_at: str = ""
    default_branch: str = ""
    head_sha: str = ""
    readme_hash: str = ""
    readme_excerpt: str = ""
    commits: tuple[tuple[str, str], ...] = ()
    workflows: tuple[str, ...] = ()

    @property
    def empty(self) -> bool:
        """Did the fetch come back with nothing usable?

        An empty digest is never stored: overwriting a good cache entry because
        `gh` was missing or rate-limited would destroy the very history this
        module exists to keep.
        """
        return not (self.head_sha or self.readme_hash or self.commits or self.workflows)

    def subjects(self) -> tuple[str, ...]:
        return tuple(subject for _, subject in self.commits)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "fetched_at": self.fetched_at,
            "default_branch": self.default_branch,
            "head_sha": self.head_sha,
            "readme_hash": self.readme_hash,
            "readme_excerpt": self.readme_excerpt,
            "commits": [[sha, subject] for sha, subject in self.commits],
            "workflows": list(self.workflows),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Digest:
        commits = tuple(
            (str(item[0]), str(item[1]))
            for item in data.get("commits", []) or []
            if isinstance(item, (list, tuple)) and len(item) >= 2
        )
        return cls(
            repo=str(data.get("repo", "")),
            fetched_at=str(data.get("fetched_at", "")),
            default_branch=str(data.get("default_branch", "")),
            head_sha=str(data.get("head_sha", "")),
            readme_hash=str(data.get("readme_hash", "")),
            readme_excerpt=str(data.get("readme_excerpt", "")),
            commits=commits,
            workflows=tuple(str(w) for w in data.get("workflows", []) or []),
        )


@dataclass(frozen=True)
class DigestDelta:
    """What changed in one project between two observations.

    ``baseline`` means there was no previous observation to diff against. It is
    deliberately NOT rendered as "everything is new": a first look records a
    starting point, and calling thirty pre-existing commits "new since last
    cycle" would send the planner mining history it has no reason to think is
    fresh.
    """

    repo: str
    baseline: bool = False
    new_commits: tuple[str, ...] = ()          # subjects, newest first
    added_workflows: tuple[str, ...] = ()
    removed_workflows: tuple[str, ...] = ()
    readme_changed: bool = False
    previous_head: str = ""
    previous_fetched_at: str = ""

    @property
    def changed(self) -> bool:
        return bool(
            self.new_commits
            or self.added_workflows
            or self.removed_workflows
            or self.readme_changed
        )

    def summary(self) -> str:
        """One line - what `hsai observe` prints per project."""
        if self.baseline:
            return "baseline recorded (first observation)"
        if not self.changed:
            return "no change since the last observation"
        bits = [f"{len(self.new_commits)} new commit(s)"]
        if self.added_workflows:
            bits.append(f"+{len(self.added_workflows)} workflow(s)")
        if self.removed_workflows:
            bits.append(f"-{len(self.removed_workflows)} workflow(s)")
        if self.readme_changed:
            bits.append("README changed")
        return ", ".join(bits)

    def render(self, *, max_commits: int = DEFAULT_DELTA_COMMITS) -> str:
        if self.baseline:
            return (
                "_Baseline observation - this project had never been studied before, "
                "so nothing below is 'new since last cycle'._"
            )
        if not self.changed:
            since = self.previous_fetched_at or "the last observation"
            return f"_No new commits, no workflow changes, README unchanged since {since}._"
        lines: list[str] = []
        if self.new_commits:
            lines.append(f"New commit subjects since `{self.previous_head or '?'}`:")
            lines.extend(f"- {s}" for s in self.new_commits[:max_commits])
            if len(self.new_commits) > max_commits:
                lines.append(f"- _(+{len(self.new_commits) - max_commits} more)_")
        if self.added_workflows:
            lines.append("Workflows added: " + ", ".join(f"`{w}`" for w in self.added_workflows))
        if self.removed_workflows:
            lines.append(
                "Workflows removed: " + ", ".join(f"`{w}`" for w in self.removed_workflows)
            )
        lines.append(f"README changed: {'yes' if self.readme_changed else 'no'}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo": self.repo,
            "baseline": self.baseline,
            "new_commits": list(self.new_commits),
            "added_workflows": list(self.added_workflows),
            "removed_workflows": list(self.removed_workflows),
            "readme_changed": self.readme_changed,
            "previous_head": self.previous_head,
            "previous_fetched_at": self.previous_fetched_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DigestDelta:
        return cls(
            repo=str(data.get("repo", "")),
            baseline=bool(data.get("baseline", False)),
            new_commits=tuple(str(s) for s in data.get("new_commits", []) or []),
            added_workflows=tuple(str(s) for s in data.get("added_workflows", []) or []),
            removed_workflows=tuple(str(s) for s in data.get("removed_workflows", []) or []),
            readme_changed=bool(data.get("readme_changed", False)),
            previous_head=str(data.get("previous_head", "")),
            previous_fetched_at=str(data.get("previous_fetched_at", "")),
        )


@dataclass(frozen=True)
class Observation:
    """A stored digest plus the delta that produced it.

    The delta is persisted, not just computed: the dossiers are regenerated
    offline by ``hsai reindex``, which has no network and must still be able to
    say what changed last cycle.
    """

    digest: Digest
    delta: DigestDelta
    path: str = ""          # where this observation lives on disk ("" if unstored)
    refreshed: bool = False  # was it re-fetched (and rewritten) this run?

    @property
    def repo(self) -> str:
        return self.digest.repo or self.delta.repo


def diff_digest(old: Digest | None, new: Digest) -> DigestDelta:
    """What is new in ``new`` relative to ``old`` (pure - no I/O).

    Commits are matched by sha, walking the newest-first stream until a sha the
    previous observation already knew about. If none is found the whole window
    is new, which is the honest answer when a project moved further than the
    stored commit window in one cycle.
    """
    if old is None or old.empty:
        return DigestDelta(repo=new.repo, baseline=True)

    known = {sha for sha, _ in old.commits if sha}
    if old.head_sha:
        known.add(old.head_sha)
    new_commits: list[str] = []
    for sha, subject in new.commits:
        if sha and sha in known:
            break
        new_commits.append(subject)

    old_workflows, new_workflows = set(old.workflows), set(new.workflows)
    return DigestDelta(
        repo=new.repo,
        baseline=False,
        new_commits=tuple(new_commits),
        added_workflows=tuple(sorted(new_workflows - old_workflows)),
        removed_workflows=tuple(sorted(old_workflows - new_workflows)),
        readme_changed=new.readme_hash != old.readme_hash,
        previous_head=old.head_sha,
        previous_fetched_at=old.fetched_at,
    )


# --- the cache ----------------------------------------------------------------

def reference_dir(root: str | Path, cfg: CoreConfig | None = None) -> Path:
    """Where digests and dossiers live, resolved from core.yaml (created if absent)."""
    path = Path(root) / ObservatoryConfig.from_core(cfg).dir
    path.mkdir(parents=True, exist_ok=True)
    return path


def digest_filename(repo: str) -> str:
    """``owner/repo`` -> ``owner__repo.json`` - flat, one file per project."""
    owner, _, name = repo.partition("/")
    return f"{_safe(owner)}__{_safe(name or owner)}.json"


def dossier_name(repo: str) -> str:
    """``owner/repo`` -> ``repo`` - the Obsidian note name of its dossier."""
    _, _, name = repo.rpartition("/")
    return _safe(name or repo)


def digest_path(directory: str | Path, repo: str) -> Path:
    return Path(directory) / digest_filename(repo)


def read_observation(directory: str | Path, repo: str) -> Observation | None:
    """The last stored observation of ``repo`` (``None`` if never observed).

    A corrupt or hand-mangled cache file reads as "never observed" rather than
    raising: the next fetch re-records a baseline, which is strictly better
    than failing a whole synthesis cycle on one bad file.
    """
    path = digest_path(directory, repo)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    digest = Digest.from_dict(data.get("digest", {}) or {})
    delta = DigestDelta.from_dict(data.get("delta", {}) or {})
    return Observation(digest=digest, delta=delta, path=str(path))


def write_observation(directory: str | Path, observation: Observation) -> Path:
    """Store one observation as pretty-printed, key-sorted JSON."""
    path = digest_path(directory, observation.repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "digest": observation.digest.to_dict(),
        "delta": observation.delta.to_dict(),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


# --- fetching -----------------------------------------------------------------

def _gh(runner: Runner, args: list[str]) -> str:
    proc = runner(["gh", "api", *args])
    return proc.stdout if proc.ok else ""


def fetch_digest(
    repo: str,
    *,
    runner: Runner = run,
    ocfg: ObservatoryConfig | None = None,
    now: datetime | None = None,
) -> Digest:
    """Collect one digest via the GitHub API. Every field degrades to empty.

    Four read-only `gh api` calls. A failure in any of them narrows the digest
    rather than raising - a thin observation beats an aborted cycle - and a
    digest that came back entirely empty is never stored (see
    :attr:`Digest.empty`).
    """
    ocfg = ocfg or ObservatoryConfig()
    default_branch = _gh(runner, [f"repos/{repo}", "--jq", ".default_branch"]).strip()
    readme = _gh(
        runner, [f"repos/{repo}/readme", "-H", "Accept: application/vnd.github.raw"]
    )
    log = _gh(
        runner,
        [
            f"repos/{repo}/commits?per_page={ocfg.commits}",
            "--jq", r'.[] | [.sha, (.commit.message | split("\n")[0])] | @tsv',
        ],
    )
    workflows = _gh(
        runner, [f"repos/{repo}/contents/.github/workflows", "--jq", ".[].name"]
    )

    commits: list[tuple[str, str]] = []
    for line in log.splitlines():
        if not line.strip():
            continue
        sha, _, subject = line.partition("\t")
        commits.append((sha.strip(), (subject or sha).strip()[: ocfg.subject_chars]))
    commits = commits[: ocfg.commits]

    return Digest(
        repo=repo,
        fetched_at=(now or _utcnow()).isoformat(),
        default_branch=default_branch,
        head_sha=commits[0][0] if commits else "",
        readme_hash=hashlib.sha256(readme.encode()).hexdigest()[:16] if readme else "",
        readme_excerpt=readme[: ocfg.readme_chars],
        commits=tuple(commits),
        workflows=tuple(w.strip() for w in workflows.splitlines() if w.strip()),
    )


def is_stale(
    observation: Observation | None, *, stale_days: int, now: datetime | None = None
) -> bool:
    """Never observed, undated, or observed longer than ``stale_days`` ago."""
    if observation is None:
        return True
    when = _parse_iso(observation.digest.fetched_at)
    if when is None:
        return True
    return (now or _utcnow()) - when > timedelta(days=stale_days)


def observe_repo(
    cfg: CoreConfig | None,
    root: str | Path,
    repo: str,
    *,
    runner: Runner = run,
    now: datetime | None = None,
) -> Observation:
    """Fetch, diff against the cache, store, and return the observation.

    When the fetch comes back empty the previous observation is returned
    untouched, so a `gh` outage cannot erase what the loop already knows.
    """
    ocfg = ObservatoryConfig.from_core(cfg)
    directory = reference_dir(root, cfg)
    previous = read_observation(directory, repo)
    fresh = fetch_digest(repo, runner=runner, ocfg=ocfg, now=now)
    if fresh.empty:
        return previous or Observation(
            digest=Digest(repo=repo), delta=DigestDelta(repo=repo, baseline=True)
        )
    delta = diff_digest(previous.digest if previous else None, fresh)
    path = write_observation(directory, Observation(digest=fresh, delta=delta))
    return Observation(digest=fresh, delta=delta, path=str(path), refreshed=True)


def observe(
    cfg: CoreConfig | None,
    root: str | Path = ".",
    *,
    repos: Sequence[str] | None = None,
    runner: Runner = run,
    force: bool = False,
    now: datetime | None = None,
) -> list[Observation]:
    """Observe each reference project, refreshing stale ones (or all of them).

    ``force`` (``hsai observe --refresh``, and every synthesis cycle) re-fetches
    unconditionally; otherwise a project observed within ``stale_days`` is
    served from the cache and costs no API call.
    """
    ocfg = ObservatoryConfig.from_core(cfg)
    directory = reference_dir(root, cfg)
    targets = list(repos) if repos is not None else [
        r.repo for r in (cfg.reference_top10 if cfg else ())
    ]
    out: list[Observation] = []
    for repo in targets:
        cached = read_observation(directory, repo)
        if not force and not is_stale(cached, stale_days=ocfg.stale_days, now=now):
            out.append(cached)  # type: ignore[arg-type]  # not stale => not None
            continue
        out.append(observe_repo(cfg, root, repo, runner=runner, now=now))
    return out


# --- the adopted-practice index ------------------------------------------------

@dataclass(frozen=True)
class Citation:
    """One knowledge-base note that cites a reference project."""

    note_name: str
    title: str


def adopted_index(
    lessons: Iterable[LessonRecord], repos: Iterable[str]
) -> dict[str, tuple[Citation, ...]]:
    """``repo -> the lessons that cite it``, sorted by note name.

    Lessons carry their reference-set evidence in the "## References" section
    (and sometimes in the prose or tags), so the whole note is scanned for each
    configured project slug. This is what "already adopted from this project"
    means in the context pack: work this repo has landed and written up while
    citing that project.
    """
    slugs = {repo: repo.lower() for repo in repos}
    index: dict[str, list[Citation]] = {repo: [] for repo in slugs}
    for record in lessons:
        haystack = "\n".join(
            (record.title, record.body, " ".join(record.tags))
        ).lower()
        for repo, slug in slugs.items():
            if slug in haystack:
                index[repo].append(Citation(note_name=record.note_name, title=record.title))
    return {
        repo: tuple(sorted(items, key=lambda c: c.note_name))
        for repo, items in index.items()
    }


def render_adopted(repo: str, index: dict[str, tuple[Citation, ...]]) -> str:
    """The per-project "do NOT re-propose" block, as Obsidian wikilinks."""
    items = index.get(repo, ())
    if not items:
        return NOTHING_ADOPTED
    return "\n".join(f"- [[{c.note_name}]] - {c.title}" for c in items)


# --- rendering ------------------------------------------------------------------

def render_digest(digest: Digest, *, max_commits: int = DEFAULT_COMMITS) -> str:
    """The baseline study material: README excerpt, commit subjects, workflows."""
    parts: list[str] = []
    if digest.readme_excerpt:
        parts.append("README (truncated):\n" + digest.readme_excerpt)
    if digest.commits:
        subjects = "\n".join(f"- {s}" for s in digest.subjects()[:max_commits])
        parts.append("Recent commit subjects:\n" + subjects)
    if digest.workflows:
        parts.append("CI workflows:\n" + "\n".join(f"- {w}" for w in digest.workflows))
    return "\n\n".join(parts) or NO_DATA


def render_section(
    observation: Observation,
    adopted: dict[str, tuple[Citation, ...]],
    *,
    ocfg: ObservatoryConfig | None = None,
) -> str:
    """One project's slice of the synthesis context pack.

    Delta first, on purpose: what changed in the field since the last cycle is
    the freshest signal the planner has, and burying it under a README makes it
    invisible.
    """
    ocfg = ocfg or ObservatoryConfig()
    observed = observation.digest.fetched_at or "never"
    return (
        f"**{DELTA_HEADING}** (last observed: {observed})\n"
        f"{observation.delta.render(max_commits=ocfg.delta_commits)}\n\n"
        f"**{DIGEST_HEADING}**\n"
        f"{render_digest(observation.digest, max_commits=ocfg.commits)}\n\n"
        f"**{ADOPTED_FROM_PROJECT_HEADING}**\n"
        f"{render_adopted(observation.repo, adopted)}"
    )


# --- staleness (surfaced in DIRECTION.md) ---------------------------------------

def stale_repos(
    cfg: CoreConfig | None, root: str | Path = ".", *, now: datetime | None = None
) -> list[str]:
    """Reference projects not observed within ``stale_days`` (never-observed included)."""
    ocfg = ObservatoryConfig.from_core(cfg)
    directory = reference_dir(root, cfg)
    repos = [r.repo for r in (cfg.reference_top10 if cfg else ())]
    return [
        repo for repo in repos
        if is_stale(read_observation(directory, repo), stale_days=ocfg.stale_days, now=now)
    ]


def staleness_line(
    cfg: CoreConfig | None, root: str | Path = ".", *, now: datetime | None = None
) -> str:
    """The DIRECTION.md "Now" line: how much of the reference set has gone dark."""
    ocfg = ObservatoryConfig.from_core(cfg)
    repos = [r.repo for r in (cfg.reference_top10 if cfg else ())]
    if not repos:
        return "Reference set: none pinned in core.yaml."
    stale = stale_repos(cfg, root, now=now)
    if not stale:
        return (
            f"Reference set: all {len(repos)} project(s) observed within the last "
            f"{ocfg.stale_days} day(s) ([[Reference Set MOC]])."
        )
    return (
        f"Reference set: **{len(stale)}/{len(repos)}** project(s) not observed in the last "
        f"{ocfg.stale_days} day(s) - run `hsai observe --refresh` ([[Reference Set MOC]])."
    )
