---
tags:
  - lesson
  - outcome/fail
  - kind/implement
created: 2026-08-13
iteration: 4135704
---

# implement: feat: single-source CI contract with local/remote parity and a governed ci-change path

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **fail** |
| kind | implement |
| iteration | 4135704 |
| ticket | #219 |
| pull request | _(none)_ |
| model | `opus` |
| remote CI | SUCCESS |

## Context
Iteration 4135704. Ticket #219. CI before: CI green (ruff=pass, pytest=pass).

## What happened
Model `opus` (heavy) ran the task. Agent ok=False. CI after: CI green (ruff=pass, pytest=pass).

Reverted off-spec workflow edits: ['.github/workflows/ci.yml'].

Agent error:
```
[phase=implement, ticket=#219] timeout after 1200s
```

## Lesson learned
Change did not reach green; auto-merge will hold until CI passes. Investigate the failure captured above before the next attempt.

## Reproduction evidence
_(not applicable: not a heal/bugfix ticket)_

## References (reference-set evidence)
- `langchain-ai/langchain`
- `FoundationAgents/MetaGPT`
- `crewAIInc/crewAI`
