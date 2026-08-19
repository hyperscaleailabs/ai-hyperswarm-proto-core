---
tags:
  - article
  - persona/architect
---

# Lessons 32–36: From Governance to Decomposition

At lesson 36, your loop has hit a hard boundary: it can build governance (lesson 32 passed), but it can't build complex features anymore (lessons 34–36 all timed out). Even escalation to opus doesn't help.

This is the inflection point. You need to choose between escalation and decomposition.

## The Evidence

**Lesson 32** (governance artifacts for block 41363): **PASS**. The loop successfully synthesized lessons 29–31, created whitepapers and persona articles, updated MOCs and DIRECTION. This is infrastructure-grade work. It landed cleanly.

**Lesson 33**: Documentation update. **PASS**, minimal.

**Lesson 34** (adopted-practice registry): **FAIL** at 1200s with sonnet. Task was to add a durable registry of practices, integrate provenance, wire it into synthesis context, and test. Complex but potentially doable with more time or smaller scope.

**Lesson 35** (failure taxonomy): **FAIL** at 1200s with sonnet. Task was to categorize failures from the ledger, trigger postmortem analysis, integrate into synthesis. Similar complexity to lesson 34.

**Lesson 36** (retrieval-grounded synthesis): **FAIL** at 1200s with **opus**. This is the critical signal. You escalated to the heavier model. It didn't help. The problem is not model capacity — it's task scope.

## Why Model Escalation Failed

When a sonnet-sized task times out, escalating to opus makes sense: maybe the lighter model is just too slow. But lesson 36 proves that's not the issue here.

Opus is 2–3x more capable than sonnet on reasoning tasks. If the problem were "the task is too hard to reason about," opus would solve it. Opus didn't. This means the problem is **time and complexity**, not capability.

The agent probably did something like:
1. Parse the ticket (retrieval-grounded synthesis)
2. Read prior lessons from disk (I/O + serialization)
3. Update synthesis context construction
4. Write new code for retrieval pipeline
5. Add tests for retrieval behavior
6. Fix CI failures
7. All within 1200s wall-clock

With opus, step 2–5 might run faster, but they still don't fit. The task is too big for one agent run, period.

## The Escalation Decision You Deferred

Lesson 31 proposed three options for the blocking ticket #220 (verifiable subscription-only execution):

1. **Escalation with Human Judgment** — When a ticket times out, escalate to architect (you) with full transcript. You review, decide if it's decomposable, and break it into subtasks.

2. **Model Routing** — Synthesis estimates complexity and routes complex tickets to opus or decomposes them automatically.

3. **Decomposition** — Synthesis itself learns to break large tickets into subtasks.

**You deferred all three.** The loop proceeded without any escalation policy, and now it has three new blocked tickets (lessons 34, 35, 36) on top of the original blocked ticket (#220).

This is what "no escalation policy" looks like at scale. It's not a crash, but it's stagnation.

## Your Move Now

You have two paths:

### Path A: Escalation + Decomposition (Recommended)

1. **Implement human escalation this week** (2–3 hours of work):
   - When an agent times out, capture the ticket and transcript
   - Route to you with a summary: "This ticket needs decomposition"
   - You review and propose subtasks (5 min of your time)
   - File new tickets for the subtasks (1 min each)
   - Loop picks up the subtasks and runs them in order

   This unblocks lessons 30, 34, 35, 36 immediately. You get to see what decomposition looks like in practice.

2. **Plan decomposition in parallel** (2–3 weeks of architecture work):
   - Study lessons 30, 34, 35, 36 to understand what made them too large
   - Build a heuristic: "if a ticket mentions more than 3 files or 500 SLOC, decompose it"
   - Teach synthesis to propose subtasks automatically
   - Build tests to verify decomposition quality

   After 2–3 weeks, the loop can self-decompose, and you stop being the bottleneck.

### Path B: Give Up on Complex Features

Drop lessons 30, 34, 35, 36. Focus on smaller tickets (tests, chores, bugfixes) that fit within 1200s. The loop stays stable but doesn't grow.

**I'd pick Path A.** It's more work short-term, but it's the difference between a tool that works and a system that scales.

## The Governance Wins (So Far)

Let me be clear about what's working. Lesson 32 proved this:

1. **Lesson capture** — Every PR contributes exactly one lesson. You have 36 of them now with full context (model, CI result, outcome, references).

2. **Synthesis** — The loop can synthesize lessons into whitepapers. This one took 36 lessons and produced a coherent analysis in minutes.

3. **MOC reindexing** — Maps of Content are auto-updated. The graph is navigable. Obsidian integration works.

4. **DIRECTION tracking** — Your steering doc reflects reality, updated every block.

This is rare infrastructure. Most autonomous systems don't have it. You do. Don't lose it.

## The Risk of Deferral

If you defer escalation another block, here's what happens:

- Lesson 37 (next task) is likely another feature ticket
- It times out at 1200s (pattern)
- Lesson 38 attempts it again (loop is stubborn)
- It times out again
- You now have 5 blocked tickets instead of 3

This isn't a crash. It's slow suffocation. The loop still runs, but it's not making progress on complex work.

## The Question You Need to Answer

Lesson 31 asked: "Can the loop ask for help and get it?"

Lessons 34–36 are your answer so far: "No."

That's not a criticism. It means you need to build the escalation policy. This is load-bearing work.

## Next Steps

For block 41367:
1. **Decide on escalation** (human review + decomposition proposal)
2. **Implement it** (1–2 blocks of work)
3. **Land lessons 30, 34, 35, 36** with human-proposed decomposition

After that, you can shift to automatic decomposition while the human escalation keeps things moving.

The loop is asking for help. Give it a clear path to ask, and a process for you to respond. That's what escalation policy means.
