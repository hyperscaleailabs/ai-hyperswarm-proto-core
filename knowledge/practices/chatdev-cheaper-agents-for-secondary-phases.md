---
tags:
  - practice
  - source/openbmb-chatdev
id: chatdev-cheaper-agents-for-secondary-phases
source_repo: OpenBMB/ChatDev
artifact: README.md - the phase pipeline and its cost discussion
created: 2026-08-13
---

# chatdev-cheaper-agents-for-secondary-phases

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source repo | `OpenBMB/ChatDev` |
| artifact | `README.md - the phase pipeline and its cost discussion` |
| cite as | `practice:chatdev-cheaper-agents-for-secondary-phases` |

## Observation
Phases that do not need the strongest model run on deliberately cheaper agents. That is what keeps a
full multi-phase pipeline affordable enough to run on every change instead of occasionally.

## Adaptation
Model selection demotes one tier on a soft budget breach (`src/hsai/models.py`), and the independent
review gate deliberately runs on a different, cheaper tier than the author (`src/hsai/review.py`).

## Adopted by
- #47 - feat: quota/cost telemetry ledger with a per-block budget gate
- #203 - feat: adversarial cross-model PR review gate
