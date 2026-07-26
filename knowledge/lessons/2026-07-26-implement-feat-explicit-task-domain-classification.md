---
tags:
  - lesson
  - outcome/pass
  - kind/implement
  - reference-extraction
created: 2026-07-26
iteration: 8
---

# implement: feat: explicit task-domain classification

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **pass** |
| kind | implement |
| iteration | 8 |
| ticket | _(self-improve)_ |
| model | `haiku` |
| reference-source | `FoundationAgents/MetaGPT` |

## Context

Iteration 8. Backlog empty, triggering self-improve. The reference-set miner ticket (iteration 3) had timed out, so returning to extract a concrete practice from the pinned reference set.

## Practice extracted

**From MetaGPT: Explicit work-domain classification for tasks**

MetaGPT decomposes software development work into roles (architect, programmer, tester, reviewer, etc.), each with domain-specific responsibilities. This explicit categorization enables clear routing and visibility into what kind of work is being done. Inspired by this, added explicit task-domain classification to hsai's model-selection pipeline.

## What happened

Added `classify_domain()` function to `hsai.models` to infer task domain (code | docs | test | infrastructure | refactor) from task title, body, and labels. The classification is now:
- Part of the Task dataclass as an optional `domain` field
- Inferred automatically by `classify_domain()` using marker-word matching
- Recorded in the model-selection rationale for auditability
- Fully tested with 8 new test cases

**Changes:**
- Added `domain: str = ""` field to Task dataclass
- New `classify_domain(task: Task) -> str` function with marker-based heuristic
- Updated `select()` to call `classify_domain()` and include domain in rationale string
- Added `TestDomainClassification` test class (8 test methods)

## Lesson learned

Explicit domain classification sets up the system for better future task routing and provides auditability into work types. The marker-based approach is lightweight and deterministic, avoiding expensive inference while still capturing meaningful categories. This follows the principle from MetaGPT of making implicit work types explicit, which in turn enables better decision-making in larger swarms.

The implementation is conservative: domain is inferred but not yet used in tier selection; it just appears in the model-choice rationale for visibility. Future improvements can use this signal to improve model selection or implement role-specific routing.

## References (reference-set evidence)

- `FoundationAgents/MetaGPT` - Role-based agent decomposition and explicit work categorization
