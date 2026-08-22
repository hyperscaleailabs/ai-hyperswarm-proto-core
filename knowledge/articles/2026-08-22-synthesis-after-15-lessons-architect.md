---
tags:
  - article
  - persona/architect
---

# Synthesis After 15 Lessons: Running an Autonomous Engineering Loop

Over five recent iterations of our self-directed build loop — two `implement`, three `improve` — the system shipped a quota/cost telemetry ledger, a reproduce-before-fix regression guard, and a governance layer built around a two-phase engine with explicit SDLC evidence artifacts. All five merged clean. That's the headline number, but the more useful read is *why* it stayed green, and where the seams still are.

## What we adopted

**Reproduce-before-fix as a hard gate.** Every `heal`/`bugfix` ticket must first demonstrate the failure in an environment close to production before any patch lands. This is the single change with the best ROI: it converts "looks fixed" into "verifiably was broken, now isn't," and it's cheap to enforce structurally rather than through review discipline.

**Two-phase engine with explicit phase artifacts (borrowed from MetaGPT).** Separating "plan/design" output from "implement" output as first-class, inspectable artifacts — rather than letting an agent silently fold planning into code — made governance review tractable. A reviewer can now read the design artifact without diffing code to reverse-engineer intent.

**Task-complexity-based model selection.** Not every ticket needs the top-tier model; routing by estimated complexity cut cost without measurably increasing defect rate in this window. This is a lever worth having, but five passing tickets is too small a sample to claim it's safe under harder tasks — we haven't yet stress-tested it against a ticket that *should* have escalated and didn't.

**Fake-runner integration tests for orchestrator paths.** Testing the `run-once`/`heal`/`implement` control flow against a fake runner instead of the real one caught orchestration bugs earlier and cheaper. Tradeoff: fidelity. A fake runner can't surface the two failure classes we already know are real — workers denied `pytest`/`ruff` inside loop worktrees, and any write under `.claude/` being blocked harness-wide — because those are sandbox/permission boundaries, not orchestration logic. Both are known, unfixed gaps that this test strategy structurally cannot catch.

**Retry + CI parity.** Loop reliability improved by making local retry semantics match CI's, closing a class of "passes locally, flakes in CI" drift.

## What to be honest about

Zero failures across five lessons is a good sign, not proof of robustness — it's a narrow window (`implement`/`improve` only; no `heal` or `bugfix` tickets appear here) with recurring themes (`build`, `change`, `cleanly`, `green`, `merged`) that read as much like "the easy tickets landed" as "the system is reliable." We haven't yet seen this loop under a ticket that needs `.claude/` writes or in-worktree test execution — both are structural blockers, not tuning problems, and no amount of prompt or model-selection improvement fixes them. The next real test of this architecture is a harder window: a `heal` ticket that requires reproducing a flaky failure, or a ticket that collides with the worktree permission boundary. Until then, "green" describes the tickets we chose, not the ceiling of the system.
