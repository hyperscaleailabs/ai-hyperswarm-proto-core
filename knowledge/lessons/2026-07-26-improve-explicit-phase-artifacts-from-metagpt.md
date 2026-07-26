---
tags:
  - lesson
  - outcome/pass
  - kind/improve
  - reference-set-practice
created: 2026-07-26
---

# improve: explicit phase artifacts - adopted from MetaGPT

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **pass** |
| kind | improve |
| iteration | 5 |
| ticket | _(self-improve)_ |
| pull request | _(merged inline)_ |
| model | `haiku` |

## Context

Self-improvement task toward G2 (Stay auditable and traceable end to end). Observed practice from reference set: FoundationAgents/MetaGPT defines agent roles with explicit output deliverables. Each agent (Product Manager, Architect, Engineer) has a documented set of artifacts it produces, making work outputs concrete and auditable.

## What happened

Added `_phase_artifacts()` helper to orchestrator.py that documents what each phase (heal / implement / improve) produces:

- **HEAL**: Root cause identified, regression test added, fix applied, CI green
- **IMPLEMENT**: Feature/fix end-to-end, tests added, code scoped, passing
- **IMPROVE**: Practice extracted, implementation added, lesson recorded, tests passing

The helper is integrated into `build_pr_body()` so every PR now includes a "## Phase artifacts" section listing what this phase produced.

Added comprehensive unit tests to verify:
- Each phase returns expected artifact descriptions
- PR body includes artifacts section when kind is provided
- PR body omits artifacts section when kind is empty

CI passes: ruff=pass, pytest=pass (including 5 new tests).

## Lesson learned

**Explicit phase outputs make work auditable.** Adopted from FoundationAgents/MetaGPT: orchestrated multi-agent systems benefit from declaring what each phase/role produces. This transforms implicit work ("run the worker") into visible deliverables ("produced: X, Y, Z"), supporting G2's traceability goal.

The practice is small but high-leverage: every PR now documents what artifacts the phase produced, making it immediately clear what the worker was responsible for delivering. This improves debugging and auditing of failed or suspicious changes.

## References (reference-set evidence)

- `FoundationAgents/MetaGPT` - multi-agent software company with explicit role-based agents; each role (ProductManager, Architect, Engineer) defines its output artifacts, making the work of orchestrated agents concrete and verifiable
