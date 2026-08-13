---
tags:
  - practice
  - source/openai-swarm
created: 2026-07-26
source_repo: openai/swarm
artifact: swarm/core.py
adopted_by:
  - lesson [[2026-07-26-implement-add-structured-execution-context-to-error-messages]]
---

# swarm-error-context

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source repo | `openai/swarm` |
| artifact | `swarm/core.py` |

## Observation
swarm's run loop carries the active agent and the current turn as explicit state
across every handoff, so anything that goes wrong is attributable to the step
that was executing rather than to "the run". The orchestration stays small
precisely because that context is threaded through rather than reconstructed
from logs afterwards.

## Adaptation
`_format_error_with_context()` in `src/hsai/orchestrator.py` prefixes every agent
error with `[phase=..., ticket=#...]` before it reaches a lesson or a ledger
record, so a post-mortem starts at the failing step instead of at a bare stack
trace.

## Adopted by
- lesson [[2026-07-26-implement-add-structured-execution-context-to-error-messages]]
