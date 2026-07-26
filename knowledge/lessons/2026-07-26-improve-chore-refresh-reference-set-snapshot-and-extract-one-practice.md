---
tags:
  - lesson
  - outcome/fail
  - kind/improve
created: 2026-07-26
iteration: 6
---

# improve: chore: refresh reference-set snapshot and extract one practice

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **fail** |
| kind | improve |
| iteration | 6 |
| ticket | #26 |
| pull request | _(none)_ |
| model | `haiku` |
| remote CI | _(pending)_ |

## Context
Iteration 6. Ticket #26. CI before: CI green (ruff=pass, pytest=pass).

## What happened
Model `haiku` (light) ran the task. Agent ok=True. CI after: CI red (ruff=pass, pytest=FAIL).

## Lesson learned
Change did not reach green; auto-merge will hold until CI passes. Investigate the failure captured above before the next attempt.

## References (reference-set evidence)
- `langchain-ai/langchain`
- `FoundationAgents/MetaGPT`
- `crewAIInc/crewAI`
