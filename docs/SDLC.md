# SDLC - every change leaves evidence

Every change in this repo, human- or agent-made, moves through five phases.
Each phase leaves **evidence** that CI and the orchestrator check; a change
without evidence does not merge.

| phase | what happens | evidence required |
| --- | --- | --- |
| **1. Plan** | A structured ticket exists: Problem, Proposal, Acceptance criteria (>= 2 checkboxes), Verification plan, size label | Ticket link (`Closes #N`) in the PR body; orchestrator refuses `needs-refinement` tickets |
| **2. Implement** | Code changes on an isolated branch/worktree; model recorded | `## Model used` section in the PR body; workflow edits auto-reverted (CI parity) |
| **3. Verify** | Local pre-flight: `ruff check .` + `pytest`; completeness guard (code tickets need code diffs); reproduce-before-fix guard for `heal`/`fix:` tickets | `## CI` section; knowledge-only diffs on code tickets are auto-recovered, never merged; heal/bugfix PRs without a test that fails pre-fix and passes post-fix are auto-recovered, never merged |
| **3b. Independent review** | A model on a *different tier* than the author reads the branch diff against the ticket's acceptance criteria and answers with a fenced JSON verdict (`hsai.review`); unparseable output is fail-closed | `## Independent review` section in the PR body + in the lesson; a blocking verdict opens no PR and returns the ticket to the backlog (`attempts:N`) |
| **4. QA** | Remote CI (the source of truth) runs the same checks on GitHub, including a `repro-guard` job re-running the reproduce-before-fix check; PR-body evidence checked | Required `ci` + `repro-guard` status checks green; evidence step passes |
| **5. Integrate** | Green-gated squash merge; ticket auto-closes; lesson lands in the knowledge base | `## Lesson learned` section + lesson file in `knowledge/lessons/` |
| **5b. Post-merge audit** | `hsai audit` (see `src/hsai/audit.py`) independently re-verifies the invariants phases 1-5 only asserted at merge time - a merged PR's evidence can rot (link rot, an orphaned note, a MOC that fell out of sync, a lesson whose ticket/PR turns out not to have closed) even though the checks above all passed on that PR alone | `audit` job in CI (network-free checks, every PR) + `audit-periodic.yml` (full audit incl. GitHub-dependent checks, daily); a failing scheduled run files/updates one `audit-drift` ticket |

### The `hsai audit` checks

Six independent, individually-testable checks (`tests/test_audit.py`), each named in the JSON report:

| check | what it verifies | needs network? |
| --- | --- | --- |
| `wikilinks` | every `[[target]]` under `knowledge/` resolves to a note that exists | no |
| `orphans` | every lesson/whitepaper/article is reachable by following links out from a MOC | no |
| `moc_freshness` | regenerating the MOCs in-memory yields no diff against what is committed (`hsai reindex --check` is the same check, standalone) | no |
| `frontmatter` | every note's frontmatter is valid YAML and carries the tags its kind requires | no |
| `lesson_ticket_pr_closure` | every lesson names a ticket + PR that exist and are closed/merged; every merged PR since `--since` has a matching lesson | yes (`gh`) |
| `model_record_consistency` | the tier a merged PR claims matches a tier the quota ledger actually recorded for that ticket | yes (`gh`) |

Run it locally with `hsai audit --json --strict` (add `--offline` to skip the two
GitHub-dependent checks, exactly what the PR-level CI job does). A finding that
is genuine pre-invariant history - not a live defect - can be silenced by
adding a documented entry to `.ai-swarm/audit_known_exceptions.yaml`, naming
the check, a substring of the exact finding it excuses, and why; this list is
meant to shrink toward empty, never to grow to hide a new violation.

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
