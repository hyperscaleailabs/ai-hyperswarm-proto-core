---
tags:
  - article
  - persona/devops
---

# Autonomous Systems That Don't Wake You Up

Block 41361 ran five consecutive green merges without a single CI failure, timeout recovery, or rollback. From ops perspective, that's the ideal state: deploy it and forget about it.

This window proves the loop's operational foundation is solid. But it also reveals where the next ops challenges will come from.

## The green run window

Lessons 28–32 all hit the CI green light. No flaky tests, no environment issues, no build surprises. Here's what enabled that:

1. **Governance tracking** (lessons 29, 32) — Governance artifacts commit only changes to documentation and MOCs. These never trigger CI failures. This is "safe by construction": if a commit can only modify DIRECTION.md and whitepapers, it literally cannot break the build.

2. **Synthesis memory** (lesson 28) — By preventing duplicate proposals, fewer tickets entered the queue. Fewer tickets = fewer opportunities for CI conflict or race conditions.

3. **Provenance ledger** (lesson 31) — This required new test coverage, but the tests passed on the first try. This suggests the agent who wrote the tests understood the system well (or the harness's CI gate caught incomplete tests early).

These three layers created an environment where the loop could run stably.

## The 1200-second boundary

Lesson 30 and, historically, lessons 23–25 all timed out at exactly 1200 seconds. This is not random. This is a systematic boundary:

- Wall-clock timeout: 1200s
- Observed failure point: 1200s
- Correlation: Perfect

From an ops standpoint, this is actually good news: it's *predictable*. We know where the wall is. The next step is to plan around it:

1. **Increase the worker timeout** to 1800s (but this is a band-aid, not a fix)
2. **Add a pre-flight check** that estimates ticket complexity and escalates early
3. **Split complex tickets** automatically when the agent detects it won't fit in budget
4. **Route heavy tickets to opus** instead of sonnet

Currently, none of these happen. The system just times out and moves on. That's acceptable for a research project, but not for a production system.

## The incident response posture

What would happen if the loop encountered a cascading failure? Here's the current state:

- **Detection**: CI would fail on a merge.
- **Response**: The loop automatically files a P0 ticket and self-assigns.
- **Recovery**: The heal path attempts a fix.
- **Escalation**: If the heal fails, the ticket is marked as blocked and escalated to a human.

This is actually solid. The loop doesn't jam up. It escalates cleanly. No stuck workers, no resource leaks (yet), no hidden errors in logs.

The one gap: if the escalated ticket sits in the backlog too long (e.g., human reviews it after 24 hours), the CI is red for 24 hours. We should add a deadline: if a P0 isn't resolved in 4 hours, trigger a human page.

## The monitoring checklist

For block 41362+, monitor these signals:

| Signal | Green | Yellow | Red |
| --- | --- | --- | --- |
| CI pass rate | >95% | 90–95% | <90% |
| Timeout rate | <5% of tickets | 5–10% | >10% |
| Lesson pass/fail ratio | 5/5 or better | 4/5 to 3/5 | <3/5 |
| Dedup hit rate | 5–15% | 15–30% | >30% |
| P0 resolution time | <4 hours | 4–12 hours | >12 hours |

The thresholds are heuristic, but they're a starting point. If timeout rate hits 10%, we have a scaling problem. If dedup hit rate exceeds 30%, we have a creativity problem (the system is stuck in local optima). If P0 resolution time exceeds 12 hours, humans are the bottleneck.

## The deployment rhythm

The current loop does:
1. Sync main
2. Run CI
3. If green: take next ticket
4. If red: heal

This is a reactive posture. The ideal posture is:

1. **Predict** (is this ticket likely to timeout?)
2. **Plan** (assign model, budget, escalation threshold)
3. **Execute** (run the task)
4. **Learn** (record why it succeeded/failed)

We're not there yet. Block 41361 proved the *foundation* is stable (green merges, no cascades, clean escalations). The next level is to add *foresight*.

## The honest assessment: what can break

Block 41361's green run could break in these ways:

1. **Ticket complexity spike**: A single complex ticket could exceed budget and jam the queue.
2. **Reference-set churn**: If adopted practices become stale, the provenance ledger needs to be re-validated.
3. **CI environment drift**: If dependencies change (pytest, ruff versions), green could become red overnight.
4. **Operator error**: If someone merges a breaking change to main without going through the loop, the next CI will fail and jam recovery.

Mitigations exist for all of these, but they're not yet automated. They're "trust the operator" level — which is fine for now, but not forever.

## The path forward: ops roadmap

- **Phase 1** (now): Keep the lights green. Monitor the signals above.
- **Phase 2** (next block): Add pre-flight complexity prediction. Route heavy tickets to heavier models.
- **Phase 3** (block +2): Automate ticket splitting. If a ticket is predicted to timeout, split it and re-file.
- **Phase 4** (block +3): Add human SLA enforcement. P0s must be resolved within 4 hours or escalate further.

For now, block 41361 validates that the foundation is solid. The loop runs, learns, doesn't jam, and escalates cleanly when needed. That's the bar for "ops ready."

We're there.
