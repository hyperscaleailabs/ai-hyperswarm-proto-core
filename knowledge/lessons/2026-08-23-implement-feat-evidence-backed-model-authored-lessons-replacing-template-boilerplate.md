---
tags:
  - lesson
  - outcome/fail
  - kind/implement
created: 2026-08-23
iteration: 4137702
---

# implement: feat: evidence-backed, model-authored lessons replacing template boilerplate

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **fail** |
| kind | implement |
| iteration | 4137702 |
| ticket | #344 |
| pull request | _(none)_ |
| model | `sonnet` |
| remote CI | _(pending)_ |

## Context
Iteration 4137702. Ticket #344. CI before: CI green (ruff=pass, pytest=pass).

## What happened
Model `sonnet` (standard) ran the task. Agent ok=False. CI after: CI red (ruff=FAIL, pytest=FAIL).

## Lesson learned
Change did not reach green; auto-merge will hold until CI passes. Investigate the failure captured above before the next attempt.

## Reproduction evidence
_(not applicable: not a heal/bugfix ticket)_

## References (reference-set evidence)
- `langchain-ai/langchain`
- `FoundationAgents/MetaGPT`
- `crewAIInc/crewAI`
