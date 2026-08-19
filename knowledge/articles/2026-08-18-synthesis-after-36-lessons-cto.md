---
tags:
  - article
  - persona/cto
---

# Lessons 32–36: Task Complexity vs. Execution Capacity

At lesson 36, your autonomous loop has revealed a fundamental limit: task complexity is growing faster than execution capacity.

## The Metrics

| Lesson | Date | Model | Outcome | Feature Scope |
| --- | --- | --- | --- | --- |
| 32 | 2026-08-16 | haiku | **PASS** | Governance artifacts (whitepaper, MOCs) |
| 33 | 2026-08-17 | haiku | **PASS** | Documentation update |
| 34 | 2026-08-17 | sonnet | **FAIL** (1200s timeout) | Practice registry + integration |
| 35 | 2026-08-17 | sonnet | **FAIL** (1200s timeout) | Failure taxonomy + triggers |
| 36 | 2026-08-18 | opus | **FAIL** (1200s timeout) | Synthesis enhancement + retrieval |

The model escalation in lesson 36 (sonnet → opus) didn't fix the timeout. This tells us the bottleneck is not inference speed or reasoning depth. It's task scope.

## Why Opus Didn't Save Lesson 36

Opus is roughly 2–3x more capable on reasoning tasks than sonnet. If the timeout were due to "the agent needs to think harder," opus would have solved it.

Instead, opus also timed out. This means one of:

1. **The task is inherently too large** — Retrieval-grounded synthesis requires modifying multiple systems, writing new code paths, and testing. Even with unlimited thinking time, some tasks are just too big to implement in one shot.

2. **The implementation approach was inefficient** — The agent might have written unoptimized code, iterated too much, or over-engineered the solution. A human would have noticed and refactored.

3. **Architectural coupling** — The task requires touching too many interdependent systems. Decoupling would reduce complexity.

Most likely: it's #1 and #3. The task is large AND it touches core systems.

## The Capacity Budget

Your current budget: 1200s wall-clock per agent run.

Breaking that down:
- **Thinking time**: 600s (large model reasoning, step-by-step planning)
- **Execution time**: 300s (writing code, running tests, fixing CI)
- **I/O time**: 150s (file read/write, network latency, Git operations)
- **Overhead**: 150s (context loading, logging, retries)

This works fine for:
- Small features (100–200 SLOC)
- Bug fixes (10–50 SLOC)
- Tests and chores (50–100 SLOC)
- Governance artifacts (governance code, not feature code)

It doesn't work for:
- Medium features (300–500 SLOC) with integration
- System-wide changes (touching 5+ files)
- Architecture changes (require re-reading the codebase)

Lessons 34, 35, 36 are all in the "doesn't work" category.

## The Three Technical Paths

### Path 1: Increase the Budget
Give agents more time (e.g., 2400s or 3600s).

**Pros**: Simple, no code changes needed
**Cons**: 2–3x quota cost increase, longer iteration cycles
**Blocker**: Your quota budget probably can't sustain this

### Path 2: Improve Model Selection
Use a heuristic to predict task complexity and route accordingly.

**Pros**: No quota increase, potentially faster with smarter routing
**Cons**: Requires ground truth (which tasks are "complex"?), takes 2–3 weeks to calibrate
**Blocker**: You need metrics from timeouts to build the heuristic

### Path 3: Decomposition
Break large tasks into smaller ones (100–200 SLOC each) and chain them across multiple agent runs.

**Pros**: Scales to arbitrarily complex features, doesn't increase quota, can use cheaper models (sonnet, haiku)
**Cons**: Requires synthesis to be smart about decomposition, higher failure rate if decomposition is wrong
**Blocker**: Need to build the decomposition logic

## My Recommendation: Path 3 + Path 1 Hybrid

Here's what I'd do:

1. **Short-term (this week)**: Implement human escalation.
   - When an agent times out, route to architect with transcript
   - Architect proposes decomposition manually
   - This unblocks the current 3 failing tickets
   - Cost: ~5 min of architect time per timeout

2. **Medium-term (2–3 weeks)**: Build automatic decomposition.
   - Analyze the 3 human decompositions to find patterns
   - Teach synthesis to propose decomposition automatically
   - Start with simple heuristic: "if modifying >3 files, split into subtasks"
   - Refine based on results

3. **Long-term (after decomposition works)**: Consider budget increase.
   - Once you have good decomposition, most tasks will fit in 1200s
   - For the occasional really-complex task, increase to 1800s (50% increase, not 2–3x)

This path keeps quota cost reasonable while scaling to more complex features.

## The Decomposition Heuristic

To get started, use these signals to detect "this task is too big":

| Signal | Threshold | Action |
| --- | --- | --- |
| Files modified | > 5 | Decompose by file |
| Lines of code | > 500 | Decompose by feature |
| Integration points | > 3 | Decompose by subsystem |
| Test additions | > 300 lines | Decompose code + tests |

If a ticket triggers any of these, propose splitting it into 2–3 subtasks of roughly equal size.

## The Risk: Incorrect Decomposition

If you split a task wrong, you get cascading failures. Lesson 34 (practice registry) depends on lesson 35 (taxonomy) depending on lesson 36 (grounding). If you decompose them independently, they might not integrate well.

Mitigation:
- Start with tasks that are self-contained (bug fixes, features with no interdependencies)
- Use human review to validate decomposition (architect approves subtasks before filing)
- Track interdependency in tickets (use labels like `depends-on:#34`)

## What's Working (Don't Break It)

Before making changes, recognize what's solid:

1. **Haiku for small tasks** (lessons 32, 33) — Very cheap, very fast, reliable
2. **Sonnet for medium tasks** — Good balance of cost and capability
3. **Opus for reasoning-heavy tasks** — But only if the task actually fits in 1200s
4. **CI gating** — Green builds are mandatory, no exceptions
5. **Lesson capture** — Every PR gets a lesson, every lesson is analyzed

Keep all of this. The problem is not with the foundation; it's with task sizing.

## The Quota Reality Check

At lessons 1–33, your quota burn was steady (mostly sonnet). Lessons 34–36 tried to jump to more complex features without changing the execution model.

If you implement decomposition well:
- Lessons 37–40 (decomposed versions of 34–36) each cost less than the original
- Overall quota stays flat or decreases
- Loop stays stable

If you just increase the budget to 2400s:
- Lessons 37–40 might succeed but at 2x cost
- Loop is more expensive but not fundamentally different
- You'll hit another boundary at lesson 50–60

Pick decomposition. It's the right move architecturally.

## For the Next Worker

When you pick up the decomposition work, you have:
- Three concrete failures (lessons 34, 35, 36) to analyze
- Clear evidence that opus escalation doesn't solve scope problems
- A working governance layer that can track task dependencies
- A quota ledger that shows cost per task

Use these as inputs to build your decomposition heuristic. Don't guess — measure.
