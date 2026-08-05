---
tags:
  - article
  - persona/architect
---

# Trajectory Capture: Building Auditability into the Loop

The last two lessons added trajectory capture and replay to the autonomous loop — worker trajectories now record every decision, state transition, and outcome. That's a fundamental shift in how observable the system is, and it needs careful architectural thought because once you're capturing everything, the decision becomes not "can we audit this" but "do we *want* to store this and at what detail level."

## What we adopted

**Worker trajectory capture.** Every agent execution now logs its inputs, reasoning checkpoint, outputs, and model metadata. This is distinct from CI logs — it's structured SDLC data, designed to be queryable later for understanding *why* a decision was made, not just *that* it was made. The cost: steady storage growth and the risk that trajectory data becomes stale or inconsistent if workers crash mid-execution.

**Replay harness for offline analysis.** We can now re-run a recorded trajectory against new models or heuristics without re-running the actual task. This lets us ask "what if we had routed this ticket to Sonnet instead of Opus" or "what if the model-selection heuristic had been calibrated differently." The honest cost: a replay is a simulation, not a re-execution. It won't catch state-dependent side effects or issues that only appear in live CI — it validates logic, not ground truth.

**JSON agent output format.** Agents now emit structured JSON alongside their narrative output, making it easier for downstream consumers (the orchestrator, telemetry collectors, replay engines) to extract and reason about intent. This is a contract — agents must be disciplined about emitting well-formed JSON even when the task goes sideways.

## What's unresolved

**Trajectory as a version control problem.** We're now storing a parallel history of decisions alongside our git history. If a lesson or outcome changes (someone corrects an annotation, a judgment call is reversed), the trajectory record becomes stale but doesn't update. We need a story for trajectory versioning or garbage collection that doesn't yet exist.

**Replay gap: side effects and concurrency.** A replay doesn't trigger the same CI, doesn't run the same remote checks, doesn't interact with external APIs. It's a pure logic simulation. The next worthwhile lesson would exercise that gap intentionally — replay something controversial and compare the simulated verdict against the real outcome.

**Storage and cost.** Trajectory capture adds latency (structured logging) and storage (every execution now has a multi-KB JSON trail). We haven't yet instrumented whether this scales to 1000s of lessons per day or if we hit a storage/cost wall. That's a forcing function we need.
