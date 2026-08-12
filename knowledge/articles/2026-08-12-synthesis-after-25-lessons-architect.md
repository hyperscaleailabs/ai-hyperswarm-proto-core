---
tags:
  - article
  - persona/architect
---

# Twenty-Five Lessons In: Hitting the Scaling Ceiling

> For: Architect level - system design, tradeoffs, patterns adopted
> From: [[2026-08-12-synthesis-after-25-lessons]]

## The Three-Failure Pattern and What It Reveals

After 22 consecutive lessons with strong outcomes (trajectory capture, durable journals, governance hygiene), lessons 23–25 show three consecutive failures. Structurally, they're different failures, but they converge on the same root cause: **context bloat at scale**.

### Lesson 23: The Timeout

Lesson-retrieval memory (injecting all prior lessons into every worker prompt) timed out at the hard 1200-second limit during the implement phase. This isn't flakiness; it's deterministic cost growth. At 25 lessons of history:
- Each lesson is ~1–2KB
- Full history ≈ 25–50KB
- Synthesized prompt + history + ticket context + expected output = ballpark 100–150KB of tokens before the model sees the problem

This is manageable in a single call, but when the worker loops (implement → verify → refine), each loop iteration recomputes the full context. The timeout likely happened in a retry cycle, not the first attempt.

**Architectural implication**: The lesson-retrieval pattern needs pruning or summarization. Injecting raw lessons works up to ~10–15 items; beyond that, it's more efficient to surface the *top-K similar lessons* via semantic search than to include all of them.

### Lessons 24–25: The Silent Halt

The vault-hygiene feature (lesson 24) and governance-artifacts chore (lesson 25) both showed **agent ok=False** — the worker ran, CI passed, but the PR didn't open. This is different from a crash or timeout; it's a clean exit with no artifact.

This pattern suggests:
1. The synthesis phase may be generating malformed tickets (impossible acceptance criteria, conflicting requirements)
2. The worker validates the ticket against the repo state, finds it's already done or contradictory, and halts
3. The loop records "agent ran" but doesn't surface "ticket was unparseable"

**Architectural implication**: The synthesis phase needs explicit validation gates that check whether generated tickets are coherent before handing them to workers. Currently, validation is implicit (workers fail, we see it in CI). We need explicit pre-flight checks.

## What Held Up to 25 Lessons

Before the stall:
- **Trajectory capture** (lessons 19–20): working perfectly, enabling offline analysis
- **Durable journals** (lessons 17–19): zero intervention needed when blocks were interrupted
- **Governance artifacts** (blocks up to 41347): full MOC updates, persona articles, DIRECTION refresh
- **Reference-set practices** (first 15 lessons): successful adoption of MetaGPT patterns (phase artifacts, retry semantics)

These are not fragile — they continue working even as the system grows.

## The Architectural Boundary

The system scaled smoothly from 0 to 22 lessons because:
- Work stayed well-scoped (features that fit in 5–10 PR tickets per block)
- Synthesis prompts and worker prompts were proportional to the problem, not to the full history
- Validation happened implicitly through CI and review

At 25 lessons, we hit a wall because:
- We added a feature (lesson-retrieval memory) that couples worker cost to history size
- We're now generating 3+ tickets per block, and the synthesis phase isn't adapting
- Implicit validation (CI green → work is good) breaks down when the worker succeeds in parsing a malformed ticket

## The Design Decision Ahead

Three paths forward:

1. **Prune and compress** (low cost, medium benefit): Summarize lessons into themes, keep only top-K relevant ones in worker context. Keeps current architecture, reduces cost growth.

2. **Two-phase synthesis** (medium cost, high benefit): Add a fast synthesis pass that generates tickets, then a validation pass that checks coherence before opening PRs. Catches malformed tickets early.

3. **Escalate to human review** (low cost, low benefit): Above 25 lessons, require manual review before a ticket reaches a worker. Works, but defeats the purpose of autonomy.

**Recommendation**: Combine 1 and 2. Implement top-K lesson retrieval (keeps cost constant even as history grows) and add a pre-flight validation gate (catches the silent-halt pattern). Together, they unblock the scaling path to 50+ lessons without architectural debt.

## Reference

This analysis incorporates scaling patterns from MetaGPT (context compression via summaries) and langchain-ai (prompt optimization for long histories).
