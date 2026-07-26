---
tags:
  - lesson
  - outcome/pass
  - kind/improve
created: 2026-07-26
iteration: 5
---

# Add execution trace logging

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **pass** |
| kind | improve (reference-set practice) |
| iteration | 5 |
| ticket | #22 |
| model | `haiku` |

## Context

Following goal G1 (Learn from the top-10 AI swarm / multi-agent projects), revisited
the pinned reference set in `.ai-swarm/core.yaml` to identify one concrete practice
that is missing from ai-hyperswarm-proto-core. After surveying the reference set,
selected **Execution Trace Logging** from SWE-agent (rank #10).

## What happened

**The Problem:** The system captures full agent output in `AIResult.output` but never
saves it. Lessons only include truncated error messages (max 800 chars). When
iterations fail (e.g., timeout), the full context of what the agent was working on
is lost, making debugging and learning from failures difficult.

**The Solution:** Implemented persistent execution trace logging:

1. **Extended KnowledgeBase** (`src/hsai/knowledge.py`):
   - Added `logs_dir` parameter (default: `knowledge/logs`)
   - Added `write_execution_trace(iteration, ticket, output)` method to save full output
   - Directory is auto-created on init; trace files ignored by `.gitignore` (*.log)

2. **Updated orchestrator** (`src/hsai/orchestrator.py`):
   - After `run_agent()` completes, capture full `ares.output`
   - Immediately save trace via `kb.write_execution_trace()`
   - Reference trace file in lesson's "what happened" section with Obsidian wikilink

**Benefits:**
- **Auditability (G2):** Every iteration's full agent output is preserved for inspection
- **Learning:** Failed iterations can be analyzed without losing context
- **Debugging:** Timeout/error scenarios have full execution history available

## Lesson learned

Small, additive changes to enable persistent observability are high-ROI. The agent
output was already captured but never saved - adding one method and four lines of
integration work unlocked full-fidelity debugging. This pattern (capture + persist +
link in narrative) is foundational to auditable autonomous systems.

Execution traces are not committed (`.gitignore: *.log`) but are preserved during the
iteration for analysis. The wikilink in the lesson provides the discovery path to
traces for any iteration.

## References (reference-set evidence)
- `SWE-agent/SWE-agent` - turns GitHub issues into validated PRs; execution trajectory system logs each action, observation, and status (sweagent/agent/agents.py). Full observability is critical for understanding failures and enabling effective retry.
