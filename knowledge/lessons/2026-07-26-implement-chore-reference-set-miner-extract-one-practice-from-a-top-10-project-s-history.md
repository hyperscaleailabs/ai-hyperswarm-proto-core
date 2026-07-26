---
tags:
  - lesson
  - outcome/fail
  - kind/implement
created: 2026-07-26
iteration: 3
---

# implement: chore: reference-set miner - extract one practice from a top-10 project's history

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **fail** |
| kind | implement |
| iteration | 3 |
| ticket | #4 |
| pull request | _(none)_ |
| model | `haiku` |

## Context
Iteration 3. Ticket #4. CI before: CI green (ruff=pass, pytest=pass).

## What happened
Model `haiku` (light) ran the task. Agent ok=False. CI after: CI green (ruff=pass, pytest=pass).

Agent error:
```
timeout after 1200s
```

## Lesson learned
Change did not reach green; auto-merge will hold until CI passes. Investigate the failure captured above before the next attempt.

## References (reference-set evidence)
- `langchain-ai/langchain`
- `FoundationAgents/MetaGPT`
- `crewAIInc/crewAI`
