---
tags:
- practice
- status/adopted
- source/openbmb-chatdev
created: '2026-08-12'
practice_id: openbmb-chatdev--review-phases-run-on-cheaper-agents
source_project: OpenBMB/ChatDev
source_artifact: harness_design
status: adopted
adopted_pr: 203
adopted_date: '2026-08-12'
---

# review phases run on cheaper agents

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source project | `OpenBMB/ChatDev` |
| source artifact | harness_design |
| status | **adopted** |
| adopted PR | #203 |
| adopted date | 2026-08-12 |

## Evidence
PR #203 (`src/hsai/review.py` module docstring): "OpenBMB/ChatDev (review
phases run on cheaper agents)".

## Notes
`cfg.review.tier_policy` biases cheap on purpose - the gate runs on EVERY
change, so a heavy reviewer would spend the block's heavy budget on critique
alone instead of implementation.

## Related
- [[2026-08-12-implement-feat-adversarial-cross-model-pr-review-gate-with-a-merge-gatekeeper]]
