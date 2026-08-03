---
tags:
  - lesson
  - outcome/pass
  - kind/improve
  - reference-set-practice
created: 2026-08-03
iteration: 1001
---

# improve: structured source citations with GitHub links

> Part of [[Lessons MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| outcome | **pass** |
| kind | improve |
| iteration | 1001 |
| ticket | _(self-improve)_ |
| pull request | _(merged inline)_ |
| model | `haiku` |

## Context

Self-improvement task toward G2 (Stay auditable and traceable end to end) and G3 (Grow the knowledge base). Observed practice from reference set: gpt-researcher structures source citations with explicit evidence links. Instead of plain text repository names, it includes clickable references so readers can verify the source of a practice without searching manually.

## What happened

Added `format_reference()` helper function to `knowledge.py` that converts repository slugs (e.g., "langchain-ai/langchain") into markdown links to the GitHub repository. The function:

- Takes a repo slug and converts it to a GitHub markdown link
- Handles edge cases: strips whitespace, validates the slash separator
- Falls back to backtick formatting for invalid inputs

Updated `_render_lesson()` to use the new formatter when rendering reference section, so all lessons now include clickable links to the projects they cite.

Added unit tests:
- `test_format_reference()`: validates the conversion logic for valid/invalid inputs
- Updated `test_write_lesson_and_reindex()`: verifies that rendered lessons include the formatted link

CI passes: ruff=pass, pytest=pass (including new tests).

## Lesson learned

**Structured citations make the knowledge base auditable.** Adopted from assafelovic/gpt-researcher: when citing evidence from reference projects, include direct links so readers can quickly verify the source and understand the practice in context. This transforms vague "inspired by X" into traceable "see Y at Z" references.

The practice is small but high-leverage: every lesson's reference section now includes clickable GitHub links, making the knowledge base immediately verifiable and supporting both auditability (G2) and knowledge growth (G3).

## References (reference-set evidence)

- [assafelovic/gpt-researcher](https://github.com/assafelovic/gpt-researcher) - autonomous research agent with structured source citation and evidence collection patterns; each reasoning step includes explicit evidence links
