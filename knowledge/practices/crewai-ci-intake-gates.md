---
tags:
  - practice
  - source/crewaiinc-crewai
created: 2026-08-13
source_repo: crewAIInc/crewAI
artifact: .github/workflows/
adopted_by:
  - the orchestrator evidence guard in src/hsai/orchestrator.py
---

# crewai-ci-intake-gates

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source repo | `crewAIInc/crewAI` |
| artifact | `.github/workflows/` |

## Observation
crewAI enforces pull-request metadata mechanically at intake: dedicated
workflows inspect the shape of the PR itself and fail it, instead of leaving
those conventions to reviewer discipline. A rule that only lives in a
CONTRIBUTING file is a rule that holds until someone is in a hurry.

## Adaptation
The orchestrator's evidence guard (`_claims_reference_evidence` in
`src/hsai/orchestrator.py`) refuses a self-improvement change whose cited
practice ids do not resolve to notes in this registry, recovering the ticket by
the same path that already catches a knowledge-only diff. Provenance is checked
before a PR exists rather than trusted because the model wrote it down.

## Adopted by
- the orchestrator evidence guard in src/hsai/orchestrator.py
