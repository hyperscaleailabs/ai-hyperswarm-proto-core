---
tags:
  - article
  - persona/devops
---

# 15 Lessons In: What Actually Kept the Loop Green

Five autonomous build/improve cycles landed in a row — 2 new features implemented, 3 existing ones hardened, zero failures in this window. That streak isn't luck; it's what fell out of fixing the things that broke the loop before it got this far. Here's the mechanics.

## What we automated

The loop runs implement/improve tickets end-to-end: pull a ticket, build, test, merge, harvest a lesson, move on. Each cycle writes a structured "lesson" artifact — outcome, kind, themes — that feeds the next planning pass. Over 15 lessons, three mechanics did the actual work of keeping it stable:

**Retry-with-CI-parity.** Early iterations had a gap between what ran locally in the worker and what CI enforced — a ticket could pass its own checks and still break on merge. The fix was making the loop's pre-merge check run the *same* command CI runs, not an approximation of it, plus a bounded retry on transient failures (flaky installs, network blips) instead of failing the whole ticket. This one lesson alone is probably responsible for most of the current 5/0 streak — most of what used to fail wasn't the code, it was drift between two "test" definitions.

**Explicit phase artifacts.** Borrowed from MetaGPT-style pipelines: instead of one opaque "agent did stuff" step, each phase (design → implement → test → review) now writes its own artifact to disk. This mattered operationally, not architecturally — when a ticket does fail, you can point at which phase produced garbage instead of re-running the whole thing blind. Cheap to add, expensive to have skipped.

**Complexity-based model selection.** Tickets get triaged by estimated complexity and routed to a cheaper or more capable model accordingly, rather than every ticket paying full-model cost. This is the one lesson where the tradeoff is live and ongoing — misclassifying a ticket as "simple" means a cheap model quietly produces a worse implementation that still passes tests, and you don't find out until later. Worth watching, not yet fully solved.

## What failed getting here

The window we're reporting on is clean — 5/0 — but that's survivorship. The lessons that produced *this* stability came from a period where CI-parity gaps caused silent merges of code that broke the real pipeline, and where the loop had no retry budget at all, so any transient failure (a flaky dependency fetch, a rate limit) killed the whole ticket instead of being absorbed. Neither of those is visible in the current metrics because they were the failures that got fixed, not the ones still happening.

## The operational takeaway

None of this was a model-quality win. Every recurring theme in this window is process language — "build," "merge," "cleanly," "green" — not feature language. The loop's reliability came from treating the CI boundary and the retry policy as first-class engineering surfaces, the same way you'd treat them for a human-driven pipeline. The model selection tradeoff is the next place this discipline needs to go: right now it's optimizing cost with no automated check that the cheap path didn't also cheapen the output.
