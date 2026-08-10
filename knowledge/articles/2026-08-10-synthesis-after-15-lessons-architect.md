---
tags:
  - article
  - persona/architect
---

# Trust the Remote, Not the Agent: Lessons from an Autonomous Dev Loop

We've been running an autonomous ticket loop — an orchestrator that assigns implement/improve/heal tickets to Claude workers (routed to `haiku` or `sonnet` by task complexity), lets them open PRs, and gates merges on CI. Fifteen lessons and five iterations of self-improvement in, the architecture-relevant findings are less about the wins and more about where "autonomous" quietly meant "unverified."

## What broke, and why it mattered

The first real multi-worker run surfaced two trust failures, not feature bugs:

1. **A local pass isn't a real pass.** A worker could edit `.github/workflows/**` as part of its change and get a green local CI run that no longer reflected what GitHub would actually check. The agent was grading its own homework.
2. **Failure had no recovery path.** A worker that failed left its ticket permanently marked as claimed — the backlog silently accumulated dead tickets with no way back to "available."

Both are the same architectural mistake: we let the executor be its own verifier and gave it no failure-safe state machine. Standard stuff in retrospect, but easy to miss when you're focused on getting the happy path working first.

## The fix pattern

- **Remote CI as the sole source of truth.** `run_once` now blocks on the PR's actual GitHub check rollup rather than trusting local test output. Slower, but it closes the gap between "looks green" and "is green."
- **CI parity guard.** Any diff under `.github/workflows/**` is reverted before commit — a task literally cannot rewrite the rubric it's graded against. This is a cheap, general pattern for any agent system where the executor has write access near its own gate.
- **Bounded retry-then-block.** Non-green PRs get closed and the ticket returns to the backlog with an `attempts:N` label; past `max_ticket_attempts` it's marked `blocked` for a human. Self-healing without an infinite retry loop — borrowed conceptually from SWE-agent's issue→validated-PR cycle.

We also adopted MetaGPT's pattern of **explicit phase artifacts**: each phase (heal/implement/improve) now declares what it's supposed to produce, and every PR body lists it. Small change, but it converts "the worker ran" into an auditable claim you can check a diff against — worth it anywhere multiple agents hand off work.

## Tradeoffs we accepted

Complexity-based model routing (haiku for light tickets, sonnet for heavier ones) saves cost but is a coarse heuristic with no feedback loop yet — we're not measuring whether haiku's "pass" tickets would have been meaningfully better on sonnet, just whether CI stayed green. Green CI is a weak signal for code quality; several of the lessons in this window reduce to "merged cleanly under a green build," which tells you the pipe didn't clog, not that the work was good. That's an honest gap, not a solved problem.

## Takeaway

The interesting architecture problem in agent loops isn't getting an agent to write code — it's building a trust boundary the agent can't reach across: an external, unmodifiable verifier, and a state machine that fails toward "blocked for a human" rather than "silently stuck" or "silently wrong."
