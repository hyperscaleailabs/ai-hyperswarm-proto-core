---
tags:
- practice
- status/adopted
- source/assafelovic-gpt-researcher
created: '2026-08-05'
practice_id: assafelovic-gpt-researcher--reconciliation-discipline
source_project: assafelovic/gpt-researcher
source_artifact: source_code
status: adopted
adopted_pr: 104
adopted_date: '2026-08-05'
---

# reconciliation discipline

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source project | `assafelovic/gpt-researcher` |
| source artifact | source_code |
| status | **adopted** |
| adopted PR | #104 |
| adopted date | 2026-08-05 |

## Evidence
PR #104 (`src/hsai/journal.py` module docstring): "assafelovic/gpt-researcher's
reconciliation discipline (fold partial, interrupted work back in without
duplicating it)".

## Notes
`journal.once(jr, step, key, fn)` runs `fn` exactly once per `(step, key)` for
a block and replays its recorded payload on every later call - a resumed block
never re-files a ticket, re-spends quota on a whitepaper, or re-opens a review
issue for a step that already completed.

## Related
- [[2026-08-05-implement-feat-durable-cycle-journal-with-idempotent-resume-for-interrupted-blocks]]
