# SDLC - every change leaves evidence

Every change in this repo, human- or agent-made, moves through five phases.
Each phase leaves **evidence** that CI and the orchestrator check; a change
without evidence does not merge.

| phase | what happens | evidence required |
| --- | --- | --- |
| **1. Plan** | A structured ticket exists: Problem, Proposal, Acceptance criteria (>= 2 checkboxes), Verification plan, size label | Ticket link (`Closes #N`) in the PR body; orchestrator refuses `needs-refinement` tickets |
| **2. Implement** | Code changes on an isolated branch/worktree; model recorded | `## Model used` section in the PR body; workflow edits auto-reverted (CI parity); a diff touching a `protected_surfaces` entry is graded (revert / require `guards-approved` / deny) before anything else |
| **3. Verify** | Local pre-flight: `ruff check .` + `pytest`; completeness guard (code tickets need code diffs); reproduce-before-fix guard for `heal`/`fix:` tickets; AST test-count comparison against the base ref | `## CI` section; knowledge-only diffs on code tickets are auto-recovered, never merged; heal/bugfix PRs without a test that fails pre-fix and passes post-fix are auto-recovered, never merged; a net test-function decrease without `guards-approved` is auto-recovered |
| **4. QA** | Remote CI (the source of truth) re-runs lint + tests on GitHub and additionally runs `hsai policy-check` against the PR's base ref, so the protected-surface guard binds human PRs too; PR-body evidence checked | Required `ci` status check green (bundling lint, tests, SDLC evidence, and the policy-check step) |
| **5. Integrate** | Green-gated squash merge; ticket auto-closes; lesson lands in the knowledge base | `## Lesson learned` section + lesson file in `knowledge/lessons/` |

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
