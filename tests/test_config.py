from hsai.config import load_config, validate


def test_core_yaml_loads_and_is_valid():
    cfg = load_config()  # searches upward to repo root
    assert cfg.name == "ai-hyperswarm-proto-core"
    assert cfg.owner == "hyperscaleailabs"
    assert cfg.license == "Apache-2.0"
    v = validate(cfg)
    assert v.ok, f"config invalid: {v.errors}"


def test_reference_set_meets_criteria():
    cfg = load_config()
    assert len(cfg.reference_top10) == 10
    for repo in cfg.reference_top10:
        assert repo.stars >= 10000, f"{repo.repo} below 10k stars"
        assert repo.license in {"MIT", "Apache-2.0"}, f"{repo.repo} not permissive"


def test_subscription_only_constraint_present():
    cfg = load_config()
    assert cfg.subscription_only is True
    assert "ANTHROPIC_API_KEY" in cfg.forbidden_env


def test_ramp_config():
    cfg = load_config()
    assert cfg.proven_at == 1
    assert cfg.ramp_target == 3
    assert cfg.max_parallel >= 1
