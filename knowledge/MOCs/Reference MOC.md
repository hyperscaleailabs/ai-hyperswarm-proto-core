---
tags:
  - moc
  - reference
updated: 2026-08-04
---

# Reference MOC

Up: [[Knowledge Base MOC]]

The corpus behind goal G1. Each note below is a durable digest of one reference
project, written by `hsai synthesize` instead of being re-fetched every
rotation, and the registry beside them (`practices.yaml`) records which of
their practices this loop proposed, adopted, or rejected. Total: **0**
project(s), **5** tracked practice(s).

## Reference projects
- _No reference notes yet._

## Adopted practices
- `assafelovic/gpt-researcher` - per-iteration cost ledger with a block budget gate - _costs.py: the research loop accounts for its own spend as it runs_ (ticket #44, PR #47, [[2026-07-26-implement-feat-quota-cost-telemetry-ledger-with-a-warn-then-halt-per-block-budget-gate]])
- `FoundationAgents/MetaGPT` - explicit phase artifacts in every PR body - _SOP roles: each stage emits a defined, auditable work product_ ([[2026-07-26-improve-explicit-phase-artifacts-from-metagpt]])
- `openai/swarm` - phase and ticket context on every error message - _README + core.py: errors carry which step raised them, not just the message_ ([[2026-07-26-implement-add-structured-execution-context-to-error-messages]])
- `run-llama/llama_index` - reproduce-before-fix guard on heal and bugfix tickets - _commit stream: a fix lands with the test that failed before it_ (ticket #43, PR #46, [[2026-07-26-implement-feat-reproduce-before-fix-regression-guard-for-heal-and-bugfix-tickets]])
- `SWE-agent/SWE-agent` - one durable trajectory record per agent run - _.traj files + the trajectory inspector: the run record is a primary artifact_ (ticket #79, PR #84, [[2026-08-04-implement-feat-worker-trajectory-store-json-agent-output-and-hsai-replay]])
