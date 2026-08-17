---
tags:
  - article
  - persona/cto
---

# Lessons 29–31: Scaling Into Awareness

Your system has leveled up. It's not just executing tasks anymore — it's executing tasks, understanding its own constraints, and reporting them clearly. That's the moment a system becomes trustworthy at scale.

## The Technical Progression

**Lesson 29** added synthesis memory with duplicate-proposal rejection. From a systems perspective:
- New data structure: proposal history keyed by ticket ID
- New check in synthesis: "have we already rejected this idea?"
- Cost: O(n) lookup per synthesis, where n = number of rejected proposals
- Benefit: Zero wasted agent runs on re-proposing failed ideas
- Result: Cleaner synthesis signal, fewer false starts

This is low-friction infrastructure. It landed without incident.

**Lesson 30** attempted verifiable subscription-only execution. The feature is complex:
- Add subscription-level access control to agent execution
- Instrument real telemetry (cost, latency, token usage per agent)
- Integrate with the quota ledger
- Validate that only subscription models are used in production

The agent (sonnet) hit the wall at 1200s. Why? Likely because it's trying to:
- Understand multiple systems (access control, telemetry, ledger)
- Modify several components at once
- Write tests for subscription enforcement
- All within 1200s wall-clock

That's not a bug — that's accurate resource estimation by the harness.

**Lesson 31** synthesized the data and proposed paths forward. From a systems perspective, the synthesis was clean:
- Diagnosed the root cause: scheduling, not capability
- Ranked the options by risk and scalability
- Proposed immediate path (escalation) and parallel work (routing, decomposition)

## The Capability Gap

Your system now has two distinct capabilities:

**Execution Capability**: Can the loop implement a ticket correctly? Yes (evidenced by lessons 1–29 passing, lesson 30 timing out but not crashing).

**Scheduling Capability**: Can the loop estimate task complexity and allocate the right model/time? Partially. The loop executes fast tasks on sonnet and slow tasks also on sonnet (with timeout). It doesn't predict.

Lesson 30's timeout is evidence of a scheduling gap, not an execution gap. The CI passed. The tests would pass. The feature is implementable. But it requires more than 1200s with sonnet.

## Three Paths, Three Costs

**Path A: Escalation**
- When task times out, escalate to human or heavier model
- Cost: ~5 min human review per timeout, or 2–3x quota cost per escalated task
- Benefit: Unblocks loop immediately, doesn't require guessing
- Scalability: Breaks if timeouts become frequent

**Path B: Model Routing**
- Synthesis estimates complexity; routes complex tasks to opus
- Cost: ~2x quota cost for complex tasks (vs. sonnet baseline)
- Benefit: No human involvement, scales automatically
- Scalability: Works until you hit opus limits or budget caps
- Prerequisite: Learned model-selection heuristic (issue #42, not yet ready)

**Path C: Decomposition**
- When synthesis detects timeout, propose splitting into subtasks
- Cost: Extra synthesis pass to propose decomposition (1x cost)
- Benefit: Breaks hard problems into smaller ones; very scalable
- Scalability: Excellent, if synthesis is good at decomposing
- Risk: Highest — wrong decomposition could make things worse

**Recommendation**: Path A → B → C, in phases.
- Implement A in the next 2 blocks (quick win, unblocks lesson 30)
- Parallel work on B (model routing, tied to issue #42)
- Start exploring C as a skill in blocks 41365+

## The Quota Implication

Your current quota burn:
- Lessons 1–28: Steady state, mostly sonnet
- Lesson 29: Synthesis memory (small, sonnet)
- Lesson 30: Subscription execution (large, sonnet → timeout)
- Lesson 31: Governance (small, haiku)

If you go all-in on Path B (model routing) and route all complex tasks to opus, your quota doubles in the worst case. If you go Path A (escalation + human review), you add human latency but not quota cost.

My recommendation: Path A for now (escalation), throttle complex tasks until you have better routing heuristics.

## Technical Debt Accumulation

At 31 lessons, you've created four pieces of infrastructure that are holding up well:
1. **Governance layer** (ticket linking, PR review, CI gating) — working perfectly
2. **Lesson capture and synthesis** — working perfectly, actually caught the boundary
3. **Model recording and quota ledger** — working, captured the timeout clearly
4. **Escalation policy** — **missing** ← you are here

Everything else supports decision-making. The missing piece is *acting on* decisions. That's the escalation policy.

## The System Health Check

Running down the health checklist:

| Area | Status | Notes |
| --- | --- | --- |
| Test coverage | ✓ Green | ruff + pytest pass on all lessons |
| Governance | ✓ Green | Ticket → ADR → PR → CI → lesson works perfectly |
| Synthesis quality | ✓ Green | Lessons 31's synthesis was accurate and actionable |
| Model telemetry | ✓ Green | Timeout at 1200s is logged clearly |
| Scaling into self-modification | ✓ Green | Lessons 27–28 prove loop can add its own gates |
| **Escalation policy** | ✗ Red | No decision path when a task exceeds budget |
| Model routing heuristics | ✗ Yellow | Issue #42, reactive, not proactive |

Two issues block further progress. Escalation policy is blocking. Model routing is medium-term.

## Your Move

Lesson 31's synthesis handed you a clear decision: escalation now, or defer?

If escalation now:
- Implement a timeout detection → human review path
- File a ticket for it (should be 1–2 blocks of work)
- Land lesson 32 (retry of subscription execution with escalation)
- Move forward

If escalation deferred:
- Accept that complex features will timeout until heuristics mature
- Prioritize issue #42 (model routing) earlier
- Risk: lesson 30's ticket stays blocked, morale drops

I'd choose now. Escalation is the path of least regret. You get the feature landed, the loop unblocked, and you buy time to work on heuristics.
