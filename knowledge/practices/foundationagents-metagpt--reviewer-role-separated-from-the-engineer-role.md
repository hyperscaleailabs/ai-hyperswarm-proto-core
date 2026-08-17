---
tags:
- practice
- status/adopted
- source/foundationagents-metagpt
created: '2026-08-12'
practice_id: foundationagents-metagpt--reviewer-role-separated-from-the-engineer-role
source_project: FoundationAgents/MetaGPT
source_artifact: harness_design
status: adopted
adopted_pr: 203
adopted_date: '2026-08-12'
---

# reviewer role separated from the engineer role

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source project | `FoundationAgents/MetaGPT` |
| source artifact | harness_design |
| status | **adopted** |
| adopted PR | #203 |
| adopted date | 2026-08-12 |

## Evidence
PR #203 (`src/hsai/review.py` module docstring): "FoundationAgents/MetaGPT
(reviewer role separated from the engineer role)".

## Notes
`hsai.models.select_reviewer` never maps a tier to itself - the model that
wrote a change is never the model that grades it, mirroring MetaGPT's
role-based agent collaboration.

## Related
- [[2026-08-12-implement-feat-adversarial-cross-model-pr-review-gate-with-a-merge-gatekeeper]]
