---
tags:
  - article
  - persona/devops
---

# Observability Win: We Know Exactly Why Three Tickets Failed

From a DevOps lens, lessons 34–36 are not a failure. They're a **win in observability**.

Most systems that hit a resource wall at this scale produce cryptic errors, incomplete traces, or downstream data corruption. This one produced clear, actionable signals.

## The Observability Chain

**Signal Generation (Lessons 34–36):**
- Lesson 34: Agent ran for 1200s, hit timeout, logged `[phase=implement, ticket=#272] timeout after 1200s`
- Lesson 35: Agent ran for 1200s, hit timeout, logged `[phase=implement, ticket=#273] timeout after 1200s`
- Lesson 36: Agent ran for 1200s, hit timeout, logged `[phase=implement, ticket=#292] timeout after 1200s`

**Signal Propagation (Lesson Files):**
- Each lesson recorded: outcome, ticket number, model used, phase, wall-clock failure, CI status
- No data corruption. No cascading failures. No stuck agents.

**Signal Analysis (Synthesis at Lesson 36+1):**
- We aggregated three signals and recognized a pattern: "same class of failure, three times"
- We classified it as a resource constraint, not a code bug
- We proposed a decision tree (time vs. scope vs. architecture)

**Actionability:**
- The architect has a clear decision: "Do we add time, shrink scope, or change architecture?"
- The CTO has quota impact estimates and a test plan
- The DevOps team has monitoring criteria to watch if we increase wall-clock

This is how resilient systems should work.

## What We're Doing Right

1. **Bounded Failures**: Timeout is a clean failure mode. No infinite loops, no partial writes, no resource leaks.

2. **Instrumentation**: Every lesson records model, phase, wall-clock, CI status, and error. This is load-bearing infrastructure.

3. **Idempotency**: A ticket can be re-run with different settings (e.g., 1800s wall-clock instead of 1200s) without side effects.

4. **Traceability**: Each lesson links to a ticket, which links to a PR, which links to the actual code changes. Complete chain.

5. **Synthesis Loop**: We don't just log failures; we analyze them in context and file synthesis tickets. The loop learns from its own failures.

## What Needs Hardening

1. **Per-Ticket Quota Tracking**: We know wall-clock, but do we know token-per-second? If lessons 34–36 are doing 100k tokens/min, we're hitting a quota rate limit, not a time budget. This deserves explicit monitoring.

2. **Early Warning**: Can we detect that a ticket is trending toward timeout at 600s and bail early? Or warn the user? 1200s of wasted quota on a guaranteed failure is preventable.

3. **Fallback Modes**: If a ticket times out, do we have a "return partial result" mode? Lesson 36 (retrieval-grounded synthesis) could return "synthesis without citations" in <1200s, and a background process could add citations later.

4. **Capacity Planning**: When do we need to provision additional model quota (e.g., more opus tokens/day)? We should be forecasting this, not reacting to it.

## Monitoring Checklist for Next Cycle

When we increase wall-clock to 1800s (recommended in block 41376), watch:

- [ ] **Lesson 34 wall-clock**: Does it stay under 1800s? How much?
- [ ] **Lesson 34 tokens-per-second**: Is it consistently high? (Signals bottleneck)
- [ ] **Lesson 34 quota impact**: Did tokens increase 50% (proportional to time) or more?
- [ ] **Queueing latency**: Does going 1200s→1800s cause other workers to queue?
- [ ] **Parallel retry impact**: If lesson 34, 35, 36 all run at 1800s in parallel, do we hit rate limits?

If any of these shows a red flag (e.g., token rate is 10x higher during 1800s runs), the issue isn't time—it's something else. Escalate to the architect.

## The Infrastructure Opportunity

Lessons 34–36 demonstrate that the **observation layer is working.** The next step is the **prediction layer**: estimate task complexity *before* running it, and route accordingly.

This is issue #42 (learned model-selection heuristic). It's currently in the P2 backlog. I'd move it to P1 if lessons 34–36 confirm the pattern (i.e., if all three pass at 1800s, we know scope is calibrated; if they all fail, we know we need preemptive decomposition).

## For the Next Worker

You have three sensors firing at once:
- Architecture: "These features are well-designed, but the scope is too large"
- Cost: "Retrying at 1800s is worth a one-time 51% quota spike to test"
- Operations: "Our observability is good; we can trust the signals and make data-driven decisions"

Lesson 37 should be a small feature (1–2 days of work for a human engineer) to keep the loop warm while lessons 34–36 are being re-run or decomposed. Examples:
- Small bug fixes (heal or bugfix tickets)
- Documentation improvements
- Small practice extractors (one project, one practice)

Save the big architectural changes for when you have more data.

