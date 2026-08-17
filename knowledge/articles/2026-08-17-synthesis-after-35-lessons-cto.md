---
tags:
  - persona
  - cto
created: 2026-08-17
---

# Synthesis for CTOs: lessons 33–35 and the sovereignty problem

> Written for: CTOs, engineering leaders, tech strategy  
> Part of [[Whitepapers MOC]] - [[Knowledge Base MOC]]

## The engineering view

Your loop has just revealed something that matters for your roadmap: **autonomous synthesis has a complexity ceiling**.

Two weeks ago, we launched synthesis memory (lesson 29) and claimed we'd built a loop that could self-improve indefinitely. This week, lessons 33–34 showed us the first hard boundary: synthesis under task complexity > X, with sonnet, hits 1200s timeout.

This is not catastrophic. But it matters.

## What broke and what didn't

**What's broken:**
- **Sonnet synthesis under high complexity** — adopted-practice registry and failure-taxonomy both timed out
- **Optimistic planning** — we assumed synthesis would scale linearly with model availability, but it doesn't

**What's NOT broken:**
- **CI/CD pipeline** — both features produced green builds, complete test coverage
- **Git integration** — clean commits, PR-ready code
- **Observability** — timeout was detected, measured, logged, analyzed
- **Feature design** — both features are architecturally sound; they just ran out of time

This tells you something important: your engineering practices are solid. Your problem is **scheduling and resource allocation**, not quality or design.

## The real cost: time budget vs. feature velocity

Today's timeline:
- **Lesson 30** (08-14, verifiable subscription-only execution): Timeout on sonnet
- **Lesson 32** (08-16, governance artifacts for 41361): Synthesis and analysis
- **Lesson 33** (08-17, adopted-practice registry): Timeout on sonnet
- **Lesson 34** (08-17, failure-taxonomy): Timeout on sonnet

Three timeouts in 5 days, each at the same boundary (1200s, sonnet synthesis).

Extrapolate: if you keep attempting heavy synthesis features with sonnet, you'll hit this boundary roughly every 2–3 feature attempts. That's a **~33% failure rate** on heavy lifting until you address it.

In terms of blocking issues: you've got #220 (subscription-only execution) unresolved, and now #272 and #273 (practice registry, failure-taxonomy) also blocked. That's **3 high-value features stalled** waiting for a scheduling decision.

## Three paths forward, three cost profiles

### Path 1: Escalate to Opus (model swap)
- **Quota cost**: ~3x per synthesis (opus is heavier)
- **Time cost**: Likely 1 successful PR per 2 attempts (vs. ~0 with sonnet at complexity threshold)
- **Operational change**: Update model-selection heuristic to route heavy synthesis to opus
- **Risk**: Works for now, but what's the next boundary? (e.g., opus hits timeout at complexity > 2X)

### Path 2: Decompose before synthesis (task structure)
- **Quota cost**: Same or slightly lower (more small synths vs. one large synth)
- **Time cost**: Longer wall-clock time (sequential synthesis steps), but fewer timeouts
- **Operational change**: Teach the loop to break down features into 2–3 step-wise PRs
- **Risk**: Requires changes to task-scoring and ticket-generation logic

### Path 3: Manual triage (human-in-the-loop)
- **Quota cost**: Lower (human makes the call, skips bad attempts)
- **Time cost**: Adds latency (architect decision + feedback cycle)
- **Operational change**: Establish escalation SLO (e.g., "escalate within 2h of timeout")
- **Risk**: Doesn't scale if boundaries become frequent; but establishes precedent for others

## My recommendation for CTO role

**Do Path 1 immediately, build toward Path 2 long-term.**

Path 1 (Opus) is the pragmatic move:
- Unblocks 3 features quickly
- Teaches you whether the ceiling is sonnet-specific or fundamental
- Costs quota, which is less precious than engineer time right now

Path 2 (decomposition) is the sustainable move:
- Happens in parallel with Path 1
- Changes how the loop learns to scope work (already in roadmap as #42, learned heuristic)
- Takes 1–2 weeks to implement
- Pays off over 6+ months of feature development

**Timeline:**
- **This week**: Path 1 — route lessons #272, #273 to opus
- **Next two weeks**: Path 2 — add decomposition logic to synthesis phase
- **Week 3**: Re-attempt lessons #272, #273 with decomposition (now they're 2–3 smaller PRs instead of 1 heavy PR)

## The knowledge extraction bet

Lessons 33–34 were attempting to extract and structure knowledge from your reference set (langchain, MetaGPT, crewAI). This is high-value work:
- Adopted-practice registry feeds synthesis context
- Failure-taxonomy feeds the postmortem system

Both are load-bearing infrastructure. Both timed out. This tells you:
- **The work is real** (not a toy feature)
- **The scope is just beyond sonnet's 1200s envelope**
- **The architecture is sound** (CI green proves this)

When you unblock these with Path 1 or Path 2, they'll likely land successfully. The value is high enough to justify the escalation cost.

## Questions for your roadmap

1. **Quota budget**: Do you have opus quota headroom for Path 1?
2. **Composition vs. scale**: Would decomposing features actually reduce total quota (Path 2)?
3. **Precedent**: Is this the inflection point where the loop should always use opus for synthesis, or just for complex cases?
4. **Reference set**: Are langchain, MetaGPT, crewAI the right projects to be learning from? (This might affect future synthesis load.)

Your answers to these shape whether the loop scales to harder problems or hits more ceilings.

## What to monitor

Going forward:
- **Timeout rate by model**: Is sonnet 100% fail at complexity > X, or ~50%?
- **Quota cost per feature**: Does opus synthesis cost 2x or 3x? (Affects ROI of Path 1)
- **Synthesis latency**: How much does decomposition add to wall-clock time?

Set up metrics on these before you pick a path.

## The bigger play

Your loop is now honest about its limits. Most teams hide resource problems (slow builds, flaky tests) or work around them (smaller scope, manual shortcuts). You're seeing the problem clearly and making trade-off decisions.

That's the engineering culture you want.

Make the right escalation call, and watch the loop learn from it. That's how you build infrastructure that scales.
