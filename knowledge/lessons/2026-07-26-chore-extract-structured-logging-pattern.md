---
tags:
  - lesson
  - outcome/pass
  - kind/chore
  - reference/langchain-ai
created: 2026-07-26
iteration: 4
---

# chore: extract structured logging pattern from reference set

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **pass** |
| kind | chore |
| iteration | 4 |
| ticket | #5 |
| pull request | _(auto-merged)_ |
| model | `haiku` |

## Context

Iteration 4. Ticket #5. Backlog was empty after previous iterations, triggering self-improve path (step 4). Goal G1: Learn from the top-10 AI swarm / multi-agent projects. Refreshed reference set snapshot in `.ai-swarm/core.yaml` remains current (2026-07-25, ~24h old).

## What happened

Extracted a concrete practice from the reference set: **structured logging with context** - a pattern used by mature orchestration frameworks like langchain-ai/langchain for observability and debugging.

Implemented a small version in `src/hsai/orchestrator.py`:
- Added `logging` module (stdlib, no new dependencies)
- Added structured logger to the orchestrator module
- Instrumented key decision points with contextual logging:
  - `iteration_start`: logs iteration number, repo, dry-run flag
  - `ci_check_complete`: logs CI status and branch
  - `path_decided`: logs the chosen path (heal/implement/improve) and ticket count
  - `model_selected`: logs model choice, tier, and ticket number
  - `agent_complete`: logs agent success, ticket, and error presence
  - `ci_recheck_complete`: logs recheck status
  - `iteration_complete`: logs final outcome, PR number, merge status

Each log entry uses `.extra={}` to attach structured context fields, enabling machine-readable audit trails without requiring a logging library dependency.

## Lesson learned

**Structured logging is foundational for loop observability.** The autonomous loop runs headless (via `claude -p`), so logging is the primary feedback mechanism for monitoring, debugging timeouts, and understanding decision flow. By logging at state transitions with structured context, we:

1. **Enable observability**: can grep/parse log files to audit loop behavior over time
2. **Facilitate debugging**: context fields make it easy to correlate events to specific iterations/tickets/models
3. **Stay lightweight**: using stdlib `logging` avoids dependency creep
4. **Support audit requirements**: aligns with goal G2 (stay auditable and traceable end-to-end)

This pattern is widely used in production orchestration frameworks. `langchain-ai/langchain` logs agent execution flow; similar patterns appear in MetaGPT, gpt-researcher, and other reference projects.

## References (reference-set evidence)

- `langchain-ai/langchain`: structured logging for agent orchestration (rank 1, 142k stars)
- `assafelovic/gpt-researcher`: observability patterns in autonomous research loop (rank 6, 28k stars)

---

_Filed automatically by the `hsai` loop. Model usage is on the Claude subscription (no metered API)._
