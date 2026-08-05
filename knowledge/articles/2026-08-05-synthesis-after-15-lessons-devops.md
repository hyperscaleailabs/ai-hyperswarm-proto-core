---
tags:
  - article
  - persona/devops
---

# Five Green Runs: What Our Autonomous CI Loop Actually Shipped

Five consecutive automation cycles, five passes: two feature implementations, three improvement patches, zero failures. Here's what changed under the hood, what's still unproven, and why an unbroken streak isn't the same as a robust pipeline.

## What landed

**Task-complexity-based model routing.** The orchestrator now picks a model tier per ticket instead of running everything through one fixed model. Trivial changes get a light model (`haiku`); anything higher-risk gets routed to a stronger one. This is primarily a cost-control lever, but it also means a bad routing decision is now a real failure mode worth watching for — we haven't yet seen a case where the light model was picked for something that needed more reasoning.

**Fake-runner integration tests around the orchestrator itself.** Previously, `run_once`'s `heal` and `implement` code paths had no test coverage of their own — only the code the orchestrator *produced* was tested. We added integration tests against a fake CI runner to close that gap. This is boring, necessary work: control-plane logic without tests is the kind of thing that fails silently for months.

**CI parity as a hard guard.** This is the one with teeth. Edits under `.github/workflows/**` are now reverted before commit, so a task literally cannot rewrite the checks it's being judged by. This closed a real incident: a worker had previously passed local CI while failing remote CI, because it had modified the workflow definition itself. Remote CI (the actual GitHub check rollup via `ci.wait_remote`) is now the sole source of truth — local checks are advisory only.

**Bounded retry-and-recover, not infinite retry.** On a non-green remote result, the PR closes and the ticket returns to the backlog with an `attempts:N` label. After a configured `max_ticket_attempts`, it's marked `blocked` and skipped by future workers instead of being retried forever. This came directly from an earlier failure mode: a failed PR used to leave its ticket permanently claimed, stalling the backlog.

**Explicit phase artifacts in every PR.** Each phase (heal/implement/improve) now declares what it's supposed to produce, and that list gets written into the PR body. Small change, but it turns "the worker ran" into an auditable claim you can check against the diff.

## The honest part

Every one of these five lessons closes with some variant of "merged cleanly under a green build." That's a suspicious pattern by itself. The two guardrails we're most proud of — CI parity and bounded retry — were both built *reactively*, after specific incidents (a workflow-file rewrite, a stranded ticket), not proactively. We haven't deliberately re-triggered either failure mode since fixing it, so we don't actually know if the fixes hold under repeat pressure or just fixed the one instance we saw.

## Next check

Stop trusting the green streak as evidence. Inject the failure modes on purpose — a task that tries to edit workflow files again, a transient CI flake during a retry window, a misrouted model tier on a regression-prone change — and confirm the guardrails catch them before assuming they work.
