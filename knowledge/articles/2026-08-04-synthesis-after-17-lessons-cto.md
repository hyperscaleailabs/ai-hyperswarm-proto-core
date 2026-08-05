---
tags:
  - article
  - persona/cto
---

# Building Observability into Autonomous Systems: The Trajectory Capture Milestone

Two new features shipped this week that form the foundation of a critical capability for operating autonomous systems at scale: **trajectory capture** and **offline replay**. Together, they transform the autonomous loop from a black box that reports final outcomes into an auditable system where every decision can be examined, contested, and learned from.

## What actually changed

**We can now replay decisions.** When a ticket gets routed to a model or an action gets disputed, we don't have to re-run the entire task. We can take the recorded trajectory, swap in a different model choice or heuristic, and see what the system *would* have decided under different conditions. This is worth emphasizing because it's the difference between "we shipped this and it worked" and "we *understand* why it worked and could have chosen differently."

**Structured decision logs.** Every agent execution now produces a JSON-formatted record of inputs, reasoning, and outputs. This isn't telemetry in the classical sense (metrics, latency, error rates). It's decision telemetry — the what-and-why of each choice the system made. For a CTO, this is gold: you can now audit whether the system is making choices you'd actually make, or whether it's drifting into patterns you didn't intend.

## The honest assessment

**The streak is longer now, but it's still just five iterations.** We have zero failures across this batch and the previous batch. That's encouraging from a reliability standpoint, but it's also a sign that our current workload doesn't stress-test the system. We're hitting the well-understood cases — incremental features and process improvements. We haven't yet shipped a risky migration, a critical bug fix under time pressure, or a controversial architectural decision where the replay capability would be most valuable.

**Cost and scale remain unknowns.** Trajectory capture adds storage and logging overhead per execution. We haven't yet measured whether this scales linearly, or whether we hit an inflection point (memory, query performance, billing) at some unknown concurrency level. That's not a blocker — it's a reminder that "works in dev" and "works at 10x scale" are different claims.

**Replay isn't reproduction.** Being able to simulate a decision under different conditions is useful, but it's not the same as understanding what happened *in the field*. A replay can tell us the model was uncertain, but it can't tell us whether the real CI failure was deterministic or flaky. That gap is worth staying aware of.

## Strategic implications

With trajectory capture and replay in place, the next three quarters should focus on:

1. **Putting replay to work.** Set up a review process that actually uses replay — when someone flags a decision as questionable, replay it against other models and let that inform calibration.
2. **Stress-testing through risky work.** Deliberately schedule higher-risk tickets (migrations, major refactors, time-sensitive fixes) so the trajectory system proves itself under conditions where it's actually needed.
3. **Learning from near-misses.** Start tracking decisions that "barely" succeeded (low confidence, high uncertainty, tight deadlines) and understand what made them work despite the risk signals.

The trajectory system is in place. The question now is whether we use it to actually become more observant, or whether it becomes invisible infrastructure that no one reads. That's an operational discipline problem, not a technical one.
