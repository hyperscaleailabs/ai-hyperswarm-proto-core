---
tags:
  - reference
  - field-notes
repo: FoundationAgents/MetaGPT
stars: 69515
license: MIT
snapshot_date: 2026-07-25
---

# FoundationAgents/MetaGPT - field notes

> Part of [[Reference MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| repo | https://github.com/FoundationAgents/MetaGPT |
| stars | 69515 |
| license | MIT |
| snapshot | 2026-07-25 |

Append-only. Every mining pass adds a dated entry below; entries already here
are never rewritten, so "what did we know about this project in July?" stays
answerable. Each entry cites the artifact it came from and carries a stable
`practice_id` that tickets and lessons reference back.

## Observations

### 2026-08-14 - `foundationagents-metagpt--dated-durable-project-memory`
- **practice**: dated, durable project memory that later phases read as input
- **artifact**: `docs/NEWS.md`
- **what it does**: keeps a running, dated record of what changed and why, so the project's own history is a document that can be read rather than state that has to be reconstructed from commits.
- **why it matters here**: the reference set had no memory at all. Field notes are that record for the projects we study, and the `practice_id` is the key that makes an individual entry addressable from a ticket or a lesson.

### 2026-08-14 - `foundationagents-metagpt--phase-artifacts-as-handoff`
- **practice**: each role in the SOP emits a named artifact that the next phase consumes
- **artifact**: the role-based SOP artifacts described in `README.md` (PRD, design, task breakdown)
- **what it does**: a phase's output is a durable file with a known shape, so the following phase reads it instead of regenerating the same reasoning.
- **why it matters here**: synthesis previously regenerated its understanding of a project on every rotation. Reading the accumulated field notes first is the same handoff, applied across cycles rather than across roles.
