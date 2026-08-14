---
tags:
  - article
  - persona/devops
---

# What Five Green Runs Taught Us About the Loop

Five lessons landed in this window — two `implement`, three `improve` — and all five merged clean. That's worth being suspicious of, not proud of: a fully green streak in an autonomous build loop usually means either the guardrails are working or they're not catching enough. Here's what actually changed underneath, and where we're still watching for the other shoe.

## The mechanics that shifted

**Model selection got task-aware.** Instead of a fixed model per pipeline stage, the loop now picks model tier based on task complexity signals in the ticket. Cheap, well-scoped tickets route to a lighter model; anything touching orchestration logic escalates. This is a cost/reliability lever, not just a cost lever — over-provisioning a trivial ticket wastes budget, but under-provisioning a complex one is how you get plausible-looking garbage that passes a shallow CI check.

**Loop reliability got a retry layer, explicitly reconciled with CI parity.** The lesson title alone tells the operational story: retries were added *because* the loop's local pass/fail didn't always match what CI decided, which is the worst kind of flaky — it doesn't fail loudly, it fails inconsistently between two systems that are supposed to agree. Retry-on-mismatch is a patch, not a cure; the real fix is making the loop's local check a faithful subset of CI, and that work isn't done.

**A fake-runner harness now covers orchestrator paths.** `run`, `heal`, and `implement` all now have integration tests against a fake runner instead of relying on real ticket execution to exercise those branches. This matters operationally: before this, orchestrator-path bugs could only surface in production runs, which is an expensive place to find them. Worth noting — hsai workers still can't run pytest/ruff inside loop worktrees, so this test coverage has to be exercised outside the worker sandbox, not from within it.

**Explicit phase artifacts, MetaGPT-style.** Each SDLC phase now writes down what it produced instead of implying it from log lines. This is a debuggability investment: when something goes wrong three phases later, you want an artifact trail, not a transcript you have to re-derive intent from.

**Reference-set snapshots got refreshed and one practice got extracted into the permanent playbook.** Routine but easy to skip — reference sets rot if nothing forces a refresh cadence, and this window forced one.

## The honest part

There's no failure to report in this window — the ledger says 5/5 pass. That's a fact, not a boast. A streak this clean coming right after a retry-and-CI-parity fix is exactly the pattern you'd expect if the fix worked — or if the retry layer is now quietly absorbing failures that used to surface as red runs. We don't have the data yet to tell which. The next thing worth checking isn't "did it pass" but "how many retries did it take to get there" — if that number creeps up, the retry layer is masking a regression, not fixing one.

**Recurring theme across all three `improve` lessons:** *build cleanly, change, merge green.* That's the loop doing what it's supposed to. The operational question going forward is whether "green" means "correct" or just "didn't trip the check we happened to write."
