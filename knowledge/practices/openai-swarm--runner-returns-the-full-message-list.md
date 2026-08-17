---
tags:
- practice
- status/adopted
- source/openai-swarm
created: '2026-08-04'
practice_id: openai-swarm--runner-returns-the-full-message-list
source_project: openai/swarm
source_artifact: source_code
status: adopted
adopted_pr: 94
adopted_date: '2026-08-04'
---

# runner returns the full message list

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source project | `openai/swarm` |
| source artifact | source_code |
| status | **adopted** |
| adopted PR | #94 |
| adopted date | 2026-08-04 |

## Evidence
PR #94 (`src/hsai/trajectory.py` module docstring): "openai/swarm (the runner
returns the full message list, so callers never reconstruct what happened)".

## Notes
`hsai traj` / `hsai replay` reconstruct a run from the stored `Trajectory`
object directly - callers never have to re-derive what happened from scattered
log lines or re-run the agent to find out.

## Related
- [[2026-08-04-implement-feat-worker-trajectory-capture-and-working-token-cost-telemetry]]
