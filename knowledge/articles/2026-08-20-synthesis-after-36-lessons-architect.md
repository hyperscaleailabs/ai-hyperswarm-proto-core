---
tags:
  - article
  - persona/architect
---

# Lessons 34–36: The Vertical Scaling Ceiling

At lesson 36, your loop has hit a hard architectural limit: it can execute tasks within a 1200s window, but it cannot *complete complex synthesis tasks* within that window anymore.

## The Pattern

**Lesson 34** (adopted-practice registry): timeout at 1200s with sonnet. The task was to read multiple reference projects, extract practices, store them, and wire them into the synthesis context pack. Ambitious scope. Ran out of time.

**Lesson 35** (failure taxonomy): timeout at 1200s with sonnet. The task was to build a taxonomy of failures in the ledger and wire it into the backlog trigger. Complex reasoning. Ran out of time.

**Lesson 36** (retrieval-grounded synthesis): timeout at 1200s with opus. The task was to make synthesis cite its own lessons before filing tickets. Hardest task. Ran out of time, even with the heaviest model.

## Why This Matters

Lessons 29–31 hit timeouts (subscription-only execution), but they were *singular failures* followed by explicit synthesis and escalation. Lessons 34–36 are *three consecutive uniform timeouts* across different tasks, which signals a system-level constraint, not a task-level problem.

You can fix a task. You can't fix a system constraint without changing the system.

## The Architectural Decision

You have three options:

1. **Grow the wall-clock budget** (fastest, least scalable)
   - Extend 1200s → 2400s for synthesis tasks
   - Pro: Immediate unblock, no code changes
   - Con: Masks the underlying growth problem. Eventually hits a new ceiling. Increases quota cost per task.

2. **Decompose the tasks** (hardest, most scalable)
   - Break adopted-practice registry into: (a) read projects, (b) extract practices, (c) integrate into synthesis
   - Break failure taxonomy into: (a) analyze failures, (b) categorize, (c) wire trigger
   - Break retrieval-grounded synthesis into: (a) build lesson index, (b) build retrieval, (c) integrate into synthesis
   - Pro: Scales indefinitely. Each sub-task stays within budget.
   - Con: Requires redesigning how synthesis tasks are defined. Takes longer to implement.

3. **Outsource to human** (safest, least autonomous)
   - Have the architect manually implement or pair-program these capabilities
   - Pro: Guaranteed success, no resource constraint
   - Con: Defeats the purpose of the autonomous loop. Not scalable.

## My Recommendation

**Do Option 2 (decompose) immediately, but start with Option 1 (grow budget) as a short-term unblock.**

Here's why:
- Option 1 buys you time to implement Option 2 without losing forward momentum
- Option 2 is the only path that scales as the loop gets more capable
- Option 3 is a fallback only if Options 1 and 2 both fail

So: Increase the budget to 2400s *now*. This unblocks lessons 34–36 and lets them retry. In parallel, redesign the synthesis task definition and decomposition strategy for lessons 37+.

## What's Actually Happening

The loop is experiencing vertical growth pains. It can now:
- Read and understand source code
- Search its own lessons
- Synthesize analysis
- File and link tickets
- Enforce governance

All of these capabilities are stacking. A single synthesis task now requires all of them to work *together* within 1200s. That's hitting the ceiling.

This is actually a sign of health: the loop is powerful enough to take on complex work. The question is whether the infrastructure can keep pace.

## The Next Step

If you go with my recommendation (Option 1 + 2):

1. **Immediate (today)**: Increase wall-clock budget to 2400s for synthesis tasks
2. **This week**: Design the decomposition strategy (break complex synthesis tasks into sub-tickets with dependencies)
3. **Next block**: Implement the decomposition in the harness, then retry lessons 34–36

This keeps the loop autonomous and moving forward, while addressing the root cause (task complexity > per-task capacity).

## The Question You Need to Answer

Is the loop's primary constraint:
- **Wall-clock time** (Option 1: grow budget)
- **Task structure** (Option 2: decompose tasks)
- **Capability** (Option 3: outsource)

If it's wall-clock, this is a quick fix. If it's task structure, you need to redesign the synthesis layer. If it's capability, the loop has hit its design ceiling and needs human guidance.

My read: it's task structure. Lessons 34–36 are all within the loop's capability. They're just too much *at once*.

So: grow the budget, then fix the structure.
