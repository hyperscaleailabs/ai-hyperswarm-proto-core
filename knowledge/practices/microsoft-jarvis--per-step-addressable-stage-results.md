---
tags:
- practice
- status/adopted
- source/microsoft-jarvis
created: '2026-08-04'
practice_id: microsoft-jarvis--per-step-addressable-stage-results
source_project: microsoft/JARVIS
source_artifact: harness_design
status: adopted
adopted_pr: 84
adopted_date: '2026-08-04'
---

# per-step addressable stage results

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source project | `microsoft/JARVIS` |
| source artifact | harness_design |
| status | **adopted** |
| adopted PR | #84 |
| adopted date | 2026-08-04 |

## Evidence
PR #84 (`src/hsai/trajectory.py` module docstring): "microsoft/JARVIS
(intermediate stage results must be separately addressable, hence per-step
data rather than a final blob)".

## Notes
A `Trajectory` stores a list of `Step` records (tool calls, tokens, timing),
not just the final output text - each step is inspectable on its own, not
buried inside one opaque blob.

## Related
- [[2026-08-04-implement-feat-worker-trajectory-store-json-agent-output-and-hsai-replay]]
