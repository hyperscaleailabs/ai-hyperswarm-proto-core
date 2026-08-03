from hsai import guard
from hsai.config import load_config


def test_classify_flags_default_protected_paths():
    changed = ["src/hsai/ai.py", "src/hsai/new.py", "knowledge/lessons/x.md"]
    result = guard.classify(changed, guard.DEFAULT_PROTECTED_PATHS)
    assert result.protected == ("src/hsai/ai.py",)
    assert result.should_revert is True
    assert result.escaped is False


def test_classify_matches_by_prefix():
    changed = [".github/workflows/ci.yml", ".ai-swarm/core.yaml"]
    result = guard.classify(changed, guard.DEFAULT_PROTECTED_PATHS)
    assert set(result.protected) == set(changed)
    assert result.should_revert is True


def test_classify_allows_unprotected_changes():
    changed = ["src/hsai/new.py", "tests/test_new.py"]
    result = guard.classify(changed, guard.DEFAULT_PROTECTED_PATHS)
    assert result.protected == ()
    assert result.should_revert is False


def test_classify_escape_hatch_requires_both_label_and_protected_change():
    changed = ["src/hsai/ai.py"]
    # label alone, no protected change -> nothing to escape
    result = guard.classify([], guard.DEFAULT_PROTECTED_PATHS, labels=(guard.ESCAPE_HATCH_LABEL,))
    assert result.protected == ()
    assert result.escaped is False

    # protected change + label -> escaped, not reverted
    result = guard.classify(changed, guard.DEFAULT_PROTECTED_PATHS, labels=(guard.ESCAPE_HATCH_LABEL,))
    assert result.protected == ("src/hsai/ai.py",)
    assert result.escaped is True
    assert result.should_revert is False

    # protected change, no label -> reverted
    result = guard.classify(changed, guard.DEFAULT_PROTECTED_PATHS, labels=())
    assert result.should_revert is True


def test_protected_paths_for_reads_config_or_defaults():
    cfg = load_config()
    paths = guard.protected_paths_for(cfg)
    assert ".github/workflows/" in paths
    assert "src/hsai/ai.py" in paths
    assert "src/hsai/guard.py" in paths
    assert ".ai-swarm/core.yaml" in paths
