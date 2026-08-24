---
tags:
  - article
  - persona/cto
---

# Autonomous Engineering Loop: Status Report — 1 in 5 Shipped Clean

## The headline number

Over our last five autonomous implementation cycles, one shipped green. Four failed to pass CI and are sitting behind our auto-merge gate rather than in production. That 20% success rate is the real signal this period, not the feature list.

## What actually failed

All four failures share the same root pattern: the agent wrote code, opened a PR, and CI rejected it. Auto-merge did its job correctly — it held the line rather than letting broken code through — but that's a safety net catching failures, not a system that's working as intended. Notably, this happened on both greenfield features (a new provenance registry, a retrieval-grounded planning change) and infrastructure hardening (a failure-taxonomy ledger, a pre-PR acceptance gate) — the failure mode isn't concentrated in one kind of work, which means it's likely upstream in how the agent verifies its own work before opening a PR, not a quirk of any single feature's complexity.

The one success in this window was, not coincidentally, the earliest and narrowest change (governance artifacts for a scoped block) — smaller, more mechanical changes are converting; larger feature work isn't yet.

## Why this matters for risk posture

The good news is structural: our gate held. Nothing broken reached main. The auto-merge-on-green design is doing exactly what it's supposed to do, and that's the part of this system I'd point to as evidence it's safe to keep running unattended. The bad news is throughput: we're paying for four full implementation cycles — agent time, review time, CI compute — for every one that lands. At current volume that's a cost-efficiency problem; at higher volume it becomes a capacity problem, because a growing backlog of "did not reach green" PRs needs human triage to diagnose, and that triage cost doesn't shrink just because the work was done autonomously.

## What we're doing about it

We just shipped the piece meant to address this directly: a pre-PR acceptance audit and diff-hygiene gate (landed this period, ironically one of the four that itself failed its own first CI run before merging — which is either a good stress test or a bad omen, and we're treating it as the former until proven otherwise). The intent is to catch the same class of failure *before* a PR is opened, not after, cutting the four-cycles-per-success ratio down. We also landed a failure-taxonomy system in the same window specifically so these four failures get classified with enough structure to spot the pattern automatically next time, instead of relying on someone reading five lesson logs by hand.

## Where I'd draw the line

I'm comfortable continuing to run this loop unattended because the failure mode is "wastes compute and time," not "ships broken code" — the gate is doing its job. I would not yet count on this loop for anything time-sensitive or on the critical path. The next window is the real test: if the acceptance gate doesn't measurably move the pass rate up from 20%, the problem isn't tooling, it's that the agent's task decomposition or self-verification approach needs to change, and we should say so rather than keep patching the gate.
