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
| `hsai.trajectory` | one durable record per agent run *and* per iteration; redaction, replay | write files |
| `hsai.bench` | offline replay of a scenario corpus through the decision code | none (fake runner) |
| `hsai.journal` | append-only per-block step journal; `once()` replay | write files |
| `hsai.knowledge` | lessons, whitepapers, MOC reindex (Obsidian) | write files |
| `hsai.recall` | BM25 index over the vault; retrieve prior notes | read files |
| `hsai.review` | independent, different-tier review of the branch diff | subprocess |
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
    participant V as review

    O->>G: sync_main + create_worktree
    O->>C: run_local (CI before)
    O->>H: claim ticket (heal / implement / improve)
    O->>R: for_task(ticket title + body + kind)
    R-->>O: top-k prior notes (bounded text + names)
    O->>A: run_agent(prompt + prior lessons, model choice)
    A-->>O: ok / error + steps + usage (JSON envelope)
    O->>T: record (before any guard can abort)
    O->>C: run_local (CI after)
    O->>G: commit_all (so the reviewer has a diff to read)
    O->>V: review_change (different tier than the author)
    V-->>O: verdict (approve / blocking findings)
    alt blocking verdict
        O->>H: no PR; return ticket to backlog (attempts:N)
    end
    O->>K: write_lesson (always, pass or fail; carries the verdict)
    O->>G: commit_all + push_branch
    O->>H: create_pr (linked to ticket, model, lesson, verdict)
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
  memory, over `knowledge/lessons`, `knowledge/whitepapers` and `docs/adr`.
  No third-party dependency, no model call, no network - so retrieval is free
  and its ranking is exactly reproducible (ties break on note name).
- **Bias.** Notes tagged `outcome/fail` are up-weighted by
  `knowledge.recall.fail_weight`; notes whose `kind/` matches the current task
  are up-weighted by `kind_weight`. Failures are the expensive knowledge, and a
  heal worker should see heal history.
- **Inject.** `orchestrator._task_prompt` appends a *Prior lessons from this
  repo* section of at most `k` wikilinked notes, hard-capped at `max_chars`;
  whole notes are dropped to fit, never truncated mid-line. An empty corpus or
  `enabled: false` renders nothing at all.
- **Plan.** `synthesis.build_prompt` carries a *What this loop has already
  tried* section (`synthesis.MemoryPack`) - open tickets, recently closed
  tickets, and lesson outcomes, titles only and hard-capped - ahead of the
  reference-project digest, so the planner stops re-proposing dead ideas.
  `synthesis.is_duplicate` then filters the model's own output before filing:
  a candidate whose title exactly matches, or whose normalized-token overlap
  with a prior title clears the configured Jaccard threshold, is dropped and
  its slot is never back-filled - `SynthesisResult.rejected` and
  `.rejected_titles` carry the count and matches into `BlockReport.notes`.
- **Audit.** What was retrieved is recorded three times: on `IterationResult`,
  as a `recalled:` list in the lesson's frontmatter, and as a
  *Prior lessons consulted* section on the PR. `hsai recall "<query>"` prints
  the same ranking by hand.

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

## Measurability: iteration trajectories and the offline bench

A `Trajectory` records one **agent run**. An `IterationTrajectory` records one
**iteration of the loop**: the decisions the orchestrator made around that run.
They are separate records at separate granularities, versioned independently,
because only the second is a replayable unit of orchestration.

### The iteration trajectory schema

One JSON file per iteration at `.hsai/trajectories/<iteration>.json`.

| field | meaning |
| --- | --- |
| `schema_version` | the record's contract version (see the rule below) |
| `iteration`, `block`, `ticket` | what ran, in which governance block, for which ticket |
| `kind` | `heal` / `implement` / `improve` |
| `tier`, `model`, `rationale`, `strategy` | the recorded model choice and why |
| `phases[]` | `{name, seconds}` per phase, in order: setup, ci_before, claim, agent, guards, ci_after, review, lesson, pr, remote_ci, merge |
| `wall_clock_seconds` | the whole iteration, end to end |
| `prompt_digest`, `prompt_excerpt` | a content hash plus a clipped, redacted head of the worker prompt |
| `diff` | `{files, code_files, knowledge_files, paths}` - counted, not quoted |
| `ci_local_before`, `ci_local` | `green` / `red` / `not-run` |
| `ci_remote` | the source-of-truth remote conclusion |
| `review` | `approve` / `blocked` / `skipped` |
| `agent_ok`, `agent_trajectory` | how the run ended, and the `hsai traj <id>` cross-reference |
| `outcome`, `attempts`, `recovered`, `pr`, `merged` | the terminal disposition |
| `ledger_ref` | the matching `ledger` line (`block=N,iteration=M`) |
| `redacted_env` | the variable **names** whose values were scrubbed |
| `notes` | the iteration's own notes, clipped |

**Versioning rule.** Adding an *optional* field keeps the version: readers drop
unknown keys, so an older reader tolerates a newer writer. Removing a field, or
changing what one means, bumps `ITERATION_SCHEMA_VERSION` - and a version a
reader was not written for is an error, never a silent misread.

**Written at one seam.** `orchestrator.run_once` emits the record from
`_record_cost`, the same closure that appends the ledger line. Every terminal
path - merged, recovered, `incomplete`, `no_repro`, `review_blocked`, a timed-out
worker, dry-run - goes through it, so cost data and quality data can never
disagree about an iteration. An *idle* iteration (an improvement already in
flight) runs no model and writes neither artifact; that is the same rule, not an
exception to it.

**Why not under `knowledge/`.** The Obsidian vault is curated, committed,
human-facing content, and every note in it is indexed by `hsai recall`. A
trajectory is the opposite: raw machine telemetry that quotes repo content and
would drown the vault's signal and skew retrieval. Trajectories live under the
gitignored `.hsai/` instead - redacted on the way to disk (credential patterns,
the live values behind `constraints.forbid_env`, and absolute home paths), size
capped, and pruned by `hsai cycle` to `execution.trajectory_retention_blocks`
blocks' worth. What *is* committed is the digest line on the PR and the redacted
tail in the lesson.

### The bench contract

`hsai bench` replays a corpus of hand-authored scenarios
(`tests/fixtures/trajectories/`) through the **real** decision code -
`decide_path`, `models.select`, `_requires_code`, `repro.requires_repro_guard` /
`repro.check_repro`, `review.parse_verdict`, `ledger.evaluate_budget` - in the
same order `run_once` runs them, driven by a fake `Runner`.

- **No model, ever.** The fake runner raises on a `claude` command, so the
  offline property is enforced rather than assumed. No network, no quota.
- **Deterministic.** Same corpus plus same code gives the same numbers; there is
  no clock or randomness in the replay path.
- **What it measures.** Pass rate, tier-choice agreement against each scenario's
  expected tier, recovery correctness (retry vs blocked vs none), and mean
  seconds per ticket.
- **How it gates.** `bench/baseline.json` is committed; `hsai bench --check`
  fails when a quality metric drops below it, when the corpus shrinks (fewer
  scenarios is coverage loss), or when mean seconds exceeds the baseline by more
  than `bench.SECONDS_TOLERANCE`. The `bench` job in `.github/workflows/ci.yml`
  runs it on every PR, alongside but separate from `ci`.

Adding a scenario, or changing the decision code on purpose, means re-reading
the replay and regenerating the baseline with `hsai bench --write-baseline` -
which refuses to run while any scenario is red, so a baseline can only be
written from a bench that was made green deliberately.

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
