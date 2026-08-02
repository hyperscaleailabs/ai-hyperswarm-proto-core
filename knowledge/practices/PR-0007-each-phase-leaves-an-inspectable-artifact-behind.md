---
tags:
  - practice
  - source/foundationagents-metagpt
id: PR-0007
source_repo: FoundationAgents/MetaGPT
artifact_kind: readme
artifact_ref: README.md
observed_on: 2026-07-26
---

# Each phase leaves an inspectable artifact behind

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source | `FoundationAgents/MetaGPT` |
| artifact | [readme: `README.md`](https://github.com/FoundationAgents/MetaGPT/blob/HEAD/README.md) |
| observed | 2026-07-26 |

## What it does
MetaGPT encodes a software company as roles running a standard operating
procedure, where each role's output is a named document - requirements, design,
task breakdown - rather than an invisible hop in a prompt chain. The pipeline
is auditable because every stage's deliverable is written down.

## Why it applies to hsai
An autonomous loop that merges its own PRs must be readable after the fact by
someone who was not there. Adopted as `_phase_artifacts()` in
`src/hsai/orchestrator.py`: every PR body carries a `## Phase artifacts` block
listing what the heal / implement / improve path was expected to produce, next
to the model used, the CI result and the lesson.

## Cited by
- _(not yet cited by a lesson)_
