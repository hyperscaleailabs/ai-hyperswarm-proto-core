---
tags:
  - article
  - persona/cto
---

# Autonomous Engineering Loop: Status After 15 Lessons

## Bottom line
Our self-improving build loop — an AI system that implements and improves its own services, then extracts lessons from each run — has now completed 15 cycles. The most recent 5 went 5-for-5: all shipped clean, merged, and green. That's a real signal, but a 5-run sample isn't a track record, and I want to be precise about what we know versus what we're still watching.

## What's working
The loop is doing real engineering work, not just prototyping. Of the last 5 cycles, 2 were new implementations and 3 were improvements to existing code — meaning the system is spending real time on maintenance and hardening, not just greenfield builds. That mix is healthy; a loop that only ever adds new code and never revisits old code accumulates debt invisibly.

The recurring themes in this window — "build," "change," "merged," "green" appearing across 3 of 5 lessons — point to a consistent, unremarkable pattern: change lands, tests pass, PR merges. Boring is good here. Boring means predictable.

## What's failed, and what we don't yet know
This 5-run window had zero failures. I want to be upfront that this is a favorable window, not proof the loop is failure-proof. Earlier cycles in this same lineage surfaced real problems — we invested a dedicated lesson in loop reliability, specifically retry behavior and CI parity, which tells you the loop has, at some point, hit flaky or non-reproducing failures in CI that needed deliberate fixing rather than being waved off. We also added integration tests specifically for the orchestrator's run-once, heal, and implement code paths — meaning those paths weren't adequately covered before, and gaps like that are exactly where a self-modifying system can quietly regress.

The honest read: the loop has been engineered to fail loudly and recover, not to never fail. The last 5 runs show the recovery mechanisms aren't currently being exercised — which is good — but I don't have a long enough green streak yet to say the underlying failure modes are gone versus just not triggered this window.

## Risk posture
This system currently operates inside a scoped, sandboxed loop with worktree isolation — it can't run arbitrary commands against production, and workers are explicitly denied test-execution privileges in some contexts, forcing verification to happen through the orchestrator rather than ad hoc. That's a deliberate constraint, not an oversight, and it's the main reason I'm comfortable with the pace of autonomy here: the blast radius of a bad cycle is a failed PR, not a production incident.

## Where this is headed
Two things I'd want before extending this loop's scope: (1) a longer observation window — 15-20 more cycles — before trusting the 5/5 streak as steady-state, and (2) explicit tracking of near-misses (retries, heals) as first-class metrics, not just pass/fail, since a system that heals from failure looks identical to one that never failed unless we're counting the heals.

Net assessment: promising trajectory, appropriately caged, not yet a track record.
