---
tags:
  - practice
  - source/assafelovic-gpt-researcher
id: PR-0002
source_repo: assafelovic/gpt-researcher
artifact_kind: code
artifact_ref: gpt_researcher/utils/costs.py
observed_on: 2026-07-26
---

# Track the cost of every model call in the run itself

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source | `assafelovic/gpt-researcher` |
| artifact | [code: `gpt_researcher/utils/costs.py`](https://github.com/assafelovic/gpt-researcher/blob/HEAD/gpt_researcher/utils/costs.py) |
| observed | 2026-07-26 |

## What it does
gpt-researcher carries a first-class cost helper and accumulates spend on the
researcher object as a report is produced, so the economics of a long
autonomous run are observable while it is still running rather than
reconstructed afterwards from a provider bill.

## Why it applies to hsai
hsai is subscription-only, so the scarce resource is quota and wall-clock, not
dollars - but the practice transfers exactly. Adopted as `src/hsai/ledger.py`:
every iteration that runs a model appends a record (tier, model, wall-clock,
attempts, outcome, parsed tokens) to a block ledger, and the budget gate warns
then halts NEW work when a per-block ceiling is crossed - never aborting an
in-flight PR.

## Cited by
- _(not yet cited by a lesson)_
