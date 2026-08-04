---
tags:
  - article
  - persona/cto
---

# Three Implement, Two Improve: The Harness Got Smarter and Kept the Throughput

Five PRs merged, zero failures, block 41337. The mix is three substantive features, two targeted improves. The business-relevant headlines: synthesis now catches its own duplicates, workers see precedent before starting, and we're closing the feedback loop on quota spend. All of that compounds into a system that makes better decisions faster.

## The numbers and the reality

**Three feature PRs hit main.** Practice registry dedupe, protected-invariants CI gate, lesson-retrieval in worker prompts. These are the kind of changes that don't move customer metrics in one week but prevent a category of mistake entirely. The practice registry alone saves us the cost of re-exploring the same improvement idea multiple times; the gate prevents regressions that tests miss; the retrieval cuts debugging time by letting workers reference "we tried this before."

**Two improve PRs refined the path.** Evidence ledger scaffolding (infrastructure, no enforcement yet) and the quota-ledger-to-model-selection feedback loop (now live, biasing selector behavior). The first is cost with deferred payoff — we're building reporting infrastructure that will matter more as the corpus of PRs grows. The second is immediate impact: the selector is already avoiding expensive-model-on-trivial-ticket cases.

**Still zero failures.** That's now two consecutive five-PR windows with zero failures. The question is whether that's a sign the system is robust or a sign we're avoiding the hard cases. Honest take: the work this window was exactly the kind suited for high success rates (straightforward features, clear acceptance criteria, reference-set precedent for most of it). We haven't stress-tested the loop on a migration, an urgent security patch, or a dependency upgrade. Don't mistake "green streak" for "production-ready under load."

## Risk and investment posture

The loop is investing in its own intelligence — synthesis that doesn't repeat itself, workers that know history, quota decisions informed by data. These are all "infrastructure for being better," not "customer-facing features." They're the right moves, but they take quota budget and development attention that could go toward other improvements.

**Cost this window:** Three heavy-model PRs (complex features), two standard-tier improves, two concurrent chores (synthesis, governance). The budget gate didn't trigger; we stayed comfortably under the soft breach threshold. That's good for velocity, but it also means we're not discovering the constraints that would force hard prioritization choices. Recommend deliberately scheduling higher-risk work next window so the gate gets real exercise.

**Quota leverage:** The feedback loop (model selection driven by ledger) is starting to show efficiency gains. Early lessons show this heuristic reducing expensive-tier PR count by ~15% on trivial tickets. With 20 PRs in the corpus, that's meaningful but not statistically solid yet. By the time we hit 50+ PRs, this should stabilize into a real cost lever.

## Strategic asks

1. **Don't over-index on the green streak.** Five-for-five is a baseline, not a target to protect by cherry-picking easy work.
2. **Schedule one deliberately risky ticket next window** — a migration, a dependency upgrade, something with real blast radius — so we get genuine data on how the loop handles failure recovery under new guardrails.
3. **Start public reporting on the business metrics** — it's not enough to know "we merged 5 PRs cleanly." We need to connect that to: customer impact, cost saved, time-to-feature measured in wall-clock days, not quota-hours.

The loop is getting visibly smarter. Don't let that become an excuse to stop pushing it toward harder cases.
