---
tags:
  - reference
  - field-notes
repo: run-llama/llama_index
stars: 51099
license: MIT
snapshot_date: 2026-07-25
---

# run-llama/llama_index - field notes

> Part of [[Reference MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| repo | https://github.com/run-llama/llama_index |
| stars | 51099 |
| license | MIT |
| snapshot | 2026-07-25 |

Append-only. Every mining pass adds a dated entry below; entries already here
are never rewritten, so "what did we know about this project in July?" stays
answerable. Each entry cites the artifact it came from and carries a stable
`practice_id` that tickets and lessons reference back.

## Observations

### 2026-08-14 - `run-llama-llama-index--automated-inbound-triage`
- **practice**: automated triage of inbound work before it reaches a human reviewer
- **artifact**: `.github/workflows/issue_classifier.yml` and `.github/workflows/close_new_integration_prs.yml`
- **what it does**: inbound issues are classified and labelled automatically, and a whole class of out-of-scope pull requests is closed by policy rather than by a maintainer reading each one.
- **why it matters here**: the synthesizer's output *is* this repo's inbound work queue, and until now nothing rejected duplicate or out-of-scope proposals before they consumed a backlog slot. The dedupe gate in `synthesis.screen_specs` applies the same discipline to ideas the loop files against itself.

### 2026-08-14 - `run-llama-llama-index--refusal-states-the-reason`
- **practice**: an automated rejection explains itself in the same place it happens
- **artifact**: `.github/workflows/close_new_integration_prs.yml` (the closing comment it posts)
- **what it does**: work closed by automation carries a written reason, so a contributor whose PR was wrongly rejected can see why and argue with it.
- **why it matters here**: a silent filter on our own proposals would be unauditable. `SynthesisResult.refused` records a reason per refusal and `governance.render_brief` renders them, so a wrongly-suppressed idea is visible to the architect.
