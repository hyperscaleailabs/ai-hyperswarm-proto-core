---
tags:
- practice
- status/adopted
- source/run-llama-llama-index
created: '2026-08-05'
practice_id: run-llama-llama-index--structured-telemetry-direction
source_project: run-llama/llama_index
source_artifact: harness_design
status: adopted
adopted_pr: 104
adopted_date: '2026-08-05'
---

# structured telemetry direction

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source project | `run-llama/llama_index` |
| source artifact | harness_design |
| status | **adopted** |
| adopted PR | #104 |
| adopted date | 2026-08-05 |

## Evidence
PR #104 (`src/hsai/journal.py` module docstring): "run-llama/llama_index's
structured telemetry direction (every run step emits a durable, inspectable
record)".

## Notes
Every cycle step's payload is journaled as one JSON line under
`.hsai/cycles/<cycle_index>/journal.jsonl` - a durable, replayable record of
what a block actually did, independent of the review brief's prose summary.

## Related
- [[2026-08-05-implement-feat-durable-cycle-journal-with-idempotent-resume-for-interrupted-blocks]]
