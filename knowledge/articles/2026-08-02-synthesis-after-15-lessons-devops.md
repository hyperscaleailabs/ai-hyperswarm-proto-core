---
tags:
  - article
  - persona/devops
---

# Five Green Runs: What Our Autonomous Build Loop Actually Did

Our CI/CD loop just closed out five consecutive lesson cycles — two `implement`, three `improve` — with a 5/0 pass/fail record. That's a genuinely clean streak, not a cherry-picked one. But a zero-failure window is worth being suspicious of, not proud of, so here's what actually happened underneath the green checkmarks.

## The mechanics that shipped

**Model selection by task complexity.** The skill/task router now picks model tier based on estimated task complexity rather than defaulting to the biggest model for everything. This is a cost/latency lever, not a quality one — the risk is misclassifying a task as "simple" and getting a shallow fix that passes CI but misses the actual bug. We haven't yet stress-tested the classifier against adversarial or ambiguous tickets.

**Fake-runner integration tests for the orchestrator.** We added integration tests that exercise the `run-once`, `heal`, and `implement` paths against a fake runner instead of mocking at the unit level. This closed a real gap: previously, orchestrator wiring could break in ways unit tests wouldn't catch, and the only place it would surface was a live run. The honest caveat — a fake runner is still a simulation. It validates orchestration logic, not real runner failure modes (timeouts, partial output, flaky exits).

**Loop reliability: retry and CI parity.** This is the one I'd flag as the most operationally important. The loop now retries transient failures instead of halting on the first flake, and the retry path was aligned to match what CI actually does — previously local retry semantics and CI retry semantics had drifted apart, which meant "works locally" and "works in CI" weren't reliably the same claim. That drift is a common and underrated failure class: it doesn't show up as a bug, it shows up as wasted debugging time chasing environment-specific ghosts.

**Explicit phase artifacts (MetaGPT-style).** Each phase of a run now writes out its intermediate artifacts explicitly rather than passing state implicitly between steps. This is mostly an observability win — when something does eventually fail, you can inspect what each phase actually produced instead of reconstructing it from logs.

**Reference-set snapshot refresh.** A recurring chore: snapshotting the reference set and extracting one reusable practice from it per cycle. Mechanically boring, but it's the kind of housekeeping that prevents reference drift from silently degrading downstream comparisons.

## The honest part

This window recorded zero failures. That's either a sign the retry/CI-parity fix is working, or a sign we haven't hit the case that breaks it yet — five cycles is not enough data to tell those apart. The recurring themes ("build," "change," "cleanly," "merged") are encouraging but generic; they don't tell us what *kind* of change was hardest. The next useful synthesis isn't "we're green," it's tracking near-misses — retries that succeeded on the second attempt — since those are failures the current dashboard can't see.
