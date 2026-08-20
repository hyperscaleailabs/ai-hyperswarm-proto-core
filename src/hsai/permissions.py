"""The worker capability contract: the committed `.claude/settings.json`
permission profile that travels with every `claude -p` invocation (see
`ai.build_command`'s `--settings`/`--allowedTools` flags) and the `hsai doctor`
checks that keep it from silently drifting or widening.

Before this module, tool permissions inside a loop worktree were ambient -
whatever the CLI happened to allow from the ephemeral cwd - so `pytest`,
`ruff`, and `python` calls were denied in practice and nothing said so. This
gives the profile a single committed source of truth and three doctor-checked
failure modes: the file is missing, it grants an unrestricted Bash command
(defeats the whole point of enumerating a narrow set), or it has drifted from
`execution.worker_tools.allowed_tools` in core.yaml (the two would silently
diverge otherwise, since nothing else compares them).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_SETTINGS_FILE = ".claude/settings.json"

# A Bash entry with no meaningful restriction - it would let a worker run any
# command at all, which is exactly the ambient-permission problem this
# contract exists to close.
_WILDCARD_BASH = ("Bash", "Bash(*)", "Bash(*:*)")


@dataclass(frozen=True)
class ProfileCheck:
    ok: bool
    message: str


def load_allow_list(path: str | Path) -> list[str] | None:
    """The committed profile's `permissions.allow` list.

    ``None`` when the file is missing or is not valid JSON with the expected
    shape - never raises, so a bad profile is a doctor finding, not a crash.
    """
    p = Path(path)
    if not p.is_file():
        return None
    try:
        data = json.loads(p.read_text())
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    allow = (data.get("permissions") or {}).get("allow")
    if not isinstance(allow, list):
        return None
    return [str(a) for a in allow]


def is_wildcard_bash(entry: str) -> bool:
    """True for a Bash permission that is effectively unrestricted."""
    return entry.strip() in _WILDCARD_BASH


def check_profile(
    settings_path: str | Path, configured_tools: tuple[str, ...] | list[str]
) -> ProfileCheck:
    """`hsai doctor`'s worker-capability-contract check.

    Fails when the profile is absent/unreadable, contains a wildcard Bash
    entry, or its allow-list has drifted from ``configured_tools`` (i.e.
    ``execution.worker_tools.allowed_tools`` in core.yaml) - the two travel
    together only if something actually compares them.
    """
    allow = load_allow_list(settings_path)
    if allow is None:
        return ProfileCheck(False, f"{settings_path} is missing or unreadable")

    wildcards = [e for e in allow if is_wildcard_bash(e)]
    if wildcards:
        return ProfileCheck(
            False, f"wildcard Bash permission(s) in {settings_path}: {wildcards}"
        )

    configured = set(configured_tools)
    committed = set(allow)
    if configured != committed:
        missing = sorted(committed - configured)
        extra = sorted(configured - committed)
        detail = []
        if missing:
            detail.append(f"in {settings_path} but not in core.yaml worker_tools: {missing}")
        if extra:
            detail.append(f"in core.yaml worker_tools but not in {settings_path}: {extra}")
        return ProfileCheck(False, f"worker_tools profile drift - {'; '.join(detail)}")

    return ProfileCheck(
        True,
        f"{len(allow)} allowed command(s) in {settings_path}, no wildcard Bash, "
        "matches core.yaml worker_tools",
    )
