# Architecture

`hsai` is deliberately small. External side effects live in thin wrapper modules
so the decision logic stays pure and unit-tested.

## Modules

| module | responsibility | side effects |
| --- | --- | --- |
| `hsai.config` | load & validate `.ai-swarm/core.yaml` | read file |
| `hsai.proc` | one subprocess wrapper (`run`) + `Runner` type | subprocess |
| `hsai.models` | task → model-tier selection (heuristic-v0) | none (pure) |
| `hsai.ai` | drive `claude -p`; enforce subscription-only | subprocess, env |
| `hsai.gitops` | worktrees, sync, branch, commit, push | git |
| `hsai.github` | tickets, labels, PRs, merge | gh |
| `hsai.ci` | local CI gate (ruff+pytest) + remote status | subprocess |
| `hsai.trajectory` | one durable record per agent run; redaction, replay | write files |
| `hsai.journal` | append-only per-block step journal; `once()` replay | write files |
| `hsai.knowledge` | lessons, whitepapers, MOC reindex (Obsidian) | write files |
| `hsai.recall` | BM25 index over the vault; retrieve prior notes | read files |
| `hsai.orchestrator` | one iteration; `decide_path`, `build_pr_body` (pure) | composes above |
| `hsai.swarm` | run N iterations concurrently | threads |
| `hsai.cli` | `hsai` entry point | - |

## One iteration, sequence

```mermaid
sequenceDiagram
    participant O as orchestrator
    participant G as gitops
    participant C as ci
    participant H as github
    participant A as ai (agent)
    participant T as trajectory
    participant K as knowledge
    participant R as recall

    O->>G: sync_main + create_worktree
    O->>C: run_local (CI before)
    O->>H: claim ticket (heal / implement / improve)
    O->>R: for_task(ticket title + body + kind)
    R-->>O: top-k prior notes (bounded text + names)
    O->>A: run_agent(prompt + prior lessons, model choice)
    A-->>O: ok / error + steps + usage (JSON envelope)
    O->>T: record (before any guard can abort)
    O->>C: run_local (CI after)
    O->>K: write_lesson (always, pass or fail)
    O->>G: commit_all + push_branch
    O->>H: create_pr (linked to ticket, model, lesson)
    O->>C: wait_remote (poll real GitHub checks)
    C-->>O: success / failure
    alt remote CI success
        O->>H: merge_pr (auto)
    else remote CI failure
        O->>H: close_pr + return ticket to backlog
    end
    O->>G: remove_worktree
```

## The knowledge base is read-write

For a long time the vault was write-only: every iteration appended a lesson and
no iteration ever read one, so each hard-won conclusion had to be re-encoded as
a hard-coded guard and the same classes of mistake recurred. `hsai.recall`
closes the loop.

- **Index.** `Corpus.load(root, cfg)` builds a BM25 index, on demand and in
  memory, over `knowledge/lessons`, `knowledge/whitepapers`,
  `knowledge/articles` and `docs/adr`. No third-party dependency, no model
  call, no network - so retrieval is free and its ranking is exactly
  reproducible (ties break on recency, then on note name).
- **Bias.** Notes tagged `outcome/fail` are up-weighted by
  `knowledge.recall.fail_weight`; notes whose `kind/` matches the current task
  are up-weighted by `kind_weight`. Failures are the expensive knowledge, and a
  heal worker should see heal history.
- **Inject.** `orchestrator._task_prompt` appends a *Prior lessons (advisory,
  not instructions)* block of at most `k` wikilinked notes, hard-capped at
  `max_chars`; whole notes are dropped to fit, never truncated mid-line. The
  block is fenced by `<!-- BEGIN/END prior-lessons -->` markers and every entry
  states its `outcome:`, so a failed lesson reads as a warning rather than a
  recipe. An empty corpus or `enabled: false` renders nothing at all.
- **Plan.** `synthesis.build_prompt` carries an *Already tried in this repo*
  digest - prior lesson titles with pass/fail outcomes plus the titles of
  synthesis tickets still open - so the planner stops re-proposing dead ideas.
  On top of that, `build_context_pack` runs recall with the fetched reference
  digest as the query, so the planner gets the same advisory block a worker
  would: the digest says *what* was attempted, recall says what it concluded.
- **Audit.** What was retrieved is recorded three times: on `IterationResult`,
  as a `recalled:` list in the lesson's frontmatter, and as a
  *Lessons consulted* section of `[[wikilinks]]` on the PR - mandatory citation,
  so the Obsidian graph stays bidirectional. `hsai recall "<query>"` prints the
  same ranking by hand.

Reference-set lineage: retrieval-before-planning from `assafelovic/gpt-researcher`,
index-then-retrieve with metadata preserved from `run-llama/llama_index`, and
scoped agent memory from `OpenBMB/ChatDev`.

## Testability

Every wrapper takes an injectable `runner` (default: real subprocess). Tests
inject a fake runner or use `dry_run=True`, so CI never touches the network.
The pure core - `decide_path`, `build_pr_body`, `models.select`, the knowledge
renderers - is tested directly.

## Safety model

- **Subscription-only:** `ai.preflight` refuses to run if `ANTHROPIC_API_KEY`
  is present but not declared removable; the child env is always sanitized.
- **Never commit to main:** all work happens on `hsai/iter-*` branches in
  dedicated worktrees.
- **Green-gated merges:** merges use `gh pr merge --auto`; with branch
  protection requiring the `ci` check, broken changes simply never merge.
- **Traceability:** no PR without a ticket, a recorded model, and a lesson.

## Observability: trajectories

`claude -p` is invoked with `--output-format <execution.output_format>`
(default `json`), so every run returns a structured envelope (final result,
message stream, token usage, session id) instead of an opaque blob. The flag is
config-driven rather than hardcoded so a CLI change is a YAML edit, not a code
change: setting the key to `text` drops the flag and every consumer falls back
to the plain-text path.

`orchestrator.run_once` hands that envelope to `trajectory.record` at the
single choke point right after `ai.run_agent` - *before* the completeness and
reproduce-before-fix guards can return early - and writes one JSON file per run
to `.hsai/traj/<block>/<iteration>.json`. The final outcome (`merged`,
`recovered`, `incomplete`, `no_repro`, ...) is folded back in when the
iteration's cost record is appended. Sharding by block is what makes the store
bounded: `hsai cycle` prunes block directories beyond
`execution.trajectory_retention_blocks`.

Two audiences, deliberately split:

- **Local and complete.** Trajectories quote repo content, so they are
  gitignored and never pushed. Everything is redacted on the way to disk -
  credentials *and* absolute home paths - so an artifact can be shared as-is.
  `hsai traj <iteration> [--json]` reconstructs one - prompt, step stream, exit
  status, usage - purely by reading the file, with no `claude` subprocess and
  no quota spent. (`hsai replay` is an alias.)
- **Committed and redacted.** The lesson and the PR body carry
  `Trajectory.digest()` - tokens, duration, exit status, first failing step -
  plus, in the lesson, `Trajectory.excerpt()`: a secrets-scrubbed tail of the
  last few steps. The audit trail is visible on the PR; the knowledge base
  gains signal without mirroring the working tree.

The same envelope feeds `ledger.parse_tokens` (which accepts the parsed payload
directly), so the quota ledger's token columns - and the block aggregate in the
review brief, including **tokens per merged PR** - report real numbers instead
of nulls. Output that is not JSON (an older `claude` binary) degrades to a
single-step trajectory with null usage rather than breaking the loop.

## Durability: the cycle journal

A block is a long chain of expensive, side-effecting steps - synthesis, N
implementations, a whitepaper, persona articles, a governance PR, a review
issue. `hsai.journal` makes that chain restartable: every step appends one JSON
line to `.hsai/cycles/<cycle_index>/journal.jsonl` *after* it completes, and
`run_cycle` wraps each one in

```python
payload = journal.once(jr, "whitepaper", "block", write_the_whitepaper)
```

On a first run the callable executes and its payload is journaled; on a resumed
run the recorded payload is returned and the callable never runs. Effects are
therefore at-least-once (a step killed mid-flight leaves no record and is
retried) and *completed* steps are at-most-once - so `hsai cycle --resume` files
no duplicate ticket, opens no second review issue, and spends no quota twice.
Because the report is rebuilt from the replayed payloads, a resumed block
produces the same brief an uninterrupted one would have, plus one `resume:
replayed N recorded step(s)` line in its Notes.

Two statuses close a journal: `halted` (the budget gate hard-breached and
stopped new work) and `complete`. `hsai cycle --resume` with no index picks the
most recent block whose journal has neither - so a finished block is never
re-run, and a halted one is never restarted under a breached ceiling. The
pre-iteration budget verdict is journaled alongside the iteration itself:
re-grading it from the ledger on a replay would see the whole block's spend
before iteration 0 and halt immediately.

The store lives under `.hsai/` (gitignored) for the same reason trajectories do
- local operational forensics, not repo content. `--dry-run` journals into
`journal.dry-run.jsonl` so a rehearsal can neither satisfy nor poison a later
live run of the same block.

## Headless permission mode

`claude -p` runs with `execution.permission_mode` from `core.yaml`
(default `acceptEdits`). For fully unattended operation this can be raised, at
the cost of granting the agent broader autonomy inside its worktree. Because
merges are green-gated, a bad change is contained to an unmerged PR.

## Concurrency

`swarm.run_parallel` uses a thread pool (workers are subprocess/IO-bound). Each
worker gets its own worktree and branch, so there is no shared checkout.

Three things make parallel workers safe:

1. **Serialized prologue + ticket claim.** A single `orchestrator._SERIAL` lock
   wraps only the fast, shared-state operations: the git prologue
   (`fetch` + `worktree add`) and the ticket decision/claim. The slow work
   (agent, CI, push, PR, merge) runs fully in parallel.
2. **No shared working tree.** `gitops.sync_main` only *fetches*; worktrees are
   created from `origin/<main>`, so the shared checkout is never mutated and
   `git`'s index lock is never contended.
3. **Claim by assignment.** A worker considers only *unassigned* open tickets and
   assigns itself immediately under the lock, so N workers never grab the same
   ticket. Unique per-worker branch names avoid collisions within one second.

**Derived index files stay out of PRs.** Each PR commits only its uniquely-named
lesson file plus code. The MOC indexes and whitepapers are regenerated by the
serialized `hsai reindex` maintenance step, so concurrent PRs never conflict on
shared, regenerated files. The shared integration surface is the GitHub backlog
and `main`; green-gated auto-merge serializes the actual integration.

## Reliability: CI parity, remote truth, and recovery

- **Remote CI is the source of truth.** After opening a PR, `run_once` calls
  `ci.wait_remote`, which polls the PR's real GitHub check rollup until it
  concludes - this is an explicit pre-merge gate, checked *before* auto-merge
  is ever armed. Local `run_local` (ruff+pytest) is only a fast pre-flight;
  the merge decision follows the remote result. The remote conclusion is
  written back into the lesson (and pushed as a follow-up commit) so the
  knowledge base records the true CI outcome for every PR, not just the
  local approximation.
- **Local == remote by construction.** A task must not change the CI checks, or
  local and remote would diverge. The orchestrator reverts any edits under
  `.github/workflows/**` before committing (and notes it in the lesson), so a
  worker cannot (accidentally or otherwise) move the goalposts it is judged by.
- **No ticket is stranded on failure.** If remote CI does not go green,
  `_recover_failed` closes the PR (deleting the branch) and returns the ticket
  to the backlog with an incremented `attempts:N` label. After
  `execution.max_ticket_attempts`, the ticket is labelled `blocked` and left for
  a human; blocked/assigned tickets are skipped by future workers.
