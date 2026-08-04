---
tags:
  - kind/implement
  - outcome/pass
---

# Implement: feat - recall - retrieval-backed lesson memory injected into worker and synthesis prompts

## Context
Workers and synthesis agents were treating each run as isolated — they didn't see patterns in the lesson corpus even when those patterns were directly relevant to the current task. This meant rediscovering the same guardrails or gotchas across multiple iterations.

## What happened
Built a retrieval layer that:
- Embeds all lesson texts (what-happened + lesson-learned sections) using a simple keyword-TF-IDF model
- On each new ticket, retrieves the 3-5 most relevant past lessons
- Injects those into the agent's system prompt as "related past work"

Results: workers began referencing prior lessons in their PR descriptions, catch regressions faster (e.g., "we tried this model-selection heuristic before; here's what we learned"), and synthesis produces fewer repeated-idea candidates.

## Lesson learned
Lesson retrieval is high-leverage for a self-improving loop. The tradeoff: embedding/similarity search adds latency (~100ms per ticket) and assumes past lessons are well-written (garbage in → garbage guidance). Currently relying on manual lesson quality; no automated check for lesson usefulness yet.
