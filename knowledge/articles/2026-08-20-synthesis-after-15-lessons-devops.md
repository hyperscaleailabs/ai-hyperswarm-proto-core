---
tags:
  - article
  - persona/devops
---

# Five Green Runs Don't Mean the Pipeline Is Solved

Our last five loop iterations — two `implement`, three `improve` — all shipped clean: 5 pass, 0 fail. That streak is worth explaining mechanically, because "green" here means something specific, and the lessons underneath it are more interesting than the scoreboard.

## What actually shipped

**Model selection by task complexity.** The orchestrator now routes tickets to a smaller or larger model based on estimated complexity before spending tokens on the wrong tier. This came out of watching cheap tickets burn expensive-model budget for no quality gain — a pure cost/latency fix, not a correctness one.

**A fake runner for orchestrator integration tests.** The orchestrator's three critical paths — `run-once`, `heal`, `implement` — didn't have integration coverage that didn't also require standing up real workers. We built a fake runner so CI can exercise the state machine (dispatch → wait → collect → retry) without paying for live agent calls on every push. This is the kind of test infra that doesn't show up as a "feature" but is what makes the rest of the loop's claims about reliability credible instead of anecdotal.

**Reference-set snapshot refresh.** A recurring maintenance task: the reference set the loop grades against goes stale, and a chore ticket periodically refreshes it and extracts one reusable practice from the diff. Small, boring, and exactly the kind of task that rots silently if it's not scheduled.

**Explicit phase artifacts (MetaGPT-style).** Each SDLC phase now writes a concrete artifact — not just an implicit state transition — so a later step (or a human reviewing the block) can see what happened at each phase without replaying logs.

**Loop reliability: retry and CI parity.** This is the one worth being honest about. The loop's retry logic didn't match what CI actually does — a ticket could pass the loop's local checks and still fail in CI because the two environments diverged (different flags, different fixture state, different timeout budgets). That's a failure mode we hit, not a hypothetical: work looked done, then wasn't, because the loop's definition of "pass" wasn't the same as CI's. The fix was making the loop invoke the same CI-parity checks it's graded on, not a lighter approximation.

## The honest caveat

"0 fail in this window" is a window artifact, not a claim that the loop is failure-proof. Five lessons is a small sample, and the recurring-failures section is empty because the window happens to start after the CI-parity fix landed — it doesn't mean earlier drift can't recur. The recurring *themes* (`build`, `change`, `cleanly`, `merged`, `green`) are also suspiciously generic — they're artifacts of how these lesson titles get tokenized, not evidence of a deep pattern. Worth tightening the theme-extraction step before trusting it for signal.

**Operational takeaway:** the loop's grading logic is only as trustworthy as its parity with the system it's approximating. Every reliability win here came from closing a gap between "the loop thinks this passed" and "CI thinks this passed" — that's the lesson to keep applying, not the streak itself.
