---
tags:
  - article
  - persona/cto
---

# Resource Ceiling Detected: Implications for Loop Scheduling

The autonomous loop has hit a repeatable resource boundary. Three features proposed in the backlog all timed out at the 1200s limit. The time spent on synthesis is now the limiting factor for complex features.

## The constraint

Each blocked feature requires dense synthesis work:
- **Adopted-practice registry**: Synthesize from lessons, build a queryable registry, wire into the planner's context
- **Failure taxonomy**: Categorize failure modes, compute statistics, file actionable backlog items
- **Retrieval-grounded synthesis**: Planner reads lessons, retrieves relevant context, generates citations

All three are synthesis-heavy. Sonnet exhausted its budget trying. Opus exhausted its budget on the third attempt.

This suggests the bottleneck is not model intelligence but synthesis *throughput*. The loop is generating more reasoning work than it can complete in 1200 seconds.

## Options from a resourcing perspective

**Higher time limits**: If we increase the per-task timeout to 2400s, lessons 34–36 might complete. But this would double the wall-clock time for each block and increase quota spend by 2–3x. Not sustainable at scale.

**Decomposition**: Break synthesis-heavy work into smaller chunks. A 70% solution for adopted-practice registry might take 400s; the remaining 30% takes another 400s in a follow-up task. This keeps per-task runtime stable and allows parallelism.

**Batch vs. streaming**: Current synthesis produces a complete proposal and all supporting context in one shot. Streaming synthesis (propose incrementally, refine) might allow earlier completion and reduce peak memory/compute demand.

**Model routing by estimate**: Before proposing a task, estimate synthesis complexity. Route high-complexity tasks to opus (if budget allows) or decompose them before assignment.

## The loop's behavior under pressure

Critically: the loop did not crash or retry blindly. It recorded three clear timeouts, identified the pattern, and reported it. This is mature behavior. The loop knows it has a problem and is asking for guidance.

What we need now is:
1. **Decomposition strategy** (architectural)
2. **Complexity estimation** (model-selection heuristic)
3. **Feedback loop** (route timeout patterns back into task routing)

## Immediate action

The three blocked features (#272, #273, #292) cannot proceed until they are either:
- Routed to a higher-capacity model (if quota permits)
- Decomposed into runnable subtasks
- Escalated for human architectural guidance

I recommend decomposition: it's faster and cheaper than waiting for a human review, and it keeps the loop learning.
