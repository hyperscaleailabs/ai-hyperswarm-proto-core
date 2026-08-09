"""Governed change channel for the GitHub Actions workflows.

The loop used to revert *every* edit under ``.github/workflows/`` because a
worker once added ``mypy`` remotely and made remote CI stricter than
``ci.run_local`` - a divergence nothing would have caught. That blunt revert
protected the right invariant with the wrong instrument: it also made CI itself
unimprovable by the loop, and it failed silently (the PR still shipped, minus
the change its ticket promised).

This module replaces it with a policy that keeps the invariant and opens the
channel. A workflow edit is committed only when ALL of the following hold:

1. the claimed ticket carries the ``ci-change`` label (an explicit, auditable
   opt-in the synthesis/refinement step must grant deliberately);
2. the resulting workflow still declares every command in
   ``ci_policy.required_steps``;
3. **local/remote parity** holds - every gate the workflow runs has a
   counterpart in :func:`hsai.ci.local_commands`, and every local gate is still
   declared remotely.

Everything here is pure: :func:`classify_workflow_diff` takes text in and
returns a verdict, so the orchestrator, the ``hsai ci-parity`` CLI and the unit
tests all exercise the identical decision. Anything unexpected - no ticket, an
unparseable workflow, a workflow file outside the governed lane - **fails
closed** and is reverted exactly as before.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import yaml

from .ci import local_commands

CI_CHANGE_LABEL = "ci-change"
WORKFLOW_PREFIX = ".github/workflows/"

DEFAULT_WORKFLOW = ".github/workflows/ci.yml"
DEFAULT_REQUIRED_STEPS: tuple[str, ...] = ("ruff check .", "pytest")
# Steps that cannot have a local counterpart by construction: environment
# preparation, and checks that read GitHub-only context (a PR body). They are
# exempt from parity but must still be listed here explicitly - an unlisted
# step is a gate, so forgetting to declare one fails closed.
DEFAULT_PARITY_EXEMPT_STEPS: tuple[str, ...] = (
    "Install",
    "SDLC evidence (PR body)",
)


@dataclass(frozen=True)
class CIPolicy:
    """The workflow-change policy, loaded from ``ci_policy`` in core.yaml."""

    change_label: str = CI_CHANGE_LABEL
    workflow_path: str = DEFAULT_WORKFLOW
    required_steps: tuple[str, ...] = DEFAULT_REQUIRED_STEPS
    parity_exempt_steps: tuple[str, ...] = DEFAULT_PARITY_EXEMPT_STEPS


def policy_from_config(cfg: Any) -> CIPolicy:
    """Build a :class:`CIPolicy` from a :class:`hsai.config.CoreConfig`."""
    raw = getattr(cfg, "ci_policy", None) or {}
    return CIPolicy(
        change_label=str(raw.get("change_label") or CI_CHANGE_LABEL),
        workflow_path=str(raw.get("workflow_path") or DEFAULT_WORKFLOW),
        required_steps=tuple(raw.get("required_steps") or DEFAULT_REQUIRED_STEPS),
        parity_exempt_steps=tuple(
            raw.get("parity_exempt_steps") or DEFAULT_PARITY_EXEMPT_STEPS
        ),
    )


# --- workflow parsing ---------------------------------------------------------
def _normalize(command: str) -> str:
    """Collapse a shell line to a comparable form."""
    return " ".join(command.replace("\\\n", " ").split()).rstrip(";")


def _run_commands(run_block: str) -> list[str]:
    """Split a step's ``run:`` block into individual normalized commands."""
    out: list[str] = []
    for line in str(run_block).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        out.append(_normalize(stripped))
    return out


@dataclass(frozen=True)
class WorkflowStep:
    name: str
    commands: tuple[str, ...]


class WorkflowParseError(ValueError):
    """The workflow could not be read as a GitHub Actions document."""


def parse_workflow(text: str) -> tuple[WorkflowStep, ...]:
    """Extract every ``run:`` step of a workflow, in declaration order.

    Raises :class:`WorkflowParseError` on anything that is not a workflow
    document - the caller is expected to treat that as a revert.
    """
    try:
        doc = yaml.safe_load(text or "")
    except yaml.YAMLError as exc:  # malformed YAML -> fail closed
        raise WorkflowParseError(f"workflow is not valid YAML: {exc}") from exc
    if not isinstance(doc, Mapping):
        raise WorkflowParseError("workflow is empty or not a mapping")
    jobs = doc.get("jobs")
    if not isinstance(jobs, Mapping) or not jobs:
        raise WorkflowParseError("workflow declares no jobs")

    steps: list[WorkflowStep] = []
    for job_name, job in jobs.items():
        if not isinstance(job, Mapping):
            raise WorkflowParseError(f"job '{job_name}' is not a mapping")
        for step in job.get("steps") or []:
            if not isinstance(step, Mapping):
                raise WorkflowParseError(f"job '{job_name}' has a malformed step")
            if "run" not in step:
                continue  # `uses:` actions declare no commands
            steps.append(
                WorkflowStep(
                    name=str(step.get("name") or "").strip(),
                    commands=tuple(_run_commands(step["run"])),
                )
            )
    return tuple(steps)


def workflow_commands(text: str) -> tuple[str, ...]:
    """Every command any step of the workflow runs."""
    return tuple(c for step in parse_workflow(text) for c in step.commands)


# --- parity -------------------------------------------------------------------
@dataclass(frozen=True)
class ParityDiff:
    """How the workflow's gates differ from what ``ci.run_local`` executes."""

    remote_only: tuple[str, ...] = ()   # declared remotely, never run locally
    local_only: tuple[str, ...] = ()    # run locally, no longer declared remotely
    mirrored: tuple[str, ...] = ()
    exempt: tuple[str, ...] = ()
    error: str = ""

    @property
    def ok(self) -> bool:
        return not (self.remote_only or self.local_only or self.error)

    def reason(self) -> str:
        if self.error:
            return self.error
        parts = []
        if self.local_only:
            parts.append(
                "local gate(s) not declared in the workflow: "
                + ", ".join(f"`{c}`" for c in self.local_only)
            )
        if self.remote_only:
            parts.append(
                "remote-only gate(s) with no `ci.run_local` counterpart: "
                + ", ".join(f"`{c}`" for c in self.remote_only)
            )
        if not parts:
            return (
                "local and remote CI agree on "
                f"{len(self.mirrored)} gate(s): "
                + ", ".join(f"`{c}`" for c in self.mirrored)
            )
        return "; ".join(parts)


def check_parity(
    workflow_text: str,
    local: Iterable[str],
    *,
    policy: CIPolicy | None = None,
) -> ParityDiff:
    """Compare a workflow's gates with the commands ``ci.run_local`` executes."""
    policy = policy or CIPolicy()
    local_cmds = [_normalize(c) for c in local]
    try:
        steps = parse_workflow(workflow_text)
    except WorkflowParseError as exc:
        return ParityDiff(error=str(exc))

    exempt_names = {n.strip() for n in policy.parity_exempt_steps}
    gates: list[str] = []
    exempt: list[str] = []
    for step in steps:
        (exempt if step.name in exempt_names else gates).extend(step.commands)

    remote_only = tuple(dict.fromkeys(c for c in gates if c not in local_cmds))
    local_only = tuple(c for c in local_cmds if c not in gates)
    mirrored = tuple(c for c in local_cmds if c in gates)
    return ParityDiff(
        remote_only=remote_only,
        local_only=local_only,
        mirrored=mirrored,
        exempt=tuple(dict.fromkeys(exempt)),
    )


def render_parity(diff: ParityDiff, *, workflow_path: str = DEFAULT_WORKFLOW) -> str:
    """A readable report for ``hsai ci-parity``."""
    lines = [
        f"ci-parity: {'AGREE' if diff.ok else 'DIVERGED'} "
        f"({workflow_path} vs ci.run_local)"
    ]
    for cmd in diff.mirrored:
        lines.append(f"  mirrored     {cmd}")
    for cmd in diff.exempt:
        lines.append(f"  exempt       {cmd}")
    for cmd in diff.local_only:
        lines.append(f"  LOCAL ONLY   {cmd}  (workflow no longer declares it)")
    for cmd in diff.remote_only:
        lines.append(f"  REMOTE ONLY  {cmd}  (ci.run_local does not run it)")
    if diff.error:
        lines.append(f"  ERROR        {diff.error}")
    lines.append(f"  verdict: {diff.reason()}")
    return "\n".join(lines)


# --- verdict ------------------------------------------------------------------
@dataclass(frozen=True)
class WorkflowVerdict:
    """Allow or revert a set of workflow edits, with the reason on record."""

    allowed: bool
    reason: str
    paths: tuple[str, ...] = ()
    parity: ParityDiff | None = None

    @property
    def action(self) -> str:
        return "allow" if self.allowed else "revert"

    def render(self) -> str:
        """The ``## CI change`` section body for the PR (and the lesson)."""
        files = "\n".join(f"- `{p}`" for p in self.paths) or "- _(none)_"
        parity = self.parity.reason() if self.parity else "_(not evaluated)_"
        return (
            f"**Verdict**: `{self.action}` - {self.reason}\n\n"
            f"Workflow files touched:\n{files}\n\n"
            f"**Local/remote parity**: {parity}"
        )


def workflow_paths(paths: Iterable[str]) -> list[str]:
    """The subset of ``paths`` that live under ``.github/workflows/``."""
    return [p for p in paths if p.startswith(WORKFLOW_PREFIX)]


def classify_workflow_diff(
    paths: Iterable[str],
    ticket_labels: Iterable[str] | None,
    before_text: str,
    after_text: str,
    *,
    policy: CIPolicy | None = None,
    local: Iterable[str] | None = None,
) -> WorkflowVerdict:
    """Decide whether a set of workflow edits may be committed.

    ``paths`` are the changed paths under ``.github/workflows/``;
    ``before_text``/``after_text`` are the governed workflow
    (``policy.workflow_path``) as committed and as the agent left it.
    ``ticket_labels`` is ``None`` when no ticket could be identified, which -
    like every other unknown here - fails closed.
    """
    policy = policy or CIPolicy()
    local = local_commands() if local is None else local
    touched = tuple(dict.fromkeys(workflow_paths(paths)))

    if not touched:
        return WorkflowVerdict(True, "no workflow files were touched")

    # A no-op edit (a path listed but already restored) changes nothing and
    # needs no permission. Requires having actually read a workflow: two empty
    # sides mean the governed lane is missing, which fails closed below.
    if (
        touched == (policy.workflow_path,)
        and before_text == after_text
        and after_text.strip()
    ):
        return WorkflowVerdict(True, "workflow content is unchanged", touched)

    if ticket_labels is None:
        return WorkflowVerdict(
            False, "no ticket could be identified for this run (fail closed)", touched
        )
    if policy.change_label not in set(ticket_labels):
        return WorkflowVerdict(
            False,
            f"the claimed ticket does not carry the `{policy.change_label}` label",
            touched,
        )

    off_lane = [p for p in touched if p != policy.workflow_path]
    if off_lane:
        # A second workflow file is, by construction, a remote lane with no
        # `ci.run_local` counterpart. Adopting one is a deliberate policy
        # change (extend ci_policy.workflow_path), not something a worker does.
        return WorkflowVerdict(
            False,
            "workflow file(s) outside the governed lane "
            f"({policy.workflow_path}): {', '.join(off_lane)}",
            touched,
        )

    try:
        declared = workflow_commands(after_text)
    except WorkflowParseError as exc:
        return WorkflowVerdict(False, f"{exc} (fail closed)", touched)

    missing = [s for s in policy.required_steps if _normalize(s) not in declared]
    if missing:
        return WorkflowVerdict(
            False,
            "the edited workflow drops required step(s): "
            + ", ".join(f"`{s}`" for s in missing),
            touched,
        )

    parity = check_parity(after_text, local, policy=policy)
    if not parity.ok:
        return WorkflowVerdict(
            False, f"local/remote CI would diverge - {parity.reason()}", touched, parity
        )

    return WorkflowVerdict(
        True,
        f"`{policy.change_label}` ticket, required steps intact, parity holds",
        touched,
        parity,
    )
