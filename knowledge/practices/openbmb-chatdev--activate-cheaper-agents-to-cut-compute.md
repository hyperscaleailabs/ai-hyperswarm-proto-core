---
tags:
- practice
- status/adopted
- source/openbmb-chatdev
created: '2026-07-26'
practice_id: openbmb-chatdev--activate-cheaper-agents-to-cut-compute
source_project: OpenBMB/ChatDev
source_artifact: harness_design
status: adopted
adopted_pr: 47
adopted_date: '2026-07-26'
---

# activate cheaper agents to cut compute

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source project | `OpenBMB/ChatDev` |
| source artifact | harness_design |
| status | **adopted** |
| adopted PR | #47 |
| adopted date | 2026-07-26 |

## Evidence
PR #47 (`src/hsai/ledger.py` module docstring): "OpenBMB/ChatDev (activate
cheaper agents to cut compute)".

## Notes
The budget gate biases model selection toward cheaper tiers on a soft breach
instead of halting outright - new work keeps flowing under a lighter model
rather than stopping the block.

## Related
- [[2026-07-26-implement-feat-quota-cost-telemetry-ledger-with-a-warn-then-halt-per-block-budget-gate]]
