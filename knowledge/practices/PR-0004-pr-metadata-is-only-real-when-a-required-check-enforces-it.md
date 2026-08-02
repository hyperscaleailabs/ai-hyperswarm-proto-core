---
tags:
  - practice
  - source/microsoft-semantic-kernel
id: PR-0004
source_repo: microsoft/semantic-kernel
artifact_kind: ci
artifact_ref: .github/workflows/merge-gatekeeper.yml
observed_on: 2026-07-26
---

# PR metadata is only real when a required check enforces it

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source | `microsoft/semantic-kernel` |
| artifact | [ci: `.github/workflows/merge-gatekeeper.yml`](https://github.com/microsoft/semantic-kernel/blob/HEAD/.github/workflows/merge-gatekeeper.yml) |
| observed | 2026-07-26 |

## What it does
semantic-kernel does not ask contributors to remember its PR conventions - it
mechanises them. A gatekeeper workflow blocks a merge until every other check
has concluded, alongside label/title-prefix workflows that stamp and verify PR
metadata. Convention that is not enforced by a required check is documentation,
not policy.

## Why it applies to hsai
hsai's invariants (a `Closes #N` ticket link, a recorded model, a lesson, cited
evidence) are asserted by the code that writes the PR body - which is exactly
the code that could drift. Adopted as the `SDLC evidence (PR body)` step in
`.github/workflows/ci.yml`, now extended with `hsai evidence-check`: a code PR
whose `## Reference-set evidence` section is empty, or names a repo outside the
pinned set, fails CI and cannot merge.

## Cited by
- _(not yet cited by a lesson)_
