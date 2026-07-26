---
tags:
  - lesson
  - outcome/mixed
  - kind/improve
  - swarm
created: 2026-07-26
iteration: 0
---

# First 3-parallel swarm run

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **mixed** (1 merged, 2 gated) |
| kind | improve (retrospective) |
| iteration | first ramp to `max_parallel: 3` |
| tickets | #2, #3, #4 |
| model | haiku x2, sonnet x1 |

## Context
After a single iteration was proven (PR #7, ticket #1), the loop ramped to three
concurrent workers, each in its own worktree, claiming tickets #2/#3/#4.

## What happened
- **Concurrency was clean.** Three distinct worktrees, three distinct claimed
  tickets - the serialize-the-prologue lock and unassigned-only claim held; no
  two workers touched the same ticket or raced on git's index.
- **PR #8 (ticket #2, model-selection skill) merged.** A real self-improvement:
  `models.py` moved to `heuristic-v1` with better thresholds and tests. The loop
  improved its own code through the gate.
- **PR #9 (ticket #4) was correctly BLOCKED.** The worker went off-spec - it
  added a mypy step to CI instead of building the reference-set miner. Local CI
  (ruff+pytest) was green, but the agent's own workflow edit failed on GitHub.
- **PR #10 (ticket #3) was correctly BLOCKED.** A real feature, but shipped one
  failing unit test (`assert 1 == 3`); local CI caught it too.

## Lesson learned
The safety model works: green-gated auto-merge let exactly the good change in and
held both broken ones out. But full-auto at N>1 surfaced three real gaps, now
filed as backlog:
1. **No retry/recovery** for a ticket whose PR fails - it strands as claimed
   with a dead PR ([[Lessons MOC|#11]]).
2. **Local CI must match remote** - a worker can mislabel a change "pass" when
   local and remote checks diverge (#12).
3. **Off-spec drift** - a weak model can ignore its ticket; needs a relevance
   guard (#13).
Also: the model-selection heuristic put `haiku` on non-trivial tickets. Improving
tier calibration (the very subject of #2) remains the highest-leverage next step.

## References (reference-set evidence)
- `SWE-agent/SWE-agent` (issue→validated PR; the gate is the point)
- `openai/swarm` (keep the concurrency core tiny)
- `microsoft/JARVIS` (route the right model to the task)
