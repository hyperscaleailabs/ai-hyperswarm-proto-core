---
tags:
  - article
  - persona/architect
---

# What Fifteen Lessons Taught Us About Running an Autonomous Build Loop

We've been running a self-improving engineering loop — an agent that picks up tickets, implements or improves a system, tests it, and merges — for five generations now. This is a synthesis of what the last five iterations (2 `implement`, 3 `improve`) actually taught us about the architecture, not just a scoreboard.

## The headline number is misleading
Five passes, zero failures, is the kind of streak that should make an architect nervous rather than proud. A loop that never fails is either solving problems too small to expose its weak points, or its failure detection is too coarse to catch degradation that isn't an outright crash. We don't yet know which. The next block's job is deliberately harder tickets specifically to find out.

## What we adopted, and why
- **Explicit phase artifacts (borrowed from MetaGPT-style pipelines).** Each stage of work — design, implement, review — now produces a durable artifact instead of living only in agent context. This was a direct response to earlier opacity: when something went wrong, we had no record of *what the agent believed* at each step, only the final diff. Tradeoff: more storage and more ceremony per ticket, which slows small changes down. We accepted that cost because debuggability compounds and speed doesn't, on a loop meant to run unattended for months.
- **Reproduce-before-fix as a hard gate.** No bugfix or heal ticket merges without a demonstrated failing reproduction first. This came from a real failure mode in an earlier generation: fixes that patched symptoms an agent could observe rather than causes it had verified, which regressed silently later. The gate adds latency to every bugfix; we judged that acceptable because a fix that doesn't reproduce the bug isn't a fix, it's a guess.
- **Quota/cost telemetry with a warn-then-halt budget gate per block.** We were running the loop with no visibility into spend until a bill surprised us. The gate is intentionally soft (warn, then halt) rather than a hard per-call cutoff, because a hard cutoff mid-task leaves work in an inconsistent state — worse than overspending by a bounded margin.
- **Task-complexity-based model selection.** Not every ticket needs the strongest model. This was pure cost/quality tradeoff, and it's the one change here we're least confident about — we don't yet have evidence the complexity classifier is accurate, only that it's plausible.

## What we didn't get right
The "recurring themes" signal — words like *build*, *change*, *merged* clustering across lessons — is trivial and not actionable in its current form. It's telling us these are commits, which we already knew. We haven't yet built theme extraction that surfaces genuine friction (e.g., "reviewers keep rejecting X") rather than restating task metadata. That's a known gap, not a hidden one.

## The honest takeaway
Every change adopted this block traded latency or cost for legibility: artifacts over opacity, reproduction over assumption, telemetry over surprise. That's the correct trade for a system meant to run without a human in the loop — but it means velocity numbers from this block understate what a human-supervised version of the same work would look like, and we should stop comparing the two directly.
