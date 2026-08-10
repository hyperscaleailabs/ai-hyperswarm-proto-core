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
| `hsai.failures` | classify a failed iteration; resolve its retry action | none (pure) |
| `hsai.trajectory` | one durable record per agent run; redaction, replay | write files |
| `hsai.journal` | append-only per-block step journal; `once()` replay | write files |
| `hsai.knowledge` | lessons, whitepapers, MOC reindex (Obsidian) | write files |
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

    O->>G: sync_main + create_worktree
    O->>C: run_local (CI before)
    O->>H: claim ticket (heal / implement / improve)
    O->>A: run_agent(prompt, model choice)
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
iteration's cost record is appended, along with the iteration's context: each
guard's verdict, the local ruff/pytest results before and after, the remote CI
conclusion, the changed-path diffstat, per-phase wall clock, the redacted agent
streams, and the failure class plus the retry action taken. Sharding by block is
what makes the store bounded: `hsai cycle` prunes block directories beyond
`execution.trajectory_retention_blocks`, and a single record is capped at
`trajectory.MAX_RECORD_CHARS` - over the cap it sheds middle steps, then the
streams and the prompt, and says so in its `truncated` field rather than
silently presenting a partial run as a whole one.

**Every iteration writes exactly one record**, including a `--dry-run`
rehearsal (which records `exit_status: "dry-run"` with an empty step stream, so
it can never be mistaken for a real model call) and a guard-aborted run. The one
exception is an *idle* iteration that never claimed a ticket and never selected
a model: it produces no trajectory and no ledger record, because no iteration
happened.

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

## Failure taxonomy and the retry policy

Every failed iteration used to be handled identically - close the PR, bump
`attempts:N`, retry with the same tier and the same prompt - so a lint slip, an
agent timeout, a red remote build, a merge conflict and a worker that edited
`.github/workflows/**` were indistinguishable in the ledger, the lesson and the
review brief. `hsai.failures` names the cause and routes the response.

`failures.classify` is pure: it takes one iteration's signals and returns one
class. The rules are **ordered**, and the order is the contract, because a
failing iteration usually trips several signals at once:

| # | class | fires when | precedence note |
| --- | --- | --- | --- |
| 1 | `workflow_tamper` | the diff touched `.github/workflows/**` | beats everything - the run moved the goalposts it is judged by |
| 2 | `merge_conflict` | git's conflict vocabulary in the agent/CI output | the branch cannot integrate, so later verdicts describe a tree that will never merge |
| 3 | `guard_incomplete` | completeness guard: knowledge-only diff on a code ticket | a **guard verdict beats a CI signal** - "was the work done" outranks "is the build happy about it" |
| 4 | `guard_no_repro` | reproduce-before-fix guard rejected the change | as above |
| 5 | `timeout` | the agent ran out of wall clock | **beats `agent_error`**: a killed agent also exits non-zero |
| 6 | `lint` | local `ruff` red | ruff runs first, and a lint slip is the cheaper, more certain fix |
| 7 | `test_failure` | local `pytest` red | both outrank `agent_error`: a red step *names* the repair |
| 8 | `agent_error` | the run failed with nothing more specific said | |
| 9 | `remote_infra` | local clean, remote CI not `SUCCESS` | the divergence is environmental, not in the diff |
| 10 | `unknown` | it failed and no signal explains it | a growing count here means the taxonomy needs work |

`retry_policy` in `.ai-swarm/core.yaml` maps each class to an action, applied by
`_recover_failed`:

| action | effect |
| --- | --- |
| `retry_same_tier` | return to the backlog unchanged (the historical behaviour) |
| `retry_with_remediation` | retry; the next prompt quotes the prior failure |
| `escalate_timeout` | retry with a doubled agent timeout (label `escalate:timeout`) |
| `demote_tier` | retry one model tier cheaper (label `tier:demote`) |
| `block_immediately` | block the ticket now, **without consuming an attempt** |

`workflow_tamper` and `merge_conflict` block immediately: a second identical
attempt cannot succeed, so burning the ticket's remaining retry only hides the
cause behind a bare `blocked`. Every recovered ticket is labelled
`failure:<class>` regardless of action, and per-class counts ride the ledger
(`LedgerRecord.failure_class` → `BlockAggregate.failure_counts`) into the
**Failure taxonomy** table rendered in both the review brief and the block
whitepaper - so the architect fixes a class once instead of rediscovering it
run by run.

On a retry, `_task_prompt` prepends a bounded `## Previous attempt failed`
excerpt drawn from the prior trajectory (`trajectory.last_failure_for_ticket` →
`Trajectory.remediation`): the failure class, which CI step was red, the guard
verdicts, the diffstat, and a redacted tail. Reflection before acting again,
rather than a second blind attempt.

**None of this touches the merge gate.** `remote == ci.SUCCESS` remains the sole
path to `merge_pr`; the failure class only steers what happens to the *ticket*
afterwards. Classification is also purely local - it reads text the loop already
has, and adds no network or API call.
