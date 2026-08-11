---
tags:
  - article
  - persona/devops
---

# Twenty-Four Iterations, Still Zero Unplanned Downtime: Observability Momentum

> For: DevOps level - CI/CD, automation mechanics, operational lessons
> From: [[2026-08-11-synthesis-after-24-lessons]]

## Failures Are Graceful, Recovery Is Clean

The two failures in blocks 41351–41353 (timeout, agent incomplete) were both caught before merge. This is the system working as designed:
- Pre-merge CI gates caught the failures
- Auto-merge held until issues were resolved
- No bad state propagated to main

Operationally, this means:
- **Zero manual incident response** needed for application logic failures (they're caught pre-merge)
- **All failures are loggable and replayable** (via trajectory store)
- **No cascading failures** (each block is isolated by quota limits and journal boundaries)

## Trajectory Store Is Now Actively Used for Forensics

Unlike previous cycles where trajectory data was "nice to have," blocks 41351–41353 demonstrated its value: when the timeout happened, we could:
1. Replay the exact sequence of model calls
2. Identify the prompt that caused context explosion
3. Propose specific truncation strategies

This is mature observability. It enables post-mortems without re-running, saving compute and time.

**Recommendation:** Expose trajectory querying via a CLI command: `hsai inspect --block <ID> --phase <phase>` to surface which agent calls consumed the most tokens. This turns ad-hoc forensics into standard practice.

## Quota Gating: The Invisible Guardian

In 24 iterations, no block has:
- Hit hard quota halt (would require >100% budget)
- Left orphaned artifacts (journal recovery is sound)
- Caused duplicate PRs or issues (idempotency is holding)

The quota ledger is working at design specification. At current rate, we can scale to 3 concurrent blocks without needing to revisit the gating logic.

## Monitoring Gaps Identified

Two operational blind spots emerged:

1. **Timeout detection is coarse:** When a block hits the 1200s timeout, we know it failed, but we don't know if it was hung (awaiting external resource) or just slow (inefficient prompting). 
   - **Action:** Add heartbeat metrics; emit a status line every 60s so the operator sees which phase is running.

2. **No alerting on trajectory anomalies:** If a block consumes 80%+ of token budget early, we should warn the operator to throttle parallelization.
   - **Action:** Add a soft alert at 70% budget spend to trigger operator review before the hard halt.

## Scaling to Parallel Blocks: Go-Forward Signal

After 24 sequential iterations, the system is operationally ready for parallelization:
- Journal recovery proved in blocks 41343, 41345
- Quota isolation per-block confirmed by ledger
- CI gates are catching failures before merge
- Trajectory store enables forensics

**Readiness checklist for 3-parallel launch:**
- [ ] Add heartbeat metrics (60s phase reporting)
- [ ] Add budget anomaly alert (70% threshold)
- [ ] Document manual recovery procedure (if a block crashes mid-merge)
- [ ] Route each parallel block to its own artifact directory (vault-per-block)
- [ ] Test cross-block conflict handling in CI (two blocks touching same file)

All items except the cross-block conflict test are low-risk.

## Operational Handoff Ready

The system has matured from "experimental automation" to "production governance layer." The next operational milestone is parallelization. After that, the next is external deployment (on-prem or cloud).

Current blockers to parallelization are not technical (architecture is sound) but observational (we lack real-time visibility into parallel block states). Fixing that is a 1-week instrumentation task.

## References

This assessment is grounded in 24 iterations of zero-incident operation, trajectory store forensics, and quota ledger data showing consistent spend patterns.
