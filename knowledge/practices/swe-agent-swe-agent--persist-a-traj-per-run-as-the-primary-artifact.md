---
tags:
- practice
- status/adopted
- source/swe-agent-swe-agent
created: '2026-08-04'
practice_id: swe-agent-swe-agent--persist-a-traj-per-run-as-the-primary-artifact
source_project: SWE-agent/SWE-agent
source_artifact: source_code
status: adopted
adopted_pr: 84
adopted_date: '2026-08-04'
---

# persist a traj per run as the primary artifact

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source project | `SWE-agent/SWE-agent` |
| source artifact | source_code |
| status | **adopted** |
| adopted PR | #84 |
| adopted date | 2026-08-04 |

## Evidence
PR #84 (`src/hsai/trajectory.py` module docstring): "SWE-agent (persist a
`.traj` per run and build a replay/inspector on it - the run record, not just
the final patch, is the primary artifact)".

## Notes
Landed as `hsai.trajectory`: one JSON trajectory file per worker run, plus
`hsai traj <iteration>` / `hsai replay` to reconstruct it for a post-mortem
without spending quota.

## Related
- [[2026-08-04-implement-feat-worker-trajectory-store-json-agent-output-and-hsai-replay]]
