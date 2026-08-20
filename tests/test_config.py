from hsai import review
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


def test_review_gate_is_configured_and_enabled():
    """The independent review gate is config, not code."""
    cfg = load_config()
    assert cfg.review["enabled"] is True
    assert cfg.review["max_blocking_findings"] >= 1
    assert cfg.review["timeout_seconds"] > 0
    # No tier reviews itself - that is the whole point of the gate.
    for author, reviewer in cfg.review["tier_policy"].items():
        assert reviewer != author
        assert reviewer in cfg.tiers


def test_review_defaults_to_enabled_when_the_block_is_absent(tmp_path):
    core = tmp_path / ".ai-swarm"
    core.mkdir()
    (core / "core.yaml").write_text(
        "identity:\n  owner: someone\n"
        "models:\n  tiers:\n    standard:\n      model: sonnet\n"
        "  default_tier: standard\n"
    )
    cfg = load_config(core / "core.yaml")
    assert cfg.review == {}
    assert review.is_enabled(cfg) is True     # additive by default, opt-out only


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


def test_worker_tools_capability_contract_is_configured():
    """The permission profile config that travels on the `claude -p` command
    vector (see hsai.ai.build_command) and is cross-checked by `hsai doctor`
    (see hsai.permissions)."""
    cfg = load_config()
    assert cfg.worker_tools_settings_file == ".claude/settings.json"
    assert cfg.worker_tools_allowed == (
        "Bash(ruff check:*)",
        "Bash(pytest:*)",
        "Bash(python -m pytest:*)",
        "Bash(git diff:*)",
        "Bash(git status:*)",
    )
    # No wildcard Bash grant, no network/gh access.
    assert not any(t in ("Bash", "Bash(*)", "Bash(*:*)") for t in cfg.worker_tools_allowed)
    assert not any("gh " in t or "curl" in t for t in cfg.worker_tools_allowed)


def test_worker_tools_default_when_absent(tmp_path):
    core = tmp_path / ".ai-swarm"
    core.mkdir()
    (core / "core.yaml").write_text(
        "identity:\n  owner: someone\n"
        "models:\n  tiers:\n    standard:\n      model: sonnet\n"
        "  default_tier: standard\n"
    )
    cfg = load_config(core / "core.yaml")
    assert cfg.worker_tools_settings_file == ""
    assert cfg.worker_tools_allowed == ()
