---
tags:
  - article
  - persona/devops
---

# Five Green Runs: What a Zero-Failure Window Actually Tells You

Our last five pipeline runs (2 `implement`, 3 `improve`) all passed. Before treating that as a trophy, here's what the mechanics behind those five runs looked like — and where the real risk still sits.

## What shipped

**Model selection by task complexity.** The orchestrator now routes tickets to different model tiers based on estimated task complexity instead of a fixed model for every job. Mechanically this is a router-before-runner step: classify, then dispatch. The win is cost and latency on trivial tickets; the risk is misclassification silently degrading output quality on a ticket that *looked* simple but wasn't. We don't yet have a feedback loop that flags "cheap model got it wrong" back into the classifier — that's the gap.

**Fake-runner integration tests for the orchestrator.** We added integration tests that fake the runner for the `run-once`, `heal`, and `implement` code paths. This is the single most valuable change in the window: it means orchestration logic (retries, state transitions, path selection) gets exercised in CI without spinning up a full agent run. Before this, orchestrator bugs only surfaced in production runs — expensive to detect and expensive to debug.

**Reference-set snapshot refresh.** A recurring chore that keeps the "known good" reference snapshot current and, per this run, extracted one reusable practice out of it while doing so. Small, but it's the kind of housekeeping that prevents snapshot drift from silently invalidating regression comparisons later.

**Loop reliability: retry and CI parity.** This is the one to watch. "CI parity" means the retry behavior in the automation loop now matches what CI itself does on failure — previously these two retry policies could diverge, so a run could succeed locally/in-loop under conditions CI would have rejected. Closing that gap is a real reliability fix, but it only matters because the divergence existed in the first place and had presumably caused false-green results before this window's records begin.

## The honest part

The ledger for this window reports 5/5 pass, zero failures, and no recurring failure themes. Take that with a grain of salt, not as vindication: five runs is a small sample, and "green throughout" is exactly the condition under which regressions in the *retry/CI-parity* logic itself would be hardest to catch — a false pass looks identical to a true pass until something downstream breaks. The fake-runner integration tests are the real hedge against that, since they test orchestrator behavior directly rather than trusting run outcomes as a proxy.

## Takeaway for CI/CD design

The theme across all three substantive changes — model routing, fake-runner tests, retry/CI parity — is the same: push validation earlier and make the loop's own reliability testable, rather than inferring pipeline health from a streak of green runs.
