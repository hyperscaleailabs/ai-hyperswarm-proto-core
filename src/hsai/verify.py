"""Worker self-verification: parse the `<<<HSAI-VERIFY>>>` block every worker
prompt now demands (see `orchestrator._task_prompt`) and reconcile it against
the orchestrator's own authoritative `ci.run_local` result.

Before this module, `_task_prompt` told every worker to "ensure `ruff check .`
and `pytest` both pass" with no enumerated permission to run either (see
`.claude/settings.json` / `execution.worker_tools` in core.yaml) and no way to
tell, after the fact, whether the worker actually ran them. This closes that
gap on the READ side: it turns the worker's free-text claim into one of three
closed statuses -

- ``verified-agree``    - the worker's self-reported result matches ours
- ``verified-disagree`` - the worker claimed a result our own CI contradicts
- ``unverified``        - no parseable claim was made at all

The orchestrator's `ci.run_local` remains the sole merge gate; this is
observability, never a second gate. A worker's claim can never open, widen, or
close the merge decision - only its own local/remote CI results can.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

START = "<<<HSAI-VERIFY>>>"
END = "<<<HSAI-VERIFY-END>>>"

VERIFIED_AGREE = "verified-agree"
VERIFIED_DISAGREE = "verified-disagree"
UNVERIFIED = "unverified"

STATUSES = (VERIFIED_AGREE, VERIFIED_DISAGREE, UNVERIFIED)

# The worker-facing instruction appended to every task prompt (see
# orchestrator._task_prompt). Gives the exact delimiters and line shape so the
# parser below is not guessing at free-form prose.
PROMPT_INSTRUCTIONS = (
    "Before you finish, run `ruff check .` and `pytest` yourself and report the "
    "REAL exit code of each - never assert a result you did not personally "
    "observe. End your final message with exactly this block (one line per "
    "command you ran, in the form `<command>: exit <code>`):\n"
    f"{START}\n"
    "ruff check .: exit 0\n"
    "pytest: exit 0\n"
    f"{END}"
)

_BLOCK_RE = re.compile(re.escape(START) + r"(.*?)" + re.escape(END), re.DOTALL)
_LINE_RE = re.compile(r"^\s*(.+?)\s*:\s*exit\s+(-?\d+)\s*$", re.IGNORECASE | re.MULTILINE)


@dataclass(frozen=True)
class VerifyClaim:
    """The worker's self-reported commands, in the order it reported them."""

    commands: tuple[tuple[str, int], ...]

    @property
    def ok(self) -> bool:
        """The worker's own claim: every command it reported exited 0."""
        return bool(self.commands) and all(code == 0 for _, code in self.commands)

    def render(self) -> str:
        if not self.commands:
            return "_(no commands reported)_"
        return "; ".join(f"`{cmd}`=exit {code}" for cmd, code in self.commands)


def parse_claim(text: str) -> VerifyClaim | None:
    """Extract the worker's self-reported verification block, if any.

    Returns ``None`` when the delimited block is absent, or present but empty
    of parseable ``<command>: exit <code>`` lines - both collapse to
    :data:`UNVERIFIED` at the call site, since an unparseable claim is exactly
    as untrustworthy as a missing one.
    """
    match = _BLOCK_RE.search(text or "")
    if not match:
        return None
    commands = tuple(
        (cmd.strip(), int(code)) for cmd, code in _LINE_RE.findall(match.group(1))
    )
    return VerifyClaim(commands=commands) if commands else None


def compare(text: str, ci_ok: bool) -> tuple[str, VerifyClaim | None]:
    """Reconcile the worker's claim against ``ci_ok`` (the orchestrator's own
    `ci.run_local` result - never replaced by this comparison).
    """
    claim = parse_claim(text)
    if claim is None:
        return UNVERIFIED, None
    return (VERIFIED_AGREE if claim.ok == ci_ok else VERIFIED_DISAGREE), claim
