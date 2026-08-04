---
tags:
  - kind/implement
  - outcome/pass
---

# Implement: feat - protected-invariants gate - classify guardrail diffs locally and assert invariants in CI

## Context
The loop has five architectural invariants (ticket-linked PRs, model recorded, lesson per PR, green-gated merges, subscription-only models). A regression in any of these isn't caught by normal tests — they're meta-properties of the repo and its governance layer.

## What happened
Built a guardrail-classification layer that:
- Parses each PR's title/description to extract model-used and lesson status
- Runs locally before pushing to detect common failures (missing model record, no lesson, broken ticket link)
- Asserts these invariants in CI as a pre-merge gate

Five PRs verified cleanly; the gate caught one edge case (a ticket link URL typo) before merge. The check is deterministic and runs in <1s, so it adds no latency.

## Lesson learned
Guardrail assertions belong in CI, not as documentation. Local pre-flight checks save iteration cycles. The tradeoff: the gate is strict (enforces invariants as Must-Have), which means edge cases need explicit override logic or the gate becomes a bottleneck. Currently: no override — all invariants are hard stops, which feels right for a loop that's self-improving.
