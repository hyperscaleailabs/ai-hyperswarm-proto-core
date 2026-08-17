---
tags:
- practice
- status/adopted
- source/openbmb-chatdev
created: '2026-08-05'
practice_id: openbmb-chatdev--session-durability
source_project: OpenBMB/ChatDev
source_artifact: harness_design
status: adopted
adopted_pr: 104
adopted_date: '2026-08-05'
---

# session durability

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source project | `OpenBMB/ChatDev` |
| source artifact | harness_design |
| status | **adopted** |
| adopted PR | #104 |
| adopted date | 2026-08-05 |

## Evidence
PR #104 (`src/hsai/journal.py` module docstring): "OpenBMB/ChatDev's session
durability (state survives the transport dying - reconnect and replay rather
than restart)".

## Notes
Landed as the per-block cycle journal (`hsai.journal`): every side-effecting
cycle step appends exactly one `JournalRecord` after it completes, so a block
killed mid-flight (crash, laptop sleep, budget halt) can be resumed with
`hsai cycle --resume` instead of restarting from scratch.

## Related
- [[2026-08-05-implement-feat-durable-cycle-journal-with-idempotent-resume-for-interrupted-blocks]]
