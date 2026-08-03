"""The tunable surface of model selection, as a versioned, reviewable file.

``models.py`` used to hard-code its keyword weights, file-count buckets and the
two tier thresholds. None of those numbers could be changed without editing
code, and none had ever been checked against an outcome. This module lifts them
into a :class:`SelectionPolicy` loaded from ``.ai-swarm/selection-policy.json``
so that:

- the calibrator (:mod:`hsai.calibrate`) can propose a bounded bump,
- the bump lands as a human-reviewable JSON diff in the governance PR,
- every PR records which policy version produced its routing.

The committed default reproduces heuristic-v1's decisions exactly; the built-in
:func:`default_policy` is the source of truth for that file and the fallback
when it is absent.

What is deliberately NOT here: the ``size:L`` / ``size:M`` label overrides, the
"feature-shaped work never routes light" guard, and the budget-gate demotion.
Those are invariants, not parameters - they live in :mod:`hsai.models` and no
calibration can touch them.

Synthesis: run-llama/llama_index (committed numeric thresholds reviewed like
code) and assafelovic/gpt-researcher (validate model-derived values before use).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

POLICY_PATH = ".ai-swarm/selection-policy.json"

# heuristic-v1's exact constants. Changing these changes the *fallback*; the
# committed policy file is what actually routes work.
DEFAULT_HEAVY_SIGNALS = (
    "architecture",
    "redesign",
    "large refactor",
    "rearchitect",
    "hard bug",
    "race condition",
    "concurrency",
    "security",
    "design",
    "migration",
    "refactor",
    "breaking",
)
DEFAULT_LIGHT_SIGNALS = (
    "typo",
    "docs",
    "documentation",
    "readme",
    "format",
    "lint",
    "rename",
    "comment",
    "index",
    "chore",
    "bump",
    "whitespace",
)
# (min_files, score_delta), highest bucket first; the last bucket must start at
# 0 so every task matches exactly one.
DEFAULT_FILE_BUCKETS = ((8, 3), (4, 1), (2, 0), (0, -1))
DEFAULT_KIND_WEIGHTS = {"heal": 2, "improve": 1}


class PolicyError(ValueError):
    """A policy file that does not satisfy the strict schema."""


@dataclass(frozen=True)
class SelectionPolicy:
    """Every number heuristic-v2 is allowed to learn, plus its version."""

    version: int = 1
    heavy_signals: tuple[str, ...] = DEFAULT_HEAVY_SIGNALS
    light_signals: tuple[str, ...] = DEFAULT_LIGHT_SIGNALS
    heavy_signal_weight: int = 2
    light_signal_weight: int = -2
    file_buckets: tuple[tuple[int, int], ...] = DEFAULT_FILE_BUCKETS
    kind_weights: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_KIND_WEIGHTS))
    narrow_docs_delta: int = -1
    heavy_threshold: int = 5
    light_threshold: int = -3
    notes: str = ""

    def kind_weight(self, kind: str) -> int:
        return int(self.kind_weights.get(kind, 0))

    def file_delta(self, est_files: int) -> int:
        """Score contribution of the file-count bucket ``est_files`` falls in."""
        for min_files, delta in self.file_buckets:
            if est_files >= min_files:
                return delta
        return 0

    def label(self) -> str:
        """Short, PR-ready identifier: what routed this change."""
        return f"policy v{self.version}"


def default_policy() -> SelectionPolicy:
    """heuristic-v1's exact values - the committed default and the fallback."""
    return SelectionPolicy()


# --- (de)serialization --------------------------------------------------------
def to_dict(policy: SelectionPolicy) -> dict[str, Any]:
    """Plain-JSON view, key order chosen so the committed file reads top-down."""
    return {
        "version": policy.version,
        "notes": policy.notes,
        "heavy_threshold": policy.heavy_threshold,
        "light_threshold": policy.light_threshold,
        "heavy_signal_weight": policy.heavy_signal_weight,
        "light_signal_weight": policy.light_signal_weight,
        "narrow_docs_delta": policy.narrow_docs_delta,
        "file_buckets": [[m, d] for m, d in policy.file_buckets],
        "kind_weights": dict(policy.kind_weights),
        "heavy_signals": list(policy.heavy_signals),
        "light_signals": list(policy.light_signals),
    }


_REQUIRED_KEYS = {
    "version",
    "heavy_threshold",
    "light_threshold",
    "heavy_signal_weight",
    "light_signal_weight",
    "narrow_docs_delta",
    "file_buckets",
    "kind_weights",
    "heavy_signals",
    "light_signals",
}
_OPTIONAL_KEYS = {"notes"}


def _int(data: dict[str, Any], key: str) -> int:
    value = data[key]
    # bool is an int subclass; a JSON true here is a schema error, not a 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise PolicyError(f"{key} must be an integer, got {value!r}")
    return value


def _signals(data: dict[str, Any], key: str) -> tuple[str, ...]:
    value = data[key]
    if not isinstance(value, list) or not value:
        raise PolicyError(f"{key} must be a non-empty list of strings")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise PolicyError(f"{key} entries must be non-empty strings, got {item!r}")
        if item != item.lower():
            raise PolicyError(f"{key} entries must be lowercase (matched case-insensitively): {item!r}")
        if item in out:
            raise PolicyError(f"{key} contains a duplicate entry: {item!r}")
        out.append(item)
    return tuple(out)


def _buckets(data: dict[str, Any]) -> tuple[tuple[int, int], ...]:
    value = data["file_buckets"]
    if not isinstance(value, list) or not value:
        raise PolicyError("file_buckets must be a non-empty list of [min_files, delta] pairs")
    out: list[tuple[int, int]] = []
    for pair in value:
        if (
            not isinstance(pair, list)
            or len(pair) != 2
            or any(isinstance(x, bool) or not isinstance(x, int) for x in pair)
        ):
            raise PolicyError(f"file_buckets entries must be [int, int] pairs, got {pair!r}")
        out.append((pair[0], pair[1]))
    if any(out[i][0] <= out[i + 1][0] for i in range(len(out) - 1)):
        raise PolicyError("file_buckets must be ordered by strictly descending min_files")
    if out[-1][0] != 0:
        raise PolicyError("file_buckets must end with a min_files=0 bucket so every task matches")
    return tuple(out)


def from_dict(data: Any) -> SelectionPolicy:
    """Parse + strictly validate a policy document (unknown keys are errors)."""
    if not isinstance(data, dict):
        raise PolicyError(f"policy must be a JSON object, got {type(data).__name__}")
    keys = set(data)
    missing = _REQUIRED_KEYS - keys
    if missing:
        raise PolicyError(f"policy is missing required keys: {sorted(missing)}")
    unknown = keys - _REQUIRED_KEYS - _OPTIONAL_KEYS
    if unknown:
        raise PolicyError(f"policy has unknown keys: {sorted(unknown)}")

    version = _int(data, "version")
    if version < 1:
        raise PolicyError("version must be >= 1")

    kinds = data["kind_weights"]
    if not isinstance(kinds, dict) or any(
        not isinstance(k, str) or isinstance(v, bool) or not isinstance(v, int)
        for k, v in kinds.items()
    ):
        raise PolicyError("kind_weights must be an object of {kind: integer}")

    notes = data.get("notes", "")
    if not isinstance(notes, str):
        raise PolicyError("notes must be a string")

    policy = SelectionPolicy(
        version=version,
        heavy_signals=_signals(data, "heavy_signals"),
        light_signals=_signals(data, "light_signals"),
        heavy_signal_weight=_int(data, "heavy_signal_weight"),
        light_signal_weight=_int(data, "light_signal_weight"),
        file_buckets=_buckets(data),
        kind_weights={k: int(v) for k, v in kinds.items()},
        narrow_docs_delta=_int(data, "narrow_docs_delta"),
        heavy_threshold=_int(data, "heavy_threshold"),
        light_threshold=_int(data, "light_threshold"),
        notes=notes,
    )
    if policy.heavy_signal_weight <= 0:
        raise PolicyError("heavy_signal_weight must be positive")
    if policy.light_signal_weight >= 0:
        raise PolicyError("light_signal_weight must be negative")
    if policy.narrow_docs_delta > 0:
        raise PolicyError("narrow_docs_delta must be <= 0")
    if policy.light_threshold >= policy.heavy_threshold:
        raise PolicyError("light_threshold must be below heavy_threshold")
    return policy


def render(policy: SelectionPolicy) -> str:
    """Serialize for the repo: stable key order, 2-space indent, trailing NL."""
    return json.dumps(to_dict(policy), indent=2) + "\n"


def write_policy(path: str | Path, policy: SelectionPolicy) -> Path:
    """Write ``policy`` as a reviewable JSON diff (validated on the way out)."""
    from_dict(to_dict(policy))  # never commit a file we would refuse to load
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(policy), encoding="utf-8")
    return path


def read_policy(path: str | Path) -> SelectionPolicy:
    """Load one specific policy file (raises :class:`PolicyError` if invalid)."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise PolicyError(f"{path} is not valid JSON: {exc}") from exc
    return from_dict(data)


def find_policy_file(start: str | Path | None = None) -> Path | None:
    """Walk upward from ``start`` (or cwd) looking for the policy file."""
    here = Path(start or Path.cwd()).resolve()
    for base in [here, *here.parents]:
        candidate = base / POLICY_PATH
        if candidate.is_file():
            return candidate
    return None


def load_policy(start: str | Path | None = None) -> SelectionPolicy:
    """Active policy for ``start``; falls back to heuristic-v1's defaults.

    A malformed file is a hard error - silently routing on a half-parsed policy
    would break the audit trail the PR body claims.
    """
    found = find_policy_file(start)
    return read_policy(found) if found else default_policy()


def policy_path(cfg: Any, repo_root: str | Path) -> Path:
    """Resolve the policy file for ``cfg`` under ``repo_root`` (may not exist)."""
    rel = (getattr(cfg, "calibration", None) or {}).get("policy_file", POLICY_PATH)
    return Path(repo_root) / rel
