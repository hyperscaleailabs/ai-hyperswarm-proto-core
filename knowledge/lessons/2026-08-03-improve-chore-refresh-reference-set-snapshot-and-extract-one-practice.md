---
tags:
  - lesson
  - outcome/fail
  - kind/improve
created: 2026-08-03
iteration: 4133702
---

# improve: chore: refresh reference-set snapshot and extract one practice

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **fail** |
| kind | improve |
| iteration | 4133702 |
| ticket | #73 |
| pull request | _(none)_ |
| model | `haiku` |
| remote CI | FAILURE |

## Context
Iteration 4133702. Ticket #73. CI before: CI green (ruff=pass, pytest=pass).

## What happened
Model `haiku` (light) ran the task. Agent ok=True. CI after: CI red (ruff=FAIL, pytest=pass).

## Lesson learned
Change did not reach green; auto-merge will hold until CI passes. Investigate the failure captured above before the next attempt.

## Reproduction evidence
_(not applicable: not a heal/bugfix ticket)_

## References (reference-set evidence)
- `langchain-ai/langchain`
- `FoundationAgents/MetaGPT`
- `crewAIInc/crewAI`
