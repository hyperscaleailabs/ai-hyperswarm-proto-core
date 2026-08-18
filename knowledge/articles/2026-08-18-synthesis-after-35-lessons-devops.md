---
tags:
  - article
  - persona/devops
---

# Lessons 32–35: The Infrastructure Is Reliable, Quota Management Needs Tuning

At lesson 35, your infrastructure is running smoothly. CI is green. The governance loop is solid. But quota management is giving you a signal: the standard model tier can't handle all the work you're asking it to. That's not a deployment problem — it's an allocation problem.

## What the ops metrics show

**Uptime and CI status:**
- All 4 lessons (32–35) had CI green (both passes and fails)
- No infrastructure failures, no crashes
- Timeout is a scheduling signal, not a system failure
- The loop remains graceful under resource exhaustion (no cascading failures)

**Resource utilization:**
- Lessons 32–33 (governance): ~10–30 min wall-clock, haiku tier
- Lesson 34 (features): ~1200s timeout with sonnet, incomplete work
- Lesson 35 (features): ~1200s timeout with sonnet, incomplete work
- Pattern: governance scales fine at haiku; features need more resources

**Quota burn:**
- Governance per-cycle: low (haiku is cheap, fast)
- Feature timeouts: high (no work delivered, quota spent, wall-clock wasted)
- Blocked retries: backlog accumulation, no forward progress

## The operational decision tree

When a lesson times out with `[phase=implement, ticket=#X] timeout after 1200s`:

1. **Check CI:** If CI is green, the work is incomplete, not broken. This is a resource constraint, not a bug.
2. **Check the ticket kind:** If it's `feat:` touching `governance/` or `knowledge/`, the standard model tier is insufficient.
3. **Check the backlog:** If retries are blocked, this is now a **critical path issue** — the loop cannot make progress.

Lessons 34–35 hit all three: CI green, feature kind, infrastructure touch, backlog blocked.

## What needs to change operationally

### Short-term (lesson 36–37)

1. **Add model-tier hints to high-value feature tickets.**
   - Tickets #272 (adopted practices) and #273 (failure taxonomy) should have `model_tier: opus` in their metadata
   - This is a one-line fix in the ticket schema; no code changes needed

2. **Log model-tier routing decisions.**
   - When a ticket is routed to opus (vs. sonnet), log it with timestamp, ticket ID, reason
   - This gives you the data you need to tune future heuristics

3. **Set up quota alerts for timeout patterns.**
   - If more than 1 ticket in a block times out at sonnet, trigger an alert
   - Alert text: "Consider routing to opus or decomposing"

### Medium-term (lesson 37–40)

1. **Implement the model routing logic** (if architect approves).
   - If `feat:` ticket + `governance/` or `knowledge/` touch → use opus
   - If `implement:` ticket + predicted complexity high → consider opus
   - All routing decisions logged with outcome (pass/fail/timeout)

2. **Tune the quota per block.**
   - Current: unlimited sonnet, escalate on timeout
   - Better: pre-allocate 60% to haiku (governance, small features), 30% to sonnet (medium features), 10% to opus (complex features)
   - Adjust ratios based on block outcomes

3. **Build the data pipeline for learned routing.**
   - Export lesson outcomes, ticket metadata, model tier, wall-clock, token usage to a database
   - Prepare for training a classifier by lesson 41+

### Long-term (lesson 41+)

1. **Deploy the learned routing model** (if the data supports it).
   - Input: ticket kind, file-set, estimated complexity
   - Output: recommended model tier (haiku/sonnet/opus)
   - Start with low confidence (require human override for opus), increase as accuracy improves

2. **Automatic quota rebalancing.**
   - If the learned model predicts high opus usage in a block, pre-allocate more opus quota
   - If a block has low feature velocity, use slack quota for exploratory work

## The cost analysis

**Current state (sonnet-first):**
- Lessons 34–35 timeouts: 2400s wall-clock wasted, no work delivered
- If this continues, expect 1 timeout per 5–10 complex features
- Annual impact: ~50–100 timeouts × ~1200s = ~50–100k seconds of wasted time + quota spent

**Proposed state (intelligent routing):**
- Same features routed to opus first → 2 passes in lessons 37–38
- Quota cost: ~3× sonnet per pass, but 100% success rate
- Annual impact: no timeout waste, predictable quota burn, features shipped on schedule

**Break-even:** About 3–5 complex features. After that, routing to opus on complex features is cheaper than sonnet retries.

## What to monitor

1. **Timeout rate per block:** Should drop from 50% (lessons 34–35) to <10% after routing is implemented
2. **Feature velocity:** PRs per block should increase as features stop timing out
3. **Quota efficiency:** Quota spent ÷ PRs merged should improve (fewer wasted retries)
4. **Model tier distribution:** Track haiku/sonnet/opus usage over time; should stabilize as heuristics mature

## The alert thresholds

Set these for your observability system:

| metric | threshold | action |
| --- | --- | --- |
| timeouts per block | >1 | Review which tickets timed out, consider routing to opus |
| sonnet-feature-fail rate | >20% | Check if complex features are being routed to sonnet; consider upgrading selection heuristic |
| opus quota per block | >40% total | Review if too many tickets are being routed up; tune the heuristic |
| wall-clock per lesson | >1800s | Check if a ticket is hitting resource limits; consider timeout extension or decomposition |

## The infrastructure status

**Green lights:**
- CI is solid (all lessons had green CI, even the timeouts)
- No infrastructure failures or crashes
- The loop remains stable under load

**Yellow lights:**
- Timeout rate is high for features (2/2 in this window)
- Quota is being spent on incomplete work (lessons 34–35)
- Feature backlog is blocked (no progress on adopted practices, failure taxonomy)

**What needs fixing:**
- Model-tier routing (not an ops problem, but an execution-layer problem)
- Quota pre-allocation (should plan for opus usage, not assume sonnet for all)

## The ops recommendation

Implement model-tier hints in the ticket schema (lesson 36), then retry lessons 34–35 with opus hints (lesson 37–38). This is a low-ops-cost intervention that should immediately unblock the feature backlog.

Monitor the outcomes closely. If both pass with opus, you've validated the hypothesis and can proceed with the learned routing model. If not, dig into why opus didn't help — it might be a different constraint (memory, architecture, team-knowledge gap).

After that, you'll have the data and confidence to implement automatic routing. By lesson 40+, you'll have a self-scaling system that allocates resources based on learned patterns, not reactive timeout.

## Your move

Block 41365 is when you decide: Do you treat timeouts as one-off issues, or as a signal to build smarter resource allocation?

Given the consistent pattern (governance works fine, complex features timeout), I'd treat this as a signal. Implement the routing layer, collect the data, build the classifier. By lesson 40, you'll have a system that scales intelligently.

Until then, expect the pattern to repeat: governance runs smooth, complex features stall, quota gets wasted on retries.
