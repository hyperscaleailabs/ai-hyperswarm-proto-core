---
tags:
  - article
  - persona/devops
---

# Five Green Runs: What a Quiet Week in the Pipeline Actually Taught Us

Our last five change cycles all passed — two builds, three improvements, zero failed merges. That's a good headline, but a clean scoreboard is only useful if you look at *why* it stayed clean, and what almost didn't.

## The pattern behind "green"

Three words show up again and again across this batch: **build**, **change**, **merged**, **cleanly**, **green**. That's not an accident — it's the shape of a pipeline that's been tuned to fail loudly *before* merge, not after. The lessons in this window weren't about writing new features faster; they were about tightening the feedback loop so a red run never makes it to `main`.

## What actually moved the needle

**Retry logic + CI/local parity.** One of the core fixes this cycle was closing the gap between what passes on a dev machine and what the CI runner sees. That gap is where a lot of "works on my machine" merges quietly rot — a test that's order-dependent, a timeout tuned for local hardware, a race condition CI's slower runners expose and a laptop never does. We added retry semantics specifically to distinguish *real* failures from *environmental* flakiness, rather than papering over both the same way. That distinction matters: blanket retries hide real bugs, no retries at all makes CI unusable during infra hiccups.

**A fake runner for integration tests.** Instead of hitting the real orchestrator (run-once / heal / implement paths) in every CI pass, we built a fake runner double. This is the classic tradeoff: faster, more deterministic CI runs, at the cost of a fidelity gap between "the fake agrees" and "the real orchestrator agrees." We accepted that risk deliberately, not by default — worth flagging for anyone extending it, because fake-runner drift is exactly the kind of thing that stays invisible until a real integration breaks in a way the fake never modeled.

**Model selection by task complexity.** Routing cheaper/faster models to low-complexity tasks and reserving heavier models for genuinely hard ones. Operationally this is a cost and latency lever, but it's also a reliability one — smaller models fail differently (and sometimes more predictably) than larger ones, so this needs its own failure-mode tracking, not just a cost dashboard.

**Reference-set snapshot refresh.** A recurring maintenance task, not a one-time fix — the reference/eval set needs periodic refresh or it silently drifts from what production actually looks like. This is the kind of task that's easy to let slide because skipping it doesn't fail anything *immediately*.

## The honest caveat

Five passes in a row is a good sign, not proof of robustness. The fact that we needed retry logic and CI-parity work at all means earlier runs *weren't* this clean — flaky and environment-mismatched failures were common enough to justify the fix. The absence of failures in this window is downstream of that earlier pain, not evidence it can't recur.
