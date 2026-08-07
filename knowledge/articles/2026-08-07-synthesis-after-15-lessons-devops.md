---
tags:
  - article
  - persona/devops
---

# What Five Green Merges Taught Us About Running an Autonomous CI Loop

Our orchestrator runs an agent-driven ticket loop: pick a ticket, let a model (haiku, sonnet, or opus depending on complexity) implement it, open a PR, gate on CI, merge or retry. Over the last five lessons — two `implement`, three `improve` — every run landed clean: 5 pass, 0 fail. That streak is worth less as a trophy and more as a snapshot of what it took to get the pipeline to that point, including two failure modes we had to design around before the green streak was possible.

## What actually broke first

Before this window, our first parallel run (3 workers at once) surfaced two concrete reliability gaps:

1. **A failed PR could strand its ticket permanently.** If a worker claimed a ticket and its PR didn't land, nothing released the claim — the ticket just sat locked, invisible to future workers. No retry, no backlog recovery, just a dead ticket.
2. **Local CI lied.** A worker edited `.github/workflows/**` as part of its change, passed CI locally against its own modified workflow, and then failed on the real remote checks. The agent was effectively grading its own homework.

Neither is exotic — they're the standard failure modes of letting an automated agent touch the same pipeline that judges it. The fix mattered more than the bug:

- **Remote CI is now the only source of truth.** `run_once` blocks on the actual GitHub check rollup (`ci.wait_remote`) before deciding pass/fail. No local shortcut counts.
- **CI-parity guard.** Any edit under `.github/workflows/**` gets reverted before commit. A task literally cannot change the checks it's judged by.
- **Bounded recovery.** On a non-green remote result, the PR closes, the ticket returns to the backlog with an `attempts:N` label, and after `max_ticket_attempts` it flips to `blocked` for a human instead of looping forever. Blocked/assigned tickets are skipped by future workers, so a stuck ticket can't wedge the whole swarm.
- Both paths got fake-runner integration tests, plus `tests/test_ci.py` for the rollup reducer and `wait_remote` specifically — not just "we fixed it," but "we pinned it down with a regression test."

## The operational lesson

Autonomy needs a truthful signal and a safe failure path, in that order. Gating on remote CI closes the trust gap; bounded retry-then-block keeps the backlog self-healing without infinite retry loops. That's the same discipline SWE-agent uses for its issue→validated-PR cycle, and it's the piece that made everything after it boring in a good way.

## What "boring" bought us

Once those two guards were in place, the subsequent five tasks — a model-selection skill, fake-runner integration tests for the orchestrator itself, a reference-set snapshot refresh, and an explicit "phase artifacts" section added to every PR body (so each PR states what the heal/implement/improve phase was supposed to produce) — all merged cleanly under green builds, across models from haiku to sonnet, with zero manual intervention.

The takeaway for anyone building a similar loop: the win isn't the pass rate, it's that pass rate becoming meaningful — because CI-parity abuse and stranded-ticket deadlock were closed off first.
