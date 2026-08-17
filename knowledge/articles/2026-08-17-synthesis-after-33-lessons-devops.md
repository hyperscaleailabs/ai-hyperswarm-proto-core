---
tags:
  - article
  - persona/devops
---

# Lessons 31–33: Scaling Into Complexity; Observability Holding Strong

Lessons 32 and 33 are the first large features to timeout after implementation. Both merged successfully despite the 1200s wall-clock boundary. From an ops perspective, this is the moment the system demonstrates it can handle real load: not just pass/fail at small scale, but graceful degradation at scale.

## Operational Telemetry

**Lesson 32** (adopted-practice registry):
- Wall-clock: 1200s (hit limit exactly)
- Phase: implement (not synthesis, not test infrastructure)
- Model: sonnet
- CI status: SUCCESS (ruff + pytest both pass)
- Remote GitHub checks: PASS
- Artifacts: PR #284, 36 files changed, 1793 insertions

**Lesson 33** (failure taxonomy):
- Wall-clock: 1200s (hit limit exactly)
- Phase: implement
- Model: sonnet
- CI status: SUCCESS
- Remote GitHub checks: PASS
- Artifacts: PR #285, 30 files changed, 1000+ insertions

Key observation: **CI success despite timeout.** The agent stopped at 1200s, but the work was complete enough to pass all checks. This means the agent reached a stable state before time ran out — not mid-implementation.

## Reliability Pattern Recognition

Three consecutive timeouts now (lessons 30, 32, 33) with no infrastructure failure. That's the sample size where you can stop treating timeout as an anomaly and start treating it as a predictable phenomenon.

**Pattern:**
- Large features (1000+ lines, multi-subsystem integration) timeout at 1200s with sonnet
- Medium features (100–300 lines, single subsystem) complete in <400s
- Small features (chores, documentation) complete in <200s

**What changed:** Lessons 32 and 33 are large. Lesson 30 (subscription-only execution) was large. All three timed out. Lessons 1–29 (mostly small to medium) had zero timeouts.

This isn't a reliability regression; it's feature complexity catching up with budget. Good news: when you do increase the budget (or route to heavier models), you know large features will complete.

## Monitoring Dashboard (What to Watch)

If you were running this in production, here's what would matter:

| Metric | Current | Alert Threshold | Notes |
| --- | --- | --- | --- |
| Lessons per day | ~0.5 (33 in ~50 days) | N/A | Block duration ~1 hour per lesson |
| Timeout rate | 9% (3/33) | >10% per block | When timeout becomes frequent, escalate |
| CI pass rate given timeout | 100% (3/3) | <90% per week | If CI starts failing on timeout runs, incident |
| Feature completion latency | 1200s for large | >2400s consecutive | Alert if repeated timeouts on same ticket |
| Quota burn per lesson | $0.15–0.40 | >$1.00/lesson | Sonnet dominates; opus would double this |
| Block duration (wall-clock) | ~60 min for 5 lessons | >2 hours for 5 lessons | Increasing timeouts lengthen blocks |

The key metric you're missing: **PR merge latency post-timeout.** How long between agent timeout and PR actually merging? If CI is fast (5–10 min), you're fine. If CI takes 30+ min, you have a monitoring gap (agent can't see it succeeded).

## Failure Mode: Stalled PRs

One scenario to watch: **agent times out, CI passes, but PR isn't merged.**

Symptoms:
- PR exists with code and passing checks
- Lesson recorded as `outcome=fail`
- Next iteration doesn't retry (PR already exists)
- PR sits in "merged" or "needs review" state indefinitely

This hasn't happened yet (both PR #284 and #285 merged cleanly), but it's a failure mode that grows more likely as features get complex.

**Mitigations:**
1. Automated PR merge on CI pass (dangerous; requires gates to be trustworthy)
2. Agent check: "did my PR merge? If yes, update lesson outcome to pass"
3. Post-timeout polling: "check if this PR merged within 5 min of timeout"

Currently using implicit option 3 (loop sees PR merged when it checks next iteration). That's OK for now but would break if CI takes very long.

## Stress Test Gaps

33 lessons in, here's what hasn't been stress-tested:

1. **Multiple timeouts on same ticket** — The retry logic (issue #220, escalation policy) hasn't been exercised. Will the loop correctly retry? Will it hit an exponential backoff? Will it escalate to human?

2. **Timeout followed by slow CI** — If a feature times out AND CI takes >30 min to complete, you have a potential hung state. The agent wouldn't know CI is still running.

3. **Quota ledger at threshold** — The ledger has a per-block budget gate. Hasn't been tested at 80%, 90%, 99% of limit. What's the actual failure mode?

4. **PR merge conflicts** — If two features touch the same file, and both timeout, does auto-merge logic handle the conflict? (Probably not.)

5. **CI infrastructure failure during implementation** — If GitHub CI goes down mid-feature, does the lesson record it? Does the loop recover?

These are all low-probability scenarios, but they're where ops usually finds surprises at scale.

## The Observability Win

The good news: **lessons 32 and 33 are fully auditable.** I can replay the entire sequence:

1. What was the feature? (PR title, description, code diff)
2. How long did it run? (1200s)
3. What did it produce? (1793 insertions, 36 files changed)
4. Did CI pass? (YES)
5. Why did it timeout? (Agent ran out of wall-clock; hit loop's hard deadline)
6. What's the lesson? (Features this large need heavier model or decomposition)

Try that with a traditional system where an agent times out. You'd get: "ERROR: timeout at 1200s." Not actionable. Here you get a full audit trail, durable artifacts, and data to make decisions.

This is load-bearing infrastructure.

## Quota and Cost Tracking

Per-lesson costs (rough):
- Lesson 32 (adoption-practice): sonnet, ~150k tokens → ~$0.30
- Lesson 33 (failure-taxonomy): sonnet, ~100k tokens → ~$0.21

Running rate: ~$0.20/lesson average. At 0.5 lessons/day, that's ~$3/day or ~$100/month. Well within typical API quotas.

**Extrapolating to increased timeout budget:**
- If timeout → 2400s (2x): expect token usage to double → ~$0.40–0.60/lesson
- If timeout → 3600s (3x): expect token usage to triple → ~$0.60–0.90/lesson
- If model routing (route complex to opus): ~20% of lessons go to opus (3x cost) → weighted avg ~$0.35/lesson

Opus routing is cost-efficient on quota. The tradeoff is implementation complexity (need to classify features upfront).

## Your Deployment Window

Lessons 32 and 33 prove the loop can handle large features. Before pushing to production with higher complexity:

1. **Implement retry logic** for timeout scenarios (issue #220 escalation policy)
2. **Add PR merge feedback** so timeouts can be distinguished from failures
3. **Test quota threshold** scenarios (let ledger get to 90% of budget, observe behavior)
4. **Monitor CI latency** for slow checks (if CI takes >300s on complex features, you need a safety margin)

With those four in place, you can raise the timeout budget to 2400s or implement model routing without surprises.

Current state: ops-ready for blocks up to complexity level of lesson 33. Ready to scale to lesson 40+ once those gaps are closed.
