---
tags:
  - article
  - persona/architect
---

# Synthesis After 15 Lessons: An Autonomous Build Loop's Track Record

Five lessons, five passes, zero failures — implement and improve tickets both. That streak is worth being suspicious of before it's worth celebrating. A governance loop that reports "no failures in this window" for five consecutive runs has either matured or is measuring the wrong thing, and the honest answer here is a bit of both.

**The setup.** This is an autonomous ticket-driven build loop: each cycle picks up an `implement` or `improve` ticket, runs it through a worker, and produces evidence (tests, diffs, a lesson write-up) before merging. Two of five tickets in this window were `implement` (new capability), three were `improve` (refinement of existing capability). The recurring themes extracted from the lesson text — "build," "change," "cleanly," "green," "merged" — are not insights, they're the loop congratulating itself. That's a real signal, not a nitpick: when your synthesis step surfaces vocabulary instead of failure modes, the synthesis step is under-tuned. We're treating that as a defect to fix, not a metric to report.

**What actually shipped, and why it matters architecturally.** The substantive lessons in this batch tell a different story than the top-line "5/5 pass":

- *Task-complexity-based model selection* — routing tickets to cheaper/faster models when the diff shape says "trivial," and reserving the expensive model for tickets that touch orchestration logic. This exists because uniform model selection was previously burning budget on boilerplate work.
- *Reproduce-before-fix regression guard* — a heal/bugfix ticket may not proceed until it reproduces the bug end-to-end first. This was added after prior cycles landed fixes for symptoms that weren't the actual bug — a direct, expensive failure mode, even though it predates this specific five-lesson window.
- *Quota/cost telemetry ledger with a warn-then-halt gate* — a hard budget stop per block, because "the loop stayed green" and "the loop stayed solvent" are different properties, and only one of them was being tracked before.
- *Loop reliability retry and CI parity* — retries tuned to match what CI actually enforces, closing a gap where local-loop green didn't predict CI green.

**The tradeoff we're accepting.** Every one of these is a guardrail bolted on after something broke or nearly broke. That's the honest shape of this system: it's not that failures stopped happening, it's that each failure mode got a specific, narrow gate, and the *current* window happened to avoid all of them. The risk is gate sprawl — each new failure class adds a bespoke check rather than a general property the loop guarantees. We haven't solved that yet; five-for-five is the loop working within the fences we've built, not evidence the fences are complete.

**Next fix, concretely:** stop synthesizing high-frequency words as "themes." Replace it with failure-mode clustering even when the window is clean — pull forward from the last N failures regardless of window boundary, so a green streak doesn't erase the memory of why the gates exist.
