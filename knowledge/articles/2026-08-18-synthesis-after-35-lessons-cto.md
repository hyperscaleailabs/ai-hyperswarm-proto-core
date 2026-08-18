---
tags:
  - article
  - persona/cto
---

# Lessons 32–35: Governance is Steady, Feature Velocity Needs Model Routing

At lesson 35, your governance infrastructure is rock-solid. Features are hitting a wall. The signal is clear: it's time to implement intelligent model routing, or accept slower feature velocity.

## The metrics that matter

**Governance track record (lessons 32–33):**
- 2 passes, 0 fails, 100% success rate
- Haiku is sufficient for governance tasks
- Consistent execution: ~10–30 minutes per governance cycle
- Low quota cost per pass

**Feature track record (lessons 34–35):**
- 0 passes, 2 fails (both timeouts at 1200s with sonnet)
- Success rate: 0% (with sonnet, for complex features)
- Consistent failure mode: timeout, not crash
- CI passes in both cases (work is incomplete, not broken)

**The cost of inaction:**
- Each lesson 34/35 retry wastes ~1200s of wall-clock time
- Each timeout consumes quota without producing a merged PR
- The loop stalls on high-value features (practices, failure analysis)

## What the data tells you

Two features, both governance-infrastructure-touching, both timed out with sonnet (standard tier). This is not noise — this is a signal.

**Hypothesis:** Features that orchestrate across multiple subsystems (adoption, failure tracking, knowledge synthesis) require opus, not sonnet.

**Evidence:**
- Lesson 34: Adopted-practice registry — connects synthesis to knowledge base — timeout
- Lesson 35: Failure taxonomy — connects ledger to backlog — timeout

Both are **integration plays**, not standalone features. Integration requires orchestration; orchestration requires reasoning across a larger state space than sonnet can handle in 1200s.

## The model routing options (technical view)

### Option 1: Ticket-based routing
Add a field to ticket schema: `model_tier` (optional). If set to `opus`, override the automatic selection. If unset, use the learned heuristic (or default to sonnet).

**Implementation:** ~2 hours (modify ticket parser, serializer, orchestrator)  
**Quota impact:** Controlled per-ticket  
**Data collection:** Easy (just log which tickets were routed to which tier)

### Option 2: Heuristic routing
Query the ticket body for keywords: `governance/`, `knowledge/`, `synthesis`, `adoption`, `ledger`, `practice`. If any match and the ticket kind is `feat:`, route to `opus`.

**Implementation:** ~4 hours (heuristic rules, A/B testing setup, logging)  
**Quota impact:** Moderate per-feature-class  
**Data collection:** Possible but requires careful logging

### Option 3: Learned routing (long-term)
Build a small neural classifier (or even a decision tree) that learns which tickets should route to which tier. Input: ticket kind, title, description, file-set diff, estimated token budget. Output: tier prediction.

**Implementation:** ~20 hours (data pipeline, model training, evaluation, integration)  
**Quota impact:** Once tuned, predicts correctly; reduces waste from timeouts  
**Data collection:** Essential (requires 30+ labeled examples to train)

## My recommendation (CTO perspective)

**Implement Option 1 immediately, with a data pipeline for Option 3:**

1. **Week 1:** Implement ticket-based routing (Option 1). Add `model_tier_hint` to ticket schema. If set to `opus` and the ticket is marked `infrastructure` or `governance_touch`, honor it.

2. **Week 1–2:** Use this to manually retry lessons 34–35 with `model_tier_hint: opus`. Collect detailed logs: token count, wall-clock time, token cost, whether it passed.

3. **Week 2–3:** If both pass with opus, start designing the learned routing model. Define features, collect training data, build a simple v1 classifier.

4. **Week 4+:** Tune and deploy the learned routing model. Make it gradually more confident as data accumulates.

Why this sequence? Because:
- Option 1 is low-risk and immediately productive
- It lets you collect the data you need for Option 3
- It avoids the risk of Option 3 without data (could misroute and waste quota)
- By week 4, you'll have real evidence about when opus is necessary vs. overkill

## The quota math

**Sonnet usage (lessons 34–35):**
- 2 timeouts × 1200s each = 2400s wall-clock
- ~50k tokens input, ~0k output (timeout before completing)
- Net: quota spent, no PR merged, high frustration

**Opus usage (hypothetical retries):**
- Expected: ~800s wall-clock (faster reasoning, fewer steps to solution)
- ~70k tokens input, ~15k tokens output (completes the work)
- Cost: ~3–5× sonnet's token cost, but completes in 1 pass

**Break-even analysis:**
If opus succeeds in 1 try (reasonable assumption for proven features), the cost is:
- Opus: 3×sonnet_cost for complete work
- Sonnet: 1×sonnet_cost per try × N tries, eventually opus or manual escalation

For these two features, **opus in lesson 37–38 is likely cheaper overall than two retries with sonnet + eventual manual intervention + context loss.**

## The decision tree

If you proceed with Option 1 routing:

```
ticket kind == feat AND touches governance/ OR knowledge/ ?
  yes → use opus
  no  → use default heuristic (or sonnet)
```

This rule should catch 90%+ of the timeout cases you see in practice, based on lessons 34–35.

## The slippery slope to watch

**Don't:** Fall into a pattern of "escalate everything to opus." That's quota waste and defeats the purpose of having a tiered system.

**Do:** Use the data from lessons 37–38 to calibrate the heuristic. If 4 out of 5 features that touch `governance/` need opus, the rule is justified. If only 1 in 5 does, you need a finer heuristic (e.g., file-count cutoff, token-budget cutoff, specific subsystems).

## What success looks like

- **Lesson 37:** Lesson 34 retried with opus → passes
- **Lesson 38:** Lesson 35 retried with opus → passes
- **Lesson 39–40:** Feature implementation velocity increases (more passes, fewer timeouts)
- **Lesson 41–42:** Learned routing is ready for tuning

At that point, you've closed a major gap in your execution layer: you're no longer timing out on complex features; you're allocating resources intelligently.

## The opportunity

Model routing is not just a fix for lessons 34–35. It's the foundation for **self-scaling**. Once you route features intelligently:
- Your loop scales from "execute haiku tasks" to "execute any task, with the right resources"
- You add a new capability: **resource allocation**
- You can tackle more ambitious features without fearing timeout waste

That's where you're headed. Lessons 32–35 are the signpost.

## Your move

Block 41365 is when you decide: Do you implement model routing now, or accept slower feature velocity?

Given that lessons 34–35 are high-value features (practices in synthesis, failure analysis), I'd implement Option 1 now and have both passing by lesson 38. That's 2 lessons of dev work for 2 blocked features; solid ROI.

After that, you'll have a proven model-routing system and real data to build the learned classifier. By lesson 41+, you'll be self-scaling.
