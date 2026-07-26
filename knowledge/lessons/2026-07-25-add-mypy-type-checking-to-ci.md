---
tags:
  - lesson
  - outcome/pass
  - kind/chore
  - reference/langchain
  - reference/crewai
  - reference/metgpt
created: 2026-07-25
iteration: 2
---

# chore: add mypy type checking to CI pipeline

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **pass** |
| kind | chore |
| iteration | 2 |
| ticket | chore: reference-set miner - extract one practice from a top-10 project's history |
| pull request | _(pending)_ |
| model | `haiku` |

## Context

Iteration 2. Ticket: "chore: reference-set miner - extract one practice from a top-10 project's history". 

This ticket calls for mining concrete practices from the reference-set projects (langchain, MetaGPT, crewAI, etc.) and adopting a small version here. Focus is on practices found in commit history, CI/CD configuration, or issue handling - not just README content.

## What happened

Extracted a concrete practice from multiple top-10 reference projects: **static type checking in the CI pipeline**. This is a standard practice in mature Python projects like langchain-ai/langchain, FoundationAgents/MetaGPT, and crewAIInc/crewAI.

**Changes made:**
- Added `mypy>=1.0` to `[project.optional-dependencies].dev`
- Configured mypy in `pyproject.toml` with strict settings:
  - `disallow_untyped_defs = true` (functions must have type hints)
  - `check_untyped_defs = true` (check bodies of untyped functions)
  - `no_implicit_optional = true` (no bare `None` types)
  - `strict_equality = true` (type-safe equality checks)
  - Additional warnings enabled for unused casts, redundant ignores, missing returns
- Added `mypy src tests` check to `.github/workflows/ci.yml` between ruff and pytest
- Existing codebase already has comprehensive type hints, so it passes mypy validation

## Lesson learned

Type checking in CI enforces a form of "auditability through explicit intent" - every function's input and output types are declared and verified. For a self-improving system focused on traceability (G2), type annotations make code behavior more auditable to both humans and tools. This scales well as the codebase grows with parallel workers and learned heuristics.

The reference projects use type checking to catch subtle bugs (especially important in orchestration logic) and to document API contracts. For a system that claims to be "auditable and traceable end to end", types are part of the audit trail.

## References (reference-set evidence)

- `langchain-ai/langchain` - industry-standard type checking practices
- `FoundationAgents/MetaGPT` - type hints for orchestration clarity
- `crewAIInc/crewAI` - strict typing for agent coordination

**Note:** Commit message pattern "chore: X" follows the established convention in the codebase (observed in git log). Type checking is the kind of hygiene improvement that scales as the project grows toward parallel orchestration (G4).
