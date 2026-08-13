---
tags:
  - practice
  - source/run-llama-llama-index
created: 2026-07-26
source_repo: run-llama/llama_index
artifact: CONTRIBUTING.md
adopted_by:
  - ticket #43
  - PR #46
  - lesson [[2026-07-26-implement-feat-reproduce-before-fix-regression-guard-for-heal-and-bugfix-tickets]]
---

# llama-index-regression-first-fixes

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source repo | `run-llama/llama_index` |
| artifact | `CONTRIBUTING.md` |

## Observation
llama_index's contributor guide requires a bug fix to arrive with a test that
demonstrates the bug, and its CI runs that suite as a hard gate rather than as a
convention. "Fixed" therefore means "proven fixed" in the repository's own terms
- the claim and its evidence land in the same change.

## Adaptation
`src/hsai/repro.py` turns the convention into a machine check: a heal or bugfix
ticket must add or modify a test that FAILS on the pre-fix (parent) tree and
PASSES on the fix branch. The orchestrator runs it as an in-loop guard
(`repro.check_repro`), and `hsai repro-check` runs the same guard on GitHub as a
pre-merge gate, so local and remote agree on what counts as proof.

## Adopted by
- ticket #43
- PR #46
- lesson [[2026-07-26-implement-feat-reproduce-before-fix-regression-guard-for-heal-and-bugfix-tickets]]
