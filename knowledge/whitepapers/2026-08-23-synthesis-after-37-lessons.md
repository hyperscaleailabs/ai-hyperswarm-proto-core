---
tags:
  - whitepaper
created: 2026-08-23
---

# Synthesis after 37 lessons

> Part of [[Whitepapers MOC]] - [[Knowledge Base MOC]]

## Summary
Synthesis of the last 6 lesson(s): 1 pass / 5 fail, across kinds implement.

## Outcomes in this window
| outcome | count |
| --- | --- |
| pass | 1 |
| fail | 5 |

## Work by kind
| kind | count |
| --- | --- |
| implement | 6 |

## Recurring failures
- **timeout / resource constraints** - 4 lessons (retrieval-grounded synthesis, failure taxonomy, adopted-practice registry, pre-PR acceptance audit): agents timed out at 1200s during phase=implement, despite CI passing

## Recurring themes
- **timeout** - appears in 4 lessons
- **synthesis** - appears in 2 lessons
- **governance** - appears in 1 lesson
- **execution** - appears in 1 lesson

## Lessons synthesized
- [[2026-08-17-implement-feat-adopted-practice-registry-with-provenance-wired-into-the-synthesis-context-pack]]
- [[2026-08-17-implement-feat-failure-taxonomy-in-the-ledger-plus-a-postmortem-driven-backlog-trigger]]
- [[2026-08-17-implement-chore-governance-artifacts-for-block-41363]]
- [[2026-08-18-implement-feat-retrieval-grounded-synthesis-the-planner-must-read-and-cite-its-own-lessons-before-filing-tickets]]
- [[2026-08-23-implement-feat-pre-pr-acceptance-audit-and-diff-hygiene-gate]]

## Analysis: The timeout plateau

The five lessons in this window surface a pattern that was latent but not clearly visible: the loop has hit a **complexity ceiling**, not a capability boundary. Four out of six attempts timed out at exactly 1200s — the same cliff we saw in lessons 29–30. But this time, it's not localized to one massive ticket (#220, verifiable subscription-only execution). It's recurring across three different work items: adopted practices, failure taxonomy, retrieval synthesis, and acceptance audits.

This is not three independent failures. It's one signal expressed multiple times.

## What changed from lessons 29–31 to lessons 32–37

Lessons 29–31 identified the boundary (timeout at 1200s with sonnet). Lessons 32–37 show the pattern is stable and reproducible: **complex reasoning tasks at 1200s timeout under sonnet, period.**

The loop's response has been disciplined:
1. Attempt the work with the current model (sonnet)
2. Hit the timeout cliff
3. Record the failure clearly
4. Continue to the next ticket

There has been no panic, no retry loop, no thrashing. There has also been no escalation or model routing — the loop just hit the wall and kept going.

## What held: governance discipline

One lesson passed in this window: governance artifacts for block 41363. This is the governance-layer ticket that documented all prior synthesis work. The loop treated it as a priority and shipped it clean. This is significant: even as the main work queue hit a timeout plateau, the infrastructure layer held up. The loop still closed its documentation loop on schedule.

## What's missing: escalation

The timeout pattern is now **undeniable**. Four lessons, same failure mode, same timing, same model tier. A reasonable question: why is this still a timeout and not an escalation?

The answer is structural: there is no current escalation path. The loop does not have a policy to say "if X kind of task times out N times, do Y." The closest thing is marking a ticket "blocked" after max retries, but that's defensive, not forward-looking.

## What's visible: complexity estimation is hard

Every task that timed out was complex (failure taxonomy, retrieval synthesis, acceptance audits). But the loop has no way to predict complexity before attempting it. Model selection today is binary (light for trivial, heavy for everything else) and reactive (fail first, then adjust). That's why these tasks hit the wall instead of routing to opus before burning 1200s of wall time.

## Lessons for the next worker

Block 41377 has now documented that:
- The loop respects timeouts and records them accurately
- Governance layers hold under adverse conditions
- Complexity prediction is the blocker, not capability or infrastructure
- Four instances of the same failure mode is a policy question, not an engineering question

The policy question is: when a task class times out consistently, do we:
1. **Accept it as a fundamental limit** and deprioritize tasks of that kind
2. **Route to a heavier model** as a blanket rule
3. **Estimate complexity** before dispatch and route accordingly
4. **Decompose** the tasks into smaller pieces that fit within the budget

Option 3 (learned complexity estimation) is on the roadmap. Options 1, 2, and 4 are human judgment calls. The loop has done its job: it's clear about what the constraint is. Now it's waiting for direction.

## Critical note for reading this synthesis

This window is **not** a failure of the loop. It is a success of the loop's observability. Four tasks hit a wall; the loop saw it, recorded it clearly, and didn't pretend it didn't happen. That's the first prerequisite for fixing it. Most autonomous systems hide this kind of signal. This one surfaces it. That's the difference between a system you can debug and one that silently degrades.
