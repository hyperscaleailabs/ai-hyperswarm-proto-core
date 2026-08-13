---
tags:
  - practice
  - source/assafelovic-gpt-researcher
created: 2026-07-26
source_repo: assafelovic/gpt-researcher
artifact: gpt_researcher/utils/costs.py
adopted_by:
  - ticket #44
  - PR #47
  - lesson [[2026-07-26-implement-feat-quota-cost-telemetry-ledger-with-a-warn-then-halt-per-block-budget-gate]]
---

# gpt-researcher-cost-accounting

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source repo | `assafelovic/gpt-researcher` |
| artifact | `gpt_researcher/utils/costs.py` |

## Observation
gpt-researcher meters its own spend in code: a small cost helper the research
loop calls as it goes, so the price of a run is a number the system reports
about itself rather than something a human reconstructs afterwards from a
provider dashboard.

## Adaptation
`src/hsai/ledger.py` appends exactly one cost record per iteration - tier, model,
wall clock, input/output tokens, attempts, outcome - to a per-block ledger that
the budget gate reads to warn and then halt a block that is burning quota. The
ledger is written to the repo root rather than the ephemeral worktree so the
economics survive the iteration that produced them.

## Adopted by
- ticket #44
- PR #47
- lesson [[2026-07-26-implement-feat-quota-cost-telemetry-ledger-with-a-warn-then-halt-per-block-budget-gate]]
