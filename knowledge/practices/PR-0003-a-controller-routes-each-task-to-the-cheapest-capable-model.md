---
tags:
  - practice
  - source/microsoft-jarvis
id: PR-0003
source_repo: microsoft/JARVIS
artifact_kind: readme
artifact_ref: README.md
observed_on: 2026-07-26
---

# A controller routes each task to the cheapest capable model

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source | `microsoft/JARVIS` |
| artifact | [readme: `README.md`](https://github.com/microsoft/JARVIS/blob/HEAD/README.md) |
| observed | 2026-07-26 |

## What it does
JARVIS (HuggingGPT) documents an LLM acting as controller over a stage
pipeline - plan the task, *select a model* for it, execute, then compose the
response. Model selection is an explicit, inspectable stage driven by the task
description, not a global constant baked into the program.

## Why it applies to hsai
Running every ticket on the heavy tier burns subscription quota on work a
cheaper tier does correctly. Adopted as `src/hsai/models.py`: a heuristic
scores each task (kind, title prefix, size label, body signals) and maps it to
the light/standard/heavy tiers declared in `.ai-swarm/core.yaml`. The chosen
model and the rationale for choosing it are recorded on the PR, which keeps
the routing decision auditable and therefore improvable.

## Cited by
- _(not yet cited by a lesson)_
