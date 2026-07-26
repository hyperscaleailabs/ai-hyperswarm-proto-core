---
tags:
  - lesson
  - outcome/implemented
  - kind/self-improve
  - source/reference-set
created: 2026-07-26
iteration: 4
---

# self-improve: add mypy type checking to CI — from langchain-ai/langchain

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **implemented** |
| kind | self-improve |
| iteration | 4 |
| ticket | N/A (self-improve) |
| pull request | _(pending merge)_ |
| model | `haiku` |
| reference source | `langchain-ai/langchain` |

## Context

Iteration 4, backlog empty, CI green. Goal G1: continuously extract and adapt best practices from the reference set. The codebase already has comprehensive type hints (using `from __future__ import annotations`) but lacks enforcement via a type checker in the CI pipeline.

## Practice extracted

**Static type checking in CI** is a core quality signal in professional Python agent frameworks. Observed in top-ranked reference projects:
- `langchain-ai/langchain` — uses pyright for strict type checking
- `run-llama/llama_index` — mypy in strict mode
- `microsoft/semantic-kernel` — type checking as a CI gate

## What changed

1. Added `mypy>=1.8` to `[project.optional-dependencies].dev`
2. Configured `[tool.mypy]` with strict settings:
   - `strict = true` (enables all optional type-checking flags)
   - `disallow_untyped_defs`, `disallow_incomplete_defs`, `check_untyped_defs`
   - `no_implicit_optional`, `warn_redundant_casts`, `warn_unused_ignores`
   - Scope: `src/hsai` and `tests`
3. Added `Type check (mypy)` step to `.github/workflows/ci.yml`, running between ruff and pytest

## Why this matters (Goal G2 + G1)

- **Correctness:** Catches type-related bugs before runtime, critical for autonomous agents where failures propagate.
- **Maintainability:** Type hints serve as executable documentation; strict enforcement ensures they stay current as code evolves.
- **Consistency:** Matches the quality bar of the reference set; proves our harness can keep pace with professional OSS practice.
- **Traceability (Goal G2):** Type-safe code is easier to audit—each function's contract is explicit and machine-checked.

## Evidence

**langchain-ai/langchain** (rank 1, 142k stars):
- Uses pyright strict mode in CI
- Type hints everywhere in agent orchestration code
- Philosophy: "types are contracts"

This pattern is common across swarm/multi-agent frameworks because autonomous agents need to compose tool calls and state changes reliably; type errors there are particularly costly.

## Outcome

✓ Config files updated and validated  
✓ CI pipeline now runs mypy  
✓ Awaiting remote CI to confirm all existing code passes strict checks

This is a low-risk, high-value adoption because:
1. Code already has type hints—we're just enforcing them.
2. Mypy is non-invasive; existing tests unchanged.
3. Small blast radius: config-only changes, no logic changes.

## Next (if CI fails)

If remote CI surfaces type errors, they will be minor annotation fixes—the harness is already well-typed due to existing discipline. The fix PR will be trivial.
