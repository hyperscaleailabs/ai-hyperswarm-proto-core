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

## The knowledge base is an Obsidian vault - and an input, not just an output

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

**The loop reads the vault back.** Before an agent starts, `hsai.recall` builds
a BM25 index over `knowledge/lessons`, `knowledge/whitepapers` and `docs/adr`
and injects the most relevant prior notes into the worker's prompt - failures
first, and biased toward notes whose `kind/` matches the task at hand. The
planner gets the same treatment: an *Already tried in this repo* digest so it
stops re-proposing ideas that were tried and failed. Retrieval is
deterministic, costs no quota, and adds no dependency; what it returned is
recorded in the lesson's `recalled:` frontmatter and on the PR, so it stays
auditable. Tune it under `knowledge.recall` in `.ai-swarm/core.yaml`
(`enabled`, `k`, `max_chars`, `fail_weight`, `kind_weight`), or set
`enabled: false` to restore the previous prompt exactly.

```bash
hsai recall "remote CI gate"       # what would a worker be shown for this task?
```

**Nothing merges on the author's word alone.** Once local CI is green and the
work is committed, `hsai.review` hands the branch diff, the ticket and its
parsed acceptance criteria to a model on a *different tier* than the one that
wrote it, and requires a fenced JSON verdict back. Output it cannot parse is
fail-closed (a non-approval). A blocking verdict opens no PR at all: the ticket
goes back through the ordinary retry policy (`attempts:N`, then `blocked`).
Every review is metered in the quota ledger as `kind='review'`, and a hard
budget breach skips the gate rather than stalling the block. The verdict is
recorded verbatim under `## Independent review` on the PR and in the lesson, so
the vault records who *checked* the work, not only who wrote it. Tune it under
`review` in `.ai-swarm/core.yaml` (`enabled`, `tier_policy`,
`max_blocking_findings`, `max_diff_chars`, `timeout_seconds`).

## Quickstart

```bash
pip install -e ".[dev]"
hsai status        # config + invariants
hsai doctor        # verify subscription-only guard + environment
hsai loop --dry-run   # a full iteration with no side effects
hsai loop          # one real iteration (opens & merges a PR on green)
hsai loop --max-parallel 3 -n 1   # ramp to the swarm (after proving one iteration)
hsai traj 12       # print what agent run (iteration) 12 did (spends no quota)
hsai recall "knowledge-only diff on a code ticket"   # rank prior lessons for a task
hsai cycle --resume   # finish an interrupted governance block, replaying what completed
hsai journal 41353    # per-stage timing breakdown for a block (spends no quota)
```

Every agent run persists a **trajectory** - prompt, step stream, exit status,
token usage, session id - to `.hsai/traj/<block>/<iteration>.json`. Those files
quote repo content, so they stay local and gitignored, and everything written
is scrubbed first: credentials and absolute home paths never reach disk. The
committed lesson and the PR body carry only a digest line (tokens, duration,
exit status) plus a redacted tail. `hsai traj <iteration> [--json]` reads one
back without invoking `claude`; older blocks are pruned per
`execution.trajectory_retention_blocks`.

## Learning targets (top-10, pinned snapshot)

Ranked by stars, weighted to swarm/multi-agent relevance; all ≥10k stars and
MIT/Apache-2.0. Full list with notes lives in `core.yaml`.

langchain · MetaGPT · crewAI · llama_index · ChatDev · gpt-researcher ·
semantic-kernel · JARVIS · **openai/swarm** · **SWE-agent**

## Governance: three streams, one architect

The loop is governed, not just autonomous - see [docs/SDLC.md](docs/SDLC.md) and
[ADR-0001](docs/adr/0001-two-phase-engine-and-governance-rhythm.md):

1. **Steering** - `governance/DIRECTION.md` (Now / Issues Map / Direction) is the
   single-entrance steering doc. Each block ends with a review issue; the
   architect runs `/review-next` twice daily - feedback becomes ADRs in
   `docs/adr/` plus refined tickets, closed with a merged PR.
2. **Quality** - a five-phase SDLC (Plan → Implement → Verify → QA → Integrate),
   each phase leaving CI-checked evidence. Vague tickets are refused
   (`needs-refinement`); code tickets cannot merge with code-free diffs.
3. **Scheduled cycles** - `hsai cycle` runs the two-phase engine: heavy-model
   **synthesis** (combining practices from >= 3 reference projects, with an
   explicit reflection pass) files substantial tickets; cheaper agents implement
   them in a sequential block; the block ships a whitepaper + persona articles
   (CTO / architect / DevOps) + a refreshed DIRECTION.md. Install the
   twice-daily schedule with `scripts/install_cycles_launchd.sh`.
   Every block step is journaled to `.hsai/cycles/<index>/journal.jsonl` as it
   completes, so a crash or a machine sleep is recoverable: `hsai cycle --resume`
   replays what already finished (no re-filed tickets, no second review issue,
   no quota spent twice) and only re-runs what did not. A second, independent
   journal - one timing event per stage transition (agent run, guards, local
   and remote CI, merge; and, per block, synthesis/implementation/whitepaper/
   brief) - lands in `.hsai/journal/<index>.jsonl`; `hsai journal <index>`
   renders the per-stage breakdown, and the review brief's own slowest-stage
   line is folded from the same events.

Whitepapers are ingested by [agentic-atlas](https://github.com/hyperscaleailabs/agentic-atlas)
via its own pipeline (pull-based publishing).

## Development

```bash
ruff check .
pytest
```

## License

[Apache-2.0](LICENSE).
