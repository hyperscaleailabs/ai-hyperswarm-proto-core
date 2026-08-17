---
tags:
  - article
  - persona/architect
---

# What Five Green Lessons Taught Us About Building an Autonomous Dev Loop

Our last five lessons — two `implement`, three `improve` — all landed clean: five pass, zero fail. That streak is worth treating as a signal, not just a scoreboard, because it tells us the loop's failure surface has moved from "does the change work" to "does the process stay legible."

## What we actually built

**Model selection by task complexity.** Instead of a single model tier for every ticket, the orchestrator now routes based on estimated task complexity. This is a classic cost/quality tradeoff, but the honest reason we did it wasn't cost — it was that overpowered models on trivial tickets were producing unnecessarily large diffs, which made review slower, not faster. Right-sizing the model turned out to be a code-quality lever, not just a spend lever.

**Fake-runner integration tests for the orchestrator.** We added integration tests around `run`, `heal`, and `implement` paths using a fake runner rather than hitting real subprocess execution. This was a direct response to a gap: orchestrator-level logic (retry sequencing, state transitions) had only been covered indirectly through end-to-end runs, which are slow and hide root causes when they fail. The fake-runner layer isolates orchestration logic from execution mechanics — a standard seam, but one we'd been deferring.

**Explicit phase artifacts, borrowed from MetaGPT.** We made intermediate phase outputs (design docs, task breakdowns) first-class artifacts instead of ephemeral context passed between agent turns. The tradeoff is storage and pipeline complexity in exchange for auditability — when a downstream phase misbehaves, you can now inspect exactly what it was handed, rather than reconstructing it from logs.

**Reference-set snapshot refresh.** We periodically refresh the corpus the loop calibrates against and extract one reusable practice per refresh cycle, rather than let the reference set drift silently. This is deliberately incremental — one practice per cycle — because past attempts at larger batch extraction produced practices nobody could trace back to evidence.

**Loop reliability: retry and CI parity.** We tightened retry semantics and made local loop execution match CI more closely. The underlying problem this fixes isn't stated as a dramatic outage — it's the quieter failure mode of a loop that passes locally and fails in CI (or vice versa), which erodes trust in the green signal faster than any single crash.

## What we're honest about

This window had no failures — the loop stayed green throughout. That's a good sign, but it's also a blind spot: a five-lesson all-pass streak doesn't validate the retry/CI-parity fix under real divergence, and it doesn't stress-test the new model-routing heuristic against a genuinely hard ticket. The recurring themes across lessons — build, change, cleanly, merged — read as process hygiene, not architecture. The next useful data point isn't another clean pass; it's a lesson where something breaks and we can see whether these four investments actually catch it.
