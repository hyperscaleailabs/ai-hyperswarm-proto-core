# SDLC - every change leaves evidence

Every change in this repo, human- or agent-made, moves through five phases.
Each phase leaves **evidence** that CI and the orchestrator check; a change
without evidence does not merge.

| phase | what happens | evidence required |
| --- | --- | --- |
| **1. Plan** | A structured ticket exists: Problem, Proposal, Acceptance criteria (>= 2 checkboxes), Verification plan, size label | Ticket link (`Closes #N`) in the PR body; orchestrator refuses `needs-refinement` tickets |
| **2. Implement** | Code changes on an isolated branch/worktree; model recorded | `## Model used` section in the PR body; workflow edits pass the governed CI-change channel or are reverted (see below) |
| **3. Verify** | Local pre-flight: `ruff check .` + `pytest`; completeness guard (code tickets need code diffs); reproduce-before-fix guard for `heal`/`fix:` tickets | `## CI` section; knowledge-only diffs on code tickets are auto-recovered, never merged; heal/bugfix PRs without a test that fails pre-fix and passes post-fix are auto-recovered, never merged |
| **4. QA** | Remote CI (the source of truth) runs the same gates on GitHub (`ruff check .`, `pytest`) plus the PR-body evidence check | Required `ci` status check green; evidence step passes. Promoting `hsai repro-check` to a remote gate is a `ci-change` ticket - it needs a `ci.run_local` counterpart first (parity) |
| **5. Integrate** | Green-gated squash merge; ticket auto-closes; lesson lands in the knowledge base | `## Lesson learned` section + lesson file in `knowledge/lessons/` |

## Changing CI itself

CI is product code and evolves like it - but local (`ci.run_local`) and remote
(`.github/workflows/ci.yml`) must always run the same gates, or the loop's
pre-flight stops predicting the merge gate. Workflow edits therefore go through
a governed channel (`src/hsai/ciguard.py`, configured under `ci_policy` in
`.ai-swarm/core.yaml`, rationale in [ADR-0002](adr/0002-governed-ci-change-channel.md)):

1. The ticket must carry the **`ci-change`** label.
2. Every command in `ci_policy.required_steps` must survive the edit.
3. **Parity** must hold - each gate the workflow declares has a counterpart in
   `ci.local_commands()` and vice versa. Setup steps and GitHub-only checks are
   exempt by name via `ci_policy.parity_exempt_steps`; anything else with a
   `run:` is a gate.

Everything else - no ticket, no label, an unparseable workflow, a second
workflow lane - is **reverted**, and the verdict is recorded in the iteration
notes, the lesson, and a `## CI change` section of the PR body naming every
workflow file touched.

Adding a gate is a two-sided change: add it to `ci.LOCAL_STEPS` *and* the
workflow in the same PR. Check any tree with:

```
hsai ci-parity        # exit 0 = local and remote agree; 1 = they diverged
```

## Governance rhythm around the SDLC

- **Blocks**: `hsai cycle` runs synthesis (heavy model) + a sequential block of
  implementations, then produces a whitepaper, persona articles, a refreshed
  `governance/DIRECTION.md`, and a **review issue**.
- **Review**: twice daily the architect reviews the brief and runs
  `/review-next` - feedback is encoded as ADRs in `docs/adr/`, tickets are
  refined or filed, and the session ends with a merged PR.
- **Retry policy**: a PR that fails the gate is closed and its ticket returns
  to the backlog (`attempts:N`); after `max_ticket_attempts` it is `blocked`
  for a human.
