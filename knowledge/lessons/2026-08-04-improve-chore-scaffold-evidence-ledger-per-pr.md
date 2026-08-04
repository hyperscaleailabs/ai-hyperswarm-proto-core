---
tags:
  - kind/improve
  - outcome/pass
---

# Improve: chore - scaffold evidence ledger per PR, track SDLC signal over time

## Context
SDLC evidence is recorded in PR descriptions (tests, CI signal, lesson), but there's no durable record of how evidence accumulates or degrades over time. This makes it hard to spot trends like "we stopped running integration tests" or "lesson quality has drifted."

## What happened
Scaffolded a simple evidence ledger:
- Each PR contribution: title, model, has-tests, has-lesson, ci-outcome, time-to-merge
- Aggregates by week/block to surface trends
- No analysis yet — just data collection and a simple CLI to dump the ledger

This is part of the infrastructure for richer governance reporting. Tests verify ledger writes and reads correctly.

## Lesson learned
Collection without analysis is a starting point, not a solution. The tradeoff: more data to maintain, but it's dumb and cheap to collect. The next step is using this ledger to detect regressions (e.g., "integration test coverage dropped this week") and alert the architect.
