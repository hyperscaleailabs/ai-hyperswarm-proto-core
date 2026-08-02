---
tags:
  - practice
  - source/run-llama-llama-index
id: PR-0001
source_repo: run-llama/llama_index
artifact_kind: readme
artifact_ref: CONTRIBUTING.md
observed_on: 2026-07-26
---

# Regression tests pin the exact broken behaviour

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source | `run-llama/llama_index` |
| artifact | [readme: `CONTRIBUTING.md`](https://github.com/run-llama/llama_index/blob/HEAD/CONTRIBUTING.md) |
| observed | 2026-07-26 |

## What it does
llama_index's contribution guide makes a test that exercises the reported
behaviour part of the definition of a bug fix, not an optional extra. The fix
stream is therefore self-describing: each bugfix carries a test that only
exists because of the bug it pins, so a later regression re-fails the same
test rather than passing silently.

## Why it applies to hsai
The loop merges its own work without a human reader, so "CI is green" is the
only signal - and a fix can be green for the wrong reason. Adopted as
`src/hsai/repro.py`: heal and `fix:` tickets must add or modify a test that
FAILS on the pre-fix parent tree and PASSES on the fix branch, and the
failing-then-passing transition is recorded in the lesson as reproduction
evidence.

## Cited by
- _(not yet cited by a lesson)_
