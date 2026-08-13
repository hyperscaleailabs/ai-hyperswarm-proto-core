---
tags:
  - practice
  - source/assafelovic-gpt-researcher
id: gpt-researcher-per-run-cost-accounting
source_repo: assafelovic/gpt-researcher
artifact: gpt_researcher/utils/costs.py - token counts converted into a per-run cost
created: 2026-08-13
---

# gpt-researcher-per-run-cost-accounting

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source repo | `assafelovic/gpt-researcher` |
| artifact | `gpt_researcher/utils/costs.py - token counts converted into a per-run cost` |
| cite as | `practice:gpt-researcher-per-run-cost-accounting` |

## Observation
Every model call is metered and the cost of a research run is reported back, so the economics of an
autonomous loop are observed rather than assumed. The same project treats unattributed output as
worthless: a claim carries the source it came from.

## Adaptation
`src/hsai/ledger.py` appends one cost record per iteration (tier, model, input and output tokens,
wall clock, outcome), and the per-block budget gate warns then halts on the aggregate.

## Adopted by
- #47 - feat: quota/cost telemetry ledger with a per-block budget gate
