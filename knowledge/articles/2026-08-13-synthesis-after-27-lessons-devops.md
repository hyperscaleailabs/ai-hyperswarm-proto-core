---
tags:
  - article
  - persona/devops
---

# From Incident Response to Preventive Instrumentation

> For: DevOps level - CI/CD, automation mechanics, operational lessons
> From: [[2026-08-13-synthesis-after-27-lessons]]

## What Lesson 25 Taught Us About System Observability

Lesson 25 (governance artifacts failure) wasn't a crash. There were no error logs to page you at 3am. Instead:
- The task ran. Took time. Completed the code. CI passed.
- The PR was created. Review passed. Merge succeeded.
- But: no knowledge artifacts were written. No lesson was recorded.

This is the worst kind of failure: **silent degradation**. The system looked healthy. The metrics said green. But a critical function (governance record-keeping) was skipped.

## What Changed Between Lesson 25 and Lesson 27

Two operational improvements became visible:

**1. Early validation gates** (PR #203: adversarial review gate)
- Before: You found bugs in code review (post-merge, after CI passed)
- After: You catch them before PR creation (different-model cross-check)
- Operational impact: No more "merge and revert" cycles; lower CI queue pressure

**2. Lesson-retrieval memory** (visible in lesson 26 success)
- Before: Workers got full prior-lesson text injected (expensive tokens)
- After: Workers get BM25-ranked prior lessons (selective injection)
- Operational impact: Better resource utilization; fewer timeout incidents

**3. Iteration ledger tracking** (implied from lesson-26 completing)
- New files: knowledge/ledger/iterations.jsonl
- Tracks: token spend, model tier, outcome per iteration
- Operational impact: You can now detect cost anomalies before they spiral

## The Three Operational Metrics That Matter Now

**Metric 1: Synthesis Validation Gate Pass Rate**
- Target: >95%
- Watch for: sudden drops (malformed ticket generation)
- Action: Scale back batch size if validation failures spike

**Metric 2: Worker Tier-Selected Correctly**
- Target: Light tasks on haiku, heavy on opus; <5% tier mismatch
- Watch for: Timeouts on haiku tier, wasted quota on opus for trivial work
- Action: Review model-selection heuristic weights

**Metric 3: Chore Task Merge Time**
- Target: Same as feature work (no systemic delay)
- Watch for: Governance tasks taking 3+ iterations to merge
- Action: Increase chore work priority in backlog ordering

## CI/CD Pipeline Changes You've Implicitly Adopted

Looking at lessons 1-27, your pipeline now includes:

1. **Local CI checks** (per-worker: ruff, pytest)
2. **Remote CI** (GitHub actions: full suite)
3. **Model-tier cross-review** (new: adversarial gate checks synthesis quality)
4. **Lesson recording** (automatic: per PR, records model + outcome)
5. **Knowledge injection** (new: prior lessons fed into next synthesis)

This is a 5-gate pipeline. Understand that each gate is a latency trade-off:
- Cross-review adds 2-3 minutes per PR
- Lesson recording adds 1 minute per PR
- Knowledge injection adds synthesis latency (amortized, but real)

If you hit CI throughput bottlenecks, you'll have to decide which gates are critical vs. which can be downsampled (e.g., cross-review every other PR, not every PR).

## Incident Response Playbook (Emerging)

You now have three classes of incidents based on lessons 23-27:

**Class A: Synthesis timeout**
- Signal: Workers timeout at 1200s
- Root cause: Prompt injection too large
- Fix: Reduce lesson-retrieval k (from 5 to 3), or prune history
- Ownership: Orchestrator team

**Class B: Silent halt** 
- Signal: Task completes, CI passes, but no merge
- Root cause: Validation gate rejected synthesis (malformed ticket)
- Fix: Spike a code-review session on the synthesis prompt
- Ownership: Synthesis team (heavy-model gate)

**Class C: Chore task dropped**
- Signal: Governance artifacts missing for 2+ blocks
- Root cause: Feature work saturated the backlog
- Fix: Increase synthetic priority weight for chore work
- Ownership: Orchestrator scheduling

## Next Operational Focus

**Weeks 1-2 (lessons 28-35)**: Stability. Monitor the three metrics above. Don't make any big changes yet.

**Weeks 3-4 (lessons 36-50)**: Scale carefully. If you run 2-3 parallel workers instead of sequential, watch for:
- Synthesis backlog depth (tickets generated faster than workers consume them)
- Knowledge base writes colliding (multiple PRs modifying the same MOC file)
- Queue depth (iteration journal growing too fast)

**Weeks 5-6 (lessons 50+)**: Plan for bottleneck. You'll hit one. Have a playbook ready:
- Shard the knowledge base (by topic, not monolithic)
- Distribute synthesis (each worker proposes tickets locally)
- Implement committee deduplication (validate cross-worker proposals)

The loop is operationally healthy now. Your job is to keep it that way as scale increases.
