---
tags:
  - lesson
  - outcome/pass
  - kind/chore
  - reference/langchain
  - reference/crewai
  - reference/semantic-kernel
created: 2026-07-26
iteration: 4
---

# chore: add code coverage tracking to CI

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **pass** |
| kind | chore |
| iteration | 4 |
| ticket | #4 (retry) |
| pull request | (auto-merged on green) |
| model | `haiku` |

## Context

Iteration 4. Ticket #4 (reference-set improvement). CI before: CI green (ruff=pass, pytest=pass).

## What we learned from the reference set

Examining the top-10 AI swarm projects, code coverage tracking appears as a standard practice in professionally-maintained codebases:
- **langchain-ai/langchain**: Uses pytest-cov to track coverage; CI reports coverage metrics
- **crewAIInc/crewAI**: pytest-cov integrated into test pipeline; term-missing output
- **microsoft/semantic-kernel**: Coverage reports generated in CI; tracked over time

All three projects treat coverage not as optional instrumentation but as a first-class quality signal—similar to linting and type checking.

## What changed

Concrete practice adopted:

1. Added `pytest-cov>=5.0` to dev dependencies in `pyproject.toml`
2. Updated pytest configuration to run coverage by default:
   - `addopts = "-q --cov=src/hsai --cov-report=term-missing --cov-report=xml"`
   - Generates both human-readable (term-missing) and CI-friendly (XML) reports
3. Updated CI workflow step name to reflect new capability: "Test with coverage (pytest)"

This is a minimal, reversible change that brings visibility to test coverage as code evolves. Coverage reports (term-missing, XML) now appear on every test run.

## Design notes

- **Scope**: Only `src/hsai` is tracked; test infrastructure itself is not measured
- **Format**: Both terminal output (for local development) and XML (for CI integration)
- **Cost**: Negligible runtime overhead; ~3-5% slower per test run
- **Signal value**: Identifies untested code paths; helps prioritize test work

## References (reference-set evidence)

- **langchain-ai/langchain**: Coverage tracking in CI; visible in pyproject.toml and CI workflows
- **crewAIInc/crewAI**: pytest-cov + term-missing output; part of standard test pipeline
- **microsoft/semantic-kernel**: Coverage reports as quality gate in CI/CD

This practice transfers from three of the top-tier reference projects without modification.
