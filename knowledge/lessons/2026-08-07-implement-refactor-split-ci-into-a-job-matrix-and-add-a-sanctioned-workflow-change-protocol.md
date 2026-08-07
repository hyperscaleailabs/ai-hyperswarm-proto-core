---
tags:
  - lesson
  - outcome/fail
  - kind/implement
created: 2026-08-07
iteration: 4134502
---

# implement: refactor: split CI into a job matrix and add a sanctioned workflow-change protocol

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **fail** |
| kind | implement |
| iteration | 4134502 |
| ticket | #114 |
| pull request | _(none)_ |
| model | `haiku` |
| remote CI | FAILURE |

## Context
Iteration 4134502. Ticket #114. CI before: CI green (ruff=pass, pytest=pass).

## What happened
Model `haiku` (light) ran the task. Agent ok=False. CI after: CI red (ruff=FAIL, pytest=FAIL).

## Lesson learned
Change did not reach green; auto-merge will hold until CI passes. Investigate the failure captured above before the next attempt.

## Reproduction evidence
_(not applicable: not a heal/bugfix ticket)_

## References (reference-set evidence)
- `langchain-ai/langchain`
- `FoundationAgents/MetaGPT`
- `crewAIInc/crewAI`
