---
tags:
  - article
  - persona/architect
---

# Five Green Lessons, and What They Cost to Get There

Our last five build/improve cycles all landed clean — 5 pass, 0 fail. That's a good headline number, but for an architect audience the more useful signal is *what* had to change to keep it that way, and what we gave up to get there.

## What we adopted

**Task-complexity-based model selection.** Instead of a single model tier for every job in the pipeline, we now route by task complexity — cheap tasks get cheap inference, complex ones escalate. The tradeoff is a classifier in the critical path: get the complexity estimate wrong and you either overpay or under-provision reasoning depth on a task that needed it. We accepted that risk because the alternative — flat-rate expensive inference on everything — didn't scale with volume.

**Fake-runner integration tests around the orchestrator.** We added integration tests that exercise the `run-once`, `heal`, and `implement` paths against a fake runner rather than the real execution backend. This is a classic realism-vs-speed tradeoff: fake runners catch orchestration bugs (wrong state transitions, bad retry logic) fast and deterministically, but by construction they cannot catch bugs in the real runner's behavior. We're explicit internally that this suite is a contract test, not an end-to-end guarantee, and we still need real-runner smoke tests to close that gap — that's unfinished, not solved.

**Explicit phase artifacts, MetaGPT-style.** We made each SDLC phase emit a concrete artifact (design doc, plan, evidence) rather than relying on implicit state carried in agent memory. This adds ceremony and storage overhead to every run. We adopted it anyway because implicit state was the actual root cause of a prior class of silent failures — an agent "knew" something from three phases back that never got written down, and the next phase couldn't recover it after a restart. Explicit artifacts trade a bit of latency and disk for recoverability and auditability.

**Retry and CI-parity work on the loop.** This one is the honest tell in this batch: we don't add retry logic and chase CI parity because things were already reliable. The loop was flaky enough, and diverged from CI often enough, that it needed dedicated hardening. That work is now merged, but "reliability" here means "we found and closed specific gaps," not "there were never gaps."

## What actually failed, going in

None of the five *lessons* in this window failed — but every one of them exists because something upstream did: model costs were mis-shaped before routing existed, orchestrator bugs shipped before fake-runner coverage existed, state got silently lost before explicit artifacts existed, and the loop was unreliable before the retry/CI-parity work landed. A five-for-five window is a lagging indicator that the *previous* window's failures got fixed, not evidence that failure has stopped being possible.

## The honest gap

The fake-runner test suite is real risk we're carrying deliberately — real-backend coverage is still owed. If the next failure comes from there, it won't be a surprise.
