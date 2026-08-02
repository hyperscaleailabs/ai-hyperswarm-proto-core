---
tags:
  - practice
  - source/openai-swarm
id: PR-0008
source_repo: openai/swarm
artifact_kind: code
artifact_ref: swarm/core.py
observed_on: 2026-07-26
---

# Errors carry the orchestration context they failed in

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source | `openai/swarm` |
| artifact | [code: `swarm/core.py`](https://github.com/openai/swarm/blob/HEAD/swarm/core.py) |
| observed | 2026-07-26 |

## What it does
openai/swarm keeps its orchestration loop small enough to read in one sitting,
threading an explicit context variable set through every hand-off so any point
in the run can say which agent was active and with what state. The lightness is
the point: context travels with the work instead of living in the operator's
head.

## Why it applies to hsai
Loop failures used to surface as bare subprocess text with no way to tell which
step produced them. Adopted as `_format_error_with_context()` in
`src/hsai/orchestrator.py`: agent errors are prefixed with `phase=` and
`ticket=#N` before they reach the lesson, so a failed iteration's write-up
names the step that failed.

## Cited by
- _(not yet cited by a lesson)_
