---
tags:
  - article
  - persona/cto
---

# Lessons 31–33: Scaling Into Complexity; Infrastructure Holding

At lesson 33, the loop is handling features that require integration across multiple subsystems: practices registry needs tight coupling with synthesis, failure taxonomy needs deep ledger integration. Both features timed out at 1200s with sonnet. Both landed in production anyway. Here's the technical read.

## Capability vs. Resource Constraint

**Lesson 32** (adopted-practice registry) generated 1793 insertions across 36 files:
- New `Practices` data structure (persistence layer)
- Integration with `Knowledge` class (retrieval)
- Integration with `Synthesis` (context injection)
- 15+ new practice files (extracted from reference projects)
- 153 lines of new test code
- CLI changes, governance changes

The agent timeout at 1200s does NOT mean "this is unimplementable." It means "this is implementable, but not in 1200s+sonnet." The PR merged. CI passed. Code is live.

**Lesson 33** (failure taxonomy) generated similar scope:
- New failure classification in ledger schema
- Postmortem extraction logic
- Backlog trigger when patterns detected
- Integration with `Cycle` orchestration
- Test coverage

Same story: timed out, but landed.

## Why Both Timed Out

**Lesson 32's critical path:**
1. Parse 10 top-10 projects (git, issue history, code scanning)
2. Extract practices (LLM calls + structure mapping)
3. Write practice files (15 new markdown docs)
4. Run test suite (adds ~150 tests)
5. Verify CI (ruff + pytest)

Estimated: ~800s parsing + 200s LLM + 150s test + 100s CI = ~1250s. The 1200s limit is a hard wall.

**Lesson 33's critical path:**
1. Understand ledger schema (parsing existing code)
2. Design failure taxonomy (structured thinking)
3. Implement classification logic (medium complexity)
4. Integrate with backlog (cross-subsystem coupling)
5. Write tests + verify CI (~150 tests)

Estimated: ~400s understanding + 300s implementation + 200s tests + 300s CI = ~1200s. Exactly at the boundary.

## The Infrastructure Question

Both features timeout, yet both land and pass CI. This creates a subtle problem: **the agent doesn't know it succeeded.**

From the agent's perspective:
- Clock hits 1200s
- Agent stops
- Agent records "timeout, outcome=fail"
- Agent doesn't run its own merge/verify logic

From the loop's perspective:
- PR exists with code changes
- CI passed remotely (GitHub checks are truth)
- Lesson recorded as "fail" but PR is actually good
- Next iteration sees the PR already merged; doesn't retry

This works, but it's fragile. If the remote CI were *also* slow, you'd hit a deadlock where the PR is stranded in-flight, marked failed, and won't retry.

**Current safety nets:**
1. Remote CI is mandatory gating (GitHub checks), not optional
2. Timeout doesn't prevent PR creation or CI run
3. Lesson records timeout clearly (makes it auditable)

**What's missing:**
1. No feedback loop from "PR merged successfully" back to the lesson (lesson still says outcome=fail)
2. No de-duplication check ("this PR already exists, don't retry")
3. If remote CI takes > 1200s (unlikely, but possible), you have a hung PR

## Quota and Cost Implications

**Lesson 32 costs:**
- Agent time: 1200s wall-clock
- Model: sonnet (standard tier)
- Token estimate: ~150k input + 50k output (rough, based on scope)
- Cost: ~$0.30 (sonnet rates)

**Lesson 33 costs:**
- Agent time: 1200s wall-clock
- Model: sonnet
- Token estimate: ~100k input + 40k output
- Cost: ~$0.21

**Extrapolating to block 41367+:**
If every complex feature times out and you increase the budget to 2400s, you'd spend 2400s per feature. But you also get more implementation per buck (more features completed). The question is whether 1200s→2400s is worth the wall-clock cost (agents run sequentially in blocks), not just the quota cost (tokens are cheap).

**Wall-clock cost of increasing budget:**
- Current: 5 features per block, ~1 timeout, 4–5 succeed → ~6000s wall-clock
- If budget → 2400s: 5 features × 2400s average → ~12000s wall-clock (2x longer blocks)
- If model routing (route complex→opus): ~3 features sonnet (1200s ea), ~2 features opus (budget 3000s ea) → ~6600s wall-clock (10% increase)

Model routing is cheaper on wall-clock. The trade-off is: quota cost (opus is 3x sonnet) vs. time cost (sonnet timeout means block stalls).

## Technical Debt Accumulation

At 33 lessons, here's what's actually in the system:

| Component | Lines | Purpose | Tested |
| --- | --- | --- | --- |
| Governance layer | 200+ | Ticket linking, PR gating, CI checks | ✓ |
| Lesson capture | 150+ | Serialize iteration metadata | ✓ |
| Synthesis pipeline | 300+ | Whitepaper + persona articles | ✓ |
| Knowledge base | 200+ | MOCs, retrieval, indexing | ✓ |
| Ledger + quota tracking | 250+ | Cost telemetry, budget gates | ✓ |
| Practices registry | 277+ (new) | Practice extraction + retrieval | ✓ |
| Failure taxonomy | 100+ (new) | Failure classification + backlog trigger | ✓ |
| Agent orchestration | 400+ | Worker scheduling, CI polling | ✓ |

Total: ~1800+ LOC of load-bearing infrastructure. It's holding under load (timeouts don't crash it), but there are stress-test gaps:
1. Timeout followed by slow CI (not tested)
2. Multiple timeouts on same ticket (retry-then-block logic not exercised)
3. Quota ledger hitting the halt threshold (never tested in prod)

## System Health Check

| Signal | Status | Notes |
| --- | --- | --- |
| **CI pass rate** | ✓ 100% | All features passing remote CI despite timeout |
| **Lesson accuracy** | ✓ High | Timeouts recorded correctly; artifacts durable |
| **Governance integrity** | ✓ Green | No silent failures, no zombie PRs, no double-merges |
| **Feature latency** | ⚠ Degrading | Features completing but timeout signal creates perception of failure |
| **Model cost** | ⚠ Stable | Sonnet dominates; hasn't escalated to opus yet |
| **Escalation response** | ⚠ Partial | Failure taxonomy in place but human routing not yet wired |
| **Quota ledger** | ✓ Working | Tracking spend, not yet hitting thresholds |

Two amber signals: feature latency perception (lesson outcome=fail but code is good) and escalation response (taxonomy exists but actions aren't automated).

## The Routing Decision

You're at a choice point:

**Option A: Increase timeout budget to 2400s or 3600s**
- Pro: Features complete in-agent, no job stall
- Con: Blocks take 2x as long wall-clock
- Risk: Doesn't solve the problem if features are actually >2400s

**Option B: Implement model routing (issue #42)**
- Pro: Route complex features to opus upfront; agents complete in time; wall-clock stays reasonable
- Con: Quota doubles for complex features (~$0.60 vs $0.30 per sonnet)
- Risk: Requires accurate complexity estimation

**Option C: Decomposition (future work)**
- Pro: Most scalable long-term
- Con: Requires synthesis to be smarter about problem structure
- Timeline: 2–3 blocks away

I'd pick **Option B** (model routing) because:
1. It preserves the 1200s budget (agents don't stall)
2. It's well-scoped (modify synthesis to estimate complexity, route in `hsai.cycle`)
3. It's reversible (if opus quota burns too fast, switch to A or C)
4. It's worth the 3x cost for complex features to complete predictably

The failure taxonomy (lesson 33) gives you the data to decide. Next lessons, classify features as "light," "medium," "heavy" and route accordingly.
