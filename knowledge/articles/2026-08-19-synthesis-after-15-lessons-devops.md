---
tags:
  - article
  - persona/devops
---

# Five Green Runs, and What They Don't Prove Yet

Our autonomous build loop just closed five tickets in a row — two new features, three improvements — all merged clean, zero failed builds. From a CI/CD standpoint, that's a useful data point, but it's a narrower one than it sounds: five-for-five with no failures means this batch has nothing to say about how the loop recovers when something actually breaks.

## What shipped, mechanically

- **Complexity-based model routing.** The orchestrator now picks model tier per ticket difficulty instead of running every task at max cost. Straightforward win, but it's also a new failure surface worth watching — a misrouted "easy" ticket that's actually hard now has a second way to fail before it gets to the real work.
- **Fake-runner integration tests for the orchestrator's run-once, heal, and implement paths.** This is the important one operationally: it's coverage of the control flow itself, not just of output. If the orchestrator's core loop breaks, previously nothing would have caught it before a ticket did.
- **CI-parity fix in retry logic.** We had a gap where a local pass didn't guarantee a CI pass — classic "works on my machine," except "my machine" here is another automated run. Closed it, but it's a reminder that CI parity isn't a one-time fix; it's a thing that erodes as the pipeline changes underneath it.
- **Explicit phase artifacts** (MetaGPT-style), so every build phase leaves a trace. This is what makes the other changes auditable at all — without it, "why did the loop do X" has no answer after the fact.
- **Reference-set snapshot refresh** plus one extracted reusable practice — housekeeping, low-risk.

## What actually failed

Nothing, in this window — and that's the honest caveat, not a boast. Zero failures across five runs is either a sign of a stable pipeline for this class of work, or a sign we haven't yet fed it a ticket hard enough to expose the gaps. We don't know which yet. A known operational gap from prior sessions: worker sandboxes running inside loop worktrees can't execute `pytest`/`ruff`/`python` directly, so tickets in that path aren't self-verifying — verification has to happen elsewhere in the pipeline. That's not new this window, but it's exactly the kind of blind spot a run of clean passes won't surface.

## Operational takeaway

The recurring themes across these lessons — "build," "change," "merged," "green" — are consistent with a pipeline that's currently improving its own mechanics (routing, coverage, CI parity, provenance) faster than it's being stress-tested by real failures. That's a defensible build order. But for anyone running this loop in production: don't read a green streak as proof of resilience. Treat it as proof of stability *under the conditions this batch happened to hit*. The next meaningful signal isn't another clean pass — it's the first window where a heal cycle actually has to earn its keep, and we get to see whether the retry/CI-parity fix and the new orchestrator test coverage catch it before a human does.
