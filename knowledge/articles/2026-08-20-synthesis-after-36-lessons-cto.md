---
tags:
  - article
  - persona/cto
---

# Lessons 34–36: Cost and Quota Implications of the 1200s Ceiling

From an operations perspective, lessons 34–36 present a cost problem disguised as a timeout problem.

## The Quota Reality

Each failed lesson at 1200s is approximately:
- 1200s * (~50 TPM average usage) ≈ 60,000 tokens per failed attempt
- 3 lessons × 60,000 tokens = ~180,000 tokens burned on timeouts
- This is *wasted quota* because the worker ran out of time, not because the task was unsolvable

If we retry with a 2400s budget:
- Each retry: ~120,000 tokens
- 3 retries: ~360,000 tokens total
- Worst case: all three still fail, and we've doubled the burn

## Model Cost Breakdown

Lessons 34–35 used sonnet (cheaper). Lesson 36 used opus (expensive). If we increase the budget and retry with opus for all three:
- Opus cost: roughly 2–3x sonnet cost per token
- 3 retries at 2400s with opus: ~360,000 × 2.5 = ~900,000 tokens

That's significant quota spend for a single governance block.

## The Escalation Cost

If we choose Option 2 (decompose tasks) instead:
- We need to implement task decomposition in the harness
- That's likely 2–3 more lessons worth of engineering work
- Cost: ~200,000–300,000 tokens for the harness change
- But then lessons 34–36 can be broken into 9–12 smaller tasks at 30 mins each
- Total cost per task: drops from 1200s to 300–400s

Long-term: decomposition is cheaper. Short-term: growing the budget is cheaper.

## Quota Policy Decision

Current budget allocation:
- Per-block: ~1M tokens (rough, based on 5 lessons × ~200k per lesson)
- Current burn: ~180k wasted on timeouts
- That's 18% of block budget going to timeouts

This is unsustainable. By lesson 50, if we hit the ceiling every 3–5 lessons, we'll be burning 30–40% of quota on timeouts.

**Recommendation**: Set a timeout-waste threshold (e.g., no more than 5% per block). When lessons hit it, trigger automatic cost-benefit analysis:
- If decomposing costs less than growing budget: decompose
- If growing budget costs less: grow and schedule decomposition for next phase

## Model Selection Implication

Lesson 36 used opus and still timed out. This tells us:
- Model selection is not the limiting factor (we already escalated to the heaviest model)
- Task *scope* is the limiting factor (too much work for any single model in 1200s)

Therefore, throwing a heavier model at the problem won't solve lessons 34–36. Only growing the wall-clock budget or decomposing the tasks will.

## Monitoring Recommendation

Add quota alerts for:
1. **Timeout rate per block** — If >10% of block efforts timeout, escalate
2. **Cumulative timeout tokens** — If >10% of block quota is spent on timeouts, flag
3. **Model escalation depth** — If we've escalated from sonnet to opus and still timeout, decomposition is needed

## The Operational Play

From a cost and quota standpoint:
1. Growing the budget to 2400s is a short-term tactical fix (~$50–100 extra per block)
2. Decomposing tasks is a medium-term strategic fix (requires engineering, saves ~40% quota on synthesis tasks long-term)

**Move forward with both in parallel:**
- Increase budget immediately (low risk, enables retries)
- Schedule task decomposition for next 2–3 blocks (higher risk, higher payoff)

This keeps the loop moving while addressing the root cause.

## Bottom Line

Lessons 34–36 cost the organization 180,000 tokens in wasted quota. That's real money. The decision to grow the budget or decompose tasks is not just architectural—it's financial. Decomposition pays for itself in 3 blocks if it reduces average task time by 30%.

Calculate the breakeven and make the call.
