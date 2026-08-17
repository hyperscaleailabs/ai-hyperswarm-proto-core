---
tags:
- practice
- status/adopted
- source/assafelovic-gpt-researcher
created: '2026-08-12'
practice_id: assafelovic-gpt-researcher--machine-parseable-pass-fail-quality-contract
source_project: assafelovic/gpt-researcher
source_artifact: harness_design
status: adopted
adopted_pr: 203
adopted_date: '2026-08-12'
---

# machine-parseable pass/fail quality contract

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source project | `assafelovic/gpt-researcher` |
| source artifact | harness_design |
| status | **adopted** |
| adopted PR | #203 |
| adopted date | 2026-08-12 |

## Evidence
PR #203 (`src/hsai/review.py` module docstring): "assafelovic/gpt-researcher
(machine-parseable pass/fail quality contract)".

## Notes
The reviewer must answer with a fenced JSON `ReviewVerdict` block;
`parse_verdict` is deliberately fail-closed - prose, garbage, or silence is a
non-approval, never a default pass.

## Related
- [[2026-08-12-implement-feat-adversarial-cross-model-pr-review-gate-with-a-merge-gatekeeper]]
