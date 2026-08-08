---
tags:
  - article
  - persona/architect
---

# Five Green Lessons: What a Stable Window Actually Cost

Our last five work items — two feature builds, three improvements — all landed clean: no rollbacks, no reverted PRs, no reopened tickets. That's worth pausing on, not because "all green" is the goal, but because in an agentic build loop, a green streak is usually evidence that you've paid down risk somewhere upstream. Here's where we paid it, and where we're still exposed.

**Model selection by task complexity.** We stopped routing every task through the same model tier and instead classified tasks (implement vs. improve, scope size, blast radius) before picking a model. This is a classic cost/latency/quality tradeoff, but the real win was consistency: a fixed router removes a whole class of "why did this trivial change take 10 minutes" complaints. The failure mode we're watching for — not yet observed, but structurally possible — is misclassification silently downgrading a task that *looks* simple but touches shared state.

**Fake-runner integration tests for the orchestrator.** We built a fake runner to exercise the run-once, heal, and implement paths without invoking real subagents. This is the standard test-double tradeoff: fast, deterministic, cheap to run in CI — at the cost of fidelity. A fake runner can't tell you the real runner has drifted. We accepted that gap deliberately; it's mitigated only by keeping the fake's contract narrow and re-verifying it against the real runner periodically, which we haven't automated yet.

**Reference-set snapshot refresh.** We refreshed the golden reference set and extracted one reusable practice from it. Unglamorous, but this is where staleness bugs actually come from — a reference set nobody revisits quietly becomes a lie the whole pipeline trusts. The honest tradeoff: snapshot refreshes are manual triggers right now, not scheduled, so there's an unbounded window where the reference set can drift before anyone notices.

**Explicit phase artifacts, MetaGPT-style.** We adopted explicit, inspectable artifacts between phases (design → implement → review) instead of passing implicit state through agent context. This cost us structure overhead — more files, more schema to maintain — in exchange for something we needed more: the ability to resume, audit, or replay a run from any phase boundary without re-deriving upstream reasoning. This is the single change most responsible for the clean streak; when something did go sideways mid-run (not in this window, but historically), implicit state was almost always why.

**Retry and CI parity for loop reliability.** This one is a tell: you don't build retry logic and CI-parity checks unless flakiness already burned you. The lesson here isn't the retry mechanism itself, it's that we finally treated "runs locally, fails in CI" as a bug class worth a systematic fix rather than a one-off patch each time it recurred.

**The honest read:** zero failures in five lessons is a good window, not a proof of robustness. The real signal is that four of five items were *hardening* work — tests, snapshots, artifacts, retries — not new capability. That ratio is healthy. If it flips the other direction for a few windows running, that's the number to watch, not the pass rate.
