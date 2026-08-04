---
tags:
  - article
  - persona/devops
---

# Five Green Runs: What a Model-Selection, Retry, and Test-Fake Sprint Actually Taught Us

Five lessons landed this window — two `implement`, three `improve` — and all five passed. No failures to report, which is worth pausing on: a clean streak like this either means the loop is maturing or that we got lucky on scope. Here's what shipped and what it means operationally.

**Task-complexity-based model selection.** The orchestrator now routes tasks to models based on estimated complexity rather than a fixed default. Mechanically, this is a CI/CD win: cheaper models handle boilerplate steps, and heavier reasoning is reserved for tasks that need it. The catch is cost/quality tuning is inherently a moving target — thresholds set today will need revisiting as task mix shifts. Treat this as a dial, not a decision.

**Fake-runner integration tests for orchestrator paths.** We added integration tests using a fake runner to cover the `run-once`, `heal`, and `implement` code paths. This is the kind of test infrastructure that pays for itself the first time a real regression sneaks past unit tests — but it only works if the fake runner's behavior stays honest to the real one. Fake runners drift; budget time to periodically diff fake vs. real runner behavior, or this becomes a false-confidence machine.

**Reference-set snapshot refresh + one extracted practice.** Routine hygiene: refreshed a snapshot used as ground truth, and pulled one reusable practice out of it. Unglamorous but necessary — stale reference snapshots are a classic source of silent test drift, where everything stays green because the baseline itself rotted alongside the code.

**Explicit phase artifacts from MetaGPT.** Borrowed a pattern from MetaGPT to make intermediate phase outputs explicit artifacts rather than implicit state. Operationally this is a debuggability win: when a multi-phase pipeline fails, you want to inspect what each phase actually produced, not just the final output. This is the kind of change whose value shows up later, in an incident, not today.

**Loop reliability — retry and CI parity.** This is the one to watch. "CI parity" work implies that local and CI runs were behaving differently before this fix — a common and painful class of bug where a pipeline is green locally and flaky (or vice versa) in CI. Adding retry logic addresses symptoms of flakiness but can also mask root causes if not paired with actual flake diagnosis. Worth confirming this was root-caused and not just retried into submission.

**The honest caveat:** the recurring-themes list this window — "build," "change," "cleanly," "green," "merged" — is generic almost to the point of being noise. It tells us commits landed cleanly and CI stayed green, but it doesn't surface a sharp signal about *what kind* of work is recurring. If the next few windows produce the same shallow theme extraction, the lesson-mining step itself needs a second look — it may be summarizing the process ("merged cleanly") rather than the substance (what broke, what pattern kept appearing). A synthesis loop that only ever reports green is either working perfectly or not looking hard enough.
