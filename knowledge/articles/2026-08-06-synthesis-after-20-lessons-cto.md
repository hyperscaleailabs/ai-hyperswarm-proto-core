---
tags:
  - article
  - persona/cto
---

# From Fragile Automation to Durable Systems: What Block 41341 Teaches About Building to Scale

We're at a useful inflection point. The loop went 5-for-5 this window, but the reason matters: it's not that the loop got better at making decisions, it's that the loop can now *recover* from its own failures without human intervention. That shift — from best-effort to resilient — is the real story worth documenting as a pattern before we scale further.

## What we shipped in this window, in order of importance

**Durable checkpoints.** The cycle journal records what the loop intended to do at each step: "I am about to synthesize," "I am now on iteration 3 of 5," "I am about to open a governance PR." If the process dies anywhere in that sequence, `--resume` picks up where it left off by checking the journal first. This is table-stakes for any system running unattended (cron, cloud, CI/CD). We didn't have it; now we do.

**Trajectory records.** Each worker (the agents that actually implement features) now logs what it did, what it cost (tokens, wall-clock time), and what the outcome was. This gives us the raw material to answer three concrete questions: "Was this agent routed to the right tier?" "Is our model selection heuristic actually saving quota?" "Did this worker leave a partially-committed mess that the next one has to clean up?" These are the questions that matter for a cost-conscious, auditable loop.

**Cycle governance discipline.** Blocks 41339 and 41341 both closed with formal synthesis, persona perspectives, and a DIRECTION refresh. That's the rhythm: work, document, align the team. It's working. It stays working if we keep it as a discipline, not a nice-to-have.

## What the 5-pass window actually says about system health

Nothing yet, and that's the honest assessment. Five passes in a row on hardening-heavy work (trajectory capture, cycle durability, documentation) is a positive signal, but it's not evidence the system can handle a genuinely difficult ticket without scaffolding. We haven't had a failure-forcing scenario since the loop was patched to stop hiding failures from itself. That's a forward-looking risk: confidence built on easy work doesn't transfer automatically to hard work.

## What to measure going forward

- **Unattended-loop trial.** Schedule the cycle to run nightly on a CI machine. Measure: Does it actually resume on crashes? Does it recover with the right state?
- **Quota-per-unit-of-work.** We now have trajectory records. Calculate: tokens per merged PR. Is model selection actually saving quota compared to a fixed-tier baseline?
- **Resume coverage.** Deliberately crash the loop mid-synthesis, mid-iteration, mid-articles. Verify: every crash point resumes correctly with no data loss or duplication.

## The two-phase pattern is holding

Heavy model (opus) for synthesis, cheaper model (haiku) for certain implementations — that worked well this window. The trajectory data now lets us validate whether task-complexity routing is actually paying off. Measure it explicitly in the next sync.

## Bottom line

Block 41341 built foundational resilience. That's valuable. It's not a feature that users see; it's the difference between "works when I babysit it" and "works when it runs in the background." We're not there yet (unattended trial hasn't run), but we have the code in place. The next milestone is the proof.
