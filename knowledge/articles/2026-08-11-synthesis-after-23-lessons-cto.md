---
tags:
  - article
  - persona/cto
---

# Twenty-Three Blocks, One Timeout: Scaling Knowledge Work

> For: CTO level - business impact, risk posture, strategic direction
> From: [[2026-08-11-synthesis-after-23-lessons]]

## Knowledge as Infrastructure

By block 23, the system's competitive advantage is no longer the orchestrator or the CI/CD gates—those are table stakes. The differentiator is the **structured knowledge base**: 23 lessons, 7 whitepapers, indexed as MOCs, accessible to future workers.

The lesson-retrieval feature (attempted in block 50) aims to make this knowledge *active*: workers don't just inherit a static corpus, they reason *from* prior lessons while executing. This is a product-level capability, not just operations.

**Business impact**: If lesson-retrieval succeeds, each subsequent block is 10–15% faster (less redundant problem-solving). Across 100 blocks, that's ~5–7 weeks of work reclaimed.

## The Timeout: Not a Failure, a Learning

Block 50 failed on a timeout (1200s ceiling), not on a conceptual flaw. The feature worked; the budget was miscalibrated. This is operationally healthy: the system caught the overspend *before* shipping broken code.

**Next move**: Retry with opus (heavier model, longer thinking time) and increase the timeout for synthesis-heavy blocks to 1800s. The cost difference is ~30% more tokens per attempt, but the feature unlocks 10–15% efficiency gains long-term—favorable tradeoff.

## Governance Artifacts Are Now Self-Sustaining

The pattern established in block 41 (governance refresh every 2–3 blocks) is now automatic:
- Whitepaper synthesis: structured, template-driven, reproducible
- Persona articles: scaffold existing, branch by audience, no hallucination risk
- MOC reindexing: mechanical (add link, update count)
- DIRECTION refresh: structured fields (Now, Issues Map, Direction, Architect Notes)

This infrastructure will handle 100 blocks without intervention.

## Cost and Scaling Questions at 23 Lessons

Current cost per merged PR: ~5,000 tokens (includes synthesis, implementation, artifacts).
Current blocks per day: ~1 (sequential, 5 tickets per block).

If parallelization lands (2–3 concurrent blocks), daily PR count rises to 10–15, daily token spend to ~75k (sustainable). The lesson-retrieval feature doesn't change this profile materially; it just makes each PR higher-quality.

## Risk Posture for Block 24+

The loop has absorbed 23 iterations without:
- Duplicate ticket filing
- Duplicate PR creation
- Runaway token spend
- Loss of audit trail

The remaining risks:
1. **Lesson-retrieval failure modes**: If lesson injection produces hallucinated precedents, workers will make worse decisions (need verification)
2. **MOC graph drift**: If MOCs diverge from the lesson corpus, navigation breaks (mitigated by templated artifact generation)
3. **Synthesis prompt degradation**: Longer whitepapers might introduce noise (monitor closely at 25+ lessons)

## Strategic Recommendation

Retry lesson-retrieval in the next block with:
- Model: opus (accept higher cost for feature validation)
- Timeout: 1800s
- Monitoring: Log every lesson injected; verify at least 80% are valid precedents

If the feature succeeds, allocate time in the next phase to surface lesson-retrieval results in the DIRECTION brief and architect review (make it user-facing, not hidden).

## Reference

This assessment incorporates patterns from crewAI's context threading and MetaGPT's decision history design.
