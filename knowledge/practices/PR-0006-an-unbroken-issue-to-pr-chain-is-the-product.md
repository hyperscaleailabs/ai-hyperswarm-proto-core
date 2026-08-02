---
tags:
  - practice
  - source/swe-agent-swe-agent
id: PR-0006
source_repo: SWE-agent/SWE-agent
artifact_kind: readme
artifact_ref: README.md
observed_on: 2026-07-25
---

# An unbroken issue-to-PR chain is the product

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source | `SWE-agent/SWE-agent` |
| artifact | [readme: `README.md`](https://github.com/SWE-agent/SWE-agent/blob/HEAD/README.md) |
| observed | 2026-07-25 |

## What it does
SWE-agent frames its whole value proposition as turning a GitHub issue into a
pull request automatically. The issue is not context that gets dropped once
work starts - it is one end of a chain that terminates in a reviewable diff,
and every run is legible because both ends are addressable artifacts.

## Why it applies to hsai
This is the shape of the hsai loop itself, so the practice shows up as an
invariant rather than a module: `require_ticket_per_pr` in
`.ai-swarm/core.yaml`, `build_pr_body()` raising when no ticket is linked, and
the self-improve path *filing* a ticket before it implements anything. This
card exists so that the invariant has a citeable source instead of being folk
knowledge.

## Cited by
- _(not yet cited by a lesson)_
