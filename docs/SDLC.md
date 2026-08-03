# SDLC - every change leaves evidence

Every change in this repo, human- or agent-made, moves through five phases.
Each phase leaves **evidence** that CI and the orchestrator check; a change
without evidence does not merge.

| phase | what happens | evidence required |
| --- | --- | --- |
| **1. Plan** | A structured ticket exists: Problem, Proposal, Acceptance criteria (>= 2 checkboxes), Verification plan, size label | Ticket link (`Closes #N`) in the PR body; orchestrator refuses `needs-refinement` tickets |
| **2. Implement** | Code changes on an isolated branch/worktree; model recorded | `## Model used` section in the PR body; workflow edits auto-reverted (CI parity) |
| **3. Verify** | Local pre-flight: `ruff check .` + `pytest`; completeness guard (code tickets need code diffs); reproduce-before-fix guard for `heal`/`fix:` tickets | `## CI` section; knowledge-only diffs on code tickets are auto-recovered, never merged; heal/bugfix PRs without a test that fails pre-fix and passes post-fix are auto-recovered, never merged |
| **3b. Review** | An **independent reviewer** (a separate agent, pinned one tier below the implementation tier and never `heavy`) grades the diff against the ticket's acceptance criteria before a PR is opened | `## Acceptance review` section with a per-criterion met/unmet table and cited evidence; an explicit `FAIL` never opens a PR - the ticket returns to the backlog with `attempts:N` |
| **4. QA** | Remote CI (the source of truth) runs the same checks on GitHub, including a `repro-guard` job re-running the reproduce-before-fix check; PR-body evidence checked | Required `ci` + `repro-guard` status checks green; `hsai evidence-check` passes |
| **5. Integrate** | Green-gated squash merge; ticket auto-closes; lesson lands in the knowledge base | `## Lesson learned` section + lesson file in `knowledge/lessons/` |

## The acceptance-review gate (phase 3b)

`src/hsai/review.py` builds a review pack (ticket, parsed criteria, staged diff
against the merge base, changed paths, local CI) and asks a *cheaper* agent for
a fenced JSON verdict, which is strictly validated - a "PASS" that reports an
unmet criterion is re-derived as `FAIL`, and a criterion the reviewer silently
skipped is not a pass. Packs and verdicts are persisted, with environment
secrets redacted, under `.hsai/reviews/`.

Fail-open is deliberate: an unparseable, timed-out or errored reviewer records
`INCONCLUSIVE` in the lesson and lets the iteration proceed, so a broken
reviewer can never wedge the loop. Everything is config-driven under `review:`
in `.ai-swarm/core.yaml`; `review.enabled: false` restores the previous
behaviour exactly.

### Enforcing it in CI

`hsai evidence-check` is the CI-callable counterpart. It re-checks the
PR-body invariants and, when the linked ticket carries acceptance criteria,
requires an `## Acceptance review` section with a row per criterion:

```yaml
      - name: SDLC evidence (PR body)
        if: github.event_name == 'pull_request'
        env:
          PR_BODY: ${{ github.event.pull_request.body }}
          GH_TOKEN: ${{ github.token }}
        run: hsai evidence-check
```

> The loop's own workers cannot land this step: the orchestrator reverts any
> `.github/workflows/` edit so local and remote CI can never diverge. Wiring it
> into `ci.yml` is an architect/governance change.

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
