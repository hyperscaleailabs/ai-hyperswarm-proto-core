# SDLC - every change leaves evidence

Every change in this repo, human- or agent-made, moves through five phases.
Each phase leaves **evidence** that CI and the orchestrator check; a change
without evidence does not merge.

| phase | what happens | evidence required |
| --- | --- | --- |
| **1. Plan** | A structured ticket exists: Problem, Proposal, Acceptance criteria (>= 2 checkboxes), Verification plan, size label | Ticket link (`Closes #N`) in the PR body; orchestrator refuses `needs-refinement` tickets |
| **2. Implement** | Code changes on an isolated branch/worktree; model recorded | `## Model used` section in the PR body; workflow edits auto-reverted (CI parity) |
| **3. Verify** | Local pre-flight: `ruff check .` + `pytest`; completeness guard (code tickets need code diffs); reproduce-before-fix guard for `heal`/`fix:` tickets | `## CI` section; knowledge-only diffs on code tickets are auto-recovered, never merged; heal/bugfix PRs without a test that fails pre-fix and passes post-fix are auto-recovered, never merged |
| **4. QA** | Remote CI (the source of truth) runs the same checks on GitHub, including a `repro-guard` job re-running the reproduce-before-fix check; PR-body evidence checked | Required `ci` + `repro-guard` status checks green; evidence step passes |
| **5. Integrate** | Green-gated squash merge; ticket auto-closes; lesson lands in the knowledge base | `## Lesson learned` section + lesson file in `knowledge/lessons/` |

## Governance rhythm around the SDLC

- **Blocks**: `hsai cycle` runs synthesis (heavy model) + a sequential block of
  implementations, then produces a whitepaper, persona articles, a refreshed
  `governance/DIRECTION.md`, and a **review issue**.
- **Review**: twice daily the architect reviews the brief and runs
  `/review-next` - feedback is encoded as ADRs in `docs/adr/`, tickets are
  refined or filed, and the session ends with a merged PR.
- **Retry policy**: a PR whose remote CI concludes `FAILURE` is closed and its
  ticket returns to the backlog (`attempts:N`); after `max_ticket_attempts` it
  is `blocked` for a human. A `TIMEOUT` (the wait window elapsed while checks
  were still unresolved) is infrastructure latency, not a verdict: the PR
  stays open, the branch stays intact, and the ticket is unassigned without
  consuming an attempt - see `ci.disposition()` in `docs/ARCHITECTURE.md`.
- **Worktree hygiene**: `run_once` reclaims its worktree in a `finally` on
  every exit path; `hsai gc [--dry-run] [--older-than HOURS]` is the safety
  net for anything a killed process still leaks.
