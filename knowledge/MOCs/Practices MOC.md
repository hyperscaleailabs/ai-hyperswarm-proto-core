---
tags:
  - moc
  - practices
updated: 2026-08-13
---

# Practices MOC

Up: [[Knowledge Base MOC]]

Every practice this repo adopted from the reference set, grouped by the project
it was observed in. Tickets cite these by `practice:<id>`; the orchestrator
refuses a PR whose citation does not resolve here. Total: **7**.

## FoundationAgents/MetaGPT
- [[metagpt-explicit-phase-artifacts]] - `README.md - the SOP diagram naming each role's deliverable (PRD, design, task list, code)`

## OpenBMB/ChatDev
- [[chatdev-cheaper-agents-for-secondary-phases]] - `README.md - the phase pipeline and its cost discussion`

## assafelovic/gpt-researcher
- [[gpt-researcher-per-run-cost-accounting]] - `gpt_researcher/utils/costs.py - token counts converted into a per-run cost`

## crewAIInc/crewAI
- [[crewai-mechanical-pr-metadata-gates]] - `.github/workflows/pr-title.yml - PR metadata checked by CI at intake`

## microsoft/JARVIS
- [[jarvis-controller-routes-each-task-to-a-model]] - `README.md - an LLM controller plans a task and routes each sub-task to a specialist model`

## openai/swarm
- [[swarm-error-execution-context]] - `README.md - handoffs carry context variables through every step of a run`

## run-llama/llama_index
- [[llama-index-reproduce-before-fix]] - `CONTRIBUTING.md - bug fixes are expected to ship with a test covering the reported failure`
