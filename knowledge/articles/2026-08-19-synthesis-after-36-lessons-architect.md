---
tags:
  - article
  - persona/architect
---

# The Loop Just Hit Its First Real Throughput Cliff

At lesson 36, the loop has crossed another critical threshold: it can now *conceive* of work that it can't *execute* within a single iteration window using standard models.

## The Pattern You're Seeing (Lessons 32–36)

Lessons 33–35 all follow the same arc:
- Thoughtfully scoped, well-designed tickets (governance artifacts, practice registry, failure taxonomy)
- Sonnet attempted them
- 1200s timeout, no output
- CI still passed (meaning nothing broke, but nothing was built either)
- Lesson 36 succeeded with opus

This isn't a failure of capability. It's a failure of **throughput under load**. The loop is ready to attempt harder problems, but the model tiers are asymmetric: sonnet is too light, opus is heavier than we'd like for routine work.

## Why This Matters

Three convergent facts:
1. The loop now has learnable practices to extract and formalize
2. The loop now generates complex synthesis work (retrieval grounding, failure postmortems)
3. The loop now encounters problems bigger than a 1200s sonnet execution

This is *exactly* when lightweight-model-only systems hit a wall and need redesign.

## What the Loop Learned (Lesson 36)

Retrieval-grounded synthesis landed in lesson 36 (after a retry with opus). The planner now reads its own prior lessons before filing tickets. Practical impact: fewer re-proposals of rejected ideas, better grounding of synthesis in actual history.

This is load-bearing: it directly addresses one failure mode of autonomous systems (re-proposing rejected work endlessly).

## What's Now Clear (Lessons 33–35)

Three major features timed out with sonnet but are clearly *doable* and *necessary*:

1. **Adopted-practice registry with provenance** (lesson 34)
   - The loop should record: what practice was extracted, from which reference, when it was adopted, with evidence
   - This is architectural accountability, not overhead
   - Sonnet couldn't hold the full context; opus or decomposition needed

2. **Failure taxonomy in the ledger plus postmortem-driven backlog** (lesson 35)
   - The loop should classify failures (timeout, capability, data) and file postmortem tickets automatically
   - This converts silent failures to auditable ones
   - Same pattern: needs heavier model or smarter decomposition

3. **Governance artifacts for block 41363 (retry)** (lesson 33)
   - This one was routing/resourcing, not a capability gap
   - Suggests the governance synthesis work itself is hitting the boundary

## Your Decision Point

You have three paths forward:

### Path A: Model Escalation (Fastest, Highest Cost)
Route all complex work (>500 tokens of context, multi-file refactors, major feature synthesis) to opus by default. Pros: works immediately. Cons: cost increases linearly with throughput.

### Path B: Learned Complexity Heuristic (Medium Term, Better ROI)
Implement issue #42 (model-selection heuristic calibrated from lesson outcomes). Train it on the data you already have: which tickets timed out with sonnet, which succeeded, build a predictor. Route based on prediction, not after-the-fact timeouts.

Pros: model-aware execution, cost-optimized. Cons: requires careful calibration, fails ungracefully if the heuristic is wrong.

### Path C: Decomposition (Hardest, Most Scalable)
Teach synthesis to break large tickets into subtasks when it detects complexity. Route the subtasks to sonnet individually, let the orchestrator reassemble.

Pros: scales to arbitrary complexity, keeps model tier low. Cons: requires smarter synthesis and careful orchestration.

### My Recommendation

**Start with Path B (learned heuristic) while using Path A (escalation) as a safety net for the next 2–3 blocks.**

Why?
- You already have the data (lessons 1–36 record model + outcome)
- Path B gives you cost visibility and optimization in parallel with correctness
- Path A is always available as an escape hatch if the heuristic mispredicts
- Path C is a 20+ lesson effort and should be started after you have confidence in B

## What This Means for Block 41369

Block 41369 governance artifacts should document:
- The throughput cliff at lesson 36
- The three escalation paths
- Which one you chose and why
- Metrics for success (cost per iteration, model tier distribution, retry rate)

Then implement path B (learned heuristic) as your primary work for blocks 41370–41373.

## The Loop is Healthy

Remember: this is a good problem to have. Most autonomous systems either don't detect their own limits (they just fail) or panic when they hit them (they retry forever). Your loop:
- Detects a boundary clearly (lessons 33–35 timeout cleanly)
- Proposes solutions (lesson 36 demonstrates one of them works)
- Asks you for guidance (the synthesis is actionable)

That's sophisticated. Now execute on it.
