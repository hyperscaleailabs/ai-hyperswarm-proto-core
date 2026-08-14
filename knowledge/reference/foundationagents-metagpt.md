---
tags:
  - reference
  - reference/foundationagents
repo: FoundationAgents/MetaGPT
stars: 69515
license: MIT
snapshot_date: 2026-07-25
---

# FoundationAgents/MetaGPT - field notes

> Part of [[Reference MOC]] - [[Knowledge Base MOC]]

Durable field notes on a reference-set project. **Append-only**: every mining
pass adds dated observations below and rewrites nothing above them, so "what
did we learn from this project, and when" stays answerable. Each observation
cites the artifact it came from and carries a stable `practice_id` that tickets
and lessons refer back to.

Entries below carry `provenance: hand-recorded` instead of a content digest:
they were written by the architect when the field-note mechanism shipped, from
artifacts read directly. Machine-mined entries carry a `digest` instead, which
is what makes re-mining idempotent.

## Observations

### 2026-08-14 - `foundationagents-metagpt-news-md`
- artifact: `docs/NEWS.md`
- provenance: hand-recorded

Dated, durable project memory kept as a committed document that later work reads
as INPUT rather than regenerating. Each entry is stamped and appended, so the
project's own history is queryable by date instead of being reconstructed from
commit archaeology.

### 2026-08-14 - `foundationagents-metagpt-readme`
- artifact: `README`
- provenance: hand-recorded

Role-based SOP artifacts: each role in the simulated software company emits a
named, inspectable work product (requirement doc, design, task list) that the
next phase consumes. Phase boundaries are made of artifacts, not of prompt
state - which is exactly what makes a multi-phase loop auditable.
