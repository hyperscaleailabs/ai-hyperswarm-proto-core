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
| **5b. Post-merge evidence** | `hsai audit` re-verifies the traceability invariants and the vault's own integrity AFTER the merge, when a PR-body grep can no longer see the whole picture | `audit` job green on every PR (vault-local checks); `audit-periodic.yml` catches history-wide drift daily (see below) |

## Post-merge evidence: `hsai audit`

Every other row in the table above checks a *single* PR before it merges. None
of them re-check what is actually true of the repo once many PRs have landed:
that every merged PR left a lesson, that every lesson still names a ticket and
PR that exist, that the model tier recorded on a PR matches the ledger, that
every `[[wikilink]]` in the vault resolves, or that the MOCs still match what
is on disk. `hsai audit` is that check - six independent, individually
testable checks (see `src/hsai/audit.py` for the authoritative list):

| check | what it verifies | needs `gh`/network? |
| --- | --- | --- |
| `wikilinks` | every `[[target]]` under `knowledge/` (and `docs/adr/`) resolves to a real note | no |
| `orphans` | every lesson/whitepaper/article is reachable from `[[Knowledge Base MOC]]` | no |
| `moc_freshness` | regenerating MOCs in-memory matches what is committed (`hsai reindex --check` runs the same logic) | no |
| `frontmatter` | every note's YAML frontmatter parses and carries the fields its kind requires | no |
| `lesson_ticket_pr` | each lesson names a ticket + PR that exist and are closed/merged | yes |
| `merged_prs_have_lessons` / `model_consistency` | every merged PR since `--since` has a lesson naming it; its recorded tier matches the ledger | yes |

`hsai audit [--since REF] [--json] [--strict]`: the first four (vault-local)
checks always run; passing `--since REF` additionally runs the two
GitHub-dependent checks, scoped to PRs merged since that ref. `--json` emits
the machine-readable report this table summarizes; `--strict` exits non-zero
if any check fails - that is what a CI gate uses.

- **Per PR** (`ci.yml`, job `audit`): the vault-local checks only, so it needs
  no network and runs on every PR alongside `ci`.
- **Daily** (`audit-periodic.yml`): the full audit, including the
  GitHub-dependent checks, on a schedule (and via `workflow_dispatch`). On
  failure it files or updates - idempotently, never duplicates - a single P1
  `audit-drift` ticket (`--file-drift-ticket`).

**Known exceptions.** `.ai-swarm/known_exceptions.yaml` documents pre-invariant
history a check would otherwise flag but that predates the check (or the code
fix behind it) - e.g. lessons filed before the orchestrator started recording
`lesson.pr`. Each entry names the check, the target, and why; the list exists
so the gate can start green and stay honest about history, not so failures can
be muted going forward. `hsai audit`'s own acceptance bar is an empty (or
fully-justified) list, not a growing one.

`hsai reindex --check` runs the same MOC-freshness logic and fails loudly
(non-zero exit, no files modified) instead of silently rewriting stale MOCs -
useful as a pre-commit habit and as what the `moc_freshness` check itself
calls under the hood.

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
