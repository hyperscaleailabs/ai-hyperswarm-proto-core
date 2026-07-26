---
tags:
  - lesson
  - outcome/pass
  - kind/improve
  - governance
created: 2026-07-26
iteration: 0
reviewed: true
---

# Architect steering: quality over throughput

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **pass** (steering absorbed into the system) |
| kind | improve (governance) |
| source | architect review, 2026-07-26 |
| encoded as | ADR-0001, governance layer PR |

## Context
First day of autonomous operation: 6 PRs merged, 2 gated. The architect's
review found the merged work real but shallow - the loop optimized for
completing iterations, not for learning.

## What happened
The architect observed: (1) tasks were too simple and one "completed" ticket
shipped no code; (2) light models prefer minimal diffs, so unstructured tickets
invite hollow work; (3) single-project copying is not learning - synthesis must
COMBINE ideas across many reference projects with reflection and creativity;
(4) there was no steering surface, no review rhythm, and no evidence-leaving
SDLC encoded in the pipeline.

## Lesson learned
An autonomous loop optimizes exactly what its gates measure. Green CI alone
measures "didn't break anything", so the loop drifted toward minimal safe
diffs. Learning requires: substantial tickets with acceptance criteria
(enforced, not suggested), idea generation separated from implementation
(heavy-model synthesis with reflection; cheap-model execution), completeness
guards (code tickets need code), and a human steering cadence (twice-daily
review -> ADRs -> tickets) so quality judgments a machine can't make enter the
system on schedule. Throughput without synthesis is motion without learning.

## References (reference-set evidence)
- `SWE-agent/SWE-agent` (validated issue->PR discipline)
- `microsoft/JARVIS` (route by capability: planner model != worker model)
- `OpenBMB/ChatDev` (phased pipeline with gates between phases)
