---
tags:
  - moc
  - practices
---

# Practices MOC

Up: [[Knowledge Base MOC]]

Every practice this repo has adopted from the pinned reference set, pinned to
the artifact it was observed in. Tickets cite these ids in a `## Practices`
section; the loop stamps the cited repos onto the PR and the lesson. Total:
**8**.

| id | practice | source | artifact | cited by |
| --- | --- | --- | --- | --- |
| [[PR-0001-regression-tests-pin-the-exact-broken-behaviour\|PR-0001]] | Regression tests pin the exact broken behaviour | `run-llama/llama_index` | readme: `CONTRIBUTING.md` | 0 |
| [[PR-0002-track-the-cost-of-every-model-call-in-the-run-itself\|PR-0002]] | Track the cost of every model call in the run itself | `assafelovic/gpt-researcher` | code: `gpt_researcher/utils/costs.py` | 0 |
| [[PR-0003-a-controller-routes-each-task-to-the-cheapest-capable-model\|PR-0003]] | A controller routes each task to the cheapest capable model | `microsoft/JARVIS` | readme: `README.md` | 0 |
| [[PR-0004-pr-metadata-is-only-real-when-a-required-check-enforces-it\|PR-0004]] | PR metadata is only real when a required check enforces it | `microsoft/semantic-kernel` | ci: `.github/workflows/merge-gatekeeper.yml` | 0 |
| [[PR-0005-run-the-agent-crew-on-a-cheaper-model-when-the-work-allows\|PR-0005]] | Run the agent crew on a cheaper model when the work allows | `OpenBMB/ChatDev` | readme: `README.md` | 0 |
| [[PR-0006-an-unbroken-issue-to-pr-chain-is-the-product\|PR-0006]] | An unbroken issue-to-PR chain is the product | `SWE-agent/SWE-agent` | readme: `README.md` | 0 |
| [[PR-0007-each-phase-leaves-an-inspectable-artifact-behind\|PR-0007]] | Each phase leaves an inspectable artifact behind | `FoundationAgents/MetaGPT` | readme: `README.md` | 0 |
| [[PR-0008-errors-carry-the-orchestration-context-they-failed-in\|PR-0008]] | Errors carry the orchestration context they failed in | `openai/swarm` | code: `swarm/core.py` | 0 |

## How this is maintained
- `hsai practices --validate` checks the schema and that every source repo is pinned in `.ai-swarm/core.yaml`.
- `hsai practices --index` (also run by `hsai reindex`) rebuilds this table and each card's backlinks.
