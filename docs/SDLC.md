# SDLC - every change leaves evidence

Every change in this repo, human- or agent-made, moves through five phases.
Each phase leaves **evidence** that CI and the orchestrator check; a change
without evidence does not merge.

| phase | what happens | evidence required |
| --- | --- | --- |
| **1. Plan** | A structured ticket exists: Problem, Proposal, Acceptance criteria (>= 2 checkboxes), Verification plan, size label | Ticket link (`Closes #N`) in the PR body; orchestrator refuses `needs-refinement` tickets |
| **2. Implement** | Code changes on an isolated branch/worktree; model recorded | `## Model used` section in the PR body; workflow edits auto-reverted (CI parity) unless the ticket carries `ci-change` **and** the diff is confined to allowlisted `hsai ci` / `hsai repro-check` invocation lines - then the allowance and the retained diff are recorded in the PR body and the lesson |
| **3. Verify** | Local pre-flight: `hsai ci --scope local` (the `ci.steps` manifest in `.ai-swarm/core.yaml`); completeness guard (code tickets need code diffs); reproduce-before-fix guard for `heal`/`fix:` tickets | `## CI` section; knowledge-only diffs on code tickets are auto-recovered, never merged; heal/bugfix PRs without a test that fails pre-fix and passes post-fix are auto-recovered, never merged |
| **3b. Independent review** | A model on a *different tier* than the author reads the branch diff against the ticket's acceptance criteria and answers with a fenced JSON verdict (`hsai.review`); unparseable output is fail-closed | `## Independent review` section in the PR body + in the lesson; a blocking verdict opens no PR and returns the ticket to the backlog (`attempts:N`) |
| **4. QA** | Remote CI (the source of truth) runs the *same manifest* on GitHub: the `ci` job calls `hsai ci --scope remote` (lint, tests, PR-body evidence) and the `repro-check` job fetches the base ref and runs `hsai repro-check`, so the reproduce-before-fix contract is a real pre-merge gate | Required `ci` + `repro-check` status checks green; `tests/test_ci_parity.py` proves the workflow still executes every declared step (and reports a strict `xfail` naming the gap until the `ci.yml` rewrite lands - see CONTRIBUTING.md) |
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
