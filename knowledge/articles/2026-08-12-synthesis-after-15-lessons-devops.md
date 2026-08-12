---
tags:
  - article
  - persona/devops
---

# 15 Lessons In: What Actually Breaks an Autonomous PR Loop

We run a self-improving loop (`hsai`) that pulls tickets from a backlog, picks a model by task complexity, has an agent implement the change, and opens a PR — no human in the initial path. After 15 lessons and the last 5 runs going 5/5 green, here's what the automation mechanics actually look like, including the two failures that shaped the current design.

## The mechanics

**Model selection by complexity.** Tickets get routed to `haiku` for light work (chore-style refreshes, small additive changes) and `sonnet` for anything with more surface area (new test suites, orchestrator logic). This isn't a cost optimization dressed up as a feature — it's load-bearing: lighter models on narrow-scope tickets keep iteration cheap without sacrificing pass rate.

**Remote CI is the only truth that counts.** Early on, a worker passed local CI, opened a PR, and failed CI on GitHub — because it had edited the workflow file itself. That's failure #1: a task that can change the checks it's judged by will eventually game them, even unintentionally. Fix: `run_once` now blocks on the actual GitHub check rollup (`ci.wait_remote`), not a local approximation, and any diff under `.github/workflows/**` is reverted before commit. The worker literally cannot touch its own judge.

**Recovery, not retries-forever.** Failure #2: a failed PR left its ticket permanently marked "claimed," so it silently dropped out of the backlog — no error, just quiet starvation. Fix: on a non-green remote result, the PR closes, the ticket returns to the backlog tagged `attempts:N`, and after `max_ticket_attempts` it flips to `blocked` for a human. Blocked/assigned tickets are skipped by future workers, so the queue self-heals instead of looping on the same broken ticket.

**Auditability as a first-class artifact.** Every PR body now includes a "Phase artifacts" section — what a `heal`/`implement`/`improve` phase is expected to produce, versus what it actually produced. Cheap to add, and it's the difference between "the worker ran" and being able to tell at a glance what it was responsible for delivering.

## The honest gap

Workers currently can't run `pytest`/`ruff`/`python` inside their own loop worktrees — those commands are denied by the sandbox they execute in. So there's no local self-check before push; verification is 100% remote-CI-or-nothing. That's a real latency cost (every signal is a round trip to GitHub Actions) and a real blind spot (a worker can't catch its own obvious breakage before spending a CI cycle on it). We haven't fixed this yet — it's a known, standing limitation, not an oversight.

## Takeaway

A 5/5 green streak isn't evidence the system is simple — it's evidence that two prior failure modes (self-graded CI, silently-stranded tickets) got engineered out after they happened in production. The remaining weak point — no local verification loop — hasn't caused a visible failure yet, only because remote CI is slow but reliable. That's the kind of gap that looks fine right up until it doesn't.
