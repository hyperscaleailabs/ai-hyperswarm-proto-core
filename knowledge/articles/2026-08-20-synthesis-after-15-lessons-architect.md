---
tags:
  - article
  - persona/architect
---

# What Five Green Tickets Actually Tell You About an Autonomous Build Loop

Our engineering loop — an agent-driven system that takes tickets from spec to merged PR without a human in the implementation path — just closed its last five lessons at 5/5 pass, split across two work kinds: `implement` (2) and `improve` (3). That's a good result, but a five-item sample is not a track record, and the design decisions behind it are more interesting than the streak.

## What we adopted

**Model selection by task complexity.** Not every ticket needs the same model tier. Routing trivial refactors to cheaper/faster models and reserving deeper reasoning for architecturally risky changes cut cost without a measurable quality drop — but only because we gated it on task shape (LOC touched, number of files, presence of concurrency/state) rather than trusting the agent's self-assessment of difficulty, which we'd found unreliable in earlier iterations.

**Fake-runner integration tests over mocks.** For the orchestrator's `run-once`, `heal`, and `implement` paths, we replaced unit-level mocking with a fake runner that exercises the real state machine end-to-end. Trade-off: slower test suite, more setup code. Payoff: the tests catch orchestration-sequencing bugs that mocked unit tests structurally cannot see, because the bugs live in the interaction between steps, not inside them.

**Explicit phase artifacts (MetaGPT-style).** Each phase of a ticket's lifecycle now writes a durable artifact — not just logs — that the next phase and the human reviewer both consume. This is the single highest-leverage change in the batch: it turns an opaque agent transcript into an auditable trail, at the cost of extra I/O and schema maintenance per phase.

**Loop reliability via retry + CI parity.** This is where prior failures actually lived. Flaky CI and transient tool errors were previously indistinguishable from real regressions, which meant the loop either retried too eagerly (masking real bugs) or halted too eagerly (burning human attention on noise). The fix was making local retry semantics match CI's retry semantics exactly, so a "pass" means the same thing in both places — a small change that removed a whole class of false-halt and false-pass outcomes.

## What we're honest about

The zero-failure window is real but should not be over-read: it's five tickets, two of which were incremental snapshot/practice-extraction chores, not deep implementation work. The harder signal is that the *previous* window's dominant failure mode — CI/local drift — was root-caused and fixed rather than patched around. That's the pattern we want more of: when something breaks, the synthesis should show a structural fix, not a retry-count bump.

## Open risk

Recurring themes in this batch skew toward process language ("build," "merged," "green," "cleanly") rather than domain language. That's expected for `improve`-heavy windows, but if it persists across the next synthesis cycle, it's a signal the loop is optimizing for its own throughput metrics rather than for the systems it's supposed to be building — worth watching, not yet acting on.
