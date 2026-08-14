---
tags:
  - reference
  - field-notes
repo: crewAIInc/crewAI
stars: 56129
license: MIT
snapshot_date: 2026-07-25
---

# crewAIInc/crewAI - field notes

> Part of [[Reference MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| repo | https://github.com/crewAIInc/crewAI |
| stars | 56129 |
| license | MIT |
| snapshot | 2026-07-25 |

Append-only. Every mining pass adds a dated entry below; entries already here
are never rewritten, so "what did we know about this project in July?" stays
answerable. Each entry cites the artifact it came from and carries a stable
`practice_id` that tickets and lessons reference back.

## Observations

### 2026-08-14 - `crewaiinc-crewai--dated-snapshot-commits`
- **practice**: knowledge is frozen and dated at a point in time, not continuously overwritten
- **artifact**: the `[docs-freeze]` snapshot commits in the repository's commit history
- **what it does**: documentation state is captured as an explicit, dated snapshot commit rather than being edited in place, so an earlier state of the docs remains recoverable and citable.
- **why it matters here**: this is precisely what makes "what did we already learn from crewAI in July?" an answerable question. `KnowledgeBase.append_field_note` uses the existing file verbatim as the prefix of the new content, so a mining pass can only ever add a dated entry - it can never rewrite one.
