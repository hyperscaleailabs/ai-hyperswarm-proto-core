---
tags:
  - practice
  - source/crewaiinc-crewai
id: crewai-mechanical-pr-metadata-gates
source_repo: crewAIInc/crewAI
artifact: .github/workflows/pr-title.yml - PR metadata checked by CI at intake
created: 2026-08-13
---

# crewai-mechanical-pr-metadata-gates

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source repo | `crewAIInc/crewAI` |
| artifact | `.github/workflows/pr-title.yml - PR metadata checked by CI at intake` |
| cite as | `practice:crewai-mechanical-pr-metadata-gates` |

## Observation
PR metadata discipline is enforced mechanically in CI rather than by review convention: a PR that
does not meet the contract fails a check at intake instead of relying on a reviewer to notice.

## Adaptation
The evidence guard in `src/hsai/orchestrator.py` resolves every practice id a self-improve or
synthesized ticket cites against this registry, and recovers the iteration with `NO_EVIDENCE` when a
citation dangles - the same mechanism that already catches knowledge-only diffs.

## Adopted by
- feat: practice registry so reference-set evidence is real and machine-checkable
