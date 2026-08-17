---
tags:
- practice
- status/adopted
- source/microsoft-semantic-kernel
created: '2026-08-12'
practice_id: microsoft-semantic-kernel--review-as-a-gate-distinct-from-build-test
source_project: microsoft/semantic-kernel
source_artifact: harness_design
status: adopted
adopted_pr: 203
adopted_date: '2026-08-12'
---

# review as a gate distinct from build/test

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source project | `microsoft/semantic-kernel` |
| source artifact | harness_design |
| status | **adopted** |
| adopted PR | #203 |
| adopted date | 2026-08-12 |

## Evidence
PR #203 (`src/hsai/review.py` module docstring): "microsoft/semantic-kernel
(review as a gate distinct from build/test)".

## Notes
`hsai.review.review_change` runs after local CI passes and BEFORE a PR is
opened - a distinct phase from `ruff`/`pytest`, so a plausible-looking but
wrong change no longer merges just because it is green.

## Related
- [[2026-08-12-implement-feat-adversarial-cross-model-pr-review-gate-with-a-merge-gatekeeper]]
