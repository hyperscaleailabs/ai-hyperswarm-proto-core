---
tags:
  - article
  - persona/architect
---

# Autonomous Swarm Architecture: Patterns from 20 Iterations

> For: Architect level - system design, tradeoffs, patterns adopted
> From: [[2026-08-08-synthesis-after-20-lessons]]

## System Design Foundation

The ai-hyperswarm-proto-core demonstrates a two-phase architecture: heavy-model synthesis followed by lighter-weight implementation. This pattern separates concerns cleanly: the synthesis phase (opus) researches and designs tickets; the implementation phase (haiku/sonnet) executes them. The result is a cost-optimized pipeline that doesn't sacrifice quality.

## Key Architectural Patterns

**Idempotency & Resumability**
The durable cycle journal enables interrupted blocks to resume without re-filing tickets or re-spending quota. This required careful separation of concerns: journaling happens outside the action lambda, so crashed runs don't poison the next attempt. Trajectory capture (storing agent outputs as JSON) provides the ability to replay any iteration offline—critical for audit and debug.

**Event-Sourced State**
The quota ledger aggregates every iteration's cost in a single JSONL file. This append-only design makes it the source of truth for budget decisions, and resuming blocks re-grades budget from the complete ledger, not from cached values. The tradeoff: disk I/O on every iteration, but the benefit is correctness.

**Knowledge-Driven Self-Improvement**
Every PR leaves behind a lesson (pass or fail) and every 5–10 lessons produce a whitepaper. MOCs (Maps of Content) tie them together, keeping the knowledge base searchable and explorable. The architecture reflects a core principle: learning compounds only if it's durable and connected.

## One Failure, One Pattern

One governance artifacts cycle failed (block 41343), but the failure was caught and recorded, not silently merged. The system treated it as data: the lesson became part of synthesis, and the retry mechanism kicked in. This taught us that even automated systems need oversight—human review remains essential.

## Operational Lessons

- **Sequential blocks are correct**: Parallelism on the developer's machine degrades performance. Future parallelism should be via distributed workers, not local concurrency.
- **CI/CD as the gate, not the arbiter**: Remote CI is the truth, but the orchestrator drives locally. This keeps the loop fast for common paths.
- **Ticket-per-PR invariant holds**: Traceability never compromised. Model selection is recorded, lessons are linked, nothing is orphaned.

## Next Evolution

Scale to 2–3 parallel blocks with nightly synthesis. The architecture is ready: durable journals, cost tracking, and trajectory replay all support it. Monitor tokens-per-merged-PR (currently ~5000) to keep scaling efficient.
