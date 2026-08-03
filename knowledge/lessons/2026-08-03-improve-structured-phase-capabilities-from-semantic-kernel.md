---
tags:
  - lesson
  - outcome/pass
  - kind/improve
  - reference-set-practice
created: 2026-08-03
---

# improve: structured phase capabilities - adopted from semantic-kernel

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **pass** |
| kind | improve |
| ticket | _(self-improve)_ |
| pull request | _(merged inline)_ |
| model | `haiku` |

## Context

Self-improvement task toward G1 (Learn from the top-10). Observed practice from reference set: microsoft/semantic-kernel defines *capabilities* for plugins/skills - what each component is designed to accomplish. This declarative approach separates *intent* (what a role/phase *can* do) from *artifacts* (what it *produces*).

Refreshed reference-set snapshot from 2026-07-25 to 2026-08-03 to keep the pin current. Then extracted semantic-kernel's structured capability model.

## What happened

Added `_phase_capabilities()` helper to orchestrator.py declaring what each phase (heal / implement / improve) is competent to accomplish:

- **HEAL**: Diagnose CI failure root causes, write regression tests, apply minimal fixes, verify resolution
- **IMPLEMENT**: Implement features end-to-end, write comprehensive tests, refactor safely, validate code quality
- **IMPROVE**: Study reference-set projects, extract concrete practices, implement with evidence, document lessons

Integrated into `build_pr_body()` so every PR now includes a "## Phase capabilities" section *before* the artifacts section, making the phase's role and scope visible upfront.

Added comprehensive unit tests to verify:
- Each phase returns expected capability descriptions
- Capabilities appear in PR bodies when kind is provided
- Capabilities are omitted when kind is empty
- Capabilities precede artifacts in the PR body structure

Also updated core.yaml snapshot_date from 2026-07-25 to 2026-08-03.

CI passes: ruff, pytest (including 7 new tests for capabilities).

## Lesson learned

**Declaring phase capabilities separates intent from outcome.** Adopted from microsoft/semantic-kernel: capability declarations (what a role *can do*) make orchestration more composable and debuggable than just listing artifacts (what it *did*).

This supports G1 by showing how semantic-kernel uses typed capabilities as first-class abstractions. In hsai's context, phase capabilities make the loop's structure self-documenting: a reader of a PR can see immediately what the HEAL phase promises to deliver vs what it actually produced.

The pattern is small but high-leverage for observability: phase capabilities are a bridge between role-based orchestration (from semantic-kernel, crewAI) and outcome verification (artifacts). They also support future work like capability-based scheduling (assign tickets that match a phase's declared strengths).

## References (reference-set evidence)

- `microsoft/semantic-kernel` - agent orchestration framework where plugins/skills are first-class objects with explicit capability definitions; each skill declares what it can do (compute, retrieve, transform) before being invoked
