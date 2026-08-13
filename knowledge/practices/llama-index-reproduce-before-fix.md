---
tags:
  - practice
  - source/run-llama-llama-index
id: llama-index-reproduce-before-fix
source_repo: run-llama/llama_index
artifact: CONTRIBUTING.md - bug fixes are expected to ship with a test covering the reported failure
created: 2026-08-13
---

# llama-index-reproduce-before-fix

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source repo | `run-llama/llama_index` |
| artifact | `CONTRIBUTING.md - bug fixes are expected to ship with a test covering the reported failure` |
| cite as | `practice:llama-index-reproduce-before-fix` |

## Observation
A fix without a regression test is treated as incomplete. The test is the evidence that the reported
failure was real and that it is now gone - quality is a structural gate in CI rather than a
convention reviewers are asked to remember.

## Adaptation
`src/hsai/repro.py` runs the changed test files on the pre-fix parent tree and on the fix branch. A
heal or bugfix PR whose test does not fail first is recovered with `NO_REPRO` and never merged.

## Adopted by
- #46 - feat: reproduce-before-fix regression guard
