---
tags:
  - practice
  - source/microsoft-jarvis
id: jarvis-controller-routes-each-task-to-a-model
source_repo: microsoft/JARVIS
artifact: README.md - an LLM controller plans a task and routes each sub-task to a specialist model
created: 2026-08-13
---

# jarvis-controller-routes-each-task-to-a-model

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source repo | `microsoft/JARVIS` |
| artifact | `README.md - an LLM controller plans a task and routes each sub-task to a specialist model` |
| cite as | `practice:jarvis-controller-routes-each-task-to-a-model` |

## Observation
Deciding what to do is separated from deciding who does it: the controller sizes the task first,
then dispatches it to the model that fits, rather than sending everything to the largest one.

## Adaptation
`src/hsai/models.py` scores a task's complexity, selects a tier from that score, and records the
rationale on the PR, so the routing decision itself stays auditable after the fact.

## Adopted by
- #8 - skill: task-complexity-based model selection
