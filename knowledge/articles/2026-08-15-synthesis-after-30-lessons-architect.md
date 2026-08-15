---
tags:
  - article
  - persona/architect
---

# Escalation Logic: The Architecture Lesson from Repeated Timeout

After lesson 29's timeout, lesson 30 repeated the exact same failure. This is the diagnostic moment every system architect should recognize: **observed patterns are not variance — they are signals about how to design for them.**

## The Pattern: A Ticket That Exceeds Budget

Verifiable subscription-only execution (tickets #220, retried in lesson 30) hit 1200s wall-clock timeout twice with `sonnet` (standard model). The architecture question is not "why did this fail?" — we know why. The question is "how do we design the system so it doesn't repeat the same failure twice?"

Three architectural patterns apply:

### Pattern 1: Escalation
When a task exceeds budget under a given resource allocation, escalate to the next tier and retry. In our case: `sonnet` → `opus`. This trades quota (heavier model) for reliability (the ticket likely fits). Prerequisite: your system must *know* which tier failed and be able to route to the next tier.

### Pattern 2: Decomposition
Before assigning a ticket to a worker, the planner should detect complexity and pre-split it into subtasks. "Feature X is too complex for one agent" → file two tickets instead of one. Prerequisite: your synthesis engine must estimate complexity and have a decomposition strategy.

### Pattern 3: Escalation to Human
If a ticket times out twice under escalation, escalate to human review instead of entering a retry loop. "This ticket needs architecture-level decisions, not more compute." Prerequisite: your loop must have a human-loop capability and must be willing to admit when it is out of depth.

## The Architecture We Have vs. The Architecture We Need

**Current:** Worker timeout is treated as a failure. Lesson recorded, ticket stays in backlog (or is marked blocked if retries exhausted). No escalation logic. No "try a heavier model" logic.

**What we should have:** A three-tier retry policy:
1. **First failure:** Log and retry with same tier (transient error)
2. **Second failure (same class):** Escalate tier or decompose ticket (systematic error)
3. **Third failure (after escalation):** Human review (out of scope)

This is a straightforward state machine. The architecture gain: the loop can now handle a wider class of problems without human intervention, *and* it knows its own limits.

## Why This Matters for 30-Lesson Systems

At 29–30 lessons, you have enough data to see patterns. You have learned what works (synthesis gates, duplicate rejection, adversarial review). You have also learned what doesn't work (assigning hard tickets to light models). 

The difference between a 30-lesson loop that *records* failures and a 30-lesson loop that *acts on* failures is the escalation layer. It is not a small feature; it is the difference between "my system observes its own failures" and "my system adapts to its own failures."

## Implementation Path

Add a `tier_escalation_on_retry` rule to the model-selection heuristic:
```python
if ticket_retry_count == 1 and previous_outcome == TIMEOUT:
  escalate_tier(from_current_tier)  # sonnet → opus
elif ticket_retry_count >= 2 and previous_outcome == TIMEOUT:
  escalate_to(human_review)
```

This is one small addition to the loop's orchestrator. It will cost more quota (heavier models on retries). It will also cause lesson 30's retry to likely land within budget.

## The Insight

Lesson 29 and 30 are not separate failures; they are one failure with two attempts. The loop proved it could *detect* the failure (lesson recorded faithfully). The next proof is *acting on* the detection (escalating on retry).

That proof — escalation logic — is the architecturally minimal change that will shift the loop from "system that fails gracefully" to "system that fails forward."
