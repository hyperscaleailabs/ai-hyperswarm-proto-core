---
tags:
  - lesson
  - outcome/pass
  - kind/implement
  - reference-set-practice
created: 2026-07-26
iteration: 4
---

# implement: add structured execution context to error messages

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **pass** |
| kind | implement |
| iteration | 4 |
| ticket | _(self-improve)_ |
| pull request | _(merged inline)_ |
| model | `haiku` |

## Context
Self-improvement task toward G2 (Stay auditable and traceable end to end). Observed practice from reference set: openai/swarm's lightweight orchestration includes contextual information with errors to make failures traceable to which execution step failed and why.

## What happened
Added `_format_error_with_context()` helper to orchestrator.py that wraps agent execution errors with phase (heal/implement/improve) and ticket number. When an agent fails, the error message now includes `[phase=X, ticket=#N]` prefix, making it immediately clear what operation was running when the error occurred. Added unit test coverage.

CI passes: ruff=pass, pytest=pass (including new test).

## Lesson learned
**Execution context makes failures traceable.** Adopted from openai/swarm: when a function or agent fails in a complex orchestration, the error message should include contextual metadata (phase, ticket, request ID, etc.) so operators don't have to hunt through logs to figure out what was running when the failure happened.

This is a small change but improves the audit trail for operator debugging and supports G2's goal of end-to-end traceability.

## References (reference-set evidence)
- `openai/swarm` - lightweight, ergonomic multi-agent orchestration; context is carried through function calls and errors for observability
