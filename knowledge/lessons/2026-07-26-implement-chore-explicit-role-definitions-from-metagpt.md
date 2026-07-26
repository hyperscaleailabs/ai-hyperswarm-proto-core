---
tags:
  - lesson
  - outcome/pass
  - kind/implement
  - reference/metagpt
created: 2026-07-26
iteration: 4
---

# implement: chore: explicit role definitions (learned from MetaGPT)

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **pass** |
| kind | implement |
| iteration | 4 |
| ticket | #4 (retry) |
| pull request | _(generated on merge)_ |
| model | `haiku` |

## Context

Iteration 4. Ticket #4 (retry). Revisit the reference set and extract a concrete practice.
Previous attempt (iter 3) timed out. This iteration adopts **explicit role definitions** 
inspired by `FoundationAgents/MetaGPT`.

## What happened

Model `haiku` (light) ran the task. Agent ok=True. CI after: CI green (ruff=pass, pytest=pass).

Made the loop's implicit roles explicit by:
1. Created `Role` dataclass with name, description, responsibility, exit_artifact fields
2. Defined three concrete roles: Healer (HEAL), Engineer (IMPLEMENT), Researcher (IMPROVE)
3. Added `get_role(kind)` function to retrieve role metadata
4. Updated PR body to include role information (name + responsibility)
5. Added tests to verify role definitions and behavior

## Lesson learned

Making implicit roles explicit improves observability and code clarity. Each branch of 
the loop (heal/implement/improve) now has a first-class Role object that describes:
- What the role does (description)
- What it's responsible for (responsibility)  
- What artifact it produces (exit_artifact)

This mirrors the role-based agent pattern from MetaGPT, where agents have clear 
responsibilities and hand-off artifacts. Future PRs will show which role executed 
them, making the loop's structure more transparent.

## References (reference-set evidence)
- `FoundationAgents/MetaGPT` - role-based agents with explicit hand-off artifacts

## Diff summary
- src/hsai/orchestrator.py: Added Role dataclass, defined _ROLES dict, added get_role()
- src/hsai/orchestrator.py: Updated build_pr_body() to accept and include role in output
- tests/test_orchestrator.py: Added tests for role retrieval and role inclusion in PR body
