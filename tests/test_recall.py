"""Retrieval-backed lesson memory: ranking, failure weighting, budget, wiring."""
import ast
import sys
from pathlib import Path

from hsai.config import load_config
from hsai.knowledge import KnowledgeBase, Lesson, Recalled, recall_for
from hsai.models import ModelChoice
from hsai.orchestrator import HEAL, IMPLEMENT, IMPROVE, _task_prompt, build_pr_body
from hsai.recall import (
    DEFAULT_CHAR_BUDGET,
    HEADING,
    BM25Index,
    Note,
    recall,
    render_block,
    tokenize,
)
from hsai.synthesis import ContextPack, build_prompt

# A fixture corpus of six notes. Three of them talk about CI to some degree, so
# the expected ranking is a real ordering rather than "the only match wins".
CORPUS = [
    Note(
        note_name="2026-07-26-ci-parity",
        title="Loop reliability: retry/recovery and CI parity",
        outcome="pass",
        kind="improve",
        tags=("lesson", "reliability"),
        lesson_text=(
            "Gating on remote CI closes the trust gap: a worker that edits the "
            "workflow files makes local and remote CI diverge."
        ),
        what_happened="Edits under .github/workflows are reverted before commit.",
    ),
    Note(
        note_name="2026-07-25-bootstrap",
        title="Bootstrap the hsai loop",
        outcome="pass",
        kind="implement",
        tags=("lesson", "bootstrap"),
        lesson_text="Start from a worktree per iteration, run local CI before opening a PR.",
        what_happened="Scaffolded the orchestrator, config loader and knowledge base.",
    ),
    Note(
        note_name="2026-07-26-knowledge-only-diff",
        title="A haiku worker closed a feature ticket with a code-free diff",
        outcome="fail",
        kind="implement",
        tags=("lesson", "completeness"),
        lesson_text="A feature ticket needs real code; a knowledge-only diff is a failure.",
        what_happened="The completeness guard now rejects a knowledge-only diff.",
    ),
    Note(
        note_name="2026-07-26-model-selection",
        title="Task-complexity based model selection",
        outcome="pass",
        kind="implement",
        tags=("lesson", "models"),
        lesson_text="Pick the tier from the task, not from a fixed default.",
        what_happened="Added a heuristic selector with a rationale string.",
    ),
    Note(
        note_name="2026-07-26-budget-ledger",
        title="Quota and cost telemetry ledger",
        outcome="pass",
        kind="implement",
        tags=("lesson", "budget"),
        lesson_text="Warn then halt on a per-block budget instead of aborting in flight.",
        what_happened="Every iteration appends a cost record after remote CI concludes.",
    ),
    Note(
        note_name="2026-07-26-whitepaper-synth",
        title="Deepen the whitepaper synthesizer",
        outcome="pass",
        kind="implement",
        tags=("lesson", "knowledge"),
        lesson_text="Group lessons by outcome and surface themes recurring across notes.",
        what_happened="Counted outcomes and kinds over a window of lessons.",
    ),
]

CI_QUERY = "workflow edits diverged local and remote CI"


def test_recall_module_imports_only_the_standard_library():
    """No new dependency: recall.py stays pure stdlib and package-independent."""
    source = Path(__file__).resolve().parents[1] / "src" / "hsai" / "recall.py"
    tree = ast.parse(source.read_text())
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules += [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "recall.py must not import from the hsai package"
            modules.append((node.module or "").split(".")[0])
    assert modules, "expected at least one import"
    for module in modules:
        if module == "__future__":
            continue
        assert module in sys.stdlib_module_names, f"{module} is not in the standard library"


def test_tokenize_drops_stopwords_and_folds_plurals():
    assert tokenize("The workflows and the Edits") == ["workflow", "edit"]
    assert tokenize("") == []


def test_ranking_order_on_the_fixture_corpus():
    hits = recall(CI_QUERY, CORPUS, k=3)
    assert [record.note_name for record, _ in hits] == [
        "2026-07-26-ci-parity",     # workflow + edits + local + remote + ci
        "2026-07-25-bootstrap",     # local + ci
        "2026-07-26-budget-ledger",  # remote + ci
    ]
    scores = [score for _, score in hits]
    assert scores == sorted(scores, reverse=True)
    assert all(score > 0 for score in scores)

    # a different query retrieves a different note - the index is not a constant
    top = recall("model tier selection for a task", CORPUS, k=1)
    assert top[0][0].note_name == "2026-07-26-model-selection"


def test_k_bounds_the_result_set():
    assert len(recall(CI_QUERY, CORPUS, k=1)) == 1
    assert recall(CI_QUERY, CORPUS, k=0) == []


def test_query_with_no_overlap_returns_nothing():
    assert recall("kubernetes helm rollout", CORPUS, k=3) == []


def test_failures_are_boosted_over_identical_passes():
    passed = Note(
        note_name="a-pass", title="Ledger budget gate", outcome="pass",
        lesson_text="Warn then halt on the per-block budget.",
    )
    failed = Note(
        note_name="b-fail", title="Ledger budget gate", outcome="fail",
        lesson_text="Warn then halt on the per-block budget.",
    )
    hits = recall("budget gate", [passed, failed], k=2)
    assert [record.note_name for record, _ in hits] == ["b-fail", "a-pass"]
    assert hits[0][1] > hits[1][1]
    # ... and the preference is a parameter, not a hard-coded rule
    flipped = recall("budget gate", [passed, failed], k=2, prefer_outcome="pass")
    assert [record.note_name for record, _ in flipped] == ["a-pass", "b-fail"]


def test_empty_corpus_is_safe():
    index = BM25Index([])
    assert len(index) == 0
    assert index.search("anything") == []
    assert recall("anything", [], k=3) == []
    assert render_block([]) == ""


def test_render_block_names_notes_and_respects_the_char_budget():
    hits = recall(CI_QUERY, CORPUS, k=3)
    block = render_block(hits, char_budget=DEFAULT_CHAR_BUDGET)
    assert HEADING in block
    assert "[[2026-07-26-ci-parity]]" in block
    assert len(block) <= DEFAULT_CHAR_BUDGET

    # An oversized leaf is clipped in place, never silently dropped, and the
    # budget holds at every size - including sizes smaller than the header.
    for budget in (1, 12, 60, 120, 300, 900):
        assert len(render_block(hits, char_budget=budget)) <= budget, f"budget {budget} exceeded"
    assert render_block(hits, char_budget=0) == ""


def _repo_with_lessons(root: Path) -> Path:
    kb = KnowledgeBase(root)
    for note in CORPUS:
        kb.write_lesson(
            Lesson(
                title=note.title,
                outcome=note.outcome,
                kind=note.kind,
                context="fixture",
                what_happened=note.what_happened,
                lesson=note.lesson_text,
            )
        )
    return root


def test_knowledge_base_recall_reads_lessons_and_whitepapers(tmp_path):
    kb = KnowledgeBase(_repo_with_lessons(tmp_path))
    kb.write_whitepaper(kb.synthesize_whitepaper())
    names = [record.note_name for record, _ in kb.recall(CI_QUERY, k=6)]
    assert any("loop-reliability" in name for name in names)

    papers = kb.read_whitepapers()
    assert papers and papers[0].kind == "whitepaper"
    assert papers[0].lesson_text  # the summary section is what gets indexed


def test_recall_for_honours_configured_budget_and_depth(tmp_path):
    cfg = load_config()  # a fresh config object per call - safe to mutate
    cfg.knowledge["recall_k"] = 2
    cfg.knowledge["recall_char_budget"] = 220
    got = recall_for(cfg, _repo_with_lessons(tmp_path), CI_QUERY)
    assert 0 < len(got.notes) <= 2
    assert len(got.block) <= 220
    assert HEADING in got.block


def test_task_prompt_injects_prior_lessons_for_every_kind(tmp_path):
    cfg = load_config()
    repo = _repo_with_lessons(tmp_path)
    for kind in (HEAL, IMPLEMENT, IMPROVE):
        prompt = _task_prompt(
            kind, cfg, "fix: local and remote CI diverge",
            "the workflow was edited by a worker", repo_dir=str(repo),
        )
        assert HEADING in prompt
        assert "[[" in prompt  # at least one note is named
        assert "hsai autonomous loop" in prompt  # the task instructions survive


def test_task_prompt_omits_the_section_when_the_lessons_dir_is_empty(tmp_path):
    cfg = load_config()
    prompt = _task_prompt(IMPLEMENT, cfg, "feat: something", "body", repo_dir=str(tmp_path))
    assert HEADING not in prompt
    assert prompt.startswith("You are a worker")


def test_task_prompt_never_exceeds_the_configured_char_budget(tmp_path):
    cfg = load_config()
    budget = 280  # smaller than the notes that match, so truncation really happens
    cfg.knowledge["recall_char_budget"] = budget
    repo = _repo_with_lessons(tmp_path)
    title, body = "fix: local and remote CI diverge", "the workflow was edited"

    with_recall = _task_prompt(IMPLEMENT, cfg, title, body, repo_dir=str(repo))
    bare = _task_prompt(IMPLEMENT, cfg, title, body, recalled=Recalled())

    # the injected block is exactly the difference, and it fits the budget
    assert with_recall.endswith(bare)
    injected = with_recall[: -len(bare)]
    assert HEADING in injected
    assert "[[" in injected  # clipped, but the recalled note is still named
    assert len(injected) <= budget + 1  # +1 for the newline separating block from task


def test_synthesis_prompt_carries_the_same_block(tmp_path):
    cfg = load_config()
    pack = ContextPack(repos=["run-llama/llama_index"], sections={"run-llama/llama_index": "d"})

    prompt = build_prompt(cfg, pack, repo_dir=str(_repo_with_lessons(tmp_path / "repo")))
    assert HEADING in prompt
    assert "PHASE 1 - DIVERGE" in prompt  # the planner instructions are intact

    assert HEADING not in build_prompt(cfg, pack, repo_dir=str(tmp_path / "empty"))


def test_recalled_notes_are_rendered_in_the_lesson_and_the_pr_body(tmp_path):
    kb = KnowledgeBase(tmp_path)
    choice = ModelChoice(tier="standard", model="sonnet", rationale="r", strategy="s")
    lesson = Lesson(
        title="implement: feat: recall",
        outcome="pass",
        kind="implement",
        context="ctx",
        what_happened="wired retrieval into the prompts",
        lesson="the loop now reads what it wrote",
        ticket=42,
        recalled=("2026-07-26-ci-parity", "2026-07-26-knowledge-only-diff"),
    )
    text = kb.write_lesson(lesson).read_text()
    assert "## Recalled (prior notes that informed this work)" in text
    assert "- [[2026-07-26-ci-parity]]" in text

    body = build_pr_body(
        ticket=42, choice=choice, lesson_note=lesson.note_name(),
        lesson_summary=lesson.lesson, ci_summary="green", recalled=lesson.recalled,
    )
    assert "## Prior lessons recalled" in body
    assert "- [[2026-07-26-knowledge-only-diff]]" in body

    # with nothing recalled the section says so rather than lying by omission
    bare = build_pr_body(
        ticket=42, choice=choice, lesson_note="n", lesson_summary="l", ci_summary="green",
    )
    assert "_(nothing in the knowledge base matched)_" in bare


def test_real_repo_corpus_recalls_the_ci_divergence_lesson():
    """The acceptance case, against the lessons actually committed to this repo."""
    cfg = load_config()
    kb = KnowledgeBase.from_config(cfg, Path(__file__).resolve().parents[1])
    assert kb.lesson_notes(), "the repo ships a non-empty lesson corpus"
    names = [record.note_name for record, _ in kb.recall(CI_QUERY)]
    assert "2026-07-26-loop-reliability-retry-and-ci-parity" in names
    assert kb.recall("ci divergence")  # the CLI's acceptance query returns hits
