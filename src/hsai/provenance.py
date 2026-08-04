"""Where a change actually came from - resolved, never fabricated.

G1 says every improvement traces back to something observed in the field; G2
says every PR is auditable. Both were being asserted rather than checked: the
loop stamped ``cfg.reference_top10[:3]`` (langchain, MetaGPT, crewAI) onto
every PR body and every lesson, no matter what was learned - including on
tickets whose synthesis rationale named entirely different projects.

This module replaces that with a resolved citation:

* :func:`parse_rationale` reads the ticket's ``## Synthesis rationale`` section
  and matches it against the pinned reference set (plus the watchlist);
* when a ticket carries no parseable rationale the answer is the explicit
  :data:`UNATTRIBUTED` marker - an honest "we do not know", never an invented
  list;
* :func:`check` validates the ``## Practice adopted`` block the worker is asked
  to emit (source repo + artifact kind + one-line claim), so an adoption cannot
  cite a project that is not in the pinned set.

The record it produces is deliberately shaped like langchain's metadata-carrying
document: not a bare repo string, but (source, artifact kind, claim).
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from .tickets import is_exempt_kind

# The artifact kinds core.yaml's `reference_set.learn_from` enumerates: what
# was actually studied, not just which repo was looked at.
ARTIFACT_KINDS = ("source_code", "commit_history", "ci_cd", "issue_history", "readme")

#: Rendered in place of a citation when nothing could be resolved.
UNATTRIBUTED = "_(unattributed)_"

PRACTICE_HEADING = "## Practice adopted"

_RATIONALE_RE = re.compile(
    r"^#{2,3}[ \t]*synthesis rationale[ \t]*$\n(.*?)(?=^#{2,3}[ \t]|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_PRACTICE_RE = re.compile(
    r"^#{2,3}[ \t]*practice adopted[ \t]*$\n(.*?)(?=^#{2,3}[ \t]|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_FIELD_RE = re.compile(
    r"^[ \t]*[-*]?[ \t]*(repos?|artifacts?(?:[ _]kind)?|practice|claim)[ \t]*:[ \t]*(.+?)[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Bare repo names too generic to count as a citation on their own - "swarm"
# and "core" appear in half the sentences in this repo. Such a project must be
# named by its full ``owner/name`` slug to be credited.
_GENERIC_NAMES = frozenset({"swarm", "core", "agent", "agents", "ai", "index"})


def slugify(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-")


@dataclass(frozen=True)
class Provenance:
    """One adopted practice and the field evidence behind it."""

    repos: tuple[str, ...] = ()
    practice: str = ""  # slug, unique per repo in the registry
    claim: str = ""  # one line: what was taken
    artifact_kind: str = ""  # source_code | commit_history | ci_cd | issue_history | readme

    def is_empty(self) -> bool:
        return not self.repos

    def render(self) -> str:
        """The markdown block committed to the lesson (and parsed back by the registry)."""
        if self.is_empty():
            return UNATTRIBUTED
        repos = ", ".join(f"`{r}`" for r in self.repos)
        return (
            f"- repos: {repos}\n"
            f"- artifact: {self.artifact_kind or 'unspecified'}\n"
            f"- practice: {self.practice or 'unnamed'}\n"
            f"- claim: {self.claim or '_(no claim recorded)_'}"
        )


def last_section(pattern: re.Pattern[str], body: str) -> str:
    """Body of the LAST section ``pattern`` matches, or ``""``.

    Last, not first: a lesson quotes a redacted tail of the agent run before it
    states its own conclusions, so an earlier match can be a truncated echo of
    the block rather than the committed one.
    """
    matches = list(pattern.finditer(body or ""))
    return matches[-1].group(1).strip() if matches else ""


def _first_position(lowered: str, repo: str) -> int | None:
    """Where ``repo`` is first cited in ``lowered`` (full slug or bare name)."""
    candidates = [repo.lower()]
    name = repo.split("/")[-1].lower()
    if name not in _GENERIC_NAMES:
        candidates.append(name)
    positions = [
        m.start()
        for c in candidates
        for m in re.finditer(rf"(?<![\w/-]){re.escape(c)}(?![\w-])", lowered)
    ]
    return min(positions) if positions else None


def match_repos(text: str, known_repos: Iterable[str]) -> tuple[str, ...]:
    """Every known repo named in ``text``, ordered by where it is first cited."""
    lowered = (text or "").lower()
    hits = []
    for repo in known_repos:
        pos = _first_position(lowered, repo)
        if pos is not None:
            hits.append((pos, repo))
    return tuple(repo for _, repo in sorted(hits))


def parse_rationale(body: str, known_repos: Iterable[str]) -> tuple[str, ...]:
    """Reference repos cited in a ticket's ``## Synthesis rationale`` section.

    Returns ``()`` when the ticket has no such section or names nothing from
    the pinned set - the caller must then fall back to :data:`UNATTRIBUTED`
    rather than inventing a citation.
    """
    section = last_section(_RATIONALE_RE, body)
    if not section:
        return ()
    return match_repos(section, known_repos)


def parse_practice(text: str) -> Provenance | None:
    """Parse a ``## Practice adopted`` block out of agent output or a lesson."""
    section = last_section(_PRACTICE_RE, text)
    if not section:
        return None
    fields: dict[str, str] = {}
    for key, value in _FIELD_RE.findall(section):
        name = key.lower()
        if name.startswith("repo"):
            name = "repo"
        elif name.startswith("artifact"):
            name = "artifact"
        fields.setdefault(name, value.strip())
    repos = tuple(
        part.strip().strip("`").strip()
        for part in re.split(r"[,;]", fields.get("repo", ""))
        if part.strip().strip("`").strip()
    )
    return Provenance(
        repos=repos,
        practice=slugify(fields.get("practice", "")),
        claim=fields.get("claim", ""),
        artifact_kind=fields.get("artifact", "").strip().lower(),
    )


@dataclass
class ProvenanceCheck:
    """Verdict on a worker's adoption claim."""

    ok: bool
    reason: str
    exempt: bool = False
    provenance: Provenance | None = None

    def note(self) -> str:
        verdict = "exempt" if self.exempt else ("verified" if self.ok else "UNVERIFIED")
        return f"provenance {verdict}: {self.reason}"


def check(
    *, ticket_title: str, text: str, known_repos: Iterable[str]
) -> ProvenanceCheck:
    """Validate the adoption claim a worker made for ``ticket_title``.

    ``docs:``/``chore:``/heal tickets are exempt - they reuse the same
    exemption shape as the well-formedness gate (:func:`hsai.tickets.is_exempt_kind`)
    so routine upkeep is never blocked on citing a reference project.
    """
    known = tuple(known_repos)
    if is_exempt_kind(ticket_title):
        return ProvenanceCheck(
            ok=True, reason="exempt kind (docs/chore/heal)", exempt=True,
            provenance=parse_practice(text),
        )

    prov = parse_practice(text)
    if prov is None:
        return ProvenanceCheck(
            ok=False,
            reason=f"no '{PRACTICE_HEADING}' block: the change cites no field evidence",
        )
    if prov.is_empty():
        return ProvenanceCheck(
            ok=False, reason="'Practice adopted' block names no source repo",
            provenance=prov,
        )
    unknown = [r for r in prov.repos if r not in known]
    if unknown:
        return ProvenanceCheck(
            ok=False,
            reason=(
                f"cites {', '.join(unknown)}, which is not in the pinned reference set"
            ),
            provenance=prov,
        )
    if prov.artifact_kind not in ARTIFACT_KINDS:
        return ProvenanceCheck(
            ok=False,
            reason=(
                f"artifact kind {prov.artifact_kind or '(missing)'!r} is not one of "
                + ", ".join(ARTIFACT_KINDS)
            ),
            provenance=prov,
        )
    return ProvenanceCheck(
        ok=True,
        reason=f"{prov.practice or 'practice'} adopted from {', '.join(prov.repos)}"
               f" ({prov.artifact_kind})",
        provenance=prov,
    )


def prompt_contract(known_repos: Iterable[str]) -> str:
    """The block every non-exempt worker must emit, spelled out for the prompt."""
    return (
        f"\n\nEnd your final message with a `{PRACTICE_HEADING}` block that names the "
        "field evidence behind this change, in exactly this shape:\n\n"
        f"{PRACTICE_HEADING}\n"
        "- repos: `owner/name` (one or more, comma separated)\n"
        f"- artifact: one of {', '.join(ARTIFACT_KINDS)}\n"
        "- practice: short-kebab-case-slug\n"
        "- claim: one line naming what you took from that project.\n\n"
        "Cite only projects from the pinned reference set ("
        + ", ".join(known_repos)
        + "). Never invent a citation: if the ticket's rationale names projects, "
        "cite those."
    )
