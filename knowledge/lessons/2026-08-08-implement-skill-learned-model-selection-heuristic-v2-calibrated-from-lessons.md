---
tags:
  - lesson
  - outcome/pass
  - kind/implement
created: 2026-08-08
iteration: 4134701
---

# implement: skill: learned model-selection heuristic-v2 calibrated from lessons

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **pass** |
| kind | implement |
| iteration | 4134701 |
| ticket | #42 |
| pull request | #139 |
| model | `sonnet` |
| remote CI | SUCCESS |

## Context
Iteration 4134701. Ticket #42. CI before: CI green (ruff=pass, pytest=pass).

## What happened
Model `sonnet` (capable) ran the task. Agent ok=True. Implemented a learned model-selection heuristic that routes tasks based on accumulated lesson outcomes. The heuristic now calibrates router weights from historical success rates per model and task kind. CI after: CI green (ruff=pass, pytest=pass).

## Lesson learned
Learned heuristics work when grounded in real outcome data. The quota ledger provides enough signal to bias selection toward models that historically succeed on similar tickets. This creates a positive feedback loop: cheaper tiers finish faster on their strengths, leaving budget for complex tasks.

## Reproduction evidence
_(not applicable: not a heal/bugfix ticket)_

## References (reference-set evidence)
- `langchain-ai/langchain` - adaptive model routing in chains
- `FoundationAgents/MetaGPT` - classifier and skill routing architecture
- `crewAIInc/crewAI` - agent role and task affinity
