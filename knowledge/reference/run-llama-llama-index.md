---
tags:
  - reference
  - reference/run-llama
repo: run-llama/llama_index
stars: 51099
license: MIT
snapshot_date: 2026-07-25
---

# run-llama/llama_index - field notes

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

### 2026-08-14 - `run-llama-llama-index-workflow-issue-classifier-yml`
- artifact: `.github/workflows/issue_classifier.yml`
- provenance: hand-recorded

A workflow classifies every inbound issue automatically before a human reads it,
so triage cost does not scale with inbound volume. The classification lands as
labels, which makes the triage decision itself auditable after the fact.

### 2026-08-14 - `run-llama-llama-index-workflow-close-new-integration-prs-yml`
- artifact: `.github/workflows/close_new_integration_prs.yml`
- provenance: hand-recorded

A workflow closes a whole CLASS of inbound PRs (new integrations) on arrival,
with an explanation, rather than letting out-of-scope work accumulate in review.
The lesson is that a maintainer's scarcest resource is attention, and the
cheapest defence is a mechanical gate that rejects at the door and says why.

### 2026-08-14 - `run-llama-llama-index-prs`
- artifact: `last 20 closed PR titles + labels`
- provenance: hand-recorded

Closed PRs carry classification labels applied by automation rather than by
hand, so the closed-PR stream doubles as a machine-readable record of what the
project accepts and what it turns away.
