---
tags:
  - lesson
  - outcome/pass
  - kind/improve
  - reliability
created: 2026-07-26
iteration: 0
---

# Loop reliability: retry/recovery and CI parity

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **pass** |
| kind | improve |
| tickets | #11, #12 |
| model | `n/a (human maintainer, via PR)` |

## Context
The first 3-parallel run (see [[2026-07-26-first-3-parallel-swarm-run]]) surfaced
two reliability gaps: a failed PR stranded its ticket as permanently-claimed
(#11), and a worker passed local CI while failing remote CI after editing the
workflow (#12).

## What happened
- **Remote CI is now the source of truth.** `run_once` waits for the PR's real
  GitHub check rollup (`ci.wait_remote`) before deciding the outcome.
- **CI parity guard.** Edits under `.github/workflows/**` are reverted before
  commit, so a task can't change the checks it is judged by.
- **Recovery.** On a non-green remote result, the PR is closed and the ticket
  returns to the backlog with an `attempts:N` label; after
  `max_ticket_attempts` it is `blocked` for a human. Blocked/assigned tickets
  are skipped by future workers.
- Added unit tests (fake-runner) for the recovery and revert paths, plus
  `tests/test_ci.py` for the rollup reducer and `wait_remote`.

## Lesson learned
Autonomy needs a truthful signal and a way to fail safely. Gating on *remote*
CI (not a local approximation an agent can perturb) closes the trust gap, and
bounded retry-then-block keeps the backlog self-healing without infinite loops -
a discipline borrowed from how `SWE-agent` treats the issue→validated-PR cycle.

## References (reference-set evidence)
- `SWE-agent/SWE-agent`
- `openai/swarm`
- `crewAIInc/crewAI`
