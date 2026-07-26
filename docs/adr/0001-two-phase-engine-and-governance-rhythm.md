# ADR-0001: Two-phase engine and twice-daily governance rhythm

- **Status**: accepted
- **Date**: 2026-07-26
- **Source**: architect steering session 2026-07-26 (first governance review)

## Context
The first autonomous runs produced working but shallow changes. Evidence:
PR #17 (haiku) closed feature ticket #4 with a knowledge-only diff - zero code;
improvement ideas were single-step copies from one reference project; tickets
were unstructured, so a trivial diff could "satisfy" them. The architect had no
surface through which to steer.

## Decision
1. **Two-phase engine.** Idea generation is separated from implementation.
   A heavy model (`synthesis.tier: heavy`) studies a rotating subset of the
   reference projects and must COMBINE practices from >= 3 of them, run an
   explicit reflection/critique pass, prioritize by impact x effort, and file
   structured tickets. Cheaper models implement against acceptance criteria.
2. **Structured tickets are enforced, not encouraged.** Feature/skill tickets
   without acceptance criteria (>= 2 checkboxes) + a verification plan are
   labeled `needs-refinement` and refused by the orchestrator.
3. **Completeness guard.** A code ticket cannot be closed by a knowledge-only
   diff; such attempts are auto-recovered and counted against retry attempts.
4. **Governance rhythm.** Work runs in sequential half-day blocks
   (`cycle.block_size`, sequential to protect the local machine). Each block
   ends with a whitepaper, persona articles (CTO / architect / DevOps),
   a refreshed `governance/DIRECTION.md`, and a review issue. The architect
   reviews twice daily via `/review-next`; feedback is encoded as ADRs and
   tickets; each session ends with a merged PR.
5. **Publishing is pull-based.** agentic-atlas ingests hyperswarm whitepapers
   through its own pipeline (source entry), keeping the repos loosely coupled.

## Consequences
- Substantial synthesis costs heavy-model quota twice daily - accepted; it is
  the learning engine, and implementation stays on cheaper tiers.
- haiku is demoted to genuinely mechanical, narrow edits (and size-labeled
  tickets can never route light).
- Enforced where: orchestrator guards (`tickets.py`, completeness guard),
  CI evidence step (`ci.yml`), `hsai cycle`/`synthesize` (engine),
  `/review-next` skill (review discipline), `core.yaml` (configuration).
