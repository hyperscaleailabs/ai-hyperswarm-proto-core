---
tags:
  - whitepaper
created: 2026-08-18
---

# Synthesis after 36 lessons

> Part of [[Whitepapers MOC]] - [[Knowledge Base MOC]]

## Summary
Synthesis of the last 5 lesson(s): 2 pass / 3 fail, across kinds implement, implement, implement.

## Outcomes in this window
| outcome | count |
| --- | --- |
| pass | 2 |
| fail | 3 |

## Work by kind
| kind | count |
| --- | --- |
| implement | 5 |

## Recurring failures
- **timeout / resource constraints** - lessons 34, 35, 36 (adopted-practice registry, failure taxonomy, retrieval-grounded synthesis): all timed out at 1200s despite CI passing
  - Lesson 34: sonnet timeout
  - Lesson 35: sonnet timeout
  - Lesson 36: opus timeout (escalation to heavier model failed)

## Recurring themes
- **synthesis capability** - appears in 2 lessons (lesson 32 governs synthesis, lesson 36 improves synthesis itself)
- **model selection and resource management** - appears in 3 lessons (escalation options from lesson 31 never implemented; heavier model doesn't help with lesson 36)
- **feature complexity** - appears in 3 lessons (registry, taxonomy, grounding all exceed 1200s budget)

## Lessons synthesized
- [[2026-08-16-implement-chore-governance-artifacts-for-block-41363]]
- [[2026-08-17-implement-chore-governance-artifacts-for-block-41363]]
- [[2026-08-17-implement-feat-adopted-practice-registry-with-provenance-wired-into-the-synthesis-context-pack]]
- [[2026-08-17-implement-feat-failure-taxonomy-in-the-ledger-plus-a-postmortem-driven-backlog-trigger]]
- [[2026-08-18-implement-feat-retrieval-grounded-synthesis-the-planner-must-read-and-cite-its-own-lessons-before-filing-tickets]]

## Analysis: the escalation policy remained unimplemented

Lesson 31's synthesis proposed three escalation paths for the blocking ticket #220 (verifiable subscription-only execution):
- **Option A**: Escalation with human judgment
- **Option B**: Model routing (complexity estimation → opus)
- **Option C**: Decomposition (break into subtasks)

None were implemented. The loop proceeded to attempt three complex features (lessons 34–36) without any escalation policy in place. All three timed out.

**Lesson 34** (adopted-practice registry): Sonnet timed out at 1200s. The task was to add a durable registry of practices with provenance, integrate it into synthesis context, and test. Complex but potentially doable with more time.

**Lesson 35** (failure taxonomy): Sonnet timed out at 1200s. The task was to categorize failures from the ledger and trigger postmortem analysis. Also complex, similar scope to lesson 34.

**Lesson 36** (retrieval-grounded synthesis): Opus timed out at 1200s. This is the critical signal: escalating to the heavier model (opus) did NOT save this ticket. The problem is not model capacity — it's task scope or decomposition.

## What this tells us

The escalation decision from lesson 31 was not a luxury. It was a load-bearing decision. By proceeding without it, the loop has now accumulated three blocked tickets (lessons 34, 35, 36) that cannot move forward.

This is different from lesson 30's timeout (verifiable subscription-only execution). Lesson 30 was a single timeout with sonnet. Lessons 34–36 are a pattern: three consecutive timeouts across different features, even with model escalation.

The root cause is not model selection (opus couldn't fix lesson 36). The root cause is likely task decomposition: the loop is attempting large feature implementations in a single agent run, and they don't fit.

## The infrastructure play: synthesis memory worked, decomposition did not

Lesson 32 (governance artifacts for block 41363) was a **pass**. The synthesis layer can successfully create whitepapers, persona articles, and MOCs. This is infrastructure that holds.

Lesson 36 attempted to deepen that same synthesis layer by adding retrieval of prior lessons as context. This is a good instinct — grounding synthesis in lesson history should make it better. But the implementation timed out, which means either:
1. The agent tried to implement retrieval + synthesis + tests all at once (too ambitious)
2. The retrieval pipeline was written inefficiently
3. The task itself is genuinely larger than 1200s

Until we see the actual error, we can't know which. But the pattern suggests decomposition.

## The strategy shift: from escalation to decomposition

Lesson 31 proposed escalation as the immediate path. But that assumed lesson 30's timeout was an outlier. Lessons 34–36 show it's not an outlier — it's the new normal.

The loop is transitioning from simple CRUD and testing tasks (lessons 1–30) to complex architectural work (lessons 31+). Each new lesson requires synthesis, governance, feature coding, and integration tests. At some point, 1200s isn't enough.

The right move now is not escalation to opus (tried and failed) or human review (expensive and slow). It's **task decomposition**: teach the loop to break large tickets into smaller subtasks and chain them across multiple lessons.

This would look like:
1. Synthesis detects a large feature
2. Proposes breaking it into 3–5 subtasks (each ≤400 SLOC)
3. Files separate tickets for each subtask
4. Sequences them across lessons
5. Runs them with a lighter model (sonnet or even haiku)

This is more complex infrastructure, but it's what the data is telling us to build.

## Immediate actions for block 41367

1. **Do NOT retry lessons 34–36 unchanged**. They will timeout again.

2. **Implement the escalation policy (lesson 31's recommendation)**. This unblocks lesson 30 and gives the loop a fallback for the next timeout.

3. **Plan decomposition** as the medium-term solution. Sketch out how synthesis should detect large features and propose subtasks.

4. **Gather metrics**: For each timeout, measure:
   - Token count in the agent prompt
   - Number of files modified in the diff
   - Lines of code written
   - Test coverage added

   This data will calibrate the decomposition heuristic.

## The question for the architect

At lesson 36, your loop has answered:
- Lessons 1–15: Can it work? (Yes)
- Lessons 15–25: Can it self-modify? (Yes)
- Lessons 25–30: Can it handle resource boundaries? (Yes, gracefully)
- Lessons 31–36: Can it scale to complex features? (No — all timeouts)

The escalation policy was supposed to unblock this question. It wasn't implemented, and now lessons 34–36 are stuck. Your move is:

**Option A**: Implement escalation now (unblocks lesson 30, lets loop retry with human help)
**Option B**: Implement decomposition (harder, but lets loop solve complex features at scale)
**Option C**: Drop the complex features and focus on smaller work (safe, but boring)

I'd recommend A then B in parallel. Escalation unblocks the loop immediately. Decomposition takes 2–3 weeks of work but pays off long-term.

## Lessons for the next worker

When you pick up lesson 37 (the next task in block 41367), you'll have:
- Clear evidence that the loop can't handle large features yet
- Three concrete tickets (lessons 34, 35, 36) waiting for decomposition
- A synthesis system that works well (lesson 32 passed)
- A synthesis enhancement that's too large for the current budget (lesson 36 failed)

Use lesson 36's failure as your starting point. The retrieval-grounded synthesis *idea* is good. The implementation just needs decomposition.
