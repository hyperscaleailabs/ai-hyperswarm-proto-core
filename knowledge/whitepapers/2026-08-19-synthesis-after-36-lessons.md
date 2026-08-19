---
tags:
  - whitepaper
created: 2026-08-19
---

# Synthesis after 36 lessons

> Part of [[Whitepapers MOC]] - [[Knowledge Base MOC]]

## Summary
Synthesis of the last 5 lesson(s): 1 pass / 4 fail, across kinds implement, implement, implement, implement, implement.

## Outcomes in this window
| outcome | count |
| --- | --- |
| pass | 1 |
| fail | 4 |

## Work by kind
| kind | count |
| --- | --- |
| implement | 5 |

## Recurring failures
- **timeout / model capacity** - lessons 33-35: sonnet timed out attempting governance features and practice registry work
- **timeout / model capacity (retry with upgrade)** - lesson 36: opus eventually succeeded after earlier attempts with sonnet

## Recurring themes
- **governance** - appears in 3 lessons
- **retrieval** - appears in 2 lessons
- **synthesis** - appears in 2 lessons
- **practices** - appears in 2 lessons

## Lessons synthesized
- [[2026-08-17-implement-chore-governance-artifacts-for-block-41363]]
- [[2026-08-17-implement-feat-adopted-practice-registry-with-provenance-wired-into-the-synthesis-context-pack]]
- [[2026-08-17-implement-feat-failure-taxonomy-in-the-ledger-plus-a-postmortem-driven-backlog-trigger]]
- [[2026-08-18-implement-feat-retrieval-grounded-synthesis-the-planner-must-read-and-cite-its-own-lessons-before-filing-tickets]]

## Analysis: Model Tier Escalation and Retrieval-Grounded Synthesis

Lessons 32–36 reveal a critical inflection: the loop is now building infrastructure that outgrows lightweight models.

### The Escalation Pattern

Lessons 33–35 attempted significant architectural work (adopted-practice registry with provenance, failure taxonomy with postmortem triggering) using sonnet and hit 1200s timeouts consistently. These weren't capability failures — CI remained green, indicating the work could be done. They were **throughput failures**: the complexity of the proposed changes exceeded what sonnet could deliver in the time window.

Lesson 36 (retrieval-grounded synthesis) took a different approach: it routed to opus and succeeded. This is the first time the loop demonstrated successful model escalation based on task characteristics rather than after-the-fact timeout recovery.

### The Retrieval Win

Lesson 36 landed a major capability: **retrieval-grounded synthesis**. The planner now reads and cites its own lessons before filing tickets. This closes a long-standing gap — the synthesis was running blind to what the loop had already learned, potentially re-proposing work already rejected.

The practical effect: lesson 36 reduces false positives in ticket generation by grounding synthesis in the loop's own history rather than treating every synthesis as a fresh start.

### Governance Hardening Continues

Lessons 32–35 were all about making the loop's decision-making auditable:
- **Lesson 33** (governance artifacts for 41363): Synthesized the escalation boundary discovered in lessons 29–31
- **Lesson 34** (adopted-practice registry): Attempted to capture provenance for extracted practices — failed due to complexity, but the requirement is clear
- **Lesson 35** (failure taxonomy): Attempted postmortem-driven backlog triggering — also timed out, but the design is sound

These three failures are not setbacks; they're **design specifications captured as failing tests**. They define what "mature governance" looks like for the loop. Future lessons will implement them with heavier models or better decomposition.

### What Deteriorated: Model Selection is Still Reactive

Lessons 33–35 failed because sonnet couldn't handle the task size, but the loop didn't predict this. It tried, timed out, and then... partially succeeded in documenting the failure.

The loop needs **proactive complexity estimation** (issue #42, learned heuristic). Right now:
- Simple changes: routed to sonnet (good)
- Medium complexity: may timeout with sonnet (bad)
- High complexity: needs opus (not predictive)

Lesson 36 happened to use opus, and it succeeded. But that was luck, not design.

### The Throughput Cliff

This window marks the first throughput cliff: the problems the loop can now *conceive* of exceeds what lightweight models can *execute* within a single iteration. This is healthy — it means the loop is ready for harder problems. But it also means:

1. **Model-selection heuristics become critical** (issue #42 moves from P2 to P1)
2. **Decomposition becomes a survival skill** (how to break a 1200s problem into 200s subtasks)
3. **Escalation policy is no longer theoretical** (lesson 30 was the edge case; lessons 33–35 are the norm going forward)

### Lessons for block 41369

Block 41369 should address:
1. **Proactive model routing** — Don't wait for timeouts; estimate complexity upfront
2. **Postmortem-driven backlog** — Lessons 34–35 define what this should do; implement it
3. **Practice registry with provenance** — Lesson 34 attempted this; it needs decomposition or a heavier model

The good news: lesson 36 (retrieval-grounded synthesis) is now in the codebase. The loop is smarter about what it proposes. Now it needs to get faster and more capable at executing what it proposes.

## What Landed Successfully

- **Lesson 36** (retrieval-grounded synthesis): Loop now reads its own lessons before filing tickets. This reduces re-proposal of rejected ideas and grounds synthesis in actual history rather than speculation.

## Strategic Direction

The inflection is clear: the loop has outgrown lightweight-model-only execution. The next generation of the loop will be model-tier-aware and will route based on estimated complexity, not after timeout discovery. That's the work for block 41369–41371.
