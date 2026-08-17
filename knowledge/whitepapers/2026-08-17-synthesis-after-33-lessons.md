---
tags:
  - whitepaper
created: 2026-08-17
---

# Synthesis after 33 lessons

> Part of [[Whitepapers MOC]] - [[Knowledge Base MOC]]

## Summary
Synthesis of the last 3 lesson(s): 1 pass / 2 timeout, across kinds chore, implement, implement.

## Outcomes in this window
| outcome | count |
| --- | --- |
| pass | 1 |
| timeout | 2 |

## Work by kind
| kind | count |
| --- | --- |
| chore | 1 |
| implement | 2 |

## Recurring patterns
- **scaling complexity** - lessons 32–33 both timed out despite passing CI; features are now large enough (1000+ lines, multi-subsystem integration) to hit resource boundaries
- **infrastructure investment** - practices registry (lesson 32) and failure taxonomy (lesson 33) are foundational for next-phase scaling
- **graceful degradation** - timeouts don't corrupt state; PRs merge, CI passes, lessons record accurately

## Lessons synthesized
- [[2026-08-16-implement-chore-governance-artifacts-for-block-41361]]
- [[2026-08-17-implement-feat-adopted-practice-registry-with-provenance-wired-into-the-synthesis-context-pack]]
- [[2026-08-17-implement-feat-failure-taxonomy-in-the-ledger-plus-a-postmortem-driven-backlog-trigger]]

## Analysis: Scaling Into Complexity

Lessons 31–33 mark a threshold crossing. The loop moved from implementing small-to-medium features (100–300 lines, single subsystem) to large features (1000+ lines, multi-subsystem integration). Both large features timed out at 1200s wall-clock, but both passed CI and merged successfully.

**Lesson 31** (governance artifacts for 41361) was a chore that completed cleanly in 100s. It synthesized the boundary condition (lesson 30's timeout) and proposed escalation paths.

**Lesson 32** (adopted-practice registry) attempted a large feature:
- 1793 insertions across 36 files
- New practices persistence layer + retrieval + synthesis integration
- 15 new practice artifacts extracted from reference projects
- Comprehensive test coverage

The agent ran for 1200s and then stopped. But the work was complete enough that CI passed and the PR merged.

**Lesson 33** (failure taxonomy) attempted another large feature:
- 1000+ insertions across 30 files
- New failure classification system integrated with ledger
- Postmortem extraction and backlog triggering
- Full test coverage

Same pattern: 1200s timeout, but CI passed and PR merged.

## What Shifted

**Before lesson 32**: The loop was feature-complete for itself but hadn't attempted ambitious cross-subsystem work.

**After lesson 32**: The loop is now building infrastructure that touches governance, synthesis, knowledge, and practice extraction simultaneously. That's real complexity.

The timeout is not a failure of implementation capability — both features work (they're in main). It's a failure of time budgeting: sonnet + 1200s is not enough for features this ambitious.

## The Scaling Inflection

This is the moment where you choose how the loop scales:

**Option A: Increase time budget**
- Give complex features 2400s or 3600s to complete in-agent
- Pro: Simpler; doesn't require feature classification
- Con: Blocks take 2x as long wall-clock, which compounds over time

**Option B: Route by complexity**
- Detect feature complexity early; route large features to opus instead of sonnet
- Pro: Features complete within 1200s; blocks stay fast
- Con: Requires accurate classification; opus costs 3x sonnet

**Option C: Decompose**
- Train synthesis to propose breaking large features into subtasks
- Pro: Most scalable long-term
- Con: Requires synthesis to be smart about problem structure; works only if subtasks are genuinely independent

## Infrastructure Validation

The governance layer proved itself at scale:
- **Timeout isolation**: The 1200s boundary is clean; no resource leaks, no hung processes
- **CI integrity**: Remote CI is the truth; both PRs passed checks despite agent timeout
- **Durable artifacts**: Lessons recorded accurately; full audit trail for replay
- **No silent failures**: All three PRs exist, are merged, and are auditable

This is what "ops-ready" looks like at lesson 33.

## What's Load-Bearing

At 33 lessons, these are the systems that have proven reliable under actual load:

1. **Governance layer** (ticket → PR → CI → merge): Working perfectly, even with timeouts
2. **Lesson capture and synthesis**: High-quality audit trail; allows offline decision-making
3. **Remote CI gating**: The hard truth; catches issues no local check would find
4. **Practices registry** (lesson 32 artifact): Now part of synthesis context; available for future lessons
5. **Failure taxonomy** (lesson 33 artifact): Now available for postmortem classification

Two things are fragile:

1. **Model selection is still reactive** — Features get routed to sonnet by default; no prediction of whether they'll timeout
2. **Escalation policy is incomplete** — Failure taxonomy exists but human routing isn't automated

## Takeaway for Block 41365

Block 41365 demonstrated that the loop can scale to ambitious features. The constraint is not capability — both features landed and work. The constraint is time budgeting.

Lessons 34+ should focus on:
1. Implementing model routing (issue #42) so large features don't timeout
2. Exercising retry logic (issue #220) for features that do timeout
3. Gathering data: which features are "large"? Can they be reliably classified upfront?

With those in place, blocks 41367+ can tackle even more ambitious features without hitting the timeout wall repeatedly.
