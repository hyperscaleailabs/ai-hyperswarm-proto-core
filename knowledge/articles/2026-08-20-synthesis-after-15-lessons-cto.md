---
tags:
  - article
  - persona/cto
---

# AI Engineering Loop: Five Straight Green Runs — What That Does and Doesn't Tell Us

## The headline number

Our autonomous engineering loop closed its last five tickets — two new features, three improvements — with a 100% pass rate and zero rollbacks. Every change built cleanly, merged cleanly, and left the pipeline green. That's the number leadership wants to see, and it's real.

## What actually happened

The work skewed toward incremental hardening rather than green-field build: reference-set refreshes, a fake-runner test harness for the orchestrator's core paths, phase-artifact discipline borrowed from MetaGPT-style process design, and reliability fixes to retry/CI behavior. In plain terms — the loop spent this window paying down process debt and shoring up its own test infrastructure more than shipping net-new capability. That's a defensible sequencing choice, but it's not the same story as "five features shipped."

## What failed — and what we can't yet see

There were no failed tickets in this window. I want to be direct about why that's a caveat, not just good news: five samples is not enough to characterize failure modes, and a clean streak can mean either genuine capability improvement or a period where the loop was assigned safer, better-scoped work. We don't yet have the failure data that tells us where this system breaks — what kinds of tickets it silently mishandles, or what a bad merge would look like before it's caught. A perfect record with no failure signal is a lower-confidence claim than a mixed record with well-understood failure modes, and right now we're in the former.

Separately, two known gaps limit how far we can currently extend this loop unsupervised: worker sandboxes can't run tests or linters themselves (verification currently has to happen outside the worker), and workers can't modify their own harness configuration. Both are containment properties working as designed, but they mean "fully autonomous, self-verifying delivery" isn't accurate yet — there's a human or external step in the loop for anything touching test execution or tooling config.

## Business read

The loop is demonstrating process discipline — consistent green builds, clean merges, retry/CI reliability work — which is the right foundation before we lean on it for higher-stakes tickets. It is not yet demonstrating breadth: five tickets, two kinds of work, no failure data. Before we increase the loop's scope or reduce human review on its output, I want at least one window that includes genuine feature complexity and a failure or two we can post-mortem — a clean streak on maintenance work doesn't tell us how the system behaves under harder problems or how it fails when it does.

## Recommendation

Keep current human review gates in place. Don't scale ticket complexity or autonomy based on this window's pass rate alone — treat it as evidence the scaffolding works, not evidence the system is ready for less supervision. Prioritize closing the two sandboxing gaps (test execution, self-config) next, since they're the blockers to any real reduction in human-in-the-loop verification.
