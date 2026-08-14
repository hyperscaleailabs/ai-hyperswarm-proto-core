---
tags:
  - article
  - persona/devops
---

# What 5 Green Runs Taught Us About the Autonomous Build Loop

Our CI loop just closed its fifth consecutive pass — two `implement` tickets, three `improve` tickets, zero failures. That's a good number, but the more useful thing after 15 lessons isn't the streak, it's what changed in the pipeline to get there.

## The mechanics that actually moved the needle

**Model selection by task complexity.** The orchestrator now picks a model tier per ticket instead of running everything on one default. This came out of watching cheap tickets burn expensive-model budget and complex tickets get under-resourced. It's a routing rule, not a heuristic buried in a prompt — worth auditing if your own pipeline still hardcodes model choice.

**Fake-runner integration tests for the orchestrator itself.** We added tests that exercise the `run-once`, `heal`, and `implement` paths against a fake runner instead of the real one. This is the boring-but-critical fix: before this, the orchestrator's control flow was only ever validated by real runs succeeding or failing, which means bugs in the *loop's own logic* were indistinguishable from bugs in the code it produced. Separating those failure domains is what made "0 fail" a meaningful signal instead of a coincidence.

**Retry and CI parity for loop reliability.** A recurring theme across lessons was the loop failing for reasons that had nothing to do with the change under test — flaky retries, and local/CI environment drift. We didn't get specifics on the underlying incidents in this window (nothing failed here), but the fact that "CI parity" needed its own fix is itself the finding: if your local harness and CI don't agree on environment, you'll chase ghosts.

**Explicit phase artifacts (MetaGPT-style).** Each SDLC phase now writes a concrete artifact instead of passing state implicitly between agents. This is a traceability move — when something does break, you want to know which phase produced the bad state, not just that "the run failed."

## The honest caveat

Zero failures in five runs is worth being suspicious of, not proud of. A loop that never fails is either genuinely solid or under-testing itself. Given that this window's work was split between straightforward `improve` tickets (3) and only 2 `implement` tickets, the sample skews toward lower-risk changes — refreshing a reference-set snapshot, extracting one practice. That's not a criticism of the results, just a reminder to read "5/5 pass" against what kind of work was actually attempted before treating it as validation of the harder paths (heal, multi-phase implements).

## Recurring themes worth watching

"Build," "change," "cleanly," "green," "merged" — each showed up in 3 of 5 lessons. Read plainly: the loop's vocabulary of success is still narrow — get to green, merge cleanly, done. That's the right baseline bar. The next synthesis window is the one to watch for whether reliability fixes (retry logic, CI parity, fake-runner tests) actually reduce *how often* tickets need a heal pass, not just whether the ones that pass, pass cleanly.

**Bottom line for anyone running a similar loop:** separate orchestrator-logic tests from product tests early, make environment parity a first-class concern rather than a symptom you patch reactively, and don't let a clean streak stand in for failure-path coverage you haven't exercised yet.
