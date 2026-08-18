"""Load and validate .ai-swarm/core.yaml into typed objects."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CORE_PATH = ".ai-swarm/core.yaml"


@dataclass(frozen=True)
class ModelTier:
    name: str
    model: str
    use_for: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReferenceRepo:
    rank: int
    repo: str
    stars: int
    license: str
    note: str = ""


@dataclass(frozen=True)
class CoreConfig:
    raw: dict[str, Any]
    name: str
    owner: str
    license: str
    mission: str
    vision: str
    goals: tuple[dict[str, Any], ...]
    max_parallel: int
    proven_at: int
    ramp_target: int
    default_branch: str
    worktrees_dir: str
    permission_mode: str
    output_format: str
    trajectory_retention_blocks: int
    agent_timeout: float | None
    ci_remote_timeout: float
    ci_poll_interval: float
    max_ticket_attempts: int
    tiers: dict[str, ModelTier]
    default_tier: str
    constraints: dict[str, Any]
    knowledge: dict[str, Any]
    reference_top10: tuple[ReferenceRepo, ...]
    governance: dict[str, Any]
    cycle: dict[str, Any]
    synthesis: dict[str, Any]
    budget: dict[str, Any]
    review: dict[str, Any]
    postmortem: dict[str, Any]
    janitor: dict[str, Any]
    personas: tuple[dict[str, Any], ...]

    # --- convenience accessors -------------------------------------------------
    @property
    def repo_slug(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def forbidden_env(self) -> tuple[str, ...]:
        return tuple(self.constraints.get("forbid_env", []) or [])

    @property
    def subscription_only(self) -> bool:
        return bool(self.constraints.get("subscription_only", True))

    def goal_ids(self) -> list[str]:
        return [str(g.get("id")) for g in self.goals if g.get("id")]


def _find_core(start: str | Path | None = None) -> Path:
    """Walk upward from ``start`` (or cwd) until a core.yaml is found."""
    here = Path(start or Path.cwd()).resolve()
    for base in [here, *here.parents]:
        candidate = base / CORE_PATH
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"Could not locate {CORE_PATH} from {here}")


def load_config(path: str | Path | None = None) -> CoreConfig:
    """Load core.yaml. If ``path`` is a directory or None, search upward."""
    if path is not None and Path(path).is_file():
        core_file = Path(path)
    else:
        core_file = _find_core(path)

    data = yaml.safe_load(core_file.read_text()) or {}
    identity = data.get("identity", {})
    execution = data.get("execution", {})
    ramp = execution.get("ramp", {})
    models = data.get("models", {})
    tiers_raw = models.get("tiers", {})

    tiers = {
        name: ModelTier(name=name, model=t["model"], use_for=tuple(t.get("use_for", [])))
        for name, t in tiers_raw.items()
    }

    ref = data.get("reference_set", {})
    top10 = tuple(
        ReferenceRepo(
            rank=r.get("rank", 0),
            repo=r["repo"],
            stars=r.get("stars", 0),
            license=r.get("license", ""),
            note=r.get("note", ""),
        )
        for r in ref.get("top10", [])
    )

    return CoreConfig(
        raw=data,
        name=identity.get("name", "ai-hyperswarm-proto-core"),
        owner=identity.get("owner", ""),
        license=identity.get("license", "Apache-2.0"),
        mission=str(data.get("mission", "")).strip(),
        vision=str(data.get("vision", "")).strip(),
        goals=tuple(data.get("goals", [])),
        max_parallel=int(execution.get("max_parallel", 1)),
        proven_at=int(ramp.get("proven_at", 1)),
        ramp_target=int(ramp.get("target", execution.get("max_parallel", 1))),
        default_branch=execution.get("default_branch", "main"),
        worktrees_dir=execution.get("worktrees_dir", ".hsai/worktrees"),
        permission_mode=execution.get("permission_mode", "acceptEdits"),
        # Config-driven so a `claude` CLI change can be worked around by editing
        # YAML instead of shipping code: "text" (or empty) drops the flag entirely.
        output_format=str(execution.get("output_format", "json") or ""),
        trajectory_retention_blocks=int(execution.get("trajectory_retention_blocks", 8)),
        agent_timeout=execution.get("agent_timeout_seconds"),
        ci_remote_timeout=float(execution.get("ci_remote_timeout_seconds", 300)),
        ci_poll_interval=float(execution.get("ci_poll_interval_seconds", 10)),
        max_ticket_attempts=int(execution.get("max_ticket_attempts", 2)),
        tiers=tiers,
        default_tier=models.get("default_tier", "standard"),
        constraints=data.get("constraints", {}),
        knowledge=data.get("knowledge", {}),
        reference_top10=top10,
        governance=data.get("governance", {}),
        cycle=data.get("cycle", {}),
        synthesis=data.get("synthesis", {}),
        budget=data.get("budget", {}),
        review=data.get("review", {}),
        postmortem=data.get("postmortem", {}),
        janitor=data.get("janitor", {}),
        personas=tuple(data.get("personas", [])),
    )


@dataclass
class ValidationResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate(cfg: CoreConfig) -> ValidationResult:
    """Sanity-check invariants the orchestrator relies on."""
    errors: list[str] = []
    warnings: list[str] = []

    if not cfg.owner:
        errors.append("identity.owner is required")
    if cfg.default_tier not in cfg.tiers:
        errors.append(f"models.default_tier '{cfg.default_tier}' is not a defined tier")
    if cfg.max_parallel < 1:
        errors.append("execution.max_parallel must be >= 1")
    if cfg.proven_at < 1:
        errors.append("execution.ramp.proven_at must be >= 1")
    if not cfg.subscription_only:
        warnings.append("constraints.subscription_only is false; API billing may occur")
    if "ANTHROPIC_API_KEY" not in cfg.forbidden_env:
        warnings.append("ANTHROPIC_API_KEY not in constraints.forbid_env")
    if len(cfg.reference_top10) < 10:
        warnings.append(f"reference_set.top10 has {len(cfg.reference_top10)} entries (< 10)")

    return ValidationResult(ok=not errors, errors=errors, warnings=warnings)
