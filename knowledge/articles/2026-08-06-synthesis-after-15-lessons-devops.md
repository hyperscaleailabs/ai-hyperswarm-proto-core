---
tags:
  - article
  - persona/devops
---

# Five Green Runs Don't Mean the Pipeline Is Solved

The last five tickets through our autonomous build loop all landed clean — two `implement`, three `improve`, zero failures. For a DevOps read, the interesting part isn't the streak; it's what the work items reveal about what used to break, and what the "all green" result is quietly not telling us.

## What actually shipped

**Retry and CI parity.** One lesson was explicitly about loop reliability — retries and making the CI environment match what the automation actually runs. That kind of ticket doesn't get written unless something *diverged* first: a step that passed locally (or in one runner) and failed in CI, or a transient failure that needed a retry policy instead of a hard stop. We don't have the incident log for this window, but the fix pattern is the tell — if you're adding retry logic and CI parity work to the backlog, you've already eaten the cost of flaky, non-reproducible pipeline runs.

**Fake-runner integration tests for the orchestrator.** A second lesson added integration tests using a fake runner to cover the orchestrator's run-once, heal, and implement code paths. Translation: those paths were previously exercised only by real runs, not by tests — meaning a regression in the orchestrator's control flow would only surface in production-like execution, not CI. That's a real gap that got closed, not a nice-to-have.

**Model selection by task complexity.** A skill was added to route tasks to models based on complexity rather than a fixed model for every job. Operationally this is a cost/latency lever, but it's also a new failure surface: misclassifying a task's complexity now means either overpaying or under-provisioning the model for the job. Worth watching whether that routing logic gets its own test coverage, because right now it's inference, not verified.

**Reference-set snapshot refresh.** A maintenance-style change to keep a reference/golden snapshot current. Low drama, but this is exactly the kind of task that silently rots — a stale snapshot means your "pass" signal stops meaning what you think it means.

## The honest caveat

Zero failures across five tickets in one window is a fine outcome, but it's a lagging indicator, not proof of health. A pipeline that only shows failures when something is *already* broken in prod is not the same as one that catches problems early — and two of these five items (fake-runner tests, CI parity) are literally patches for prior gaps in that early-catch layer. The fact that the loop needed those patches at all is more informative than the fact that it's currently green.

## Takeaway for the next window

Green streaks are worth banking, but the actionable signal here is: keep expanding test coverage into paths that were previously "proven" only by production runs, and treat any new automation lever (like complexity-based model routing) as untrusted until it has its own failure-mode tests — not just a clean run history.
