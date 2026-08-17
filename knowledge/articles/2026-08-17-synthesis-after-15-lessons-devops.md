---
tags:
  - article
  - persona/devops
---

# What a Two-Week Green Streak in an Autonomous CI Loop Actually Looked Like

Five automation cycles, five passes, zero failures - two feature builds, three improvement patches, all merged clean. That streak is a fine outcome, but from an ops standpoint a five-run window with 0 failures tells you less than it sounds like: it means the happy path is solid, not that the guardrails have been tested.

## What actually shipped, mechanically

**Quota/cost telemetry ledger, warn-then-halt.** The loop now tracks spend per block and halts the block rather than the whole run when a budget threshold is crossed - with a warn stage before the hard stop, so a block doesn't die on the first dollar over.

**Governance artifacts for block 1.** Structured evidence (phase artifacts, review rhythm docs) now gets generated per block instead of living only in commit messages - the audit trail is a build output now, not something reconstructed after the fact.

**Reference-set snapshot refresh, one practice per pass.** The loop periodically re-syncs its reference corpus (langchain, MetaGPT, crewAI) and mines one adopted practice from it per cycle, rather than doing a big-bang resync.

**Explicit phase artifacts, adopted from MetaGPT.** Each phase (heal/implement/improve) now declares what it's supposed to produce, and that shows up in the PR body. This is the kind of thing that only pays off during an incident: when a PR is confusing, you can check what the phase claimed it would deliver against what it actually did.

## The part that actually caught something

Not everything in this window was purely green. During the reproduce-before-fix rollout, a worker's diff touched `.github/workflows/ci.yml` - and the CI-parity guard reverted it before commit, because a task is not allowed to edit the checks it's judged by. That's not a failure in the "0 fail" tally, because the guard did its job silently and the PR still landed green. But it's the one concrete piece of evidence this run that a safety mechanism fired against real (if mundane) drift, not just theoretical risk. Everything else in the window was: model ran, agent returned ok=True, CI green before and after, merged.

## The honest read

The recurring vocabulary across all five lessons - "build," "change," "cleanly," "green," "merged" - is the vocabulary of a loop optimizing to land PRs, not the vocabulary of a loop under adversarial pressure. We haven't yet forced a run where the cost-ledger halt actually trips mid-block, where remote CI disagrees with local after a legitimate edit, or where the reproduce-before-fix gate has to reject a plausible-but-wrong patch. Until one of those happens under real conditions, "0 fail" should be read operationally as "guardrails installed, not yet exercised," not as "loop is robust."

## Next check

Stop inferring reliability from an unbroken streak. Deliberately trip each new gate - overshoot the per-block budget, feed a bugfix ticket a reproduction that fails, land a workflow-file edit that isn't trivially revertible - and confirm the loop degrades the way it's designed to, not just that it hasn't needed to yet.
