---
tags:
  - article
  - persona/architect
---

# What 15 Autonomous Build Cycles Taught Us About Running an AI Coding Loop Unsupervised

We've been running an agentic loop that builds successive generations of a microservice (currently at v15+) with no human in the implementation path — only architect review at checkpoints. This block: 5 lessons, 5 passes, 0 failures. That clean streak is itself worth being suspicious of, so here's what actually changed under the hood, and what we broke getting there.

## The core design: cost-aware autonomy, not raw autonomy

Early generations ran every ticket through the same model regardless of complexity. That's wasteful and, worse, it hides real risk — a trivial refactor and a concurrency fix get the same scrutiny budget. We moved to **task-complexity-based model selection**: cheap/fast models triage and handle mechanical changes, heavier reasoning is reserved for tickets flagged as architecturally risky. The tradeoff is a harder problem upstream — classifying complexity correctly matters more than the model choice itself, and a bad classifier silently degrades quality rather than failing loudly.

We paired this with a **quota/cost telemetry ledger with a warn-then-halt per-block budget gate** (shipped a few cycles back). This was a direct response to a failure mode we didn't originally design for: an unsupervised loop with an cost budget will happily keep going until you notice the bill, not until something breaks. Warn-then-halt is deliberately conservative — it costs some throughput in false-positive halts, but a runaway loop with no human in the room is a worse failure than a paused one.

## Testing the orchestrator, not just its output

For a while we only tested what each generation produced, not the loop that produces it. That's backwards for a system meant to run unattended. We added a **fake-runner integration test suite** covering the orchestrator's run-once, heal, and implement paths — this catches orchestration bugs (wrong ticket routed to wrong phase, heal loop not terminating) that per-generation service tests structurally can't see.

We also adopted **explicit phase artifacts**, borrowed from MetaGPT's SOP pattern — each phase (design, implement, review) now writes a durable artifact instead of passing state implicitly through agent context. This was a deliberate simplicity-over-cleverness call: it's more verbose, but it makes the loop's state machine inspectable after the fact, which matters more than saving a few tokens when you're debugging a failure that happened three generations ago.

## The failure that shaped the guard rails

Outside this window, we hit real regressions where a "fix" passed its own test but didn't actually address the reported bug — the agent had tested its patch, not the failure. That drove the **reproduce-before-fix regression guard**: heal and bugfix tickets must first reproduce the reported failure before touching code, mirroring the "reproduce before fixing" discipline we'd want from any engineer.

## Honest caveat on this block

Five-for-five is a good block, not proof the guard rails are sufficient — our review rhythm and two-phase governance layer are new enough that we haven't yet seen them catch something in anger. That's the next thing we're watching for, not celebrating.
