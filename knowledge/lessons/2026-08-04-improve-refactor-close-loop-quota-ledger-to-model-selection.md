---
tags:
  - kind/improve
  - outcome/pass
---

# Improve: refactor - close the loop from the quota ledger back into model selection (heuristic-v2)

## Context
The quota ledger measures cost per PR (wall-clock, model tier, outcome), but model-selection decisions were still using a static complexity heuristic. This created a gap: we could see which model choices wasted quota, but we weren't feeding that signal back into the selector.

## What happened
Refactored the model-selector to:
- Check the ledger for recent PRs of similar complexity
- Calculate cost-to-success ratio for each tier given that similar work
- Bias the selector toward the tier that historically had the best cost/success ratio for that complexity band

Tested against synthetic data (controlled complexity bands, known ledger); results show the selector now avoids the expensive-model-on-trivial-work case that was common in the first ~10 lessons.

## Lesson learned
Closing the feedback loop is powerful but has latency. The ledger only accumulates signal over time — with 15-20 PRs, the signal is noisy; with 50+, it stabilizes. The tradeoff: earlier PRs might be mis-routed if the ledger is sparse, so we fall back to the static heuristic until we have enough data. This is a good example of "measure first, optimize second."
