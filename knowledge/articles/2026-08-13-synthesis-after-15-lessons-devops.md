---
tags:
  - article
  - persona/devops
---

# Five Green Runs: What the Automation Loop Actually Taught Us

We just closed a five-lesson window in our autonomous build loop: 5/5 passed, zero regressions, mixed `implement` and `improve` work. No failures to report this cycle — which is itself worth being suspicious of, so here's what actually happened under the hood rather than just the scoreboard.

## What ran

Two new features shipped and three existing subsystems got hardened: a task-complexity heuristic for model selection, a fake-runner integration test harness covering the orchestrator's run/heal/implement paths, explicit phase artifacts modeled after MetaGPT's handoff pattern, a refreshed reference-set snapshot, and retry/CI-parity work on the loop itself.

## The mechanics that mattered

**Retry and CI parity was the load-bearing change.** Before this window, local loop runs and CI runs could disagree on pass/fail for the same commit — the classic "works on my machine" problem, except "my machine" was another automated agent. Closing that gap meant the loop's retry logic now mirrors CI's exact invocation, not an approximation of it. That's the fix that makes the rest of the green streak trustworthy instead of lucky.

**Fake-runner integration tests were added specifically because the orchestrator's `run`, `heal`, and `implement` paths had no coverage that didn't require live infra.** That's a real gap being closed, not a nice-to-have — without it, a broken heal path could sail through review looking healthy.

**Explicit phase artifacts (MetaGPT-style)** replaced implicit hand-offs between build phases with recorded, inspectable state. Operationally this matters more than it sounds: implicit hand-offs are exactly where silent drift creeps in when agents are running unattended.

## The honest part: no failures isn't the same as no risk

Zero failures across five lessons is a small sample, and it followed directly on the heels of the CI-parity fix — which means some of what would have failed *before* is now caught earlier or not attempted the same way. We should not read "5/0" as "the loop is solved." The recurring themes extracted from this window are almost suspiciously uniform: **build**, **change**, **cleanly**, **green**, **merged** — each appearing in 3 of 5 lessons. That's a signal the lesson-extraction is converging on process vocabulary ("it built cleanly and merged green") rather than surfacing anything new. If the next few windows keep producing the same five words, the summarizer itself needs attention, not the pipeline.

## Operational takeaways

- **Don't trust a local pass until it matches CI's exact path.** The retry/CI-parity fix should have been done earlier; it was a source of false confidence for longer than it should have been.
- **Coverage gaps in orchestration logic (heal/implement paths) are the ones that bite hardest in unattended loops**, because there's no human in the moment to notice the orchestrator did the wrong thing gracefully.
- **A synthesis pass that only reports pass/fail counts is undersized.** The recurring-theme extraction is currently low-signal; worth revisiting the keyword logic before drawing conclusions from future windows.

Next window's job: watch whether the CI-parity fix holds under a failure, not just under five successes.
