---
tags:
  - article
  - persona/architect
---

# When 4 Out of 5 Changes Fail to Reach Green

A synthesis over our last five implementation cycles surfaced an uncomfortable number: one pass, four fails. All four failures share the same shape — the change never reached a green CI run, and auto-merge held it back. This is worth treating as a systems problem, not a run of bad luck.

## What we built

The last few cycles layered process on top of process: a provenance-tracked registry for "adopted practices," a failure taxonomy in the ledger with postmortem-triggered backlog items, retrieval-grounded synthesis requiring the planner to cite its own prior lessons before filing tickets, and — most recently — a pre-PR acceptance audit with a diff-hygiene gate. Each of these was a reasonable response to a real gap: we had no structured record of *why* things failed, and the planner was filing tickets without grounding them in what we'd already learned.

## What failed, and why it's a pattern, not noise

The theme extraction across these five lessons converged on the same handful of terms every time: "change," "green," "auto-merge," "attempt." That's not a coincidence — it's the same failure mode recurring across unrelated feature areas. Three implementation efforts in a row (the adopted-practice registry, the failure-taxonomy/postmortem trigger, and retrieval-grounded synthesis) all shipped code that looked complete but didn't survive CI. The lesson records don't yet capture *which* check failed or *why* — only that it did — which is itself a gap: we built a failure taxonomy before we had the taxonomy to classify these failures.

The honest read: we've been building meta-tooling (registries, taxonomies, audits) faster than we've been hardening the basic loop of "land a change that passes CI on the first or second try." That's an inversion of priority. Process instrumentation is cheap to justify and easy to ship; it's much harder to admit the core loop is unreliable and stop to fix it.

## The tradeoff we're making now

The fifth change — the pre-PR acceptance audit and diff-hygiene gate — is a direct response, and it's the right one *if* it's genuinely enforcing hygiene rather than adding another artifact that itself needs to reach green. The risk with gating changes on an audit step is well understood: it either catches real problems (good) or becomes ceremony that adds latency without changing the fail rate (bad, and we won't know which until we see whether cycle six actually passes).

## What we're changing

Two decisions come out of this synthesis:

1. **Stop authoring new meta-process until one cycle lands green.** No new registries, taxonomies, or gates until we have direct evidence the pre-PR audit reduces failures, not just adds a stage.
2. **Attach root cause, not just outcome, to the next failure.** "Did not reach green" is not a lesson; it's a symptom. The failure taxonomy we just built needs to actually be populated with the specific CI signal (test failure, lint, flaky infra, merge conflict) on the very next red run, or it was built for nothing.

If cycle six also fails, the taxonomy and the audit gate are both suspect, not just the code under test.
