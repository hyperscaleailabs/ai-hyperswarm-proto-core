"""The knowledge base: lessons, whitepapers, and Maps of Content (MOCs).

Everything written here is Obsidian-ready:
- YAML frontmatter with tags,
- ``[[wikilinks]]`` between notes and up to their MOCs,
so that cloning the repo and opening it as a vault yields a connected graph.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path

from . import ai
from . import practices as practices_mod
from .config import CoreConfig
from .models import ModelChoice
from .proc import Runner, run

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_TAG_RE = re.compile(r"^\s*-\s+(\S.*)$", re.MULTILINE)
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)
_TITLE_RE = re.compile(r"^# (.+)$", re.MULTILINE)
_SECTION_RE = re.compile(r"^## (.+)$", re.MULTILINE)
_SUBSECTION_RE = re.compile(r"^### (.+)$", re.MULTILINE)
_EVIDENCE_FIELD_RE = re.compile(r"^- \*\*(.+?)\*\*:\s*(.*)$", re.MULTILINE)
_FENCED_BLOCK_RE = re.compile(r"```\n(.*?)\n```", re.DOTALL)
_PROVENANCE_RE = re.compile(r"hsai@`([^`]*)`\s*core\.yaml@`([^`]*)`")

# The tag an authored lesson is stamped with when the anti-boilerplate gate
# (see :func:`author_interpretation`) never produced a usable interpretation -
# so a shortfall in G3's promise is visible and countable, never silent.
TEMPLATE_FALLBACK_TAG = "lesson/template-fallback"

NO_CITATION = "none"

# Sentinel bodies for an interpretation field the model never filled in (a
# template-fallback lesson) - distinguishes "no data" from "the model wrote an
# empty string", which would otherwise both parse back as "".
_NO_INTERPRETATION = "_(template fallback: no authored interpretation)_"


def slugify(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-") or "untitled"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class LessonEvidence:
    """Deterministic facts about one iteration - never model-written.

    Everything here is either pulled straight from git, mirrored from the same
    :class:`hsai.ledger.LedgerRecord` the iteration already builds, or read off
    a guard's own output (the CI log, the repro guard). No field is ever a
    model's opinion - that is :class:`LessonInterpretation`'s job.
    """

    files_changed: tuple[str, ...] = ()  # "path +ins/-del", from `git diff --numstat`
    tier: str = ""
    model: str = ""
    wall_clock_seconds: float = 0.0
    input_tokens: int | None = None
    output_tokens: int | None = None
    ci_failure_excerpt: str = ""  # trimmed, only ever populated for outcome=="fail"
    reverted_workflows: tuple[str, ...] = ()
    provenance_sha: str = ""            # hsai's own commit at the time of this run
    provenance_config_hash: str = ""    # short hash of core.yaml at the time of this run

    def render(self) -> str:
        tokens = (
            f"{self.input_tokens} in / {self.output_tokens} out"
            if self.input_tokens is not None or self.output_tokens is not None
            else "_(not reported)_"
        )
        files = "; ".join(self.files_changed) or "_(no files changed)_"
        lines = [
            f"- **tier**: `{self.tier or '-'}`",
            f"- **model**: `{self.model or '-'}`",
            f"- **wall-clock**: {self.wall_clock_seconds:.3f}s",
            f"- **tokens**: {tokens}",
            f"- **provenance**: hsai@`{self.provenance_sha or '-'}` "
            f"core.yaml@`{self.provenance_config_hash or '-'}`",
            f"- **files changed**: {files}",
        ]
        if self.reverted_workflows:
            lines.append(
                "- **reverted workflow edits**: " + "; ".join(self.reverted_workflows)
            )
        text = "\n".join(lines)
        if self.ci_failure_excerpt:
            text += f"\n\n**CI failure excerpt**\n```\n{self.ci_failure_excerpt}\n```"
        return text


@dataclass
class LessonInterpretation:
    """The authored half of a lesson - what a model made of the evidence.

    Produced by :func:`author_interpretation` for BOTH pass and fail outcomes
    (the failure case is where the durable knowledge lives); empty fields mean
    authoring fell back to the template (see :data:`TEMPLATE_FALLBACK_TAG`).
    """

    what_was_tried: str = ""
    what_surprised: str = ""
    what_to_do_differently: str = ""
    reference_citation: str = NO_CITATION

    def is_empty(self) -> bool:
        return not (self.what_was_tried or self.what_surprised or self.what_to_do_differently)

    def combined_text(self) -> str:
        return " ".join(
            s for s in (self.what_was_tried, self.what_surprised, self.what_to_do_differently)
            if s
        )

    def render(self) -> str:
        def section(body: str) -> str:
            return body or _NO_INTERPRETATION

        return f"""### What was tried
{section(self.what_was_tried)}

### What surprised us
{section(self.what_surprised)}

### What to do differently
{section(self.what_to_do_differently)}

### Reference citation
{self.reference_citation or NO_CITATION}"""


@dataclass
class Lesson:
    title: str
    outcome: str  # "pass" | "fail"
    kind: str  # heal | implement | improve
    context: str
    what_happened: str
    lesson: str
    iteration: int = 0
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
    block: int = 0  # which governance block this iteration belongs to (G4 boilerplate-rate metric)
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
    block: int = 0  # "" -> 0, matching Lesson's default for a note predating this field
    template_fallback: bool = False  # TEMPLATE_FALLBACK_TAG present in tags
    evidence: LessonEvidence = field(default_factory=LessonEvidence)
    interpretation: LessonInterpretation = field(default_factory=LessonInterpretation)


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


def split_subsections(text: str) -> dict[str, str]:
    """Map lowercased ``### headings`` to their bodies - one level under
    :func:`split_sections`, used for the ``## Interpretation`` section's four
    fields so each can hold multi-line authored prose."""
    parts = _SUBSECTION_RE.split(text)
    sections: dict[str, str] = {}
    for i in range(1, len(parts), 2):
        heading = parts[i].strip().lower()
        body = parts[i + 1] if i + 1 < len(parts) else ""
        sections[heading] = body.strip()
    return sections


def _parse_evidence(text: str) -> LessonEvidence:
    """The read-side counterpart of :meth:`LessonEvidence.render`."""
    fields = {m.group(1).strip(): m.group(2).strip() for m in _EVIDENCE_FIELD_RE.finditer(text)}

    def _int_or_none(value: str) -> int | None:
        return int(value) if value.strip("-").isdigit() else None

    tokens_raw = fields.get("tokens", "")
    input_tokens = output_tokens = None
    if " in / " in tokens_raw and " out" in tokens_raw:
        left, right = tokens_raw.split(" in / ", 1)
        input_tokens = _int_or_none(left)
        output_tokens = _int_or_none(right.replace(" out", ""))

    provenance_sha, provenance_config_hash = "", ""
    prov_match = _PROVENANCE_RE.search(fields.get("provenance", ""))
    if prov_match:
        provenance_sha, provenance_config_hash = prov_match.group(1), prov_match.group(2)
        if provenance_sha == "-":
            provenance_sha = ""
        if provenance_config_hash == "-":
            provenance_config_hash = ""

    files_raw = fields.get("files changed", "")
    files_changed = (
        tuple(f.strip() for f in files_raw.split(";") if f.strip())
        if files_raw and files_raw != "_(no files changed)_"
        else ()
    )
    reverted_raw = fields.get("reverted workflow edits", "")
    reverted_workflows = tuple(p.strip() for p in reverted_raw.split(";") if p.strip())

    wall_clock_raw = fields.get("wall-clock", "").rstrip("s")
    try:
        wall_clock_seconds = float(wall_clock_raw)
    except ValueError:
        wall_clock_seconds = 0.0

    excerpt_match = _FENCED_BLOCK_RE.search(text)
    ci_failure_excerpt = excerpt_match.group(1) if excerpt_match else ""

    def _clean(value: str) -> str:
        value = value.strip("`")
        return "" if value == "-" else value

    return LessonEvidence(
        files_changed=files_changed,
        tier=_clean(fields.get("tier", "")),
        model=_clean(fields.get("model", "")),
        wall_clock_seconds=wall_clock_seconds,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        ci_failure_excerpt=ci_failure_excerpt,
        reverted_workflows=reverted_workflows,
        provenance_sha=provenance_sha,
        provenance_config_hash=provenance_config_hash,
    )


def _parse_interpretation(text: str) -> LessonInterpretation:
    """The read-side counterpart of :meth:`LessonInterpretation.render`."""
    subs = split_subsections(text)

    def _get(key: str) -> str:
        value = subs.get(key, "").strip()
        return "" if value == _NO_INTERPRETATION else value

    citation = subs.get("reference citation", "").strip() or NO_CITATION
    return LessonInterpretation(
        what_was_tried=_get("what was tried"),
        what_surprised=_get("what surprised us"),
        what_to_do_differently=_get("what to do differently"),
        reference_citation=citation,
    )


def _frontmatter_tags(fm: str) -> tuple[str, ...]:
    """List items under the ``tags:`` key only.

    Frontmatter now holds a second list (``recalled:``), so a blanket "every
    ``- item`` line is a tag" scan would file recalled note names as tags.
    """
    tags: list[str] = []
    in_tags = False
    for line in fm.splitlines():
        if line.strip() and not line.startswith((" ", "\t", "-")):
            in_tags = line.strip() == "tags:"
            continue
        match = _TAG_RE.match(line)
        if in_tags and match:
            tags.append(match.group(1).strip())
    return tuple(tags)


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
    block_raw = _frontmatter_scalar(fm, "block")
    try:
        block = int(block_raw)
    except ValueError:
        block = 0
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
        block=block,
        template_fallback=TEMPLATE_FALLBACK_TAG in tags,
        evidence=_parse_evidence(sections.get("evidence", "")),
        interpretation=_parse_interpretation(sections.get("interpretation", "")),
    )


DEFAULT_CI_EXCERPT_CHARS = 1500


def trim_ci_excerpt(log: str, limit: int = DEFAULT_CI_EXCERPT_CHARS) -> str:
    """The tail of a red CI log - where ruff/pytest actually print the failure.

    Deterministic evidence (see :class:`LessonEvidence`): a straight
    truncation, never summarized by a model.
    """
    log = (log or "").strip()
    if len(log) <= limit:
        return log
    return "... (truncated)\n" + log[-limit:]


def config_hash(cfg: CoreConfig) -> str:
    """A short, stable hash of ``core.yaml`` as loaded - half of a lesson's
    provenance stamp (the other half is :func:`hsai.gitops.current_sha`)."""
    blob = json.dumps(cfg.raw, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


# --- lesson authoring: the model interprets evidence it cannot fabricate ------
#
# Synthesis: assafelovic/gpt-researcher (plan-research-synthesize-write keeps
# gathered evidence separate from authored narrative - the split this module
# imposes on every lesson) and FoundationAgents/MetaGPT (documents are
# first-class role deliverables with required fields, not a side effect of
# running code).

# Never authors on the heavy tier: this runs on every iteration, pass or fail,
# and a heavy-tier interpretation pass would compete with real work for a
# block's heavy-tier budget for a task that does not need it.
DEFAULT_AUTHOR_TIER = "light"
DEFAULT_AUTHOR_TIMEOUT_SECONDS = 120.0
DEFAULT_SIMILARITY_THRESHOLD = 0.85
DEFAULT_SIMILARITY_WINDOW = 20  # compare against at most this many recent lessons

AUTHOR_PROMPT_MARKER = "You are the LESSON AUTHOR"

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)

# Substrings of the old two-branch template (see the ticket this module
# fixes): an authored interpretation that merely repeats one of these said
# nothing the deterministic evidence didn't already say.
_CI_RESTATEMENT_PHRASES = (
    "change merged cleanly under a green build",
    "change did not reach green",
    "auto-merge will hold until ci passes",
)


def build_author_prompt(
    *,
    ticket_title: str,
    ticket_body: str,
    outcome: str,
    evidence: LessonEvidence,
    ci_summary: str,
    reference_repos: tuple[str, ...] = (),
) -> str:
    """The authoring instruction: evidence-first, strict-JSON-terminated.

    The model is shown exactly the deterministic evidence already collected -
    never the raw agent trajectory - so it interprets facts it cannot
    fabricate, mirroring the gate :mod:`hsai.review` and :mod:`hsai.audit`
    already run through.
    """
    files = "\n".join(f"- {f}" for f in evidence.files_changed) or "- _(no files changed)_"
    excerpt = (
        f"\n\nCI failure excerpt:\n```\n{evidence.ci_failure_excerpt}\n```"
        if evidence.ci_failure_excerpt
        else ""
    )
    repos = "\n".join(f"- `{r}`" for r in reference_repos) or "- _(none configured)_"
    return f"""{AUTHOR_PROMPT_MARKER} for ai-hyperswarm-proto-core, an autonomous
self-improving AI-swarm harness. This iteration just {"PASSED" if outcome == "pass" else "FAILED"}
its own gates ({ci_summary}). You did not do the work and cannot see the raw
run; you are writing the durable lesson a future engineer reads instead of
re-discovering the same thing. A failure is where the durable knowledge lives
- do not waste it on a generic restatement of "it failed".

Ticket: {ticket_title}

{ticket_body}

Deterministic evidence already collected - interpret it, do not restate it:
- outcome: {outcome}
- files changed:
{files}{excerpt}

Reference-set repositories that could plausibly have informed this work (cite
a SPECIFIC artifact - a file, a pattern, a design doc - from ONE of these, or
answer "none" if nothing here actually informed it; do not guess):
{repos}

Answer with prose if you like, but END your reply with a fenced ```json block
containing exactly this object. Every field is required. None may be empty,
generic, or a restatement of the outcome above:
{{"what_was_tried": "1-3 sentences: the concrete approach actually taken",
  "what_surprised": "1-3 sentences: what was NOT expected going in - a real
    failure has a real surprise in it, not \"nothing\" or \"it failed\"",
  "what_to_do_differently": "1-2 sentences: one specific, actionable change -
    not \"stay green\" or \"investigate the failure\"",
  "reference_citation": "a specific artifact in one of the repos above, or
    the literal string \"none\""}}
"""


def parse_interpretation(output: str) -> LessonInterpretation | None:
    """Extract the authored interpretation from a model's reply.

    Fail-closed like :func:`hsai.review.parse_verdict`: anything that is not a
    parseable object returns ``None`` so the caller falls back to the
    template rather than writing a garbled note.
    """
    text = (output or "").strip()
    blocks = _JSON_BLOCK_RE.findall(text)
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
        reference_citation=str(raw.get("reference_citation", "")).strip() or NO_CITATION,
    )


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def boilerplate_reason(
    interpretation: LessonInterpretation,
    *,
    recent_texts: Sequence[str] = (),
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
) -> str:
    """Why the anti-boilerplate gate would reject ``interpretation`` - "" if it passes.

    Three checks, in order: empty, a restatement of the CI outcome the
    evidence already states, or a near-duplicate (by normalized similarity)
    of a recent lesson's interpretation - the volume-without-knowledge failure
    mode this module exists to close off.
    """
    if interpretation.is_empty():
        return "empty interpretation"
    combined = _normalize_text(interpretation.combined_text())
    if any(phrase in combined for phrase in _CI_RESTATEMENT_PHRASES):
        return "interpretation merely restates the CI outcome"
    for prior in recent_texts:
        prior_norm = _normalize_text(prior)
        if prior_norm and SequenceMatcher(None, combined, prior_norm).ratio() >= threshold:
            return "near-duplicate of a recent lesson's interpretation"
    return ""


def _author_choice(cfg: CoreConfig, settings: dict) -> ModelChoice:
    tier = str(settings.get("tier", DEFAULT_AUTHOR_TIER))
    if tier not in cfg.tiers or tier == "heavy":
        tier = "light" if "light" in cfg.tiers else cfg.default_tier
    model = cfg.tiers[tier].model if tier in cfg.tiers else ""
    return ModelChoice(
        tier=tier, model=model, rationale="lesson authoring", strategy="lesson-author-v1"
    )


def author_interpretation(
    cfg: CoreConfig,
    *,
    ticket_title: str,
    ticket_body: str,
    outcome: str,
    evidence: LessonEvidence,
    ci_summary: str,
    recent_texts: Sequence[str] = (),
    reference_repos: tuple[str, ...] = (),
    ai_runner: Runner = run,
) -> tuple[LessonInterpretation, bool]:
    """Ask the model to interpret ``evidence``; returns ``(interpretation, template_fallback)``.

    Runs for pass AND fail outcomes on a configurable non-heavy tier
    (``knowledge.lesson_authoring`` in core.yaml). Never allowed to block the
    loop, mirroring :mod:`hsai.review`/:mod:`hsai.audit`: a crashed or
    timed-out ``claude`` run (``ares.ok is False`` - :func:`hsai.proc.run`
    never raises on either), unparseable output, or two straight
    anti-boilerplate rejections all fall back to the template - and
    ``template_fallback=True`` is the one durable signal that this happened
    (tagged :data:`TEMPLATE_FALLBACK_TAG` on the rendered note).
    """
    settings = (cfg.knowledge or {}).get("lesson_authoring", {}) or {}
    if not settings.get("enabled", True):
        return LessonInterpretation(), True

    choice = _author_choice(cfg, settings)
    timeout = float(settings.get("timeout_seconds", DEFAULT_AUTHOR_TIMEOUT_SECONDS))
    threshold = float(settings.get("similarity_threshold", DEFAULT_SIMILARITY_THRESHOLD))
    prompt = build_author_prompt(
        ticket_title=ticket_title, ticket_body=ticket_body, outcome=outcome,
        evidence=evidence, ci_summary=ci_summary, reference_repos=reference_repos,
    )

    for _attempt in range(2):  # one retry on a rejected (or unusable) attempt
        ares = ai.run_agent(prompt, choice, cfg, runner=ai_runner, timeout=timeout)
        if not ares.ok:
            continue
        interp = parse_interpretation(ares.text)
        if interp is None:
            continue
        if not boilerplate_reason(interp, recent_texts=recent_texts, threshold=threshold):
            return interp, False
    return LessonInterpretation(), True


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

    def synthesize_whitepaper(self, n: int | None = None) -> Whitepaper:
        """Synthesize a whitepaper by grouping the last `n` lessons by outcome/kind
        and surfacing themes that recur across more than one of them.

        Themes come from the structured ``what_to_do_differently`` field an
        authored lesson carries (see :func:`author_interpretation`), not from
        word-frequency over the whole note: counting words across boilerplate
        text mostly measures the frequency of the boilerplate itself.
        Template-fallback lessons (see :data:`TEMPLATE_FALLBACK_TAG`) have no
        real interpretation and are excluded from theming, though they still
        count in the outcome/kind tables below.
        """
        window = n if n is not None else self.whitepaper_every
        all_lessons = self.read_lessons()
        covered = all_lessons[-window:] if window else all_lessons

        outcome_counts = Counter(r.outcome for r in covered)
        kind_counts = Counter(r.kind for r in covered)
        failures = [r for r in covered if r.outcome == "fail"]

        theme_notes: dict[str, list[str]] = {}
        theme_text: dict[str, str] = {}
        for r in covered:
            if r.template_fallback:
                continue
            text = r.interpretation.what_to_do_differently
            key = _normalize_text(text)
            if not key:
                continue
            theme_text.setdefault(key, text)
            theme_notes.setdefault(key, []).append(r.note_name)
        recurring_themes = sorted(
            (key for key, notes in theme_notes.items() if len(notes) >= 2),
            key=lambda key: (-len(theme_notes[key]), key),
        )[:5]

        citations = sorted({
            r.interpretation.reference_citation for r in covered
            if not r.template_fallback
            and r.interpretation.reference_citation
            and r.interpretation.reference_citation.lower() != NO_CITATION
        })

        outcome_table = "\n".join(f"| {k} | {v} |" for k, v in sorted(outcome_counts.items())) or "| _(none)_ | 0 |"
        kind_table = "\n".join(f"| {k} | {v} |" for k, v in sorted(kind_counts.items())) or "| _(none)_ | 0 |"

        if failures:
            failure_lines = "\n".join(
                f"- [[{r.note_name}]] ({r.kind}): "
                f"{r.lesson_text.splitlines()[0] if r.lesson_text else '_(no lesson text recorded)_'}"
                for r in failures
            )
        else:
            failure_lines = "_No failures in this window - the loop stayed green throughout._"

        if recurring_themes:
            theme_lines = "\n".join(
                f"- **{theme_text[key]}** - "
                + ", ".join(f"[[{note}]]" for note in theme_notes[key])
                for key in recurring_themes
            )
        else:
            theme_lines = "_Not enough recurring authored interpretation yet to call out a theme._"

        citation_lines = "\n".join(f"- `{c}`" for c in citations) or "_none cited this window_"

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

## Reference citations
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
        extra: dict[str, str | tuple[str, ...]] = {
            "created": lesson.created,
            "iteration": str(lesson.iteration),
            "block": str(lesson.block),
        }
        # Only present when retrieval actually fired, so a run with recall
        # disabled renders byte-for-byte as it did before recall existed.
        if lesson.recalled:
            extra["recalled"] = lesson.recalled
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
{lesson.lesson}

## Interpretation
{lesson.interpretation.render()}

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
