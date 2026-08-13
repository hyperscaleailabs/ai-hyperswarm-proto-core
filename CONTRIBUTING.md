# Contributing

This repo is primarily maintained by the `hsai` autonomous loop, but human
contributions are welcome.

## Ground rules (enforced by the loop and CI)

1. **No direct commits to `main`.** Work on a branch, open a PR.
2. **Every PR links a ticket.** Use `Closes #<n>` in the body.
3. **Every PR records the model used** (if produced by an agent) and a
   **lesson learned** in `knowledge/lessons/`.
4. **CI must be green** (`hsai ci`) before merge.

## Local setup

```bash
pip install -e ".[dev]"
hsai ci --scope local     # the same steps GitHub runs; --json for per-step results
hsai doctor
```

## The CI contract

What "a green build" means is declared **once**, in `.ai-swarm/core.yaml` under
`ci.steps`. Each step has an `id`, a `command` (argv list), a `scope`
(`local` / `remote` / `both`), a `required` flag, and a `job`:

```yaml
ci:
  steps:
    - id: ruff
      command: [ruff, check, .]
      scope: both
      required: true
```

`hsai ci --scope local`, the loop's own pre-flight (`hsai.ci.run_local`), and
`.github/workflows/ci.yml` (via `hsai ci --scope remote`) all execute that one
manifest, so local and remote cannot drift. `tests/test_ci_parity.py` enforces
it: it fails if a declared step is no longer reachable from the workflow, or if
the workflow starts running a lint/test command inline.

**To change what CI does, edit `ci.steps` - not the workflow.** A new step is
picked up by both callers with no YAML change at all.

> **Status.** The workflow rewrite that turns `ci.yml` into a pure caller ships
> with this contract. A loop worker cannot land it on its own - the orchestrator
> reverts uncommitted edits under `.github/workflows/**`, and the governed
> `ci-change` hatch below covers invocation lines, not the structural rewrite.
> If your checkout's `ci.yml` still runs `ruff`/`pytest`/`grep` inline,
> `tests/test_ci_parity.py` reports the affected assertions as **expected
> failures** (`xfail`, strict) naming exactly what is missing; they become
> ordinary green tests the moment the rewrite lands, and a red build if it is
> ever undone.

**To change how the workflow *calls* the contract** (a new `hsai ci` invocation,
different flags, a new `hsai repro-check` job), the loop needs an explicit
licence: the ticket must carry the **`ci-change`** label, and the diff under
`.github/workflows/**` may touch only `hsai ci` / `hsai repro-check` invocation
lines. Anything else a worker writes there is reverted before it is committed.
When such an edit is kept, the PR body and the lesson both record the allowance
and the exact retained diff. Human contributors edit the workflow normally -
the guard only constrains the loop's workers.

## The reproduce-before-fix gate

`heal:` and `fix:` PRs must add or modify a test that **fails** on the pre-fix
tree and **passes** on the branch. It runs locally inside the loop and remotely
as the `repro-check` job (`hsai repro-check --pr-title "$PR_TITLE"`), so a fix
without a regression test is blocked pre-merge. `docs:`/`chore:` work is exempt.

## Priorities

Tickets are prioritized with labels `priority:P0` (highest) … `priority:P3`.
The loop always takes the highest-priority open ticket first.

## Knowledge base

Lessons and whitepapers are Obsidian-ready markdown. After adding notes, run
`hsai reindex` to rebuild the MOCs.
