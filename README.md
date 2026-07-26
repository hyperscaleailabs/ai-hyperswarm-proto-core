# ai-hyperswarm-proto-core

[![CI](https://github.com/hyperscaleailabs/ai-hyperswarm-proto-core/actions/workflows/ci.yml/badge.svg)](https://github.com/hyperscaleailabs/ai-hyperswarm-proto-core/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

A **self-improving prototype core for AI swarms**. It ships `hsai`, an autonomous
loop that continuously studies the best open-source multi-agent / swarm projects
and folds their proven practices back into itself - leaving behind, every
iteration, a merged & tested change *plus* a written lesson.

> Mission, vision, goals, and methodology are the source of truth in
> [`.ai-swarm/core.yaml`](.ai-swarm/core.yaml).

## How the loop works

```
        ┌──────────────────────────────────────────────────────────┐
        │  hsai loop  (each iteration = its own git worktree)        │
        └──────────────────────────────────────────────────────────┘
 1. sync main → run CI → check status
 2. CI red     → file a P0 ticket, self-assign, fix,  PR, merge on green
 3. CI green + backlog → take top-priority ticket, implement, PR, merge on green
 4. CI green + no backlog → explore, pick ONE improvement toward the goals
                            (learning from the top-10), FILE a ticket, implement…
```

Every PR is held to three **traceability invariants**:

| invariant | enforced by |
| --- | --- |
| linked to a ticket | `build_pr_body` refuses to build a body without one |
| records the model used | model + tier + selection rationale in the PR body |
| carries a lesson learned (pass **or** fail) | written to `knowledge/lessons/` every iteration |

## Subscription-only, no metered API

The loop drives **Claude Code headless (`claude -p`)**, so all model usage runs
against your Claude subscription quota. `hsai` strips `ANTHROPIC_API_KEY` from the
child environment (and refuses to run if it cannot guarantee that), so the
metered API is never touched. See `hsai doctor`.

## Model-size selection (a learnable skill)

The orchestrator picks a model *tier* per task - `light` (haiku) / `standard`
(sonnet) / `heavy` (opus) - from complexity signals, and records the choice on
the PR. Improving this heuristic is itself tracked as a backlog skill.

## The knowledge base is an Obsidian vault

Clone the repo and **open the folder as an Obsidian vault** - the committed
`.obsidian/` config and `[[wikilinks]]` give you a working graph immediately.

```
knowledge/
├── MOCs/          # Maps of Content: Knowledge Base / Lessons / Whitepapers
├── lessons/       # one article per iteration (pass or fail)
├── whitepapers/   # periodic syntheses (every N lessons)
└── templates/     # note templates
```

`hsai reindex` rebuilds the MOCs from what is on disk.

## Quickstart

```bash
pip install -e ".[dev]"
hsai status        # config + invariants
hsai doctor        # verify subscription-only guard + environment
hsai loop --dry-run   # a full iteration with no side effects
hsai loop          # one real iteration (opens & merges a PR on green)
hsai loop --max-parallel 3 -n 1   # ramp to the swarm (after proving one iteration)
```

## Learning targets (top-10, pinned snapshot)

Ranked by stars, weighted to swarm/multi-agent relevance; all ≥10k stars and
MIT/Apache-2.0. Full list with notes lives in `core.yaml`.

langchain · MetaGPT · crewAI · llama_index · ChatDev · gpt-researcher ·
semantic-kernel · JARVIS · **openai/swarm** · **SWE-agent**

## Development

```bash
ruff check .
pytest
```

## License

[Apache-2.0](LICENSE).
