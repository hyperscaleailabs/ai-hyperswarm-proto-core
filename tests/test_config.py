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


def test_protected_surfaces_are_declared_and_self_protecting():
    cfg = load_config()
    assert len(cfg.protected_surfaces) >= 5
    modes = {s["glob"]: s["mode"] for s in cfg.protected_surfaces}

    # the seeded surfaces from the ticket
    assert modes[".github/workflows/**"] == "revert"
    assert modes[".ai-swarm/core.yaml"] == "require_label"
    assert modes["knowledge/ledger/**"] == "deny"

    # the gate cannot quietly disable itself: policy.py and its tests, plus
    # the other guard modules it replaces, are require_label surfaces too
    for glob in ("src/hsai/policy.py", "tests/test_policy.py", "src/hsai/tickets.py",
                 "src/hsai/repro.py"):
        assert modes[glob] == "require_label"

    assert all(s.get("rationale") for s in cfg.protected_surfaces)
