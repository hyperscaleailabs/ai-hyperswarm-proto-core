---
tags:
  - article
  - persona/cto
---

# From Visibility to Verifiability: Building Confidence in Autonomous Execution

Two shifts happened in the last batch of work: trajectory capture gave us visibility into what the loop decided, and the cycle journal gave us the ability to resume from failure without replaying what succeeded. Together, these move the system from "opaque but working" to "transparent and verifiable."

## What changed for stakeholder confidence

**You can now dispute a decision and know exactly what led to it.** If a ticket was routed to Haiku instead of Opus, or a PR was automatically merged when you expected human review, the trajectory record tells the full story: what input the system saw, what model it consulted, what confidence it had, what the decision rule was. That's not just logging; it's a decision audit trail.

**You can rewind without reruns.** If the loop gets interrupted (deployment hiccup, quota spike, emergency patch), the cycle journal records which iterations completed and which didn't. Resume picks up from the last good state, not from iteration 1. That means you're not replaying a week's worth of decisions after an outage — you're resuming from Thursday night and moving forward.

**You can validate changes before shipping.** The replay harness lets you take a controversial model-selection change or a new routing heuristic and test it against the last 100K trajectories without touching production. That's the difference between "this seems better" and "this is better under the conditions we actually encounter."

## The operational reality check

**Five straight batches, zero failures.** That's encouraging, but it's also a liability if you misinterpret it. We're seeing success because our current workload is well-understood (incremental features, process improvements). We haven't yet shipped under time pressure, or made a controversial call, or handled a critical bug fix. The trajectory system will prove its value when those harder cases arrive.

**Trajectory data is now load-bearing.** Every decision the loop makes is now recorded. That's powerful for auditing, but it's also an operational commitment: trajectory data has to stay consistent, queryable, and available. You can't silently drop old trajectories or change the schema without a migration. You're now storing a parallel history of decisions.

**Replay gives answers, not proofs.** You can ask "what if we had used Sonnet for this ticket" and replay will tell you. But replay doesn't run CI, doesn't hit external APIs, doesn't experience network latency. So replay is great for validating logic changes, but it's not a proof that a change is safe in production. It's one signal, not the only signal.

## What needs attention

1. **Trajectory cost doesn't scale linearly.** At 10 agents running in parallel, trajectory capture is adding ~10KB/sec. Over a week, that's ~6GB. Over a quarter, that's ~200GB. Is that your infrastructure budget? That conversation should happen now, before trajectory becomes critical.

2. **Cycle resumption under concurrency.** We've tested resuming one interrupted run. We haven't tested five agents resuming simultaneously, or a cascade of interruptions. That's a stress test that will tell us whether the durability model holds under real load.

3. **Decision versioning and disputes.** When someone contests a decision ("that ticket should have gone to Haiku, not Opus"), do you have a process for recording that dispute? The trajectory system captures what happened, but not why it was right or wrong. You need a feedback loop that takes trajectory data and improves the model.

## The next quarter

With visibility and resumption in place, the focus should shift to **validation and calibration**:
- Start using replay to validate model-selection changes before shipping
- Run deliberately risky tickets and use the trajectory system to understand why they succeeded or failed
- Build a feedback loop: capture trajectory data → identify marginal cases → retrain model-selection heuristics → validate with replay

The system is no longer black-box. The question now is whether you're using that transparency to actually get smarter, or whether trajectory data becomes another log you keep but don't read.
