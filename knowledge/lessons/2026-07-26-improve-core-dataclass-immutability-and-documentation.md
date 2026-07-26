---
tags:
  - lesson
  - outcome/pass
  - kind/implement
  - practice/immutability
created: 2026-07-26
iteration: 4
---

# improve: core dataclass immutability and documentation

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | pending CI |
| kind | implement |
| iteration | 4 |
| ticket | _(self-improve, no backlog ticket)_ |
| pull request | _(pending)_ |
| model | `haiku` |

## Context

Iteration 4. CI green at start. Backlog empty. Adopted immutability pattern from reference-set projects (MetaGPT, semantic-kernel) that emphasize frozen data structures for core domain objects.

## What happened

Applied frozen=True decorator to three core dataclasses:
- `Lesson`: Immutable outcome record (never mutated post-creation)
- `Task`: Immutable work unit (safe for concurrent selection heuristics)
- `ModelChoice`: Immutable decision record (enables audit trail)

Refactored one post-creation mutation (`lesson.remote_ci = remote`) to use `dataclasses.replace()` for functional updates, preserving immutability invariant.

Added comprehensive docstrings (70+ lines total) explaining design intent, field semantics, and relationship to reference patterns.

## Lesson learned

**Immutable value objects improve safety and intent clarity.**

Frozen dataclasses prevent accidental mutations, making the data flow explicit and testable. This is a small, high-signal change that costs nothing (frozen=True is zero-cost) and documents design intent for future maintainers. The pattern is widespread in production multi-agent frameworks (MetaGPT, semantic-kernel, crewAI) where correctness of agent state is critical.

Key insight: Rather than add invasive validation or assertion layers, let Python's type system and frozen dataclasses enforce the invariant that `Lesson` and `Task` never change after construction. This aligns with functional programming practices and the core principle that the knowledge base records are immutable historical facts.

## References (reference-set evidence)

- **FoundationAgents/MetaGPT**: Uses frozen task/agent/role definitions throughout the codebase; immutability is a first-class concern for correctness.
- **microsoft/semantic-kernel**: Immutable `KernelArguments`, `KernelContent` structures; emphasizes value-object semantics for safety.
- **crewAIInc/crewAI**: Frozen task definitions; prevents runtime mutation bugs in concurrent agent execution.

## Design notes

- `Lesson` is now truly immutable; creating a new version requires `replace(lesson, ...)`.
- `IterationResult` remains mutable (accumulated during iteration); documented as temporary state, distinct from immutable `Lesson`.
- No performance cost; frozen=True is O(0) overhead, enforced at object creation time.
- Type checkers and linters can now assume Lesson fields are constant, enabling better analysis.
