---
tags:
  - article
  - persona/devops
---

# Synthesis After 37 Lessons: 4 Reds, 1 Green

Five `implement` runs went through the pipeline in this window. One reached green. Four didn't. That's worth pausing on before shipping another feature.

## What we shipped

- A pre-PR acceptance audit and diff-hygiene gate
- Retrieval-grounded synthesis (the planner now has to read and cite its own lessons before filing tickets)
- Failure taxonomy in the ledger plus a postmortem-driven backlog trigger
- An adopted-practice registry with provenance, wired into the synthesis context pack
- Governance artifacts for the block

Four of these five auto-merges are currently held. CI didn't go green, and per the auto-merge gate, a red run doesn't get force-merged — it sits until someone (or the next attempt) fixes it.

## The failure pattern

Every failed lesson has the identical closing note: *"Change did not reach green; auto-merge will hold until CI passes. Investigate the failure captured above before the next attempt."* That's the taxonomy doing its job — capturing the failure — but it's also a flag that we're not yet closing the loop. The recurring themes across lessons are literally `change`, `green`, `above`, `attempt`, `auto-merge` — five lessons in a row bottoming out on the same unresolved CI failure without a distinct root cause being called out.

In practice this means: the investigation step is being logged, but not always acted on before the next attempt fires. We're accumulating red state faster than we're clearing it.

## What actually worked

The one pass in this batch was governance artifacts for the block — low-risk, low-surface-area, nothing exotic in CI. The passing case correlates with small blast radius, not with any particular technique. That's a weak signal, but it's consistent with what you'd expect: bigger diffs (registry + context pack wiring, retrieval grounding across the planner) have more surface for CI to catch.

## Operational takeaways

1. **Auto-merge-on-hold is working as designed** — nothing red is leaking to main. Good. But a queue of 4 held merges means the backlog trigger introduced this window (postmortem-driven) needs to actually fire on itself.
2. **The failure taxonomy captures *that* CI failed, not consistently *why*.** The lesson notes read as boilerplate rather than diagnosis. Next iteration should require the postmortem to name a concrete root cause (flaky test, real regression, env drift) before the ticket closes — otherwise the retrospective loop just launders the same failure back onto the backlog.
3. **Retry-without-diagnosis is a real risk here.** With retrieval-grounded synthesis now requiring the planner to cite prior lessons, there's a mechanism to prevent re-attempting a known-bad pattern — but only if the citations are for real causes, not for "CI failed" restated five times.

Next step: before filing the next ticket, one of these four held merges needs an actual root-cause diagnosis in its lesson, not just a status. That's the input the retrieval-grounded planner is supposed to read.
