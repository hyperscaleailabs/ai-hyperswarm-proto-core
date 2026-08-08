---
tags:
  - whitepaper
created: 2026-08-08
---

# Synthesis after 22 lessons

> Part of [[Whitepapers MOC]] - [[Knowledge Base MOC]]

## Summary
Synthesis of the last 2 lesson(s): 2 pass / 0 fail, across kinds implement.

## Outcomes in this window
| outcome | count |
| --- | --- |
| pass | 2 |
| fail | 0 |

## Work by kind
| kind | count |
| --- | --- |
| implement | 2 |

## Recurring successes
- [[2026-08-08-implement-skill-learned-model-selection-heuristic-v2-calibrated-from-lessons]] (implement): Learned heuristics from outcome data improve routing and reduce quota waste.
- [[2026-08-08-implement-feat-adversarial-acceptance-criteria-review-gate]] (implement): Early adversarial verification catches issues before expensive CI cycles.

## Recurring themes
- **quality gates** - appears in 2 lessons
- **model selection** - appears in 2 lessons
- **feedback loops** - appears in 2 lessons
- **cost optimization** - appears in 2 lessons

## Patterns observed
1. **Learned routing works when grounded in outcome data**: The model-selection heuristic demonstrates that calibrating from historical success rates creates a positive feedback loop: cheaper models finish faster on their strengths, leaving budget for complex tasks.

2. **Early validation beats late CI**: The adversarial acceptance-criteria gate proves that catching issues before CI is more cost-effective than failing at CI. A cheap skeptic agent refuting criteria upfront reduces rework and improves signal quality.

3. **Self-improvement compounds**: The combination of trajectory capture, outcome logging, and learned heuristics creates a closed loop where each iteration makes the system faster and cheaper.

## Lessons synthesized
- [[2026-08-08-implement-skill-learned-model-selection-heuristic-v2-calibrated-from-lessons]]
- [[2026-08-08-implement-feat-adversarial-acceptance-criteria-review-gate]]
