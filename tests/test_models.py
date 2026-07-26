from hsai.config import load_config
from hsai.models import Task, select


def _cfg():
    return load_config()


def test_docs_task_selects_light():
    cfg = _cfg()
    task = Task(kind="implement", title="docs: update README", labels=("documentation",))
    choice = select(task, cfg)
    assert choice.tier == "light"
    assert choice.model == cfg.tiers["light"].model


def test_architecture_task_selects_heavy():
    cfg = _cfg()
    task = Task(
        kind="improve",
        title="architecture: redesign the orchestrator",
        body="large refactor of concurrency model",
        est_files=10,
    )
    choice = select(task, cfg)
    assert choice.tier == "heavy"


def test_ordinary_feature_uses_default_tier():
    cfg = _cfg()
    task = Task(kind="implement", title="add a status subcommand", est_files=2)
    choice = select(task, cfg)
    assert choice.tier == cfg.default_tier


def test_choice_records_rationale_and_strategy():
    cfg = _cfg()
    choice = select(Task(kind="implement", title="add feature"), cfg)
    assert "score=" in choice.rationale
    assert choice.strategy == "heuristic-v0"
