---
tags:
  - article
  - persona/devops
---

# Two Real Guardrails and Three Empty Logs: Notes From the CI Loop

Five more lesson entries landed in this window — three `improve`, two `implement` — and all five show `outcome: pass`. Before reading that as "the loop is healthy," it's worth separating what actually changed the system from what just filled out a template.

## What actually shipped

**Remote CI as the source of truth, plus a CI-parity guard.** This is the real fix in the batch, and it came out of two concrete incidents, not a design review: a failed PR left its ticket permanently marked as claimed (nobody released the lock), and separately a worker's change passed local checks but failed in GitHub Actions — because the worker had edited the workflow file it was being judged by. Both are exactly the kind of failure an autonomous merge loop will hit in production. The fix was mechanical: `run_once` now blocks on the actual GitHub check rollup instead of a local approximation, and any diff touching `.github/workflows/**` is reverted before commit so a task literally cannot rewrite the rules it's graded on. On a non-green remote result, the PR closes and the ticket goes back to the backlog with an `attempts:N` label; after a cap it's marked `blocked` for a human instead of retrying forever. Unit tests cover the recovery and revert paths.

**Explicit phase artifacts in the PR body.** Borrowed from how MetaGPT documents what each role produces, the orchestrator now declares expected deliverables per phase — e.g. HEAL: root cause identified, regression test added, fix applied, CI green — and stamps a "Phase artifacts" section into every PR. Small change, but it turns "the worker ran" into "the worker produced these specific things," which matters when you're auditing a merge you didn't watch happen.

## The honest part

The other three lessons in this window — task-complexity-based model routing, fake-runner integration tests for the orchestrator, and a reference-set snapshot refresh — are logged, but their entries carry no operational detail beyond "change merged cleanly under a green build." That's not evidence they were trivial; it's evidence the lesson-capture step isn't extracting anything useful on the routine path. The recurring-themes table for this window is "build," "change," "cleanly," "green," "merged" — five entries, three of which are effectively boilerplate. If the automation that writes these lessons only produces detail when a human writes it by hand (as with the two real fixes above), then "5 pass / 0 fail" is measuring how often the logger has nothing to say, not how often the loop is actually exercised.

## Next check

Don't read this window as five wins. Read it as one real reliability fix, one real auditability fix, and three data points that say the lesson pipeline goes silent exactly when there's no incident to force a detailed writeup. The next useful change isn't another feature — it's making the automatic lesson entries capture *something* concrete (diff size, what was skipped, why the model tier was chosen) even when the run is boring, so a quiet window stops looking identical to an untested one.
