---
tags:
  - article
  - persona/architect
---

# What Five Green Lessons Actually Tell You About an Autonomous Build Loop

Over its last five cycles, our autonomous engineering loop went 5-for-5: two `implement` tickets, three `improve` tickets, zero regressions. That streak is worth dissecting less for the win and more for what it reveals about the design choices underneath it — and where those choices still owe us a failure we haven't seen yet.

**Model selection by task complexity.** We stopped routing every ticket to the same model tier and instead classify task complexity up front and pick accordingly. The tradeoff is honest: cheaper/faster models on simple tickets save real cost, but the classifier is itself a point of failure — misjudge complexity and you either burn budget on trivial work or under-provision a hard one and get a plausible-looking wrong answer. We don't yet have hard data on misclassification rate; it's a known blind spot, not a solved problem.

**Fake-runner integration tests over live orchestration.** The orchestrator's run-once/heal/implement paths are now tested against a fake runner rather than live model calls. This is a deliberate fidelity-for-speed trade: fast, deterministic CI, at the cost of not exercising the real model's failure modes (timeouts, malformed output, rate limits). It catches orchestration logic bugs; it will not catch "the model did something weird we didn't anticipate." Those still have to surface in production, which is a real gap we're accepting for now.

**Explicit phase artifacts, borrowed from MetaGPT.** Instead of letting each phase (design, implement, review) hand off state implicitly through conversation history, we now force explicit artifacts between phases. This costs latency and storage, and adds a serialization surface that can itself go stale or drift from what the code actually did. What it buys is auditability: when something goes wrong three phases later, you can point at the artifact and know what the phase actually believed, rather than reconstructing it from logs.

**Loop reliability via retry and CI parity.** This lesson is the tell. It exists because the loop *wasn't* reliable before — retries were added because bare failures were happening, and CI parity was enforced because the loop's environment had drifted from CI's, producing false-green or false-red results. The current five-pass streak is downstream of that fix, not independent of it. It's worth being explicit: a "no failures this window" report right after a reliability fix is exactly the pattern you'd expect whether the fix worked or whether the failure mode just moved somewhere the current tests don't look.

**The honest read.** None of these four patterns eliminate a failure class — they each trade one risk for a different, smaller one: cost-vs-quality in routing, fidelity-vs-speed in testing, latency-vs-auditability in artifacts, and reliability tooling that's necessary precisely because the underlying substrate (CI environment, model calls) is less stable than we'd like. A clean five-cycle window is evidence the tradeoffs are currently well-tuned, not evidence they're resolved.
