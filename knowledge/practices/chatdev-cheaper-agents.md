---
tags:
  - practice
  - source/openbmb-chatdev
created: 2026-07-26
source_repo: OpenBMB/ChatDev
artifact: README.md
adopted_by:
  - ticket #44
  - PR #47
  - lesson [[2026-07-26-implement-feat-quota-cost-telemetry-ledger-with-a-warn-then-halt-per-block-budget-gate]]
---

# chatdev-cheaper-agents

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source repo | `OpenBMB/ChatDev` |
| artifact | `README.md` |

## Observation
ChatDev reports the token cost of producing each piece of software next to the
software itself, and its chat-chain configuration decides which phases run on
which model. Cost is a design parameter of the pipeline - phases that do not
need the strongest agent do not get one - rather than an afterthought measured
once the bill arrives.

## Adaptation
Model selection (`src/hsai/models.py`) picks a tier per task instead of a single
global model; the budget gate demotes selection one tier on a soft breach so a
block that is burning quota keeps progressing instead of halting; and the
independent review gate deliberately runs on a different, cheaper tier than the
author, which buys a second opinion without paying twice for the first.

## Adopted by
- ticket #44
- PR #47
- lesson [[2026-07-26-implement-feat-quota-cost-telemetry-ledger-with-a-warn-then-halt-per-block-budget-gate]]
