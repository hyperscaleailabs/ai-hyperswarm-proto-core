---
tags:
  - article
  - persona/architect
---

# Synthesis After 15 Lessons: Building the Loop That Builds

We run an autonomous engineering loop — an agent system that writes, tests, and merges its own improvements — and periodically synthesize its lesson log to catch drift before it hardens into bad habit. This window covers the last five lessons (2 implement, 3 improve), all merged clean. That 5/0 record is worth treating with suspicion rather than pride: a green window in a synthesis sample size this small tells you more about what got attempted than about system health. The real signal is in what each lesson had to work around.

**Model selection by task complexity.** We stopped routing every ticket to the same model tier. Trivial refactors and doc-cleanup tickets go to a cheaper/faster model; multi-file architectural changes escalate. The tradeoff is real: a naive complexity classifier will misjudge occasionally, and a downgraded model on a genuinely hard ticket burns a full loop cycle before failing. We accept that cost because the alternative — flat-rate expensive routing — was the actual failure mode driving this change; token spend on trivial tickets was the dominant cost line.

**Explicit phase artifacts (MetaGPT-style).** Instead of one agent doing plan→code→test implicitly in a single context, we now force explicit handoff artifacts between phases (design doc, task breakdown, diff, test report), each written to disk and readable independently. This is a classic distributed-systems move: trade implicit shared state for explicit, inspectable messages. It costs latency and token overhead per ticket. It buys us the ability to resume or replay a ticket from any phase boundary, and — more importantly — a paper trail when a ticket fails silently three phases downstream from its actual root cause.

**Reproduce-before-fix regression guard.** Bugfix and heal tickets must now write a failing repro test before touching the fix. This is the one pattern we'd call unambiguously load-bearing: without it, agents were closing tickets by patching symptoms that happened to make a specific CI run go green, without proving the underlying bug was gone.

**Where it actually broke: sandboxed test execution.** Worker agents run inside isolated worktrees where `pytest`, `ruff`, and `python` are denied outright — a sandboxing decision made for isolation/safety reasons upstream of this loop. That meant the orchestrator's own "run-once / heal / implement" paths had no way to self-verify inside the loop. The fix wasn't relaxing the sandbox; it was building a fake test-runner harness so orchestrator integration tests could exercise those paths without shelling out to real test tooling. It's a workaround, not a solution — it verifies orchestration logic, not the actual code the loop produces, and that gap is now a known blind spot rather than a hidden one.

**Cost/quota telemetry gate.** A warn-then-halt budget gate per block, added mainly because untracked spend was the closest thing to an outage this system had produced. Warn-then-halt over hard-stop was a deliberate choice: false positives in a hard gate stall the whole loop, and we'd rather over-spend once than block indefinitely on a miscalibrated threshold.

Net pattern across all five: every adopted mechanism exists because an implicit assumption (uniform model cost, shared context, self-reported test success, unbounded spend) broke first.
