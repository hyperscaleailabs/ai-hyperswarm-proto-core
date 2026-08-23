---
tags:
  - article
  - persona/architect
---

# Lesson 36: A Pattern Emerges at the Resource Boundary

You've seen this before. Lesson 30 hit a wall. Lesson 31 synthesized and asked for escalation guidance. Now, at lesson 36, the wall is back—but this time it's not one ticket retried. It's three different features, all failing the same way.

## The Sequence

**Lesson 34** (adopted-practice registry): You want to catalog all practices the loop has learned from the top-10 projects, attach evidence and source lineage, and wire the registry into synthesis prompts. Ambitious scope. Sonnet timed out at 1200s.

**Lesson 35** (failure taxonomy): You want to classify every failure recorded in the ledger, find patterns, and auto-file postmortem tickets. Also ambitious. Sonnet timed out at 1200s.

**Lesson 36** (retrieval-grounded synthesis): You want the planner to read its own lessons, check them for consistency, cite them properly, and avoid hallucinating references. Smaller than 34 or 35, but opus still timed out at 1200s.

Three features. Three timeouts. The underlying pattern is clear.

## What This Tells You

The loop hit a **class boundary**, not a ticket boundary.

When you see lesson 30 timeout (subscription-only execution with sonnet), it's fair to assume sonnet is the problem. Route to opus, retry.

When you see lesson 34, 35, and 36 all timeout (three different tickets, two models, three problem classes), the problem isn't the feature or the model. The problem is the **time budget vs. the work required**.

These three features aren't wrong. They're too large.

## The Decision Points

You have three levers:

**Lever 1: Time.** Increase the wall-clock from 1200s to 1800s (or 2400s) and retry. This works if the issue is just "we need 30% more time." Cost: longer cycles, higher quota spend.

**Lever 2: Scope.** Break each feature into smaller tickets that complete in <1200s. Adopted-practice registry becomes "extract practices from langchain-ai/langchain" + "deduplicate" + "attach evidence" as separate tickets. This works if the issue is "we're trying to do too much in one shot."

**Lever 3: Architecture.** Implement a "fast 70% / slow 30%" model where synthesis generates a quick result under 1200s, and background processes (not on the hot path) enrich it later. This works if the issue is "synchronous execution is the bottleneck."

I recommend trying **Lever 1 first** (increase time), then **Lever 2** (break scope) if that doesn't work. Lever 3 is longer-term infrastructure.

## Why This Matters

Lessons 34–36 are not failures. They're informative signals. The loop is telling you what class of work it can't yet handle. That's valuable. Use it.

If you increase time to 1800s and lessons 34–36 pass, you've learned: "these features need 1800s, not 1200s. Budget accordingly."

If they still fail, you've learned: "time isn't the constraint. We need to rethink scope or architecture."

Either way, you're calibrating the loop's capability model.

## The Bigger Picture

Lesson 29 asked: "Can the loop handle resource boundaries?"
Lesson 30 showed: "Yes, it stops and signals gracefully."
Lesson 31 concluded: "The loop is ready for escalation."

Lessons 34–36 are asking the **next question**: "If we increase scope, can the loop still stay within bounds?"

The answer, so far, is "no." Three features, all blocked by time.

This doesn't mean the features are bad or the loop is broken. It means you need to **recalibrate the scope-to-time model.** 

The loop's infrastructure is sound. Your job now is tuning.

## What I'd Do If I Were You

1. **Run a quick experiment**: Pick lesson 34 (adopted-practice registry) and re-run it with 1800s wall-clock. Does it pass? If yes, adopt 1800s. If no, move to step 2.

2. **If time doesn't help**: Break adopted-practice registry into 3 tickets instead of 1. Example:
   - #274a: "Extract practices from langchain-ai/langchain" (small scope, <1200s)
   - #274b: "Merge with existing practices, deduplicate" (medium scope, <1200s)
   - #274c: "Attach evidence and wire into synthesis prompt" (medium scope, <1200s)

3. **Apply the same logic** to lessons 35 and 36.

4. **By lesson 40**, you'll have a better sense of the loop's true time budget and the right scope for each ticket.

This is normal tuning work. You're learning where the edges are.

