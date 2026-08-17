---
tags:
- practice
- status/adopted
- source/assafelovic-gpt-researcher
created: '2026-07-26'
practice_id: assafelovic-gpt-researcher--cost-accounting
source_project: assafelovic/gpt-researcher
source_artifact: source_code
status: adopted
adopted_pr: 47
adopted_date: '2026-07-26'
---

# cost accounting

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source project | `assafelovic/gpt-researcher` |
| source artifact | source_code |
| status | **adopted** |
| adopted PR | #47 |
| adopted date | 2026-07-26 |

## Evidence
PR #47 (`src/hsai/ledger.py` module docstring): "assafelovic/gpt-researcher
(costs.py cost accounting)".

## Notes
Landed as the quota/cost telemetry ledger (`hsai.ledger`): an append-only
record of every model run's tier, wall-clock, attempts, and token counts,
aggregated per block for the warn-then-halt budget gate.

## Related
- [[2026-07-26-implement-feat-quota-cost-telemetry-ledger-with-a-warn-then-halt-per-block-budget-gate]]
