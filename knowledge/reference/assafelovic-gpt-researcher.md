---
tags:
  - reference
  - reference/assafelovic
repo: assafelovic/gpt-researcher
stars: 28641
license: Apache-2.0
snapshot_date: 2026-07-25
---

# assafelovic/gpt-researcher - field notes

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

### 2026-08-14 - `assafelovic-gpt-researcher-readme`
- artifact: `README`
- provenance: hand-recorded

The whole shape of the project is a planning-and-synthesis loop whose OUTPUT is
a durable, citation-bearing report written to disk - not an answer left in the
model's context. Findings are aggregated from many sources, each claim keeps its
source, and the report survives the run that produced it. The transferable rule:
if a loop's research is worth paying for, it is worth persisting with citations,
because the alternative is paying for it again next cycle.

### 2026-08-14 - `assafelovic-gpt-researcher-commits`
- artifact: `last 30 commit subjects`
- provenance: hand-recorded

Commit history shows report persistence and source-citation handling treated as
first-class, repeatedly maintained features rather than a debug convenience -
the durable artifact is the product, not a side effect of the run.
