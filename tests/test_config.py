from hsai.config import load_config, validate


def test_core_yaml_loads_and_is_valid():
    cfg = load_config()  # searches upward to repo root
    assert cfg.name == "ai-hyperswarm-proto-core"
    assert cfg.owner == "hyperscaleailabs"
    assert cfg.license == "Apache-2.0"
    v = validate(cfg)
    assert v.ok, f"config invalid: {v.errors}"


def test_execution_telemetry_keys():
    """The agent-output format and trajectory retention are config, not code."""
    cfg = load_config()
    assert cfg.output_format == "json"
    assert cfg.trajectory_retention_blocks >= 1


def test_execution_telemetry_defaults_when_keys_absent(tmp_path):
    core = tmp_path / ".ai-swarm"
    core.mkdir()
    (core / "core.yaml").write_text(
        "identity:\n  owner: someone\n"
        "models:\n  tiers:\n    standard:\n      model: sonnet\n"
        "  default_tier: standard\n"
    )
    cfg = load_config(core / "core.yaml")
    assert cfg.output_format == "json"          # structured envelope by default
    assert cfg.trajectory_retention_blocks == 8
    # An un-migrated config has no retry_policy at all and must still validate;
    # every class then falls back to the historical retry_same_tier behaviour.
    assert cfg.retry_policy == {}
    assert validate(cfg).ok


def test_retry_policy_is_loaded_from_core_yaml():
    cfg = load_config()
    assert cfg.retry_policy["default"] == "retry_same_tier"
    classes = cfg.retry_policy["classes"]
    assert classes["workflow_tamper"] == "block_immediately"
    assert classes["merge_conflict"] == "block_immediately"
    assert classes["test_failure"] == "retry_with_remediation"


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
