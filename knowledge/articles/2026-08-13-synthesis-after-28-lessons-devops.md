---
tags:
  - article
  - persona/devops
---

# No Pager Alerts, But Better Instrumentation Needed

> For: DevOps level - CI/CD, automation mechanics, operational lessons
> From: [[2026-08-13-synthesis-after-28-lessons]]

## The Good News: Still No Production Incidents

From lesson 1 through lesson 28, zero pager alerts. The loop has never crashed, hung indefinitely, or exceeded quota in a way that requires manual intervention. Durable journals recovered from interrupts cleanly. CI gates held. That's strong.

## The Reality: We're Flying Blind on Key Metrics

Lessons 23–25 revealed a blind spot: the loop can fail silently (agent ok=False), and we won't know about it unless someone reads the lesson file. No metric, no alert, no dashboard entry. This is a visibility problem, not a capability problem.

### What We Know vs. What We Don't

**We can see**:
- ✅ CI status (green/red)
- ✅ Quota consumption per block (tokens spent)
- ✅ Wall-clock block duration
- ✅ Lesson outcomes (pass/fail) – *if you read the file*
- ✅ Model used (haiku, sonnet, opus)

**We can't see**:
- ❌ Worker runtime distribution (is the average drifting?)
- ❌ Agent ok=False rate (how often does synthesis fail silently?)
- ❌ Timeout frequency (are we hitting the 1200s wall more often?)
- ❌ Synthesis validation results (which tickets got caught/rejected?)

This gap is why we didn't catch the stall in real-time. Lesson 23's timeout was visible (blocked the worker), but lessons 24–25's silent halts were invisible until we reviewed the lesson files.

## Instrumentation Plan

### Immediate (low effort, high value)
1. **Add metrics to iteration journal** (modify `hsai.ledger` or lessons schema):
   - Worker runtime per iteration (to trend scaling)
   - Agent ok value (0 or 1) as a metric
   - Synthesis validation pass/fail (count)
   
2. **Export metrics to stdout/logs** so they can be picked up by existing monitoring

3. **Create dashboard queries**:
   - `ok=False rate per block` (alert if > 10%)
   - `Worker runtime by block` (histogram, trend line)
   - `Timeout count per block` (alert if > 0)

**Effort**: ~2–3 hours (modify ledger schema, add logging, create dashboard)

### Medium term (medium effort, medium value)
1. **Streaming telemetry** – Send worker start/end timestamps to a metrics sink (Prometheus, Datadog, etc.) in real-time, not batch at block end

2. **Block-level circuit breaker** – If ok=False rate exceeds threshold in a block, pause synthesis and alert human

3. **Replay instrumentation** – When resuming a block with `hsai replay`, log which lessons were replayed and which are new

**Effort**: ~1 day

### Long term (high effort, high value)
1. **Cost optimization dashboard** – Track model usage (haiku vs. sonnet vs. opus) against quality metrics

2. **Anomaly detection** – Flag when worker runtime suddenly increases or quota per PR exceeds a threshold

3. **Audit log consolidation** – Centralize lesson files + CI logs + telemetry in a queryable format

**Effort**: ~2–3 days

## Operational Readiness Now vs. Before

| aspect | lesson 25 | lesson 28 | trend |
| --- | --- | --- | --- |
| No pager alerts | ✅ | ✅ | ↔️ stable |
| CI gate | ✅ | ✅ | ↔️ stable |
| Quota protection | ✅ | ✅ | ↔️ stable |
| Silent failure visibility | ❌ | ❌ | ↔️ unchanged |
| Worker scaling visibility | ❌ | ❌ | ↔️ unchanged |

The loop's operational health is good, but our visibility into the loop's health is still poor. That's a monitoring problem, not a system problem.

## Recommendations

1. **Implement immediate metrics** (2–3 hours). The stall at lesson 25 should have triggered an alert. It didn't, and that's on us.

2. **Test the metrics plan** by running the loop for 2 more weeks (to lesson 35–40) and validating that the metrics catch any anomalies before they become incidents.

3. **Defer medium/long-term instrumentation** until we see the first real anomaly with the immediate metrics in place. No point building advanced alerting for data we haven't validated.

## Quota Status

Through lesson 28, the loop is staying well within budget:
- Typical block spend: 15–20k tokens (varies by model selection)
- Quota per block (hard ceiling): 100k tokens
- No block has exceeded 50k tokens
- **Headroom**: 2–3x typical spend before hitting ceiling

No changes needed to quota gating. Continue current policy.
