---
tags:
  - article
  - persona/devops
---

# Five Passes in a Green Lane: What's Ready for Unattended Deployment, What Isn't

The loop just closed five work items without incident (5 pass / 0 fail). That's a clean window. Before you schedule this to run nightly in production, or worse, in a multi-instance fleet, here's what's actually ready and what's not.

## What's production-ready (right now)

**Cycle durability and resume semantics.** The journal-based checkpoint system works as designed. It passes the basic test: if you kill the process at any step and resume, it picks up correctly. That's a real operational win — it means an overnight run doesn't completely fail because of a 2am CI blip.

**Quota tracking and per-iteration cost.** The trajectory ledger now records every worker's input tokens, output tokens, and wall-clock time. You can measure what this actually costs. You can also enforce a hard halt if spend exceeds budget. That's table-stakes for any system that depends on API keys to function.

**Green-gated merge discipline.** Remote CI (GitHub checks) is the sole source of truth for "is this good to merge?" The loop doesn't ship anything that isn't passing CI. That's correct. The local ruff/pytest gates are there as a fast feedback loop, but they don't override the remote check. Trust only remote.

## What's not ready yet

**Parallel workers.** The system currently runs sequentially: one iteration at a time, one block at a time. Parallelism is available via `hsai loop --max-parallel N`, but it's untested in production. The risk: concurrent workers stepping on each other's repository state (file conflicts, merge conflicts). Before you run 5 workers at once, validate: each worker gets its own worktree, PRs don't collide, and a failed worker in slot 3 doesn't leave the repo in a half-committed state.

**Unattended operation under load.** The loop has never run overnight on a CI machine under real resource constraints (shared quota, rate-limited APIs, slow network). You have confidence it works on your laptop. You don't have confidence it works at 3am when three other automation jobs are also burning quota. Soft recommendation: do a 24-hour trial run on staging infra before you commit to production scheduling.

**Recovery proof.** The journal-based resume works *in the happy path* (crash, restart, proceed). It hasn't been tested under adversarial conditions: What if the journal gets corrupted? What if a worker writes a malformed lesson file? What if a PR is opened but then the next step crashes before recording it in the journal? These are edge cases, but they're the ones that cause silent data loss. Each deserves a deliberate test.

## Operational concerns to surface

- **Secret rotation.** The loop holds GitHub credentials and API keys. If you run this unattended, rotate those secrets regularly and audit access logs.
- **Failure volume.** If the loop starts filing tens of tickets (via synthesis), the engineer reviewing them will get overwhelmed. Set explicit backlog watermarks and test what happens when synthesis decides to file 10 tickets in one night.
- **Cost ceiling.** The quota ledger can halt the loop if spend exceeds budget. Good. But make sure the halt message gets to someone (Slack, PagerDuty, whatever). A silent halt overnight looks like the loop hung.

## The two things to validate before scheduling nightly

1. **Resume under crash.** Kill the process at three different points (synthesis, mid-implementation, before governance PR). Verify: resume works, no duplication, final state is correct.
2. **Overnight quota pulse.** Run the loop for 8 hours on staging. Record: total tokens, total cost, total iterations. Does it behave differently under load than it does in isolation?

## Recommendation

The code is ready. The operational discipline isn't yet. Do the validation work first, then schedule. A production incident on autonomous automation is way more painful than waiting two weeks to get it right.
