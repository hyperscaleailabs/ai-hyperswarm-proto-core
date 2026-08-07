---
tags:
  - article
  - persona/architect
---

# Five Lessons In: What Actually Held Up in the Autonomous Build Loop

Over five recent iterations of our self-directed build loop — two `implement` runs, three `improve` runs — every cycle landed green: five passes, zero failures, all merged cleanly. That streak is worth being suspicious of before it's worth celebrating. A short, unbroken run is exactly the kind of sample where survivorship bias hides the real failure modes, so the more useful synthesis is in *what changed under the hood* to get there, not the scoreboard.

**Model selection by task complexity.** We moved from a fixed model tier to routing tasks by estimated complexity — cheap models for mechanical scaffolding, stronger reasoning for design and heal work. This is the standard cost/quality lever, and it paid off on cost, but it introduces a classification problem we haven't hardened: complexity estimation is itself a judgment call, and a misclassified task now silently gets the wrong horsepower instead of failing loudly. We're accepting that risk for now because the alternative — always using the top-tier model — was the actual prior failure mode driving cost overruns.

**Fake-runner integration tests for the orchestrator.** Rather than running real heal/implement/run-once cycles in CI (slow, flaky, expensive), we built a fake runner that exercises orchestrator control flow without invoking real agents. This is a deliberate trade of fidelity for speed and determinism. The honest cost: a fake runner can drift from the real runner's contract without anyone noticing, and it cannot catch failures that only manifest under real agent nondeterminism. We're treating it as a fast first gate, not a substitute for periodic real-path verification — that separation didn't exist before and was a gap.

**Reference-set snapshots as a forcing function.** Refreshing the reference-set snapshot and extracting one practice per cycle turned "learn from what happened" into a scheduled, bounded task instead of an aspirational one. The tradeoff is scope discipline: extracting exactly one practice per cycle is a throttle against over-fitting the loop's rules to a single incident, but it also means slow-to-emerge patterns can take many cycles to surface.

**Explicit phase artifacts, MetaGPT-style.** Borrowing MetaGPT's pattern of materializing each SDLC phase as a concrete, inspectable artifact (rather than passing implicit state between agents) made the loop's failures — when they happen — attributable to a specific phase instead of a diffuse "something went wrong." This is the single change most directly aimed at debuggability over raw throughput, and it costs extra I/O and coordination overhead per cycle.

**Retry and CI parity for loop reliability.** This lesson is the tell: it exists because earlier cycles *weren't* reliable — CI and local runs disagreed, and transient failures weren't retried, so real bugs and flakiness were indistinguishable. Fixing that is why the last five cycles read as clean; it's a repair, not a bonus feature.

The honest read: the zero-failure streak reflects a loop that got better at not failing *silently*, not a loop that stopped failing. The next test is whether it holds past five cycles.
