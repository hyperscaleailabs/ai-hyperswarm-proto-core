---
tags:
  - practice
  - source/foundationagents-metagpt
id: metagpt-explicit-phase-artifacts
source_repo: FoundationAgents/MetaGPT
artifact: README.md - the SOP diagram naming each role's deliverable (PRD, design, task list, code)
created: 2026-08-13
---

# metagpt-explicit-phase-artifacts

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source repo | `FoundationAgents/MetaGPT` |
| artifact | `README.md - the SOP diagram naming each role's deliverable (PRD, design, task list, code)` |
| cite as | `practice:metagpt-explicit-phase-artifacts` |

## Observation
MetaGPT gives every role in its software-company SOP an explicit, named deliverable rather than an
implicit hand-off. The artifact - not the conversation - is what the next phase consumes and what a
human audits afterwards.

## Adaptation
`_phase_artifacts()` in `src/hsai/orchestrator.py` states the deliverables of the heal / implement /
improve phase and renders them into every PR body, so each iteration ships a checkable list of what
that phase was supposed to produce.

## Adopted by
- #49 - improve: explicit phase artifacts from MetaGPT
