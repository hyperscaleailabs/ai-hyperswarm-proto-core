---
tags:
- practice
- status/adopted
- source/langchain-ai-langchain
created: '2026-08-04'
practice_id: langchain-ai-langchain--observability-as-a-cross-cutting-layer-at-one-choke-point
source_project: langchain-ai/langchain
source_artifact: source_code
status: adopted
adopted_pr: 94
adopted_date: '2026-08-04'
---

# observability as a cross-cutting layer at one choke point

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source project | `langchain-ai/langchain` |
| source artifact | source_code |
| status | **adopted** |
| adopted PR | #94 |
| adopted date | 2026-08-04 |

## Evidence
PR #94 (`src/hsai/trajectory.py` module docstring): "langchain (observability
as a cross-cutting layer captured at one choke point)".

## Notes
Token/cost telemetry and the trajectory write both happen at the single point
where `run_agent` returns, not scattered across every caller - one choke point
that every worker, reviewer, and synthesis call passes through.

## Related
- [[2026-08-04-implement-feat-worker-trajectory-capture-and-working-token-cost-telemetry]]
