---
tags:
  - whitepaper
  - reference-set
created: 2026-07-25
---

# Founding Study: What the Top-10 AI Swarm Projects Teach Us

> Part of [[Whitepapers MOC]] - [[Knowledge Base MOC]]

## Summary
The reference set pinned in `.ai-swarm/core.yaml` (all ≥10k stars, MIT/Apache-2.0)
is our field to learn from. This founding note records *why* each was chosen and
the concrete practice we intend to borrow first. It anchors goal **G1**
(learn from the top-10) so future lessons can cite specific evidence.

## The set and one practice from each

| repo | practice worth adopting |
| --- | --- |
| `langchain-ai/langchain` | clean separation of orchestration from tool/provider adapters |
| `FoundationAgents/MetaGPT` | role-based agents with explicit hand-off artifacts |
| `crewAIInc/crewAI` | declarative "crew" config as the unit of work (mirrors our `core.yaml`) |
| `run-llama/llama_index` | pluggable retrieval so context assembly is swappable |
| `OpenBMB/ChatDev` | a phased pipeline (design → code → test) with gates between phases |
| `assafelovic/gpt-researcher` | plan-then-execute with an explicit synthesis step |
| `microsoft/semantic-kernel` | planners + typed "skills" as first-class, composable units |
| `microsoft/JARVIS` | an LLM controller that routes sub-tasks to the right model |
| `openai/swarm` | minimal, ergonomic hand-off primitives - keep the core tiny |
| `SWE-agent/SWE-agent` | turn a GitHub issue directly into a validated PR |

## Practices to adopt first
1. **Issue → validated PR** (from SWE-agent) - already the spine of the `hsai`
   loop; keep every change ticket-linked and CI-gated.
2. **Model routing** (from JARVIS) - our `hsai.models.select` is the seed;
   improving it is a tracked skill.
3. **Keep the core tiny** (from openai/swarm) - resist framework sprawl; wrappers
   stay thin and the decision logic stays pure.
4. **Config-as-crew** (from crewAI) - `core.yaml` is the single declarative
   source of mission, goals, and execution limits.

## How we will learn deeper
Beyond READMEs, future iterations should mine each project's **commit history,
CI/CD config, and issue history** for patterns, and record each borrowed
practice as a [[Lessons MOC|lesson]] that cites its source.

## Lessons synthesized
- [[2026-07-25-bootstrap-the-hsai-loop]]
