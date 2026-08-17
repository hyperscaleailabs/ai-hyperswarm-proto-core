---
tags:
- practice
- status/adopted
- source/run-llama-llama-index
created: '2026-07-26'
practice_id: run-llama-llama-index--a-hard-numeric-ci-gate
source_project: run-llama/llama_index
source_artifact: ci_cd
status: adopted
adopted_pr: 47
adopted_date: '2026-07-26'
---

# a hard numeric CI gate

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source project | `run-llama/llama_index` |
| source artifact | ci_cd |
| status | **adopted** |
| adopted PR | #47 |
| adopted date | 2026-07-26 |

## Evidence
PR #47 (`src/hsai/ledger.py` module docstring): "run-llama/llama_index (a hard
numeric CI gate)".

## Notes
`ledger.evaluate_budget` is a pure numeric comparison against `cfg.budget`
ceilings (max heavy iterations, max seconds, soft ratio) - no prose judgment
call, a gate that behaves the same way every time it is evaluated.

## Related
- [[2026-07-26-implement-feat-quota-cost-telemetry-ledger-with-a-warn-then-halt-per-block-budget-gate]]
