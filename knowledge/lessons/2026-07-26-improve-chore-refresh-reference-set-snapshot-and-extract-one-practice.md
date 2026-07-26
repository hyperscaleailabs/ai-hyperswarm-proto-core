---
tags:
  - lesson
  - outcome/pass
  - kind/improve
created: 2026-07-26
iteration: 1
---

# improve: chore: refresh reference-set snapshot and extract one practice

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **pass** |
| kind | improve |
| iteration | 1 |
| ticket | #35 |
| pull request | _(none)_ |
| model | `haiku` |
| remote CI | _(pending)_ |

## Context
Iteration 1. Ticket #35. CI before: CI green (ruff=pass, pytest=pass).

## What happened
Model `haiku` (light) ran the task. Agent ok=True. CI after: CI green (ruff=pass, pytest=pass).

Reverted off-spec workflow edits: ['.github/workflows/ci.yml'].

## Lesson learned
Change merged cleanly under a green build.

## References (reference-set evidence)
- `langchain-ai/langchain`
- `FoundationAgents/MetaGPT`
- `crewAIInc/crewAI`
