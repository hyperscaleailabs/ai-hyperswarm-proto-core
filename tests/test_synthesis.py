import json
import re
from pathlib import Path

from hsai import ai, retrieval
from hsai.config import load_config
from hsai.models import ModelChoice
from hsai.practices import ADOPTED_HEADING, REJECTED_RULE, build_practice
from hsai.proc import Proc
from hsai.retrieval import PRIOR_ART_HEADING, PriorArt
from hsai.synthesis import (
    DEFAULT_MEMORY_MAX_CHARS,
    DUPLICATE_JACCARD_THRESHOLD,
    LABELS_CHARS,
    MEMORY_HEADING,
    README_CHARS,
    WORKFLOW_BODY_CHARS,
    ContextPack,
    MemoryPack,
    build_context_pack,
    build_prompt,
    gather_prior_art,
    goal_queries,
    is_duplicate,
    parse_observed_practices,
    parse_ticket_specs,
    pick_rotation,
    synthesize,
)
from hsai.tickets import NO_PRIOR_ART, TicketSpec, studied_repos

REPO_ROOT = Path(__file__).resolve().parents[1]


def _cfg():
    return load_config()


def test_rotation_covers_the_set_over_cycles():
    cfg = _cfg()
    seen: set[str] = set()
    for i in range(4):
        subset = pick_rotation(cfg, i)
        assert len(subset) == 3
        seen.update(subset)
    assert len(seen) >= 10  # 4 cycles x 3 repos wraps the whole top-10


def test_prompt_demands_combination_and_reflection():
    cfg = _cfg()
    pack = ContextPack(repos=["a/b"], sections={"a/b": "digest"})
    prompt = build_prompt(cfg, pack)
    assert "PHASE 1" in prompt and "PHASE 2" in prompt and "PHASE 3" in prompt
    assert "at least 3 different reference projects" in prompt or "combine" in prompt.lower()
    assert "acceptance_criteria" in prompt


def test_parse_ticket_specs_takes_last_json_block():
    output = """PHASE 1 ... PHASE 2 ...
```json
[{"wrong": "block"}]
```
PHASE 3:
```json
[{"title": "feat: adaptive budget", "problem": "p", "proposal": "pp",
  "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
  "size": "L", "goal_ids": ["G4"], "synthesis_rationale": "combines x+y+z"}]
```"""
    specs = parse_ticket_specs(output)
    assert len(specs) == 1
    spec = specs[0]
    assert spec.title == "feat: adaptive budget"
    assert spec.size == "L"
    assert "size:L" in spec.all_labels()
    assert len(spec.acceptance_criteria) == 3


def test_parse_handles_garbage():
    assert parse_ticket_specs("no json here") == []
    assert parse_ticket_specs("```json\nnot json\n```") == []


def test_parse_ticket_specs_reads_practice_ids():
    output = """PHASE 3:
```json
[{"title": "feat: adaptive budget", "problem": "p", "proposal": "pp",
  "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
  "size": "L", "goal_ids": ["G4"], "synthesis_rationale": "combines x+y+z",
  "practice_ids": ["openbmb-chatdev--session-durability"]}]
```"""
    specs = parse_ticket_specs(output)
    assert specs[0].practice_ids == ("openbmb-chatdev--session-durability",)
    assert "openbmb-chatdev--session-durability" in specs[0].render()


def test_parse_ticket_specs_omitting_practice_ids_still_parses():
    """Existing (pre-practices-registry) synthesis output must not break."""
    output = """PHASE 3:
```json
[{"title": "feat: adaptive budget", "problem": "p", "proposal": "pp",
  "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
  "size": "L", "goal_ids": ["G4"], "synthesis_rationale": "combines x+y+z"}]
```"""
    specs = parse_ticket_specs(output)
    assert specs[0].practice_ids == ()
    assert "practice_ids: -" in specs[0].render()


# --- adopted-practice registry in the prompt ----------------------------------

def test_prompt_includes_adopted_practices_and_do_not_repropose_instruction():
    cfg = _cfg()
    pack = ContextPack(repos=["a/b"], sections={"a/b": "digest"})
    practice = build_practice(
        title="session durability", source_project="OpenBMB/ChatDev",
        source_artifact="harness_design", evidence="PR #104",
    )
    prompt = build_prompt(cfg, pack, practices=[practice])
    assert ADOPTED_HEADING in prompt
    assert "session durability" in prompt and "OpenBMB/ChatDev" in prompt
    assert "do not" in ADOPTED_HEADING.lower() or "not re-propose" in prompt.lower()
    assert "practice_ids" in prompt

    # the heading survives with no practices recorded yet
    empty_prompt = build_prompt(cfg, pack)
    assert ADOPTED_HEADING in empty_prompt
    assert "no practices recorded yet" in empty_prompt.lower()


def test_prompt_names_adopted_ids_and_visibly_discourages_re_proposing_them():
    """A seeded catalog must reach the planner by id, with an explicit rule."""
    cfg = _cfg()
    pack = ContextPack(repos=["a/b"], sections={"a/b": "digest"})
    catalog = [
        build_practice(
            title="a hard numeric CI gate", source_project="run-llama/llama_index",
            source_artifact="ci_cd", evidence="PR #47", status="adopted", adopted_pr=47,
        ),
        build_practice(
            title="per-worker vector memory", source_project="microsoft/JARVIS",
            source_artifact="harness_design", evidence="tried in PR #61, reverted",
            status="rejected",
        ),
    ]
    prompt = build_prompt(cfg, pack, practices=catalog)

    for practice in catalog:
        assert f"`{practice.id}`" in prompt
    assert "status: adopted" in prompt and "status: rejected" in prompt
    # re-proposing an adopted practice is forbidden outright...
    assert "do NOT" in prompt or "Do NOT" in prompt
    assert "re-adopts one of these" in prompt
    # ...and resurrecting a rejected one needs new evidence, not just optimism
    assert REJECTED_RULE in prompt


def test_prompt_asks_for_the_observed_practice_catalog():
    prompt = build_prompt(_cfg(), ContextPack(repos=["a/b"], sections={"a/b": "d"}))
    assert '"source_artifact"' in prompt
    assert "ci_cd" in prompt          # the artifact vocabulary is spelled out
    assert "FIRST json block" in prompt
    assert "LAST fenced block" in prompt


def test_synthesize_feeds_the_practices_registry_to_the_model(tmp_path):
    from hsai.knowledge import KnowledgeBase
    from hsai.practices import append

    cfg = _cfg()
    kb = KnowledgeBase.from_config(cfg, tmp_path)
    append(
        tmp_path,
        build_practice(
            title="cost accounting", source_project="assafelovic/gpt-researcher",
            source_artifact="source_code", evidence="PR #47",
        ),
        cfg=cfg,
    )
    assert kb.read_practices()  # sanity: the note is visible through the KB too

    runner = _plain_text_runner()
    synthesize(cfg, cycle_index=0, root=str(tmp_path), runner=runner, ai_runner=runner)
    claude_call = next(c for c in runner.calls if c[:1] == ["claude"])
    assert "cost accounting" in claude_call[2]
    assert ADOPTED_HEADING in claude_call[2]


# --- plain-text (non-JSON) CLI output must never break synthesis -------------

PLAIN_TEXT_OUTPUT = """PHASE 1 - DIVERGE: ten candidates considered.
PHASE 2 - REFLECT: three survived critique.
PHASE 3 - PRIORITIZE:
```json
[{"title": "feat: adaptive budget", "problem": "p", "proposal": "pp",
  "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
  "size": "L", "goal_ids": ["G4"], "synthesis_rationale": "combines x+y+z"}]
```"""


def _plain_text_runner():
    """A `claude` that prints plain text - an older binary, or a crash."""
    calls: list[list[str]] = []
    issue_numbers = iter(range(321, 400))

    def runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        calls.append(list(cmd))
        if cmd[:1] == ["claude"]:
            return Proc(cmd, 0, PLAIN_TEXT_OUTPUT, "")
        if cmd[:3] == ["gh", "issue", "create"]:
            return Proc(cmd, 0, f"https://github.com/o/r/issues/{next(issue_numbers)}\n", "")
        return Proc(cmd, 0, "", "")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def test_synthesize_survives_output_without_a_json_envelope():
    """`payload is None` is a supported state, not a failure mode."""
    cfg = _cfg()
    runner = _plain_text_runner()

    # The CLI exposed no structured envelope at all...
    result = ai.run_agent(
        "p", ModelChoice(tier="heavy", model="opus", rationale="t"), cfg, runner=runner
    )
    assert result.payload is None and result.usage is None
    assert result.text == PLAIN_TEXT_OUTPUT      # falls back to raw stdout

    # ...and synthesis still parses its ticket specs off the raw text and files them.
    res = synthesize(cfg, cycle_index=0, runner=runner, ai_runner=runner)
    assert res.ok is True
    assert res.filed == [321]
    assert res.error == ""
    assert res.rejected == 0
    assert res.rejected_titles == []


# --- MemoryPack: what this loop already knows about its own state ------------

OPEN_ISSUES = [
    {
        "number": 40, "title": "feat: lesson-retrieval memory",
        "labels": [{"name": "self-improve"}, {"name": "priority:P2"}],
        "assignees": [], "body": "",
    },
    {
        "number": 41, "title": "ci: main is red - auto-heal",
        "labels": [{"name": "ci"}], "assignees": [], "body": "",
    },
]

CLOSED_ISSUES = [
    {
        "number": 30, "title": "feat: adaptive budget throttling",
        "labels": [{"name": "self-improve"}], "assignees": [], "body": "",
        "closedAt": "2026-08-01T00:00:00Z",
    },
]


def _memory_runner(*, open_issues=None, closed_issues=None):
    """A fake `gh` that answers both `issue list --state open` and `--state closed`."""

    def runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        if cmd[:3] == ["gh", "issue", "list"]:
            state = cmd[cmd.index("--state") + 1] if "--state" in cmd else "open"
            if state == "closed":
                data = CLOSED_ISSUES if closed_issues is None else closed_issues
            else:
                data = OPEN_ISSUES if open_issues is None else open_issues
            return Proc(cmd, 0, json.dumps(data), "")
        return Proc(cmd, 0, "", "")

    return runner


def _write_lesson(root, name, *, outcome, title):
    directory = root / "knowledge" / "lessons"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{name}.md").write_text(
        f"---\ntags:\n  - lesson\n  - outcome/{outcome}\n  - kind/implement\n"
        f"created: 2026-01-01\n---\n\n# {title}\n\n## Lesson learned\nSomething.\n"
    )


def test_memory_pack_gather_collects_open_closed_and_lessons(tmp_path):
    cfg = _cfg()
    _write_lesson(tmp_path, "2026-01-01-a", outcome="pass", title="Poll remote CI")
    _write_lesson(tmp_path, "2026-01-02-b", outcome="fail", title="Edit the workflows")

    memory = MemoryPack.gather(cfg, root=str(tmp_path), runner=_memory_runner())

    assert [i.title for i in memory.open_tickets] == [
        "feat: lesson-retrieval memory", "ci: main is red - auto-heal",
    ]
    assert memory.closed_titles == ("feat: adaptive budget throttling",)
    # read_lessons() is oldest-first; the pack flips it so the newest lesson leads.
    assert memory.lessons == (("fail", "Edit the workflows"), ("pass", "Poll remote CI"))


def test_memory_pack_render_lists_all_three_sources(tmp_path):
    cfg = _cfg()
    _write_lesson(tmp_path, "2026-01-02-b", outcome="fail", title="Edit the workflows")
    memory = MemoryPack.gather(cfg, root=str(tmp_path), runner=_memory_runner())

    text = memory.render()
    assert "#40 feat: lesson-retrieval memory" in text
    assert "feat: adaptive budget throttling" in text
    assert "**fail** - Edit the workflows" in text


def test_memory_pack_render_degrades_to_a_placeholder_when_empty():
    assert MemoryPack().render() == "_(nothing recorded yet - this is an early cycle)_"


def test_memory_pack_render_is_hard_capped():
    many_lessons = tuple(("pass", f"lesson number {i} about something") for i in range(500))
    memory = MemoryPack(lessons=many_lessons)

    capped = memory.render(max_chars=200)
    assert len(capped) <= 200
    assert capped.endswith("...")

    # A generous cap does not truncate content that already fits.
    small = MemoryPack(lessons=(("pass", "one short lesson"),))
    assert not small.render(max_chars=DEFAULT_MEMORY_MAX_CHARS).endswith("...")


def test_memory_pack_gathering_degrades_gracefully_when_gh_is_unavailable(tmp_path):
    """`gh` missing (exit 127, empty stdout) and an empty knowledge base must
    yield an empty memory section, never raise."""
    cfg = _cfg()

    def broken_runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        return Proc(cmd, 127, "", "gh: command not found")

    memory = MemoryPack.gather(cfg, root=str(tmp_path), runner=broken_runner)
    assert memory.open_tickets == ()
    assert memory.closed_titles == ()
    assert memory.lessons == ()
    assert memory.render() == "_(nothing recorded yet - this is an early cycle)_"

    # And synthesis itself must not abort because memory gathering came back empty:
    # it runs to completion (the model call still happens) rather than raising.
    res = synthesize(cfg, cycle_index=0, root=str(tmp_path), runner=broken_runner,
                      ai_runner=_plain_text_runner())
    assert res.rejected == 0
    assert res.rejected_titles == []


def test_prompt_puts_memory_section_before_the_study_digest():
    cfg = _cfg()
    pack = ContextPack(repos=["a/b"], sections={"a/b": "digest of a/b"})
    memory = MemoryPack(closed_titles=("feat: something already shipped",))

    prompt = build_prompt(cfg, pack, memory)
    assert MEMORY_HEADING in prompt
    assert "feat: something already shipped" in prompt
    assert "Do NOT" in prompt or "DROPPED" in prompt   # an explicit instruction, not a hint
    assert prompt.index(MEMORY_HEADING) < prompt.index("Study digest of reference projects")

    # the heading survives even with nothing to report, so the planner always
    # knows this section exists
    assert MEMORY_HEADING in build_prompt(cfg, pack)
    assert "nothing recorded yet" in build_prompt(cfg, pack)


def test_synthesize_feeds_memory_to_the_model():
    cfg = _cfg()
    runner = _plain_text_runner()
    synthesize(cfg, cycle_index=0, root=".", runner=runner, ai_runner=runner)
    claude_call = next(c for c in runner.calls if c[:1] == ["claude"])
    assert MEMORY_HEADING in claude_call[2]


# --- is_duplicate(): pure normalized-title overlap ----------------------------

def _spec(title: str) -> TicketSpec:
    return TicketSpec(
        title=title, problem="p", proposal="pp",
        acceptance_criteria=("a", "b", "c"), verification_plan=("v1", "v2"),
    )


def test_is_duplicate_exact_title_match():
    memory = MemoryPack(closed_titles=("feat: adaptive budget throttling per tier",))
    dup, matched = is_duplicate(_spec("feat: adaptive budget throttling per tier"), memory)
    assert dup is True
    assert matched == "feat: adaptive budget throttling per tier"


def test_is_duplicate_prefix_only_difference():
    """`feat:` vs `refactor:` on an otherwise identical title is still a duplicate."""
    memory = MemoryPack(closed_titles=("feat: adaptive budget throttling per tier",))
    dup, matched = is_duplicate(
        _spec("refactor: adaptive budget throttling per tier"), memory
    )
    assert dup is True
    assert matched == "feat: adaptive budget throttling per tier"


def test_is_duplicate_genuine_near_duplicate():
    """Same idea, reworded - high token overlap, not an exact or prefix match."""
    memory = MemoryPack(closed_titles=("feat: retry queue backoff for flaky CI checks",))
    dup, matched = is_duplicate(
        _spec("feat: add exponential backoff to the retry queue for flaky checks"), memory
    )
    assert dup is True
    assert matched == "feat: retry queue backoff for flaky CI checks"


def test_is_duplicate_distinct_idea_is_not_rejected():
    memory = MemoryPack(closed_titles=("feat: retry queue backoff for flaky CI checks",))
    dup, matched = is_duplicate(_spec("feat: cost ledger visualization dashboard"), memory)
    assert dup is False
    assert matched == ""


def test_is_duplicate_threshold_is_configurable_and_documented():
    memory = MemoryPack(closed_titles=("feat: retry queue backoff for flaky CI checks",))
    spec = _spec("feat: add exponential backoff to the retry queue for flaky checks")
    # A stricter threshold than the documented default rejects the same pair.
    assert DUPLICATE_JACCARD_THRESHOLD < 1.0
    dup, _ = is_duplicate(spec, memory, threshold=0.99)
    assert dup is False


def test_is_duplicate_checks_open_and_closed_tickets_and_lessons():
    memory = MemoryPack(
        open_tickets=(),
        closed_titles=(),
        lessons=(("fail", "feat: adaptive budget throttling per tier"),),
    )
    dup, matched = is_duplicate(_spec("feat: adaptive budget throttling per tier"), memory)
    assert dup is True
    assert matched == "feat: adaptive budget throttling per tier"


# --- synthesize() drops duplicates before filing ------------------------------

DUPLICATE_AND_NOVEL_OUTPUT = """PHASE 1 ... PHASE 2 ... PHASE 3:
```json
[
  {"title": "feat: lesson-retrieval memory", "problem": "p", "proposal": "pp",
   "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
   "size": "M", "goal_ids": ["G4"], "synthesis_rationale": "combines x+y+z"},
  {"title": "feat: cost ledger visualization dashboard", "problem": "p", "proposal": "pp",
   "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
   "size": "M", "goal_ids": ["G1"], "synthesis_rationale": "combines a+b+c"},
  {"title": "feat: recall-weighted worker prompts", "problem": "p", "proposal": "pp",
   "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
   "size": "M", "goal_ids": ["G3"], "synthesis_rationale": "combines d+e+f"}
]
```"""


def _duplicate_fixture_runner():
    calls: list[list[str]] = []
    issue_numbers = iter(range(500, 600))

    def runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        calls.append(list(cmd))
        if cmd[:1] == ["claude"]:
            return Proc(cmd, 0, DUPLICATE_AND_NOVEL_OUTPUT, "")
        if cmd[:3] == ["gh", "issue", "create"]:
            return Proc(cmd, 0, f"https://github.com/o/r/issues/{next(issue_numbers)}\n", "")
        if cmd[:3] == ["gh", "issue", "list"]:
            state = cmd[cmd.index("--state") + 1] if "--state" in cmd else "open"
            data = OPEN_ISSUES if state == "open" else []
            return Proc(cmd, 0, json.dumps(data), "")
        return Proc(cmd, 0, "", "")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def test_synthesize_drops_duplicates_and_files_only_the_survivors():
    """One of three candidates duplicates open ticket #40 - only two get filed."""
    cfg = _cfg()
    runner = _duplicate_fixture_runner()

    res = synthesize(cfg, cycle_index=0, root=".", runner=runner, ai_runner=runner)

    assert res.ok is True
    assert len(res.filed) == 2
    assert res.rejected == 1
    assert res.rejected_titles == ["feat: lesson-retrieval memory"]

    created_titles = [
        c[c.index("--title") + 1] for c in runner.calls if c[:3] == ["gh", "issue", "create"]
    ]
    assert "feat: lesson-retrieval memory" not in created_titles
    assert "feat: cost ledger visualization dashboard" in created_titles
    assert "feat: recall-weighted worker prompts" in created_titles


def test_synthesize_never_backfills_a_thin_block():
    """Filtering below `file_top` files only the survivors - no padding."""
    cfg = _cfg()
    assert int(cfg.synthesis.get("file_top", 3)) >= 2  # the fixture must be thinner than this
    runner = _duplicate_fixture_runner()

    res = synthesize(cfg, cycle_index=0, root=".", runner=runner, ai_runner=runner)
    assert len(res.filed) < int(cfg.synthesis.get("file_top", 3))
    assert len(res.filed) == 2


# --- prior art: the planner reads its own knowledge base ----------------------

def _prior_art(note_name: str, title: str, outcome: str = "pass") -> PriorArt:
    return PriorArt(note_name=note_name, title=title, outcome=outcome, score=1.0)


def test_prompt_carries_a_prior_art_section_with_note_names_and_outcomes():
    cfg = _cfg()
    pack = ContextPack(
        repos=["a/b"],
        sections={"a/b": "digest of a/b"},
        prior_art=(
            _prior_art("2026-01-01-budget", "feat: adaptive budget", outcome="fail"),
            _prior_art("2026-01-02-recall", "feat: lesson recall"),
        ),
    )
    prompt = build_prompt(cfg, pack)

    assert PRIOR_ART_HEADING in prompt
    assert "[[2026-01-01-budget]] (fail) - feat: adaptive budget" in prompt
    assert "[[2026-01-02-recall]] (pass) - feat: lesson recall" in prompt
    # The planner is told to USE it, not just shown it.
    assert "CHECK IT AGAINST THE PRIOR ART" in prompt
    assert "duplicate-risk verdict" in prompt
    assert '"prior_art"' in prompt
    assert prompt.index(PRIOR_ART_HEADING) < prompt.index("Study digest of reference projects")


def test_prompt_prior_art_section_survives_an_empty_knowledge_base():
    prompt = build_prompt(_cfg(), ContextPack(repos=["a/b"], sections={"a/b": "d"}))
    assert PRIOR_ART_HEADING in prompt
    assert "No prior art found" in prompt


def test_goal_queries_cover_every_goal_in_core_yaml():
    cfg = _cfg()
    queries = goal_queries(cfg)
    assert len(queries) == len(cfg.goals)
    assert any("knowledge base" in q.lower() for q in queries)


def test_gather_prior_art_grounds_the_cycle_in_the_real_vault():
    """The committed knowledge base must produce citations for our own goals."""
    cfg = _cfg()
    art = gather_prior_art(cfg, retrieval.load_index(REPO_ROOT, cfg))
    assert art, "the real vault must yield prior art for the core goals"
    assert all(p.note_name for p in art)
    rendered = ContextPack(repos=[], sections={}, prior_art=art).render_prior_art()
    assert rendered.startswith("- [[")


def test_synthesize_feeds_prior_art_citations_from_the_real_vault_to_the_model():
    """The captured prompt cites this repo's own notes, by name and outcome."""
    cfg = _cfg()
    runner = _plain_text_runner()
    synthesize(cfg, cycle_index=0, root=str(REPO_ROOT), runner=runner, ai_runner=runner)

    prompt = next(c for c in runner.calls if c[:1] == ["claude"])[2]
    assert PRIOR_ART_HEADING in prompt
    section = prompt.split(PRIOR_ART_HEADING, 1)[1].split("Study digest", 1)[0]
    labels = re.findall(r"\[\[[^\]]+\]\] \(([^)]+)\)", section)
    assert labels, section
    # Every citation carries what the note recorded - an outcome for a lesson,
    # otherwise what kind of note it is.
    assert all(lbl.split("/")[0] in {"pass", "fail", "whitepaper", "adr"} for lbl in labels)


# --- filed tickets cite their prior art ---------------------------------------

FAILED_IDEA = (
    "Give every worker its own persistent vector memory of past runs so it can "
    "look up similar situations before acting, backed by an embedding store "
    "refreshed on every iteration."
)

RESTATED_AND_NOVEL_OUTPUT = """PHASE 1 ... PHASE 2 ... PHASE 3:
```json
[
  {"title": "feat: agent situation store", "problem": "PROBLEM",
   "proposal": "PROPOSAL",
   "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
   "size": "L", "goal_ids": ["G4"], "synthesis_rationale": "combines x+y+z",
   "prior_art": ["2026-01-04-vector-memory", "2099-12-31-invented"]},
  {"title": "feat: signed provenance attestation per merged pull request",
   "problem": "Downstream consumers cannot verify which model produced a diff.",
   "proposal": "Publish a signed attestation naming the model, ticket and PR.",
   "acceptance_criteria": ["a", "b", "c"], "verification_plan": ["v1", "v2"],
   "size": "M", "goal_ids": ["G2"], "synthesis_rationale": "combines a+b+c",
   "prior_art": []}
]
```""".replace("PROBLEM", FAILED_IDEA).replace("PROPOSAL", FAILED_IDEA)


def _prior_art_runner(output: str):
    calls: list[list[str]] = []
    issue_numbers = iter(range(700, 800))

    def runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        calls.append(list(cmd))
        if cmd[:1] == ["claude"]:
            return Proc(cmd, 0, output, "")
        if cmd[:3] == ["gh", "issue", "create"]:
            return Proc(cmd, 0, f"https://github.com/o/r/issues/{next(issue_numbers)}\n", "")
        return Proc(cmd, 0, "", "")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def _seed_vault(root) -> None:
    """A failed lesson plus an unrelated one - the smallest honest corpus."""
    directory = root / "knowledge" / "lessons"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "2026-01-04-vector-memory.md").write_text(
        "---\ntags:\n  - lesson\n  - outcome/fail\n  - kind/implement\n"
        "created: 2026-01-04\n---\n\n"
        "# feat: per-worker persistent vector memory of past runs\n\n"
        f"## Lesson learned\n{FAILED_IDEA} It was abandoned: the store never stayed fresh.\n"
    )
    (directory / "2026-01-05-quota.md").write_text(
        "---\ntags:\n  - lesson\n  - outcome/pass\n  - kind/implement\n"
        "created: 2026-01-05\n---\n\n# feat: quota ledger\n\n## Lesson learned\nCost telemetry.\n"
    )


def _created_bodies(runner) -> list[str]:
    return [
        c[c.index("--body") + 1] for c in runner.calls if c[:3] == ["gh", "issue", "create"]
    ]


def test_every_filed_ticket_carries_a_prior_art_section(tmp_path):
    _seed_vault(tmp_path)
    runner = _prior_art_runner(RESTATED_AND_NOVEL_OUTPUT)

    res = synthesize(_cfg(), cycle_index=0, root=str(tmp_path), runner=runner, ai_runner=runner)

    assert res.filed
    bodies = _created_bodies(runner)
    assert bodies and all("## Prior art" in b for b in bodies)
    for body in bodies:
        assert "[[" in body or NO_PRIOR_ART in body


def test_filed_wikilinks_resolve_to_notes_that_exist(tmp_path):
    """An invented citation would be a dead link in the Obsidian graph."""
    _seed_vault(tmp_path)
    runner = _prior_art_runner(RESTATED_AND_NOVEL_OUTPUT)
    synthesize(_cfg(), cycle_index=0, root=str(tmp_path), runner=runner, ai_runner=runner)

    on_disk = {p.stem for p in (tmp_path / "knowledge" / "lessons").glob("*.md")}
    for body in _created_bodies(runner):
        for name in re.findall(r"\[\[([^\]]+)\]\]", body):
            assert name in on_disk
    assert "2099-12-31-invented" not in "\n".join(_created_bodies(runner))


def test_a_ticket_with_no_prior_art_says_so_explicitly(tmp_path):
    """Empty vault: "we looked and found none" must be stated, not implied."""
    runner = _prior_art_runner(RESTATED_AND_NOVEL_OUTPUT)
    synthesize(_cfg(), cycle_index=0, root=str(tmp_path), runner=runner, ai_runner=runner)

    bodies = _created_bodies(runner)
    assert bodies
    assert all(f"## Prior art\n{NO_PRIOR_ART}" in b for b in bodies)


def test_synthesis_drops_a_candidate_that_restates_a_failed_lesson(tmp_path):
    _seed_vault(tmp_path)
    runner = _prior_art_runner(RESTATED_AND_NOVEL_OUTPUT)

    res = synthesize(_cfg(), cycle_index=0, root=str(tmp_path), runner=runner, ai_runner=runner)

    titles = [
        c[c.index("--title") + 1] for c in runner.calls if c[:3] == ["gh", "issue", "create"]
    ]
    assert "feat: agent situation store" not in titles
    assert "feat: signed provenance attestation per merged pull request" in titles
    assert res.risk_dropped == 1

    # Both halves of the verdict are recorded: the flag AND the decision.
    dropped = next(f for f in res.risk_flags if f.startswith("feat: agent situation store"))
    assert "drop" in dropped
    assert "[[2026-01-04-vector-memory]]" in dropped
    kept = next(f for f in res.risk_flags if f.startswith("feat: signed provenance"))
    assert "keep" in kept


# --- deeper mining: workflow CONTENTS, templates, label taxonomy -------------

WORKFLOW_YAML = (
    "name: pr-size\non: pull_request\njobs:\n  size:\n    runs-on: ubuntu-latest\n"
    "    steps:\n      - uses: actions/labeler@v5\n"
)

FAKE_REPO = {
    "readme": "# crewAI\nFramework for orchestrating role-playing agents.\n",
    "commits": "feat: record the running release on every emitted span\nfix: retry\n",
    "labels": "bug\nenhancement\ngood first issue\n",
    "dirs": {
        ".github/workflows": (
            "vulnerability-scan.yml\npr-title.yml\npr-size.yml\ntype-checker.yml\n"
        ),
        ".github/ISSUE_TEMPLATE": "bug.yml\nfeature.yml\n",
    },
    "files": {
        ".github/workflows/pr-size.yml": WORKFLOW_YAML,
        ".github/workflows/pr-title.yml": "name: pr-title\n",
        ".github/workflows/type-checker.yml": "name: type-checker\n",
        ".github/workflows/vulnerability-scan.yml": "name: vulnerability-scan\n",
        ".github/ISSUE_TEMPLATE/bug.yml": "name: Bug report\nbody:\n  - type: textarea\n",
        ".github/ISSUE_TEMPLATE/feature.yml": "name: Feature request\n",
        ".github/PULL_REQUEST_TEMPLATE.md": "## Why\n## How\n## Checklist\n",
    },
}


def _mining_runner(repo_data=None):
    """A fake `gh api` serving one repo's README, commits, tree and labels."""
    data = FAKE_REPO if repo_data is None else repo_data
    calls: list[list[str]] = []

    def runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        calls.append(list(cmd))
        if cmd[:2] != ["gh", "api"]:
            return Proc(cmd, 0, "", "")
        path = cmd[2]
        if path.endswith("/readme"):
            return Proc(cmd, 0, data["readme"], "")
        if "/commits?" in path:
            return Proc(cmd, 0, data["commits"], "")
        if "/labels?" in path:
            return Proc(cmd, 0, data["labels"], "")
        rel = path.split("/contents/", 1)[1] if "/contents/" in path else ""
        source = data["files"] if "-H" in cmd else data["dirs"]
        body = source.get(rel)
        return Proc(cmd, 0, body, "") if body else Proc(cmd, 1, "", "Not Found")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def test_context_pack_fetches_workflow_contents_not_just_filenames():
    pack = build_context_pack(["crewAIInc/crewAI"], runner=_mining_runner())
    digest = pack.sections["crewAIInc/crewAI"]

    # the inventory survives...
    assert "CI workflows:" in digest
    assert "vulnerability-scan.yml" in digest
    # ...but the point is what the workflow actually gates on
    assert "Workflow `pr-size.yml`" in digest
    assert "uses: actions/labeler@v5" in digest


def test_context_pack_caps_how_many_workflow_bodies_it_pulls():
    """Bodies are capped by count as well as size; the inventory still lists all."""
    pack = build_context_pack(["crewAIInc/crewAI"], runner=_mining_runner())
    digest = pack.sections["crewAIInc/crewAI"]

    fetched = sorted(
        name for name in FAKE_REPO["dirs"][".github/workflows"].split()
        if f"Workflow `{name}`" in digest
    )
    # sorted order decides WHICH three, so the pack is reproducible run to run
    assert fetched == ["pr-size.yml", "pr-title.yml", "type-checker.yml"]
    assert "Workflow `vulnerability-scan.yml`" not in digest


def test_context_pack_fetches_issue_and_pr_templates_and_labels():
    digest = build_context_pack(
        ["crewAIInc/crewAI"], runner=_mining_runner()
    ).sections["crewAIInc/crewAI"]

    assert "Issue template `bug.yml`" in digest
    assert "type: textarea" in digest
    assert "PR template" in digest and "## Checklist" in digest
    assert "Label taxonomy:" in digest and "good first issue" in digest


def test_context_pack_size_caps_every_section():
    huge = dict(FAKE_REPO)
    huge["readme"] = "R" * (README_CHARS + 500)
    huge["labels"] = "\n".join(f"label-{i}" for i in range(500))
    huge["files"] = dict(FAKE_REPO["files"], **{
        ".github/workflows/pr-size.yml": "W" * (WORKFLOW_BODY_CHARS + 500),
    })

    digest = build_context_pack(
        ["crewAIInc/crewAI"], runner=_mining_runner(huge)
    ).sections["crewAIInc/crewAI"]

    assert "R" * README_CHARS in digest
    assert "R" * (README_CHARS + 1) not in digest
    assert "W" * WORKFLOW_BODY_CHARS in digest
    assert "W" * (WORKFLOW_BODY_CHARS + 1) not in digest
    labels_section = digest.split("Label taxonomy:\n", 1)[1]
    assert len(labels_section) <= LABELS_CHARS


def test_context_pack_degrades_when_a_repo_has_no_ci_or_templates():
    bare = dict(FAKE_REPO, dirs={}, files={}, labels="")
    digest = build_context_pack(
        ["crewAIInc/crewAI"], runner=_mining_runner(bare)
    ).sections["crewAIInc/crewAI"]

    assert "README (truncated)" in digest
    assert "CI workflows:" not in digest
    assert "Issue template" not in digest
    assert "Label taxonomy:" not in digest


# --- PHASE 1 observations become durable catalog entries ---------------------

OBSERVED_OUTPUT = """PHASE 1 - DIVERGE: ten candidates considered.
```json
[
  {"practice_id": "crewaiinc-crewai--pr-size-gate", "title": "PR size gate in CI",
   "source_project": "crewAIInc/crewAI", "source_artifact": "ci_cd",
   "evidence": ".github/workflows/pr-size.yml labels a PR by diff size"},
  {"practice_id": "run-llama-llama-index--issue-classifier",
   "title": "automated issue classifier", "source_project": "run-llama/llama_index",
   "source_artifact": "issue_history",
   "evidence": ".github/workflows/issue_classifier.yml"}
]
```
PHASE 2 - REFLECT: one survived.
PHASE 3 - CONVERGE:
```json
[{"title": "feat: diff-size gate on every hsai pull request", "problem": "p",
  "proposal": "pp", "acceptance_criteria": ["a", "b", "c"],
  "verification_plan": ["v1", "v2"], "size": "M", "goal_ids": ["G4"],
  "synthesis_rationale": "combines crewAI + llama_index + MetaGPT",
  "practice_ids": ["crewaiinc-crewai--pr-size-gate"], "prior_art": []}]
```"""


def test_parse_observed_practices_reads_the_phase_one_catalog():
    observed = parse_observed_practices(OBSERVED_OUTPUT)
    assert [o.resolved_id() for o in observed] == [
        "crewaiinc-crewai--pr-size-gate", "run-llama-llama-index--issue-classifier",
    ]
    assert observed[0].source_artifact == "ci_cd"
    assert "pr-size.yml" in observed[0].evidence


def test_parse_observed_practices_never_mistakes_a_ticket_for_a_practice():
    """The PHASE 3 block has no source_project and always has acceptance criteria."""
    assert parse_observed_practices(PLAIN_TEXT_OUTPUT) == []
    assert parse_observed_practices("no json at all") == []
    assert parse_observed_practices("```json\n[{\"title\": \"no project\"}]\n```") == []


def _observing_runner(output: str = OBSERVED_OUTPUT):
    calls: list[list[str]] = []
    issue_numbers = iter(range(900, 999))

    def runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        calls.append(list(cmd))
        if cmd[:1] == ["claude"]:
            return Proc(cmd, 0, output, "")
        if cmd[:2] == ["git", "rev-parse"]:
            return Proc(cmd, 0, "abc1234\n", "")
        if cmd[:3] == ["gh", "issue", "create"]:
            return Proc(cmd, 0, f"https://github.com/o/r/issues/{next(issue_numbers)}\n", "")
        return Proc(cmd, 0, "", "")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def test_synthesize_writes_observed_practice_notes_that_reach_the_moc(tmp_path):
    """The `hsai synthesize` path, with a fake `gh`: the study becomes durable."""
    from hsai.knowledge import KnowledgeBase

    cfg = _cfg()
    runner = _observing_runner()
    res = synthesize(cfg, cycle_index=7, root=str(tmp_path), runner=runner, ai_runner=runner)

    notes = sorted(p.stem for p in (tmp_path / "knowledge" / "practices").glob("*.md"))
    assert notes == [
        "crewaiinc-crewai--pr-size-gate", "run-llama-llama-index--issue-classifier",
    ]
    assert sorted(res.observed) == notes

    catalog = {p.id: p for p in KnowledgeBase.from_config(cfg, tmp_path).read_practices()}
    # the practice a filed ticket cites is `proposed`, and names that ticket...
    proposed = catalog["crewaiinc-crewai--pr-size-gate"]
    assert proposed.status == "proposed"
    assert proposed.ticket == res.filed[0]
    assert res.proposed == ["crewaiinc-crewai--pr-size-gate"]
    # ...while one nobody ticketed stays `observed`, still remembered.
    assert catalog["run-llama-llama-index--issue-classifier"].status == "observed"

    # every note is stamped and dated to the cycle that saw it
    for practice in catalog.values():
        assert practice.first_seen_cycle == 7
        assert practice.provenance.startswith("hsai@abc1234 core.yaml@")

    moc = [p for p in KnowledgeBase.from_config(cfg, tmp_path).reindex_mocs()
           if p.name == "Practices MOC.md"][0].read_text()
    assert "[[crewaiinc-crewai--pr-size-gate]]" in moc
    assert "[[run-llama-llama-index--issue-classifier]]" in moc


def test_synthesize_re_observing_a_practice_does_not_duplicate_or_downgrade(tmp_path):
    from hsai.practices import load

    cfg = _cfg()
    first = _observing_runner()
    res_a = synthesize(cfg, cycle_index=7, root=str(tmp_path), runner=first, ai_runner=first)

    second = _observing_runner()
    res_b = synthesize(cfg, cycle_index=8, root=str(tmp_path), runner=second, ai_runner=second)

    assert res_b.observed == []          # nothing new was learned the second time
    assert res_b.proposed == []          # and the existing claim was not repointed
    assert len(list((tmp_path / "knowledge" / "practices").glob("*.md"))) == 2
    catalog = {p.id: p for p in load(tmp_path, cfg)}
    # still pointing at the FIRST ticket, and still first seen in cycle 7
    assert catalog["crewaiinc-crewai--pr-size-gate"].ticket == res_a.filed[0]
    assert catalog["crewaiinc-crewai--pr-size-gate"].first_seen_cycle == 7
    assert res_b.filed  # the ticket itself is still filed


# --- the studied repos travel with the ticket --------------------------------

def test_filed_tickets_record_the_repos_actually_studied(tmp_path):
    cfg = _cfg()
    runner = _observing_runner()
    res = synthesize(cfg, cycle_index=7, root=str(tmp_path), runner=runner, ai_runner=runner)

    body = _created_bodies(runner)[0]
    assert studied_repos(body) == tuple(res.studied)
    # and NOT the first three repos in config, which is what used to be claimed
    assert tuple(r.repo for r in cfg.reference_top10[:3]) != tuple(res.studied)
    assert "hsai@abc1234" in body   # harness provenance stamped on the ticket too
