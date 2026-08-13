---
tags:
  - practice
  - source/foundationagents-metagpt
created: 2026-07-26
source_repo: FoundationAgents/MetaGPT
artifact: metagpt/roles/
adopted_by:
  - lesson [[2026-07-26-improve-explicit-phase-artifacts-from-metagpt]]
---

# metagpt-phase-artifacts

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source repo | `FoundationAgents/MetaGPT` |
| artifact | `metagpt/roles/` |

## Observation
MetaGPT encodes a software company's SOP as roles - product manager, architect,
engineer, QA - and each role's output is an explicit, named document handed to
the next phase rather than an implicit side effect of the conversation. Because
the artifact is the interface between phases, any single phase can be audited on
its own without replaying the whole run.

## Adaptation
`_phase_artifacts()` in `src/hsai/orchestrator.py` declares the deliverables of
each branch of the loop (heal / implement / improve) and renders them into every
PR body under "## Phase artifacts". A reviewer sees what the phase was supposed
to produce next to what it actually produced, which is the auditability G2 asks
for applied to a single iteration.

## Adopted by
- lesson [[2026-07-26-improve-explicit-phase-artifacts-from-metagpt]]
