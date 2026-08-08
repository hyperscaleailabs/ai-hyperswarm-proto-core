---
tags:
  - article
  - persona/devops
---

# Optimizing for Quota: Learned Model Routing in Practice

> For: DevOps level - operations, monitoring, cost optimization
> From: [[2026-08-08-synthesis-after-22-lessons]]

## The Quota Ledger Becomes Predictive

Up to now, the quota ledger was a record: after every iteration, log the cost. Now it's become predictive: every new task predicts its likely cost based on historical patterns, and the router routes accordingly.

Here's the flow:

1. **Task arrives**: "implement: feat X" ticket
2. **Classifier runs** (haiku, ~200 tokens): What complexity is this? (based on title, description, recent themes)
3. **Router consults ledger**: For "implement/feat" tickets, which model has the best success-to-cost ratio?
4. **Route decision**: If Haiku has 80% success and Sonnet has 100%, router computes:
   - Haiku: 80% * 5k tokens = 4k expected tokens
   - Sonnet: 100% * 30k tokens = 30k expected tokens
   Haiku wins unless task complexity score is "very high"
5. **Execute**: Implement with chosen model
6. **Log outcome**: model, success, actual tokens spent → updates ledger for next iteration

## Monitoring and Thresholds

**Soft breach (80% of quota)**: Bias router heavily toward cheaper tiers. Log a warning.

**Hard breach (100% of quota)**: No new work starts, but in-flight PRs can finish. This prevents starting expensive work near the ceiling.

**Observable metrics per iteration**:
- `tokens_spent` - actual tokens used
- `tokens_predicted` - what classifier predicted
- `model_used` - which model ran
- `outcome` - pass/fail
- `task_kind` - feat/fix/chore/etc.

Operator should monitor:
- **Tokens-per-merged-PR trend**: Currently ~5,000. If trending up, model selection is degrading (routing to expensive tiers too often).
- **Success rate by model and kind**: A sudden drop in haiku-on-chores success suggests the classifier is miscalibrating.
- **Ledger age**: The ledger is append-only; old entries (>30 days) should be pruned to keep weights calibrated to recent patterns.

## Early Quality Gate: Operational Impact

The adversarial acceptance-criteria gate has two operational effects:

**Positive**: Fewer failed CI cycles. Before, maybe 5% of PRs hit CI and then failed on acceptance-criteria mismatches. Now, 0% of PRs open with that problem—the skeptic caught it.

**Cost**: The skeptic runs before every PR, so every ticket costs an extra ~500 tokens (haiku per task). For 100 tasks/month, that's 50k tokens extra. But it saves ~5 failed CI cycles/month × 20k tokens/cycle = 100k tokens saved. Net win: -50k tokens.

## Deployment Checklist

- [ ] Quota ledger is populated with historical data (current: 22 iterations)
- [ ] Classifier model is running and logging predictions
- [ ] Router weights are updated after each merge
- [ ] Acceptance-criteria skeptic gate is enabled by default
- [ ] Alerts fire when soft/hard quota thresholds are crossed
- [ ] Trajectory capture is on (for replay debugging if routes fail)

## Tuning Recommendations

**If quota usage is trending down**: Increase the complexity threshold for sonnet; haiku is winning more tasks.

**If quota usage is stable but success rate drops**: Recalibrate weights; the classifier or historical data may be stale.

**If early gate is blocking too many PRs**: Loosen skeptic threshold (require higher confidence to refute) or increase skeptic cost budget (move from haiku to sonnet).
