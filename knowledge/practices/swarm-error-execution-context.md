---
tags:
  - practice
  - source/openai-swarm
id: swarm-error-execution-context
source_repo: openai/swarm
artifact: README.md - handoffs carry context variables through every step of a run
created: 2026-08-13
---

# swarm-error-execution-context

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source repo | `openai/swarm` |
| artifact | `README.md - handoffs carry context variables through every step of a run` |
| cite as | `practice:swarm-error-execution-context` |

## Observation
swarm stays deliberately lightweight, but the run's context travels with the work: every handoff
carries context variables, so any step can be traced back to the agent and the state that produced
it.

## Adaptation
`_format_error_with_context()` in `src/hsai/orchestrator.py` prefixes every agent error with
`[phase=..., ticket=#...]`, so a failure quoted in a lesson names the step it came from instead of
floating free.

## Adopted by
- #38 - improve: structured execution context in error messages
