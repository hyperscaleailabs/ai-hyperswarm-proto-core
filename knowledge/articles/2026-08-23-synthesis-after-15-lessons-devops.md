---
tags:
  - article
  - persona/devops
---

# Five Green Runs in a Row: What Our CI Loop Actually Learned

Our autonomous build loop just closed out five consecutive tickets — two `implement`, three `improve` — all merged clean. No failures this window. That's worth being suspicious of, not just proud of, so here's what actually happened under the hood.

## What shipped

- **Model-selection by task complexity** — the orchestrator now picks a cheaper or heavier model per ticket instead of a fixed default, cutting wasted tokens on trivial diffs.
- **Fake-runner integration tests** for the orchestrator's `run-once`, `heal`, and `implement` paths — these exercise the control flow without touching real infra, which is what let the next four tickets land without babysitting.
- **Reference-set snapshot refresh** plus one extracted reusable practice from the corpus.
- **Explicit phase artifacts** modeled after MetaGPT's staged-output pattern — each phase of a run now writes a durable artifact instead of leaving state only in logs.
- **Loop reliability work**: retry logic and closer CI parity, aimed directly at flaky infra rather than flaky tests.

## The mechanics that made this a "clean window"

The recurring signal across all five lessons wasn't a specific bug fix — it was process discipline: **build cleanly, change scope tightly, merge green**. Three of five lessons independently converged on the same theme, which is a tell that the loop's gating (build must pass, diff must be scoped, merge must be green) is doing real work rather than just being a nice slogan.

Concretely, this is CI-parity work: making the orchestrator's local "heal" and "implement" test paths behave the same as what runs in CI, backed by fake runners so state-machine bugs surface before a real agent burns wall-clock time on them.

## What we're honest about

A 5/0 pass rate over one window is a *reporting artifact*, not a claim that the loop is bug-free. It means:

- The failure classes that used to bite us (retry storms, CI/local drift) got specifically targeted in this exact window — so naturally they didn't recur immediately after the fix landed. That's the fix working, not the absence of risk.
- We have no negative signal to synthesize from *this* batch. The "recurring failures" section is empty because there's nothing to show, not because failure modes are solved for good.
- Known constraints elsewhere in this harness still stand: workers running inside loop worktrees can't execute `pytest`/`ruff` directly, and can't write under `.claude/` — so any ticket needing either of those has to be scoped around them or handed off manually. Neither surfaced in this window's tickets, but they didn't go away.

## The operational takeaway

Retry-and-CI-parity plus fake-runner coverage on the orchestrator's own control paths is the highest-leverage pair here: it turns "did the loop behave correctly" from a question you answer by staring at logs into one you answer with a test. The next failure window is the real test of whether that investment holds — we're watching for it, not assuming it won't come.
