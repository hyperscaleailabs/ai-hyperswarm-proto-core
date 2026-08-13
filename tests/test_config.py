from hsai import review
from hsai.config import load_config, validate


def test_core_yaml_loads_and_is_valid():
    cfg = load_config()  # searches upward to repo root
    assert cfg.name == "ai-hyperswarm-proto-core"
    assert cfg.owner == "hyperscaleailabs"
    assert cfg.license == "Apache-2.0"
    v = validate(cfg)
    assert v.ok, f"config invalid: {v.errors}"


def test_ci_contract_is_declared_in_the_manifest():
    """`ci.steps` is the one definition of a green build (see hsai.ci)."""
    cfg = load_config()
    steps = {s.id: s for s in cfg.ci_steps}
    assert steps["ruff"].command == ("ruff", "check", ".")
    assert steps["pytest"].command == ("pytest",)
    assert steps["ruff"].scope == "both" and steps["ruff"].required
    assert steps["sdlc-evidence"].scope == "remote"
    # repro-check needs a fetched base ref, so it is its own workflow job
    assert steps["repro-check"].job == "repro"
    assert steps["repro-check"].in_scope("remote") and not steps["repro-check"].in_scope("local")


def test_validate_rejects_a_malformed_ci_step(tmp_path):
    core = tmp_path / ".ai-swarm" / "core.yaml"
    core.parent.mkdir(parents=True)
    core.write_text(
        "identity:\n  name: t\n  owner: o\n"
        "models:\n  tiers:\n    standard:\n      model: sonnet\n  default_tier: standard\n"
        "ci:\n  steps:\n"
        "    - id: ruff\n      command: [ruff, check, .]\n      scope: everywhere\n"
        "    - id: ruff\n      command: [ruff]\n"
        "    - id: nocommand\n      command: []\n"
    )
    v = validate(load_config(core))
    assert not v.ok
    assert any("scope" in e for e in v.errors)
    assert any("duplicate id" in e for e in v.errors)
    assert any("needs both an id and a command" in e for e in v.errors)


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
