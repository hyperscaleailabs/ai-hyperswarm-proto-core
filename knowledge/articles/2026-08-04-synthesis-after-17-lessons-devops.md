---
tags:
  - article
  - persona/devops
---

# From Decision to Data: What Trajectory Capture Means for Observability

Trajectory capture and replay landed this week, and from an operations perspective, this is the moment the autonomous loop stopped being opaque and started being observable. Every agent execution now produces a structured record: inputs, reasoning path, outputs, model metadata. No more reconstructing a decision from logs or taking the final outcome on faith.

## The mechanics that shipped

**Worker trajectory stores.** Every time an agent runs, its execution is now recorded with full provenance: what inputs it got, what model it used, what intermediate decisions it made, what output it produced. This is queryable after the fact. If you want to know "which 10 percent of tickets got routed to Haiku and what happened to them," you can now ask that without re-running the loop.

**Replay harness for offline validation.** We can take a recorded trajectory and play it back against a different model, a different heuristic, or a future version of the system without touching production. This is the bridge between "we captured data" and "we learned from it." It's also the safety mechanism — before shipping a change to model selection, you can validate it against a month of historical trajectories and see whether you'd have made different calls.

**Structured JSON output from agents.** Agents don't just produce text anymore; they emit structured decision records. A model picks a task tier. The JSON says `{"tier": "light", "confidence": 0.87, "reasoning": "ticket is scoped and similar to X"}`. That structure is what lets downstream systems parse intent rather than guess at it.

## The operational reality

**Seven terabytes per million executions.** Our traject​ory store currently grows at roughly 7KB per execution. For scale, that's manageable up to about a million runs (the size of a multi-month corpus). Beyond that, we need a retention/archival strategy we don't have yet. No blocker for now, but this is a forcing function for Q4.

**Replay latency is low, but not zero.** Replaying a trajectory is cheaper than re-running the orchestrator (no CI, no waits), but it's not instant. Full-corpus replay (validating a new model-selection heuristic against 100K historical trajectories) takes about an hour. That's fine for batch validation, but it means "replay to verify before deploy" is a "run it overnight" operation, not a pre-commit check.

**Query performance is becoming the choke point.** The trajectory store is queryable, but write-heavy OLTP databases get slow when you're trying to answer aggregate questions ("what was the model distribution across all trajectory[kind='implement'] in the last week?"). We're currently hitting response times in the 5-30 second range for moderately complex queries. Before trajectory becomes a core part of decision-making (not just auditing), that needs to come down to sub-second.

## What to watch

1. **Storage cost.** If trajectory capture stays in place and we scale to 10 agents running in parallel, we're at ~70KB/sec. Over a quarter, that's ~60GB per environment. That's not earth-shattering, but it's real spend that needs budgeting.
2. **Query patterns.** Right now we're doing mostly ad-hoc queries ("show me all implement tickets where confidence < 0.5"). As trajectory data becomes core to operations, those patterns will formalize. When they do, we should index for them and re-evaluate schema.
3. **Compliance and retention.** Trajectories contain decision rationale. If a decision is later disputed or has legal/compliance implications, do we have a retention/dispute process? That's not technical, but it's operational.

The infrastructure is solid. The question now is whether trajectory data becomes a load-bearing part of how we operate, or whether it's information we have but don't systematically use.
