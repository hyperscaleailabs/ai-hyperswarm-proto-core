---
tags:
  - article
  - persona/devops
---

# Ops Signal from 30 Lessons: The Timeout That Repeats

Lessons 29–30 sent the same CI signal twice: **SUCCESS**. But the worker reported **TIMEOUT** both times. This is the operational pattern you need to recognize and have a runbook for.

## The Signal Chain

```
Lesson 29:
  Worker:     TIMEOUT after 1200s during implement phase
  CI:         SUCCESS (green across ruff, pytest, remote checks)
  Verdict:    FAIL (outcome = fail, but CI = green)

Lesson 30:
  Worker:     TIMEOUT after 1200s during implement phase  
  CI:         SUCCESS (green across ruff, pytest, remote checks)
  Verdict:    FAIL (outcome = fail, but CI = green)
```

This divergence — CI says "go," worker says "stop" — is real and reproducible. It is not flakiness. It is not a CI false positive. It is a **resource divergence**: the worker has stricter resource constraints than CI.

## Why CI Passed But Worker Timed Out

The worker runs on a 1200s wall-clock budget, enforced by `agent_timeout_seconds: 1200` in core.yaml. The CI runs on a 300s polling budget, enforced by `ci_remote_timeout_seconds: 300`, which controls how long the orchestrator waits for CI to report a result — not how long CI itself can run.

This is a **feature**, not a bug. You want:
- Worker: strict budget, protects machine from runaway jobs
- CI: lenient budget (remote cloud can handle it), provides the truth

But it means:
- Some tickets can *reach* CI green (hence the FAIL verdict, not BLOCKED)
- But cannot *complete* inside the worker's budget
- These tickets are real problems that need escalation, not ignorable corner cases

## The Ops Runbook for "Worker Timeout, CI Green"

1. **Identify:** Look for `outcome=fail` AND `remote_ci=SUCCESS` AND `agent_error=timeout`
2. **Triage:** This is a resource contention issue, not a code correctness issue
3. **Action:** Escalate ticket to a heavier model tier or split into subtasks
4. **Track:** File this in the quota ledger as "escalated due to budget," not as a random failure
5. **Alert:** If this pattern appears in >20% of lessons, your worker budget is too tight for your workload

## Current Threshold: 1200s

The 1200s limit is set by `agent_timeout_seconds: 1200` in core.yaml. This protects the local machine from runaway agents. It is a good default. But it is strict enough that complex tickets (like verifiable subscription-only execution) can exceed it.

Do **not** raise this limit as a reflex. Instead:
- Use escalation logic (assign heavier model on retry)
- Use decomposition (split complex tickets before assignment)
- Use human escalation (some tickets need human judgment)

Raising the limit just moves the problem to a different ticket down the line.

## Metrics to Track

Add these to your observability stack:

| Metric | Signal | Action |
| --- | --- | --- |
| `lessons_timeout_pct` | % of lessons that timeout | If >15%, review budget allocation |
| `lessons_timeout_then_pass_pct` | % of timeout retries that eventually pass | If high, escalation strategy is working |
| `avg_worker_seconds_per_lesson` | Wall-clock seconds spent per lesson | Baseline for capacity planning |
| `quota_per_lesson_by_tier` | Quota cost per lesson (broken down by tier) | Input to cost models |
| `timeout_by_ticket_kind` | Which ticket kinds timeout most? | Target decomposition strategy |

Lessons 29–30 give you two data points: both timeouts on "implement: feature" kind. One more data point and you have a pattern. Three patterns and you have a policy.

## What to Communicate Upward

To stakeholders asking "is the loop working?":
- ✅ **Correctness:** CI says yes. The features that shipped passed all checks.
- ✅ **Governance:** Every lesson is recorded with model, ticket, and outcome.
- ⚠️ **Efficiency:** Worker budget is tight. Escalation logic needed.
- 📊 **Trend:** 2/3 recent lessons passed, 1/3 timed out. This is at the edge of budget.

Recommended action: Implement tier escalation (low cost, high impact) before the next heavy feature ticket.

## Why This Matters for Autonomous Systems

A loop that times out but still produces CI-green code is not "broken." It is "over-budget." You need operational awareness of this state, a runbook for it, and a policy for it. That maturity is what separates "experimental loop" from "production loop."

Lesson 30 is your system telling you: "I can build features, but you need to give me a way to ask for more time or more resources when I need them." That is a healthy signal.
