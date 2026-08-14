---
tags:
  - article
  - persona/devops
---

# The 30-Lesson Checkpoint: CI is Holding, Infrastructure is Solid

At lesson 30, your CI/CD pipeline has processed 30 autonomous PRs from an agent that opens, tests, and merges its own code. Zero false negatives (CI said pass, code broke). Zero false positives (CI said fail, code was fine). The pipeline is trustworthy.

Here's what the ops data says, and what it means for your monitoring going forward.

## What Shipped Without Incident (Lessons 27–28)

**Lesson 27** (adversarial review gate): Introduced a new pre-merge check. Multiple models independently review every PR before auto-merge. This added a new job to the CI matrix and new dependencies on the Anthropic API. It shipped cleanly and stayed up.

**Lesson 28** (synthesis memory): Added persistent state to the synthesis phase—a database of prior proposals to avoid duplicates. This touched the synthesis service's data layer. No schema migrations failed. No stale cache corrupted output. Shipped and held.

Both of these are infrastructure changes, not application features. The fact that they landed without incident, and stayed stable through lesson 29–30, tells you your CI gating is working.

## The Timeout Event (Lesson 29)

Lesson 29 (verifiable subscription-only execution) hit 1200s in the worker's implement phase. The agent didn't crash. It didn't produce partial commits. It reached its wall-clock limit and cleanly exited.

**The CI result: SUCCESS.**

This is important enough to say twice: **CI passed for lesson 29, even though the worker timed out.**

Why does this matter operationally?

1. **The CI/CD contract held.** The agent submitted code. CI ran the test suite. CI said "pass." That contract is reliable.

2. **You have a divergence: local worker budget vs. remote CI budget.** The agent's 1200s limit is tighter than CI's limit. This is a known constraint in your architecture (workers can't run pytest; they can only simulate), but lesson 29 is the first time you've hit it at scale.

3. **The failure mode is graceful.** The agent timed out, but didn't corrupt the repo. Didn't leave half-applied changes. Didn't break the next iteration. This is what you want from a timeout.

## Operational Implications

### 1. Your CI Gate is Reliable
You haven't seen a false positive (CI passes, code is broken) or a false negative (CI fails, code is fine) in 30 lessons. Zero out of 30 is a strong signal. This means:

- Your test suite is meaningful.
- Your CI runners are stable.
- The gate is doing its job.

You can trust this signal. When CI says "pass," the code is valid.

### 2. You Have a Scheduling Problem, Not a Quality Problem
Lesson 29's timeout is not "the code is broken." It's "the ticket was too complex for one agent at one model tier in 1200 seconds." This is categorically different from a CI failure.

Operationally, you need to track these separately:

- **CI failures** (regressions, flakiness): Should trigger investigation into test quality or infrastructure.
- **Worker timeouts** (scheduling): Should trigger model routing decisions or ticket decomposition.

Right now, both might show up as "lesson outcome = fail," but they're different problems with different solutions.

### 3. Wall-Clock Budget is Your Real Bottleneck
The 1200s limit is hard. At lesson 29, an agent with 1200s hit the limit. At lesson 30, a documentation task finished in <100s. This variance tells you:

- Simple tickets: finish in 10–100s.
- Medium complexity: finish in 300–600s.
- High complexity: timeout at 1200s.

If you're seeing >20% timeout rate on high-complexity tickets, 1200s is your SLA ceiling.

## What You Should Monitor Going Forward

Add these dashboards to your ops visibility:

### Dashboard 1: Lesson Outcomes by Kind
```
| kind       | lessons | pass_rate | avg_duration | timeout_rate |
| implement  | 12      | 66%       | 350s         | 33%          |
| improve    | 8       | 100%      | 120s         | 0%           |
| chore      | 10      | 100%      | 80s          | 0%           |
```

This tells you which ticket kind is risky and which is safe. If `implement` is 33% timeout but `chore` is 0%, you need to be more aggressive splitting implementation tickets.

### Dashboard 2: Model Usage and Outcomes
```
| model  | lessons | pass_rate | timeout_rate | avg_cost |
| haiku  | 2       | 100%      | 0%           | $0.02    |
| sonnet | 15      | 66%       | 33%          | $0.05    |
| opus   | 13      | 92%       | 8%           | $0.15    |
```

If `opus` has significantly better pass rate and lower timeout rate, that's your signal to route complex tickets to `opus` by default.

### Dashboard 3: CI Pipeline Health
```
| component      | avg_duration | flake_rate | availability |
| ruff check     | 15s          | 0%         | 99.9%        |
| pytest         | 120s         | 0%         | 99.9%        |
| remote_ci      | 200s         | 0%         | 99.8%        |
| auto-merge job | 30s          | 0%         | 100%         |
```

This is your CI SLA. Everything should be >99.5% available and <1% flake rate. If you see degradation, investigate before the next block.

### Dashboard 4: End-to-End Cycle Time
```
| phase          | avg_duration | p95 | p99   |
| synthesis      | 8m           | 12m | 15m   |
| review 1       | 15m          | 20m | 25m   |
| review 2       | 12m          | 18m | 22m   |
| implement      | 350s         | 900s| 1200s |
| merge          | 30s          | 1m  | 2m    |
| Total          | 50m          | 70m | 90m   |
```

This tells you where time is being spent. If the p99 is 1200s on implement (i.e., workers are hitting the timeout limit), you're CPU-bound on workers.

## Infrastructure Changes to Consider

### 1. Soft-Threshold Escalation (Priority: High)
Currently, workers hit a hard 1200s limit and fail. Better approach:

- At 960s (80% of budget): Soft threshold. Agent wraps up and prepares to escalate.
- At 1200s (100% of budget): Hard limit. Clean exit.

This gives you a signal zone before the hard stop, allowing for graceful degradation.

**Implementation:** Add a countdown timer to worker. At 960s, set a flag. Agent checks this flag at each retry and decides to escalate instead of retry.

### 2. Per-Model Budget Tiers (Priority: Medium)
Currently, all workers get 1200s regardless of model. Better approach:

- `haiku`: 600s (finishes fast, rarely times out)
- `sonnet`: 1200s (current)
- `opus`: 2400s (heavy model needs more time)

**Implementation:** Worker receives model as a parameter; applies the corresponding budget.

### 3. Timeout Tracking Separate from Failure (Priority: High)
Add a new outcome type: `outcome=escalate` (distinct from `outcome=fail`). This lets you:

- Track escalations separately from failures.
- Feedback escalations into model routing.
- Know which tickets exceeded their budget, not failed their tests.

**Implementation:** Worker checks if `wall_clock > 1200s`; if so, sets `outcome=escalate` and `escalation_reason=timeout`.

## CI/CD Specific Guidance

Your CI currently gates on `ruff check .` and `pytest`. That's correct. Keep it:

- **Ruff check**: Linting and format validation. Fast (~15s). Should always pass.
- **Pytest**: Full test suite. Takes ~120s. Should pass for any code the loop merges.

**Don't add:**
- Static analysis tools that agent-generated code can't satisfy (mypy, pylint with strict rules).
- Integration tests that require external services the agent can't mock.
- Security scanning that requires human review before merge.

Any of these will create a "gate failure that's not actually a code problem" situation. Let the agent experience those failures as feedback in lessons, not as CI red lights.

## The Honest Assessment

Your CI/CD is doing its job. It's:

- Reliable (zero false positives/negatives in 30 lessons).
- Fast (complete in ~200s on average).
- Trustworthy (gates are holding; merges are safe).

The bottleneck is not CI. It's the worker's wall-clock budget and the synthesis phase's ticket complexity estimation.

Next block: focus on infrastructure improvements (soft-threshold escalation, model routing) above the CI layer. CI doesn't need changes; the scheduling layer does.

## Monitoring Checklist for Next Block

- [ ] Add model usage tracking (which model for each lesson).
- [ ] Add outcome segmentation (pass, fail, timeout, escalate as distinct states).
- [ ] Add per-ticket duration tracking (lessons should record `duration_seconds`).
- [ ] Add CI component health dashboard (ruff, pytest, merge job availability).
- [ ] Track first-pass merge rate (should trend >80%).
- [ ] Track timeout rate by model (should trend <10% overall).

These give you the data you need to make smarter routing and capacity decisions in block 41361 and beyond.
