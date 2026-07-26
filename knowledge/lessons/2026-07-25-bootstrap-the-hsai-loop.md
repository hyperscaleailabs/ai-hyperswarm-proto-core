---
tags:
  - lesson
  - outcome/pass
  - kind/improve
created: 2026-07-25
iteration: 0
---

# Bootstrap the hsai loop

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **pass** |
| kind | improve |
| iteration | 0 |
| ticket | _(founding)_ |
| pull request | _(founding)_ |
| model | `n/a (human + Claude Code scaffold)` |

## Context
Iteration 0 - the scaffolding itself. Before the loop can improve the repo, the
repo needs a loop, a CI gate, a knowledge base, and a pinned reference set.

## What happened
Built `hsai` (config → models → ai → gitops → github → ci → knowledge →
orchestrator → swarm → cli) with the decision logic kept pure and unit-tested,
and side effects isolated in wrappers. Pinned the top-10 reference set and wrote
the [[2026-07-25-founding-study-top-10-ai-swarm-projects|founding study]].

## Lesson learned
Start with the invariants, not the features: ticket-linked PRs, green-gated
merges, subscription-only model use, and a lesson per iteration. Everything else
(better model routing, deeper reference mining) can be grown by the loop once
those rails exist. Keep the core tiny - a practice borrowed from `openai/swarm`.

## References (reference-set evidence)
- `SWE-agent/SWE-agent`
- `openai/swarm`
- `crewAIInc/crewAI`
