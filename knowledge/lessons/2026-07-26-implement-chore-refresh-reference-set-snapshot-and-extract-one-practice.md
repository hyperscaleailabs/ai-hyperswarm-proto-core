---
tags:
  - lesson
  - outcome/fail
  - kind/implement
created: 2026-07-26
iteration: 9
---

# implement: chore: refresh reference-set snapshot and extract one practice

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **fail** |
| kind | implement |
| iteration | 9 |
| ticket | #29 |
| pull request | _(none)_ |
| model | `haiku` |
| remote CI | FAILURE |

## Context
Iteration 9. Ticket #29. CI before: CI green (ruff=pass, pytest=pass).

## What happened
Model `haiku` (light) ran the task. Agent ok=True. CI after: CI red (ruff=FAIL, pytest=pass).

## Lesson learned
Change did not reach green; auto-merge will hold until CI passes. Investigate the failure captured above before the next attempt.

## References (reference-set evidence)
- `langchain-ai/langchain`
- `FoundationAgents/MetaGPT`
- `crewAIInc/crewAI`
