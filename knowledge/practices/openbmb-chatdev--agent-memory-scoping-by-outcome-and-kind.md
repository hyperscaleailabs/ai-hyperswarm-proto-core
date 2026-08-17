---
tags:
- practice
- status/adopted
- source/openbmb-chatdev
created: '2026-08-10'
practice_id: openbmb-chatdev--agent-memory-scoping-by-outcome-and-kind
source_project: OpenBMB/ChatDev
source_artifact: harness_design
status: adopted
adopted_pr: 170
adopted_date: '2026-08-10'
---

# agent memory scoping by outcome and kind

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source project | `OpenBMB/ChatDev` |
| source artifact | harness_design |
| status | **adopted** |
| adopted PR | #170 |
| adopted date | 2026-08-10 |

## Evidence
PR #170 (`src/hsai/recall.py` module docstring): "kind matches the task - a
heal worker should see heal history first (`kind_weight`), the scoping idea
ChatDev's agent memory makes explicit".

## Notes
The BM25 lesson-retrieval index up-weights `outcome/fail` notes
(`fail_weight`) and notes whose `kind/*` tag matches the current task
(`kind_weight`) - retrieval that scopes to what is actually relevant, not just
what is textually similar.

## Related
- [[2026-08-10-implement-feat-lesson-retrieval-memory-inject-prior-lessons-into-worker-and-synthesis-prompts]]
