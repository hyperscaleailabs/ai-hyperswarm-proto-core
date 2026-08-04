---
tags:
  - article
  - persona/architect
---

# What Five Green Lessons Actually Taught Us

Our autonomous build loop — an agent that implements and improves its own service lineage without a human in every cycle — just closed its fifth consecutive clean window: 5/5 passes, split across two `implement` and three `improve` tasks. Zero failures. That streak is worth interrogating, because a loop that never fails is either well-engineered or not being tested hard enough. Here's what actually got us there, including the parts that didn't work the first time.

## The pattern that mattered: reproduce before you touch anything
Every bugfix and heal ticket now runs through a mandatory reproduce-first gate before any patch lands. This wasn't the original design — earlier iterations let the agent patch on inference from logs alone, and it repeatedly "fixed" symptoms while missing root cause, which surfaced later as regressions in the *next* cycle. The fix was structural, not a prompt tweak: no patch is accepted without a failing E2E repro checked in first. This is the single biggest contributor to the current streak, and it cost real cycle time to enforce.

## Model selection by task complexity, not by default
We stopped routing every task through the same model tier. Task complexity now gates model selection — cheap, well-scoped changes get a smaller/faster model; ambiguous design or heal work escalates. The tradeoff is real: misclassification risk (a complex task under-routed) is worse than the cost savings, so the classifier is intentionally conservative and over-escalates on uncertainty. We haven't yet built confidence that the classifier itself won't drift as task shapes change — that's an open risk, not a solved one.

## CI parity and retry reliability had to be engineered, not assumed
The loop's biggest source of *false* failures before this window wasn't logic bugs — it was environment drift between what the agent tested locally and what CI actually ran. We closed that gap explicitly (CI parity work) and added retry semantics for transient failures, rather than treating every red run as a real defect. This is a tradeoff against signal purity: retries can mask genuine flakiness. We accepted that risk because the false-failure rate was actively degrading throughput.

## Governance over vibes
We layered in a two-phase engine, explicit SDLC evidence, and a review rhythm — plus, most recently, a quota/cost telemetry ledger with a warn-then-halt budget gate per block. These are guardrails against a specific failure mode: an unattended loop silently burning budget or skipping verification steps under time pressure. The cost is latency and process overhead on every cycle; we judged that acceptable because the alternative (an ungoverned autonomous loop) is not something you want discovering its own failure modes in production.

## Honest caveat
Five clean lessons is a short window. The reliability and reproduce-first work exists precisely *because* earlier windows weren't clean. The absence of failures here is a result of engineering, not evidence the system can't fail — we're watching the next window, not declaring victory.
