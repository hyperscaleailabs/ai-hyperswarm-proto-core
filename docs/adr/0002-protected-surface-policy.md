# ADR-0002: Protected-surface policy - guard integrity and the self-modification gate

- **Status**: accepted
- **Date**: 2026-08-10
- **Source**: synthesis ticket (G2, G4) combining OpenBMB/ChatDev,
  run-llama/llama_index, assafelovic/gpt-researcher, and SWE-agent/SWE-agent

## Context
The loop had already learned once that a worker can change the rules it is
judged by: an agent slipped `mypy` into the CI workflow, so
`orchestrator.run_once` grew a hard-coded revert of any `.github/workflows`
edit. That fix was one string check covering one path. Nothing stopped a
worker from raising the budget ceilings in `.ai-swarm/core.yaml`, weakening
the well-formedness rules in `tickets.py`, weakening the reproduce-before-fix
contract in `repro.py`, deleting tests to turn a red build green, or editing
the SDLC-evidence step in `ci.yml` - and every one of those diffs would
satisfy every *existing* gate, including the completeness guard and remote
CI. A self-improving loop that can edit its own guardrails has preferences,
not invariants, which put G2's traceability guarantees and G4's safety work
permanently at risk.

## Decision
1. **`protected_surfaces` in `core.yaml`** is the declared allowlist of what a
   diff may touch - a list of `{glob, mode, rationale}`. Untrusted input
   (here, our own agent's diff) is validated against a declared allowlist
   rather than trusted by default (OpenBMB/ChatDev's path-traversal-fix
   posture, generalized). Three modes:
   - `revert` - silently restored to `HEAD` before commit. Seeded with
     `.github/workflows/**`, preserving the exact pre-existing behaviour.
   - `require_label` - blocked unless the ticket carries `guards-approved`
     (the architect's explicit escape hatch - gpt-researcher's
     plugin-quality-gate shape: a declared contract a contribution must
     satisfy before admission). Seeded with `.ai-swarm/core.yaml`,
     `src/hsai/{policy,tickets,repro}.py`, and their tests.
   - `deny` - always blocked, no label can waive it. Seeded with
     `knowledge/ledger/**` (append-only).
2. **`src/hsai/policy.py`** holds a pure `evaluate(changed_paths, test_delta,
   ticket_labels, policy) -> PolicyVerdict` - glob matching, mode resolution,
   and the label escape hatch, with no I/O - plus the small I/O glue that
   gathers those facts from git. SWE-agent's constrained agent-computer
   interface: an autonomous agent is safest when the surface it can act on is
   deliberately bounded and declared.
3. **`orchestrator.run_once`** replaces the old ad-hoc workflow revert with
   `policy.evaluate`: revert-mode paths are restored exactly as before,
   deny-mode paths abort the iteration into the existing `_recover_failed`
   path with no PR opened, and require_label paths pass only with
   `guards-approved`. Violations are recorded in the lesson, the ledger
   record (`outcome=policy_violation`), and the iteration notes - auditable,
   not silent.
4. **Test-integrity guard.** Test functions are counted by AST parse (not by
   running collection, so it works on the base ref without installing it) on
   the base ref and the PR tree; a net decrease without `guards-approved` is
   a violation. Counting the whole test corpus rather than per-file is what
   makes a rename or a file move net zero.
5. **`hsai policy-check --base-ref origin/main`** mirrors `hsai repro-check`
   as a CI gate, so the policy binds human PRs too, not only the loop
   (run-llama/llama_index's repo-policy-as-CI posture: `codeql.yml`,
   `coverage_check.yml` - contribution rules as automated gates, not review
   conventions).
6. **The gate protects itself.** `policy.py`, its tests, and the
   `protected_surfaces` block in `core.yaml` are themselves `require_label`
   surfaces, so the gate cannot quietly disable itself.

## Consequences
- A worker that needs to touch a `require_label` surface (raise a budget
  ceiling, adjust the well-formedness rules, add a CI step) must get the
  ticket labelled `guards-approved` by the architect first; it cannot self
  -approve mid-run. This is friction by design.
- `.github/workflows/**` edits are still silently reverted, not blocked -
  changed intentionally in a *future* ticket if CI needs to evolve through
  the loop; for now it still requires a human/architect commit directly
  (a known bootstrap limitation this ADR does not remove).
- Enforced where: `orchestrator.run_once` (loop-side guard),
  `hsai policy-check` + the `policy-check` CI job (human-PR-side guard),
  `.ai-swarm/core.yaml` (the declared policy itself).
