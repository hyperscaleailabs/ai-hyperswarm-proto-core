# ADR-0002: A governed CI-change channel replaces the blanket workflow revert

- **Status**: accepted
- **Date**: 2026-08-09
- **Source**: ticket "feat: governed CI-workflow change channel with local/remote
  parity enforcement" (G4, G2)

## Context
An early loop iteration added `mypy` to `.github/workflows/ci.yml` but not to
`ci.run_local`. Remote CI became stricter than the pre-flight the loop runs
before opening a PR, so the loop kept shipping changes it believed were green
and GitHub kept failing them. The response was blunt: `orchestrator.run_once`
reverted **every** path under `.github/workflows/` before committing.

That guard protected the right invariant with the wrong instrument.

1. **It made a whole class of G4 work structurally impossible.** Job timeouts,
   concurrency cancellation, tiered lanes, dependency scanning and promoting
   `hsai repro-check` to a required check all live in the workflow. The loop
   could file tickets for them and never implement them.
2. **It failed silently.** A worker that edited the workflow *and* one source
   file had its CI change stripped, still satisfied the completeness guard, and
   opened a PR whose stated intent was never delivered. Nothing in the PR body
   said so.
3. **It froze a file that accrues bugs.** Workflows break on their own
   (FoundationAgents/MetaGPT: `bugfix: Missing download info for
   actions/upload-artifact@v3`). "Never touch it" is not a safe default.

The reference set treats CI as product code that ships through the normal
review gate - crewAIInc/crewAI evolves its workflows continuously
(`ci: skip python heavy CI on Actions-only PRs`), and run-llama/llama_index
declares each gate (lint, unit_test, core-typecheck, coverage_check, codeql)
explicitly rather than leaving it implied.

## Decision
Replace the blanket revert with an audited channel, implemented as the pure
function `ciguard.classify_workflow_diff` and configured under `ci_policy` in
`.ai-swarm/core.yaml`. A workflow edit is committed only when **all** hold:

1. **Labelled.** The claimed ticket carries `ci-change`. The label is part of
   `github.ensure_labels`, so granting it is a deliberate, visible act during
   refinement - not something a worker can award itself mid-run.
2. **Required steps intact.** Every command in `ci_policy.required_steps`
   (seeded with `ruff check .` and `pytest`) is still declared by the workflow.
3. **Parity holds.** `ciguard.check_parity` compares the gates the workflow
   declares against `ci.local_commands()` - the same list `ci.run_local`
   executes - and both directions must be empty: no local gate may vanish from
   the workflow, and no remote-only gate may appear without a `run_local`
   counterpart. Steps in `ci_policy.parity_exempt_steps` (environment setup, and
   checks that read GitHub-only context such as the PR body) are exempt; every
   other `run:` step is a gate. `hsai ci-parity` runs the identical check
   standalone and exits non-zero on divergence.

The verdict is recorded three ways: in `IterationResult.notes`, in the lesson,
and in a `## CI change` section of the PR body naming every workflow file
touched. A stripped edit can no longer be invisible.

## Consequences
**Fails closed.** Anything unknown reverts exactly as before: no identifiable
ticket, a missing or non-`ci-change` label, a workflow that will not parse as a
GitHub Actions document, a missing required step, a parity gap, or a touched
workflow file outside `ci_policy.workflow_path` (a second lane is by
construction a remote gate with no local mirror; adopting one is a policy edit,
not a worker action). `ci.LOCAL_STEPS` is now the single definition of a green
build - `run_local` iterates it and the parity check reads it, so the two cannot
drift.

**Easier.** CI can finally be improved by the loop: adding a gate is legal in
one direction only - add it to `ci.LOCAL_STEPS` *and* the workflow in the same
`ci-change` PR, and parity passes.

**Harder.** Remote-only checks are now a deliberate configuration change
(`parity_exempt_steps`), reviewed as such.

**Untouched.** Subscription-only execution, ticket-per-PR, lesson-per-PR and
green-merge are unchanged: a rejected CI edit still produces a lesson and a
ticket-linked PR, and nothing merges without `ci.SUCCESS`.
