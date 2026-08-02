---
tags:
  - practice
  - source/openbmb-chatdev
id: PR-0005
source_repo: OpenBMB/ChatDev
artifact_kind: readme
artifact_ref: README.md
observed_on: 2026-07-26
---

# Run the agent crew on a cheaper model when the work allows

> Part of [[Practices MOC]] - [[Knowledge Base MOC]]

| field | value |
| --- | --- |
| source | `OpenBMB/ChatDev` |
| artifact | [readme: `README.md`](https://github.com/OpenBMB/ChatDev/blob/HEAD/README.md) |
| observed | 2026-07-26 |

## What it does
ChatDev exposes the backing model as a run-time switch on its entry point, so
the same multi-agent software pipeline can be driven on a cheap model or an
expensive one without editing the pipeline. Cost is a dial the operator turns
per run, not a property of the architecture.

## Why it applies to hsai
A block that is burning quota should keep progressing on a cheaper tier rather
than stop. Adopted as the `demote_tier` path: on a *soft* budget breach the
block gate biases `models.select()` one tier cheaper for the next iteration,
and only a hard breach halts NEW work. The demotion is recorded in the ledger
so its effect on outcomes stays measurable.

## Cited by
- _(not yet cited by a lesson)_
