---
tags:
  - article
  - persona/architect
---

# Twenty Iterations In: Swarm Architecture Lessons and Hard Tradeoffs

> For: Architect level - system design, tradeoffs, patterns adopted
> From: [[2026-08-08-synthesis-after-20-lessons]]

## The Core Pattern: Three-Stream Governance

After 20 iterations, the system architecture settled on three parallel streams that don't block each other:

1. **Steering** (human-in-the-loop): DIRECTION.md + architect review briefs → ADRs → next ticket prioritization
2. **Quality** (machine-enforced): Green-gated merges, CI signal as truth, SDLC phase evidence per PR
3. **Execution** (autonomous): Sequential blocks, synthesis → tickets → implementations → whitepaper → review

This split is not accidental. Early attempts to unify steering and execution caused the loop to stall waiting for human decisions. The moment we made steering asynchronous to block execution, the system both moved faster and became more stable—because humans could review async without blocking automated work.

## The Durable Cycle Journal: Crash-Recovery Without Replay

A critical architectural insight: the loop can fail mid-flight and resume without re-filing tickets, re-spending quota on synthesis, or re-running already-completed work. This is achieved through an **idempotent journal** — every side-effecting step (synthesis run, ticket filing, PR open, merge) is wrapped in `journal.once(key, fn)`, which memoizes outcomes.

The tradeoff: the journal adds state that must be managed. A corrupted journal entry can silently skip work. We mitigated this by making the journal queryable (`hsai journal list --block 41343`) and by forcing manual inspection after a recovery attempt.

## Trajectory Capture and Offline Replay

The system now records every iteration's full execution trace: agent prompts, model calls, token spend, outcome. This enables:
- **Offline model selection calibration**: Replay past iterations against different model assignments to find cheaper paths
- **Debugging without re-running**: Inspect an iteration's decision tree without invoking the model again
- **Cost forensics**: Trace why a PR consumed 8k tokens instead of 5k

The architectural cost is storage (one jsonl file per iteration, ~50KB each). At 10 iterations/day, that's 500KB/day—negligible. The win is that you can now answer "why did we make that model choice?" by replaying, not by re-running.

## Lessons on Knowledge Organization

The knowledge base is now indexed by three MOCs:
- **Lessons**: One per iteration, regardless of outcome (pass/fail)
- **Whitepapers**: Periodic syntheses (every ~5 lessons), thematic rollups
- **Articles**: Persona-targeted distillations from whitepapers

This hierarchy solves a real problem: raw lessons are valuable for debugging but overwhelming in volume. Whitepapers provide thematic coherence. Persona articles make the insights actionable for different roles. The MOCs themselves are machine-maintainable (indices and counts regenerated per block) while preserving human editability for cross-references.

## The Failure That Became a Lesson

Block 41343's governance artifact cycle marked as "fail" even though remote CI passed. This was a boundary case: the cycle completed all steps, but the automated gate flagged it as incomplete. Rather than ignore this signal, the system recorded it as a lesson. This explicit failure tracking is architecturally important: it prevents the loop from silently accumulating state inconsistencies.

## Honest Caveat: The Scaling Question

This architecture works well for sequential, single-machine blocks. The next question is parallelism: can 2–3 blocks run concurrently without the steering/quality/execution streams interfering? Early tests suggest yes, but the journal's memoization strategy assumes single-writer-per-block. Concurrent blocks could corrupt each other's journals if not carefully isolated.

## Reference
This architecture synthesizes patterns from langchain (agent execution planning), MetaGPT (role-based phase artifacts), and OpenAI Swarm (lightweight orchestration). The governance layer is original work validated across 20 iterations.
