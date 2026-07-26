---
tags:
  - lesson
  - outcome/pass
  - kind/improve
  - reference-set-practice
created: 2026-07-26
---

# improve: phase capability registry - adopted from langchain and semantic-kernel

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **pass** |
| kind | improve |
| iteration | 105 |
| ticket | #48 |
| pull request | _(merged)_ |
| model | `haiku` |

## Context

Self-improvement task toward G2 (Stay auditable and traceable end to end). Observed practice from reference set: langchain-ai/langchain and microsoft/semantic-kernel both maintain explicit tool/capability registries that document what operations are available, with structured metadata (name, description). This makes systems more transparent and auditable.

## What happened

Added three functions to orchestrator.py:

1. **`_phase_capabilities(kind: str) -> tuple[str, ...]`**: Returns the set of capability names available to each phase (HEAL, IMPLEMENT, IMPROVE). Each phase has a distinct set of tools it can use, making constraints explicit and queryable.

2. **`_CAPABILITY_REGISTRY`**: A centralized dictionary mapping capability names to descriptions. This follows langchain's pattern of explicit tool definitions with metadata, supporting discovery and understanding of what the system can do.

3. **`_format_phase_capabilities(kind: str) -> str`**: Formats capabilities for display in PR bodies. In IMPROVE mode, the PR body now includes a "## Phase capabilities" section listing exactly what tools that phase has access to.

Updated `build_pr_body()` to include phase capabilities in IMPROVE mode PRs, making the context and constraints visible in every self-improvement PR.

Added comprehensive tests:
- `test_phase_capabilities_heal()`, `test_phase_capabilities_implement()`, `test_phase_capabilities_improve()`: verify each phase returns expected capabilities
- `test_format_phase_capabilities()`: verifies formatting includes descriptions from the registry
- Updated `test_build_pr_body_includes_phase_artifacts()`: now verifies IMPROVE mode includes capabilities section

CI passes: ruff=pass, pytest=pass (all new tests pass).

## Lesson learned

**Explicit capability registries make orchestrated systems auditable.** Adopted from langchain-ai/langchain and microsoft/semantic-kernel: systems that declare what tools/operations each phase can perform are more transparent and debuggable.

This is a lightweight practice with high leverage:
- **Transparency**: Every PR shows what capabilities the phase had access to
- **Auditability**: Readers can see if a phase did something outside its declared capability set
- **Learning**: Future iterations can reason about phase constraints and capabilities
- **Debugging**: When something fails, the capability registry makes it clear what tools were available

The registry is centralized in `_CAPABILITY_REGISTRY`, making it maintainable. Phase capabilities are derived from what each phase actually needs to do (HEAL needs CI analysis, IMPLEMENT needs code writing, IMPROVE needs research and practice extraction). This creates a tight feedback loop between what phases declare and what they actually do.

## References (reference-set evidence)

- `langchain-ai/langchain` - tool definitions with structured metadata (name, description, parameters) for transparency and composability
- `microsoft/semantic-kernel` - plugins and skills with explicit capability declarations and constraints
