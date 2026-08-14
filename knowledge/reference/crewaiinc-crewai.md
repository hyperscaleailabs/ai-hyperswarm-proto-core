---
tags:
  - reference
  - reference/crewaiinc
repo: crewAIInc/crewAI
stars: 56129
license: MIT
snapshot_date: 2026-07-25
---

# crewAIInc/crewAI - field notes

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

### 2026-08-14 - `crewaiinc-crewai-commits`
- artifact: `last 30 commit subjects`
- provenance: hand-recorded

`[docs-freeze]` snapshot commits: documentation is frozen and dated at a point
in time and committed as its own artifact, rather than continuously overwritten
in place. The discipline that makes "what did the docs say in July" answerable
at all is that the snapshot is append-only and carries its date in the commit
subject.

### 2026-08-14 - `crewaiinc-crewai-readme`
- artifact: `README`
- provenance: hand-recorded

Role-playing crews are assembled from declarative role/goal/backstory
definitions kept beside the code, so the composition of a run is a reviewable
artifact rather than an argument list buried in a call site.
