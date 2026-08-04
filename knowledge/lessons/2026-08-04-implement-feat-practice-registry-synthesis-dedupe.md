---
tags:
  - kind/implement
  - outcome/pass
---

# Implement: feat - practice registry with synthesis dedupe against filed, merged, and rejected work

## Context
The synthesis engine was filing candidate practice tickets without checking if similar practices had already been explored. This created duplicate effort and polluted the backlog with variations of already-attempted ideas.

## What happened
Built a practice registry that:
- Collects all filed, merged, and rejected ticket summaries
- Compares new synthesis candidates against this registry using semantic similarity
- Blocks or merges duplicate candidates before filing

Work went cleanly; tests cover the dedup logic and an integration test verifies the registry filters candidate practices before a PR.

## Lesson learned
Dedup-before-file is cheaper than dedup-after-merge. The registry catches most repeats early, reducing churn. Edge case: near-duplicates (same idea, different framing) still slip through and require human judgment during review. The registry is a filter, not a substitute for architect discretion.
