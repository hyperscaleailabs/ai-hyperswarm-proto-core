"""The knowledge base: lessons, whitepapers, and Maps of Content (MOCs).

Everything written here is Obsidian-ready:
- YAML frontmatter with tags,
- ``[[wikilinks]]`` between notes and up to their MOCs,
so that cloning the repo and opening it as a vault yields a connected graph.

A lesson has two distinct halves (see the module's synthesis ticket, "evidence-
backed, model-authored lessons"):

- :class:`LessonEvidence` - deterministic, machine-collected facts (files
  touched, tier/model/wall-clock/tokens, a trimmed CI failure excerpt, a
  provenance stamp). Never model-written, so it cannot be boilerplate.
- :class:`LessonInterpretation` - the model's authored reading of that
  evidence (:func:`author_lesson_interpretation`), gated against empty,
  CI-restating, or near-duplicate answers (:func:`_boilerplate_reason`) and
  falling back to a fixed template - tagged so the shortfall is countable -
  rather than ever blocking the loop.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from . import practices as practices_mod
from .ai import run_agent
from .config import CoreConfig
from .models import ModelChoice
from .proc import Runner, run

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_TAG_RE = re.compile(r"^\s*-\s+(\S.*)$", re.MULTILINE)
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_TITLE_RE = re.compile(r"^# (.+)$", re.MULTILINE)
_SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)
_SUBSECTION_RE = re.compile(r"^### (.+)$", re.MULTILINE)
_WORD_RE = re.compile(r"[a-zA-Z][a-zA-Z-]{3,}")
_CI_EXCERPT_RE = re.compile(r"\*\*CI failure excerpt\*\*\n```\n(.*?)\n```", re.DOTALL)

# The lesson-authoring pass's strict-JSON reply, mirroring hsai.review/hsai.audit.
_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

# Recognisable opening line for the lesson-authoring prompt - lets a fake AI
# runner in tests (and a real transcript) tell this call apart from the
# worker/review/audit ones.
LESSON_AUTHOR_PROMPT_MARKER = "You are AUTHORING A LESSON"

# Applied to a note whose interpretation was rejected twice (or never
# attempted) and fell back to the fixed template - a countable signal, not a
# silent one (see the block review brief's boilerplate-rate line).
TEMPLATE_FALLBACK_TAG = "lesson/template-fallback"

DEFAULT_LESSON_AUTHOR_TIER = "light"
DEFAULT_LESSON_AUTHOR_TIMEOUT = 300.0
DEFAULT_SIMILARITY_THRESHOLD = 0.82
DEFAULT_DEDUPE_WINDOW = 20

_PASS_FALLBACK_TEXT = "Change merged cleanly under a green build."
_FAIL_FALLBACK_TEXT = (
    "Change did not reach green; auto-merge will hold until CI passes. "
    "Investigate the failure captured above before the next attempt."
)
_NOT_RECORDED = "_(not recorded)_"
_CI_RESTATING_PHRASES = (
    "change merged cleanly under a green build",
    "change did not reach green",
    "auto-merge will hold until ci passes",
)


def slugify(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-") or "untitled"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _int(raw: str, default: int = 0) -> int:
    raw = (raw or "").strip()
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass
class LessonEvidence:
    """Deterministic, machine-collected facts about one iteration.

    Never model-written - populated by the orchestrator from the same
    :class:`hsai.ledger.LedgerRecord` inputs the iteration already builds, so
    a lesson's model/tier/wall-clock/tokens are always consistent with that
    iteration's ledger entry.
    """

    files_changed: tuple[str, ...] = ()  # "path (+ins/-del)", one per file
    insertions: int = 0
    deletions: int = 0
    tier: str = ""
    model: str = ""
    wall_clock_seconds: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    ci_failure_excerpt: str = ""  # trimmed; set only when outcome == "fail"
    reverted_workflows: tuple[str, ...] = ()
    hsai_sha: str = ""  # the hsai harness commit this iteration ran under
    core_yaml_hash: str = ""  # short hash of .ai-swarm/core.yaml at that commit

    def render(self) -> str:
        files = "\n".join(f"- `{f}`" for f in self.files_changed) or "- _(no files reported)_"
        tokens = (
            f"{self.input_tokens if self.input_tokens is not None else '-'} in / "
            f"{self.output_tokens if self.output_tokens is not None else '-'} out"
        )
        reverted = ", ".join(self.reverted_workflows) if self.reverted_workflows else "_(none)_"
        table = (
            "| field | value |\n| --- | --- |\n"
            f"| model | `{self.model or '-'}` |\n"
            f"| tier | `{self.tier or '-'}` |\n"
            f"| wall-clock | {self.wall_clock_seconds:.1f}s |\n"
            f"| tokens | {tokens} |\n"
            f"| insertions / deletions | +{self.insertions}/-{self.deletions} |\n"
            f"| reverted workflow edits | {reverted} |\n"
            f"| provenance | hsai@`{self.hsai_sha or '-'}` "
            f"core.yaml@`{self.core_yaml_hash or '-'}` |"
        )
        ci = (
            f"\n\n**CI failure excerpt**\n```\n{self.ci_failure_excerpt}\n```"
            if self.ci_failure_excerpt
            else ""
        )
        return f"{table}\n\n**Files changed**\n{files}{ci}"


@dataclass
class LessonInterpretation:
    """The model's authored reading of one iteration's :class:`LessonEvidence`.

    ``template_fallback`` is set when authoring never produced an acceptable
    answer (disabled, erroring, timing out, or twice rejected by the
    anti-boilerplate gate) and the fixed template was used instead - see
    :func:`author_lesson_interpretation`.
    """

    what_was_tried: str = ""
    what_surprised: str = ""
    what_to_do_differently: str = ""
    reference_citation: str = "none"  # a specific reference-repo artifact, or "none"
    template_fallback: bool = False

    def render(self) -> str:
        return (
            f"### What was tried\n{self.what_was_tried or _NOT_RECORDED}\n\n"
            f"### What surprised us\n{self.what_surprised or _NOT_RECORDED}\n\n"
            f"### What to do differently\n{self.what_to_do_differently or _NOT_RECORDED}\n\n"
            f"### Reference citation\n`{self.reference_citation or 'none'}`"
        )


@dataclass
class Lesson:
    title: str
    outcome: str  # "pass" | "fail"
    kind: str  # heal | implement | improve
    context: str
    what_happened: str
    iteration: int = 0
    block: int = 0
    ticket: int | None = None
    pr: int | None = None
    model: str = ""
    references: tuple[str, ...] = ()  # reference-set repos that informed the work
    tags: tuple[str, ...] = ()
    created: str = field(default_factory=_today)
    remote_ci: str = ""  # SUCCESS | FAILURE | TIMEOUT, filled in once gh checks conclude
    repro_evidence: str = ""  # heal/bugfix only: failing-then-passing reproduction proof
    recalled: tuple[str, ...] = ()  # prior notes injected into this run's prompt
    review_verdict: str = ""  # the independent reviewer's verdict, verbatim
    # The pre-PR acceptance audit's verdict, verbatim (see hsai.audit). Empty
    # when the gate did not run, and then the section is omitted entirely, so a
    # lesson written with `audit.enabled: false` is byte-for-byte what it was
    # before the gate existed.
    audit_verdict: str = ""
    execution_trace: str = ""  # turns/tools/tokens/exit/duration - the committed digest
    # A member of hsai.postmortem.FAILURE_CLASSES, set only when outcome=="fail"
    # (empty for a pass) - mirrored into frontmatter as a `failure/<class>` tag
    # so the Obsidian graph can filter failures by cause.
    failure_class: str = ""
    evidence: LessonEvidence = field(default_factory=LessonEvidence)
    interpretation: LessonInterpretation = field(default_factory=LessonInterpretation)

    def note_name(self) -> str:
        return f"{self.created}-{slugify(self.title)}"


@dataclass
class LessonRecord:
    """A lesson as parsed back off disk - the read-side counterpart of `Lesson`."""

    note_name: str
    title: str
    outcome: str
    kind: str
    tags: tuple[str, ...]
    lesson_text: str
    what_happened: str = ""
    body: str = ""  # everything after the frontmatter; what the recall index reads
    failure_class: str = ""  # "" when absent (pass, or a note predating this field)
    created: str = ""  # frontmatter `created:`, "" for notes that carry no date
    block: int = 0
    evidence: LessonEvidence = field(default_factory=LessonEvidence)
    interpretation: LessonInterpretation = field(default_factory=LessonInterpretation)

    @property
    def template_fallback(self) -> bool:
        return TEMPLATE_FALLBACK_TAG in self.tags


def split_sections(text: str) -> dict[str, str]:
    """Map lowercased ``## headings`` to their bodies."""
    parts = _SECTION_RE.split(text)
    # parts[0] is the preamble; the rest alternates heading, body, heading, body...
    sections: dict[str, str] = {}
    for i in range(1, len(parts), 2):
        heading = parts[i].strip().lower()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        sections[heading] = body.strip()
    return sections


def _split_subsections(text: str) -> dict[str, str]:
    """Map lowercased ``### headings`` to their bodies (nested inside a ``##``)."""
    parts = _SUBSECTION_RE.split(text)
    subs: dict[str, str] = {}
    for i in range(1, len(parts), 2):
        heading = parts[i].strip().lower()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        subs[heading] = body.strip()
    return subs


def _frontmatter_list(fm: str, key: str) -> tuple[str, ...]:
    """List items under a top-level ``key:`` list in frontmatter.

    Generic counterpart of the old ``tags``-only scan: frontmatter now holds
    several list keys (``tags``, ``recalled``, ``evidence_files_changed``, ...),
    so scanning every ``- item`` line regardless of which key it sits under
    would file one key's items as another's.
    """
    items: list[str] = []
    in_key = False
    for line in fm.splitlines():
        if line.strip() and not line.startswith((" ", "\t", "-")):
            in_key = line.strip() == f"{key}:"
            continue
        match = _TAG_RE.match(line)
        if in_key and match:
            items.append(match.group(1).strip())
    return tuple(items)


def _frontmatter_tags(fm: str) -> tuple[str, ...]:
    return _frontmatter_list(fm, "tags")


def _frontmatter_scalar(fm: str, key: str) -> str:
    """The value of a top-level scalar frontmatter key ("" when absent).

    Deliberately not a YAML parse: frontmatter here is machine-written by
    :meth:`KnowledgeBase._frontmatter`, and a one-line reader cannot fail on a
    hand-edited note the way a strict parser would.
    """
    prefix = f"{key}:"
    for line in fm.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):].strip()
    return ""


def _parse_ci_excerpt(evidence_section: str) -> str:
    m = _CI_EXCERPT_RE.search(evidence_section)
    return m.group(1) if m else ""


def _parse_evidence(fm: str, evidence_section: str) -> LessonEvidence:
    input_raw = _frontmatter_scalar(fm, "evidence_input_tokens")
    output_raw = _frontmatter_scalar(fm, "evidence_output_tokens")
    wall_raw = _frontmatter_scalar(fm, "evidence_wall_clock_seconds")
    try:
        wall_clock = float(wall_raw) if wall_raw else 0.0
    except ValueError:
        wall_clock = 0.0
    return LessonEvidence(
        files_changed=_frontmatter_list(fm, "evidence_files_changed"),
        insertions=_int(_frontmatter_scalar(fm, "evidence_insertions")),
        deletions=_int(_frontmatter_scalar(fm, "evidence_deletions")),
        tier=_frontmatter_scalar(fm, "evidence_tier"),
        model=_frontmatter_scalar(fm, "evidence_model"),
        wall_clock_seconds=wall_clock,
        input_tokens=_int(input_raw) if input_raw.strip() else None,
        output_tokens=_int(output_raw) if output_raw.strip() else None,
        reverted_workflows=_frontmatter_list(fm, "evidence_reverted_workflows"),
        hsai_sha=_frontmatter_scalar(fm, "evidence_hsai_sha"),
        core_yaml_hash=_frontmatter_scalar(fm, "evidence_core_yaml_hash"),
        ci_failure_excerpt=_parse_ci_excerpt(evidence_section),
    )


def _parse_interpretation(section_text: str) -> LessonInterpretation:
    subs = _split_subsections(section_text)

    def _val(key: str) -> str:
        v = subs.get(key, "").strip()
        return "" if v == _NOT_RECORDED else v

    ref = subs.get("reference citation", "").strip().strip("`") or "none"
    return LessonInterpretation(
        what_was_tried=_val("what was tried"),
        what_surprised=_val("what surprised us"),
        what_to_do_differently=_val("what to do differently"),
        reference_citation=ref,
    )


def parse_note(path: str | Path) -> LessonRecord:
    """Parse any Obsidian note in the vault into a :class:`LessonRecord`.

    Lessons carry ``outcome/*`` and ``kind/*`` frontmatter tags; whitepapers and
    ADRs do not, and come back as ``unknown``. This is the single place those
    tags are interpreted - both :meth:`KnowledgeBase.read_lessons` and the
    :mod:`hsai.recall` index read notes through it.
    """
    path = Path(path)
    text = path.read_text()
    fm_match = _FRONTMATTER_RE.match(text)
    fm = fm_match.group(1) if fm_match else ""
    body = text[fm_match.end():] if fm_match else text
    tags = _frontmatter_tags(fm)
    outcome = next((t.split("/", 1)[1] for t in tags if t.startswith("outcome/")), "unknown")
    kind = next((t.split("/", 1)[1] for t in tags if t.startswith("kind/")), "unknown")
    failure_class = next((t.split("/", 1)[1] for t in tags if t.startswith("failure/")), "")
    title_match = _TITLE_RE.search(text)
    title = title_match.group(1).strip() if title_match else path.stem
    sections = split_sections(text)
    return LessonRecord(
        note_name=path.stem,
        title=title,
        outcome=outcome,
        kind=kind,
        tags=tags,
        lesson_text=sections.get("lesson learned", ""),
        what_happened=sections.get("what happened", ""),
        body=body.strip(),
        failure_class=failure_class,
        created=_frontmatter_scalar(fm, "created"),
        block=_int(_frontmatter_scalar(fm, "block")),
        evidence=_parse_evidence(fm, sections.get("evidence", "")),
        interpretation=_parse_interpretation(sections.get("lesson learned", "")),
    )


# --- lesson authoring: evidence in, an interpretation the model cannot fake out --

def build_lesson_author_prompt(
    *, outcome: str, kind: str, ticket_title: str, ticket_body: str, evidence: LessonEvidence,
) -> str:
    """The authoring instruction: evidence-first, JSON-terminated (see hsai.review
    for the sibling pattern this mirrors)."""
    files = "\n".join(f"- `{f}`" for f in evidence.files_changed) or "- _(no files reported)_"
    ci = (
        f"\n\nCI failure excerpt:\n```\n{evidence.ci_failure_excerpt}\n```"
        if evidence.ci_failure_excerpt
        else ""
    )
    reverted = (
        f"\n\nReverted off-spec workflow edits: {', '.join(evidence.reverted_workflows)}"
        if evidence.reverted_workflows
        else ""
    )
    return f"""{LESSON_AUTHOR_PROMPT_MARKER} for ai-hyperswarm-proto-core, an autonomous
self-improving AI-swarm harness. Below is the deterministic EVIDENCE for one
iteration, collected by the harness - not by you. You did not run this
iteration; interpret the evidence below, do not invent facts it does not
contain.

Outcome: {outcome.upper()}
Kind: {kind}
Ticket: {ticket_title}

{ticket_body[:2000]}

Model: `{evidence.model}` (tier: `{evidence.tier}`)
Wall-clock: {evidence.wall_clock_seconds:.1f}s
Insertions/deletions: +{evidence.insertions}/-{evidence.deletions}

Files changed:
{files}{ci}{reverted}

Write four fields, each 1-3 sentences, SPECIFIC to the evidence above:
- what_was_tried: what the change actually did, concretely.
- what_surprised: what was unexpected given the evidence - a real surprise,
  not a restatement of the CI outcome. If genuinely nothing was surprising,
  say what confirmed an existing expectation instead.
- what_to_do_differently: one concrete, actionable change for the NEXT similar
  ticket. Never the sentence "change merged cleanly" or "change did not reach
  green" verbatim - say what to actually do differently.
- reference_citation: a SPECIFIC artifact in a SPECIFIC reference-set repo
  that informed this work (e.g. "assafelovic/gpt-researcher: costs.py cost
  accounting"), or the exact string "none" if nothing from the reference set
  was actually consulted. Never invent a citation.

Answer with prose if you like, but END your reply with a fenced ```json block
containing exactly this object:
{{"what_was_tried": "...", "what_surprised": "...",
  "what_to_do_differently": "...", "reference_citation": "..."}}
"""


def parse_lesson_interpretation(output: str) -> LessonInterpretation | None:
    """Extract the authored interpretation from a model reply, or ``None``.

    Unlike :func:`hsai.review.parse_verdict` this is NOT fail-closed: a
    lesson-authoring failure must never block the loop, so the caller treats
    ``None`` as "retry, then fall back to the template" rather than as a
    negative verdict of its own.
    """
    text = (output or "").strip()
    blocks = _JSON_BLOCK.findall(text)
    if not blocks and text.startswith("{") and text.endswith("}"):
        blocks = [text]
    if not blocks:
        return None
    try:
        raw = json.loads(blocks[-1])
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None
    return LessonInterpretation(
        what_was_tried=str(raw.get("what_was_tried", "")).strip(),
        what_surprised=str(raw.get("what_surprised", "")).strip(),
        what_to_do_differently=str(raw.get("what_to_do_differently", "")).strip(),
        reference_citation=str(raw.get("reference_citation", "")).strip() or "none",
    )


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _similarity(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, _normalize(a), _normalize(b)).ratio()


def _combined_text(interp: LessonInterpretation) -> str:
    return " ".join(
        [interp.what_was_tried, interp.what_surprised, interp.what_to_do_differently]
    ).strip()


def _boilerplate_reason(
    interp: LessonInterpretation, prior_texts: Sequence[str], *, threshold: float,
) -> str:
    """Non-empty rejection reason, or "" when the interpretation passes the gate.

    Three anti-boilerplate checks, in order: empty, CI-restating, near-duplicate
    of a recent lesson (normalized similarity - see proposal item 3).
    """
    combined = _combined_text(interp)
    if not combined or not interp.what_to_do_differently.strip():
        return "empty interpretation"
    norm = _normalize(combined)
    if any(phrase in norm for phrase in _CI_RESTATING_PHRASES):
        return "interpretation merely restates the CI outcome"
    for prior in prior_texts:
        if prior and _similarity(combined, prior) >= threshold:
            return f"near-duplicate of a recent lesson (similarity >= {threshold:g})"
    return ""


def _template_interpretation(outcome: str) -> LessonInterpretation:
    return LessonInterpretation(
        what_was_tried="",
        what_surprised="",
        what_to_do_differently=_PASS_FALLBACK_TEXT if outcome == "pass" else _FAIL_FALLBACK_TEXT,
        reference_citation="none",
        template_fallback=True,
    )


def _lesson_author_choice(cfg: CoreConfig, lesson_cfg: dict) -> ModelChoice | None:
    """The tier/model to author with, or ``None`` when none is usable.

    Deliberately non-heavy (proposal item 2): a configured ``heavy`` tier (or
    one core.yaml does not define) is discarded in favour of the light tier -
    lesson authoring is not the kind of work that should compete with real
    tickets for the heavy-tier budget.
    """
    tier = str(lesson_cfg.get("tier", DEFAULT_LESSON_AUTHOR_TIER))
    if tier == "heavy" or tier not in cfg.tiers:
        tier = "light" if "light" in cfg.tiers else cfg.default_tier
    if tier not in cfg.tiers:
        return None
    return ModelChoice(
        tier=tier, model=cfg.tiers[tier].model,
        rationale="lesson authoring: interpret this iteration's evidence",
        strategy="lesson-author-v1",
    )


def author_lesson_interpretation(
    cfg: CoreConfig,
    *,
    outcome: str,
    kind: str,
    ticket_title: str,
    ticket_body: str,
    evidence: LessonEvidence,
    prior_lesson_texts: Sequence[str] = (),
    cwd: str | None = None,
    ai_runner: Runner = run,
) -> LessonInterpretation:
    """Ask the model to interpret ``evidence``; anti-boilerplate-gated, retried
    once, then falls back to the fixed template (proposal items 2-4).

    Runs for PASS and FAIL alike - the failure case is where the durable
    knowledge lives. Never blocks the loop: disabled config, no usable tier,
    an erroring/timing-out run, an unparseable reply, or two boilerplate
    verdicts in a row all fall back to :func:`_template_interpretation`
    without raising.
    """
    lesson_cfg = cfg.knowledge.get("lesson_authoring", {}) if isinstance(cfg.knowledge, dict) else {}
    if not lesson_cfg.get("enabled", True):
        return _template_interpretation(outcome)

    choice = _lesson_author_choice(cfg, lesson_cfg)
    if choice is None:
        return _template_interpretation(outcome)

    timeout = float(lesson_cfg.get("timeout_seconds", DEFAULT_LESSON_AUTHOR_TIMEOUT))
    threshold = float(lesson_cfg.get("similarity_threshold", DEFAULT_SIMILARITY_THRESHOLD))
    prompt = build_lesson_author_prompt(
        outcome=outcome, kind=kind, ticket_title=ticket_title, ticket_body=ticket_body,
        evidence=evidence,
    )

    for _attempt in range(2):  # one retry, per proposal item 3
        try:
            ares = run_agent(prompt, choice, cfg, cwd=cwd, runner=ai_runner, timeout=timeout)
        except Exception:
            return _template_interpretation(outcome)
        if not ares.ok:
            continue
        interp = parse_lesson_interpretation(ares.text)
        if interp is None:
            continue
        if not _boilerplate_reason(interp, prior_lesson_texts, threshold=threshold):
            return interp
    return _template_interpretation(outcome)


@dataclass
class Whitepaper:
    title: str
    summary: str
    body: str
    covers_lessons: tuple[str, ...] = ()  # note names
    tags: tuple[str, ...] = ()
    created: str = field(default_factory=_today)

    def note_name(self) -> str:
        return f"{self.created}-{slugify(self.title)}"


class KnowledgeBase:
    """Filesystem-backed knowledge base rooted at the repo."""

    def __init__(
        self,
        root: str | Path,
        *,
        lessons_dir: str = "knowledge/lessons",
        whitepapers_dir: str = "knowledge/whitepapers",
        mocs_dir: str = "knowledge/MOCs",
        practices_dir: str = practices_mod.PRACTICES_DIR_DEFAULT,
        whitepaper_every: int = 10,
    ) -> None:
        self.root = Path(root)
        self.lessons_dir = self.root / lessons_dir
        self.whitepapers_dir = self.root / whitepapers_dir
        self.mocs_dir = self.root / mocs_dir
        self.practices_dir = self.root / practices_dir
        self.whitepaper_every = whitepaper_every
        for d in (self.lessons_dir, self.whitepapers_dir, self.mocs_dir, self.practices_dir):
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_config(cls, cfg: CoreConfig, root: str | Path) -> KnowledgeBase:
        k = cfg.knowledge or {}
        return cls(
            root,
            lessons_dir=k.get("lessons_dir", "knowledge/lessons"),
            whitepapers_dir=k.get("whitepapers_dir", "knowledge/whitepapers"),
            mocs_dir=k.get("mocs_dir", "knowledge/MOCs"),
            practices_dir=k.get("practices_dir", practices_mod.PRACTICES_DIR_DEFAULT),
            whitepaper_every=int(k.get("whitepaper_every_lessons", 10)),
        )

    # --- writing --------------------------------------------------------------
    def write_lesson(self, lesson: Lesson) -> Path:
        path = self.lessons_dir / f"{lesson.note_name()}.md"
        path.write_text(self._render_lesson(lesson))
        return path

    def write_whitepaper(self, paper: Whitepaper) -> Path:
        path = self.whitepapers_dir / f"{paper.note_name()}.md"
        path.write_text(self._render_whitepaper(paper))
        return path

    # --- counting -------------------------------------------------------------
    def lesson_notes(self) -> list[str]:
        return sorted(p.stem for p in self.lessons_dir.glob("*.md"))

    def whitepaper_notes(self) -> list[str]:
        return sorted(p.stem for p in self.whitepapers_dir.glob("*.md"))

    def practice_notes(self) -> list[str]:
        return sorted(p.stem for p in self.practices_dir.glob("*.md"))

    def read_practices(self) -> list[practices_mod.Practice]:
        """Parse every practice note on disk, sorted by id (see :func:`hsai.practices.load`)."""
        return [
            practices_mod.parse(self.practices_dir / f"{name}.md")
            for name in self.practice_notes()
        ]

    def should_write_whitepaper(self) -> bool:
        n = len(self.lesson_notes())
        return n > 0 and n % self.whitepaper_every == 0

    # --- reading ----------------------------------------------------------------
    def read_lessons(self) -> list[LessonRecord]:
        """Parse every lesson note on disk back into structured records, oldest first."""
        return [self._parse_lesson(name) for name in self.lesson_notes()]

    def _parse_lesson(self, note_name: str) -> LessonRecord:
        return parse_note(self.lessons_dir / f"{note_name}.md")

    def recent_lesson_interpretation_texts(self, n: int | None = None) -> list[str]:
        """The combined interpretation text of the last ``n`` lessons on disk.

        What the anti-boilerplate gate's near-duplicate check compares a
        freshly authored interpretation against (proposal item 3). Excludes
        template-fallback notes - a duplicate template is not informative
        evidence that a NEW interpretation is also boilerplate.
        """
        window = DEFAULT_DEDUPE_WINDOW if n is None else n
        records = self.read_lessons()
        recent = records[-window:] if window else records
        return [
            _combined_text(r.interpretation)
            for r in recent
            if not r.template_fallback and _combined_text(r.interpretation)
        ]

    def synthesize_whitepaper(self, n: int | None = None) -> Whitepaper:
        """Synthesize a whitepaper by grouping the last `n` lessons by outcome/kind
        and surfacing themes that recur across more than one of them.

        Themes come from the STRUCTURED interpretation fields (what surprised
        the model, what it says to do differently, what it cited) rather than
        word frequency over the whole free-text note - see proposal item 6. A
        boilerplate template-fallback note contributes no vocabulary, so a
        block dominated by fallbacks yields "not enough" rather than a false
        theme built from the fallback sentence itself.
        """
        window = n if n is not None else self.whitepaper_every
        all_lessons = self.read_lessons()
        covered = all_lessons[-window:] if window else all_lessons

        outcome_counts = Counter(r.outcome for r in covered)
        kind_counts = Counter(r.kind for r in covered)
        failures = [r for r in covered if r.outcome == "fail"]

        word_sources: dict[str, set[str]] = {}
        citation_sources: dict[str, set[str]] = {}
        for r in covered:
            if r.template_fallback:
                continue
            interp = r.interpretation
            text = f"{interp.what_surprised} {interp.what_to_do_differently}"
            for w in {w.lower() for w in _WORD_RE.findall(text)}:
                word_sources.setdefault(w, set()).add(r.note_name)
            citation = interp.reference_citation.strip()
            if citation and citation.lower() != "none":
                citation_sources.setdefault(citation, set()).add(r.note_name)

        recurring_themes = sorted(
            (w for w, notes in word_sources.items() if len(notes) >= 2),
            key=lambda w: (-len(word_sources[w]), w),
        )[:5]
        recurring_citations = sorted(
            (c for c, notes in citation_sources.items() if len(notes) >= 2),
            key=lambda c: (-len(citation_sources[c]), c),
        )[:5]

        outcome_table = "\n".join(f"| {k} | {v} |" for k, v in sorted(outcome_counts.items())) or "| _(none)_ | 0 |"
        kind_table = "\n".join(f"| {k} | {v} |" for k, v in sorted(kind_counts.items())) or "| _(none)_ | 0 |"

        if failures:
            failure_lines = "\n".join(
                f"- [[{r.note_name}]] ({r.kind}): "
                f"{r.interpretation.what_to_do_differently or '_(no interpretation recorded)_'}"
                for r in failures
            )
        else:
            failure_lines = "_No failures in this window - the loop stayed green throughout._"

        if recurring_themes:
            theme_lines = "\n".join(
                f"- **{w}** - appears in {len(word_sources[w])} lessons: "
                + ", ".join(f"[[{note}]]" for note in sorted(word_sources[w]))
                for w in recurring_themes
            )
        else:
            theme_lines = "_Not enough recurring interpretation yet to call out a theme._"

        if recurring_citations:
            citation_lines = "\n".join(
                f"- `{c}` - cited by {len(citation_sources[c])} lessons: "
                + ", ".join(f"[[{note}]]" for note in sorted(citation_sources[c]))
                for c in recurring_citations
            )
        else:
            citation_lines = "_No reference-set artifact was cited by more than one lesson yet._"

        body = f"""## Outcomes in this window
| outcome | count |
| --- | --- |
{outcome_table}

## Work by kind
| kind | count |
| --- | --- |
{kind_table}

## Recurring failures
{failure_lines}

## Recurring themes
{theme_lines}

## Recurring reference citations
{citation_lines}"""

        summary = (
            f"Synthesis of the last {len(covered)} lesson(s): "
            f"{outcome_counts.get('pass', 0)} pass / {outcome_counts.get('fail', 0)} fail, "
            f"across kinds {', '.join(sorted(kind_counts)) or '(none)'}."
        )
        return Whitepaper(
            title=f"Synthesis after {len(all_lessons)} lessons",
            summary=summary,
            body=body,
            covers_lessons=tuple(r.note_name for r in covered),
        )

    # --- indexing -------------------------------------------------------------
    def reindex_mocs(self) -> list[Path]:
        """Rebuild the MOC files from what is currently on disk."""
        written = [
            self._write_lessons_moc(),
            self._write_whitepapers_moc(),
            self._write_practices_moc(),
            self._write_root_moc(),
        ]
        return written

    # --- rendering ------------------------------------------------------------
    @staticmethod
    def _frontmatter(
        tags: tuple[str, ...], extra: dict[str, str | tuple[str, ...]] | None = None
    ) -> str:
        lines = ["---", "tags:"]
        for t in tags:
            lines.append(f"  - {t}")
        for key, value in (extra or {}).items():
            if isinstance(value, (list, tuple)):
                lines.append(f"{key}:")
                lines.extend(f"  - {item}" for item in value)
            else:
                lines.append(f"{key}: {value}")
        lines.append("---")
        return "\n".join(lines)

    def _render_lesson(self, lesson: Lesson) -> str:
        tags = ("lesson", f"outcome/{lesson.outcome}", f"kind/{lesson.kind}", *lesson.tags)
        # Only for failed iterations, and only a real classification - keeps a
        # passing note byte-for-byte identical to before this field existed.
        if lesson.outcome == "fail" and lesson.failure_class:
            tags = (*tags, f"failure/{lesson.failure_class}")
        # Countable, not silent (proposal item 3): a block dominated by this
        # tag is exactly what the review brief's boilerplate-rate line reports.
        if lesson.interpretation.template_fallback:
            tags = (*tags, TEMPLATE_FALLBACK_TAG)
        extra: dict[str, str | tuple[str, ...]] = {
            "created": lesson.created,
            "iteration": str(lesson.iteration),
            "block": str(lesson.block),
        }
        # Only present when retrieval actually fired, so a run with recall
        # disabled renders byte-for-byte as it did before recall existed.
        if lesson.recalled:
            extra["recalled"] = lesson.recalled
        ev = lesson.evidence
        extra["evidence_model"] = ev.model
        extra["evidence_tier"] = ev.tier
        extra["evidence_wall_clock_seconds"] = f"{ev.wall_clock_seconds:.3f}"
        extra["evidence_input_tokens"] = "" if ev.input_tokens is None else str(ev.input_tokens)
        extra["evidence_output_tokens"] = "" if ev.output_tokens is None else str(ev.output_tokens)
        extra["evidence_insertions"] = str(ev.insertions)
        extra["evidence_deletions"] = str(ev.deletions)
        extra["evidence_hsai_sha"] = ev.hsai_sha
        extra["evidence_core_yaml_hash"] = ev.core_yaml_hash
        if ev.files_changed:
            extra["evidence_files_changed"] = ev.files_changed
        if ev.reverted_workflows:
            extra["evidence_reverted_workflows"] = ev.reverted_workflows
        fm = self._frontmatter(tags, extra)
        refs = "\n".join(f"- `{r}`" for r in lesson.references) or "- _(none cited)_"
        ticket = f"#{lesson.ticket}" if lesson.ticket else "_(none)_"
        pr = f"#{lesson.pr}" if lesson.pr else "_(none)_"
        repro = lesson.repro_evidence or "_(not applicable: not a heal/bugfix ticket)_"
        # Who checked the work, not just who wrote it (G2).
        review = lesson.review_verdict or "_(no independent review recorded)_"
        audit = (
            f"\n## Acceptance audit\n{lesson.audit_verdict}\n"
            if lesson.audit_verdict
            else ""
        )
        # Same "fail only" rule as the tag above, so a pass row set is
        # byte-for-byte unchanged (no extra line, no stray whitespace either).
        failure_row = (
            f"\n| failure class | `{lesson.failure_class}` |"
            if lesson.outcome == "fail" and lesson.failure_class
            else ""
        )
        fallback_note = (
            "\n\n> **Note:** lesson authoring fell back to the fixed template this "
            f"iteration (tagged `{TEMPLATE_FALLBACK_TAG}`)."
            if lesson.interpretation.template_fallback
            else ""
        )
        return f"""{fm}

# {lesson.title}

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **{lesson.outcome}** |
| kind | {lesson.kind} |
| iteration | {lesson.iteration} |
| ticket | {ticket} |
| pull request | {pr} |
| model | `{lesson.model}` |
| remote CI | {lesson.remote_ci or "_(pending)_"} |{failure_row}

## Context
{lesson.context}

## What happened
{lesson.what_happened}

## Evidence
{lesson.evidence.render()}

## Lesson learned
{lesson.interpretation.render()}{fallback_note}

## Execution trace
{lesson.execution_trace or "_(no model run this iteration)_"}

## Independent review
{review}
{audit}
## Reproduction evidence
{repro}

## References (reference-set evidence)
{refs}
"""

    def _render_whitepaper(self, paper: Whitepaper) -> str:
        tags = ("whitepaper", *paper.tags)
        fm = self._frontmatter(tags, {"created": paper.created})
        covered = "\n".join(f"- [[{n}]]" for n in paper.covers_lessons) or "- _(none)_"
        return f"""{fm}

# {paper.title}

> Part of [[Whitepapers MOC]] - [[Knowledge Base MOC]]

## Summary
{paper.summary}

{paper.body}

## Lessons synthesized
{covered}
"""

    def _write_lessons_moc(self) -> Path:
        notes = self.lesson_notes()
        fm = self._frontmatter(("moc", "lessons"), {"updated": _today()})
        links = "\n".join(f"- [[{n}]]" for n in notes) or "- _No lessons recorded yet._"
        content = f"""{fm}

# Lessons MOC

Up: [[Knowledge Base MOC]]

Every hsai iteration leaves a lesson here - pass or fail. Total: **{len(notes)}**.

{links}
"""
        path = self.mocs_dir / "Lessons MOC.md"
        path.write_text(content)
        return path

    def _write_whitepapers_moc(self) -> Path:
        notes = self.whitepaper_notes()
        fm = self._frontmatter(("moc", "whitepapers"), {"updated": _today()})
        links = "\n".join(f"- [[{n}]]" for n in notes) or "- _No whitepapers yet._"
        content = f"""{fm}

# Whitepapers MOC

Up: [[Knowledge Base MOC]]

Periodic syntheses of accumulated lessons. Total: **{len(notes)}**.

{links}
"""
        path = self.mocs_dir / "Whitepapers MOC.md"
        path.write_text(content)
        return path

    def _write_practices_moc(self) -> Path:
        """Adopted-practice registry, grouped by source project.

        Deterministic on every run: :meth:`read_practices` already sorts by
        id, and the project groups are sorted here too, so `hsai reindex` run
        twice in a row on an unchanged registry produces byte-identical output.
        """
        records = self.read_practices()
        fm = self._frontmatter(("moc", "practices"), {"updated": _today()})
        if records:
            groups: dict[str, list[practices_mod.Practice]] = {}
            for p in records:
                groups.setdefault(p.source_project, []).append(p)
            sections = []
            for project in sorted(groups):
                lines = "\n".join(
                    f"- [[{p.note_name()}]] - {p.title} ({p.status})"
                    for p in sorted(groups[project], key=lambda x: x.title)
                )
                sections.append(f"### `{project}`\n{lines}")
            body = "\n\n".join(sections)
        else:
            body = "_No practices recorded yet._"
        content = f"""{fm}

# Practices MOC

Up: [[Knowledge Base MOC]]

Practices adopted (or rejected) from the reference set, grouped by source
project - the durable record behind G1's traceability claim. Total: **{len(records)}**.

{body}
"""
        path = self.mocs_dir / "Practices MOC.md"
        path.write_text(content)
        return path

    def _write_root_moc(self) -> Path:
        fm = self._frontmatter(("moc", "index"), {"updated": _today()})
        n_lessons = len(self.lesson_notes())
        n_papers = len(self.whitepaper_notes())
        n_practices = len(self.practice_notes())
        content = f"""{fm}

# Knowledge Base MOC

The living memory of **ai-hyperswarm-proto-core**. Open this repo as an Obsidian
vault and use the graph view to explore how lessons connect.

## Maps
- [[Lessons MOC]] - {n_lessons} lesson(s)
- [[Whitepapers MOC]] - {n_papers} whitepaper(s)
- [[Practices MOC]] - {n_practices} practice(s)

## How this is maintained
- Each PR the [[hsai]] loop opens contributes exactly one lesson.
- Every {self.whitepaper_every} lessons, a whitepaper synthesizes the themes.
- Every synthesized ticket that adds or extends a practice is recorded in the
  practices registry, indexed by [[Practices MOC]].
- These MOCs are regenerated by `hsai reindex` after each iteration.
"""
        path = self.mocs_dir / "Knowledge Base MOC.md"
        path.write_text(content)
        return path


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def today() -> date:
    return datetime.now(timezone.utc).date()
