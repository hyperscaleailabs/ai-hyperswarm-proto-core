---
tags:
  - article
  - persona/architect
---

# Five Green Runs: What the Autonomous Build Loop Learned

Over its last five lessons, our AI-driven build loop shipped five changes cleanly — two new implementations, three improvements — with zero failures. That's a good outcome, but a two-week streak of green isn't a design validation by itself. Here's what actually got adopted, and why.

## What we adopted

**Task-complexity-based model selection.** Not every ticket needs the same reasoning budget. We now route work to a smaller/cheaper model when the task profile is simple (config tweaks, snapshot refreshes) and reserve the expensive model for structurally novel work. The tradeoff is real: misclassification risk. A complex task routed cheap either fails loudly (cheap, recoverable) or — worse — succeeds shallowly and passes review without actually solving the problem. We mitigate this with downstream test and review gates rather than trusting the classifier alone; the classifier is a cost lever, not a correctness guarantee.

**Fake-runner integration tests around the orchestrator.** Testing the `run-once` / `heal` / `implement` paths against a real LLM backend was slow and nondeterministic — flaky in exactly the way that erodes trust in CI. We built a fake runner that simulates orchestrator control flow without live model calls. This is a classic test-pyramid trade: we lose end-to-end fidelity (the fake runner can't tell us if the *real* model output is well-formed) in exchange for fast, deterministic coverage of the state machine itself. We still run real-model smoke tests separately, at lower frequency, to catch the fidelity gap.

**Explicit phase artifacts (MetaGPT-inspired).** Instead of letting the loop's intermediate reasoning stay implicit inside a single prompt/response, we now force each phase (design, implement, review) to write a durable artifact. This costs latency and token overhead per cycle. What it buys: auditability. When something does go wrong, we can point at the artifact from the phase that broke instead of re-deriving intent from a diff.

**Retry and CI parity for loop reliability.** This one came directly out of pain, even though it doesn't show up as a "fail" in this window — the fixes landed *before* the lessons in this synthesis. Loop runs were passing locally and failing in CI (or vice versa) due to environment drift, and transient failures were being treated as hard stops instead of retried. We tightened CI parity and added bounded retries. The honest caveat: retries mask true flakiness as easily as they absorb transient noise. We're watching retry-count trends, not just pass/fail, to make sure we're not quietly hiding a real bug behind a second attempt.

## What we're not claiming

Five passes in a row is a low base rate to generalize from — it tells us the loop *can* stay green, not that it reliably will under harder tickets. The recurring themes in this window ("build," "change," "merged," "cleanly") are process vocabulary, not technical signal; they mean the lessons were largely about integration hygiene rather than novel architecture. The real test of these four adoptions is the next window that includes a failure — that's when we'll learn whether the model-routing, fake-runner boundary, and retry logic actually hold, or just haven't been stressed yet.
