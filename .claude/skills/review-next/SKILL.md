---
name: review-next
description: Architect review session - walk un-reviewed lessons and open review briefs sequentially, capture feedback as ADRs and tickets, end with a merged PR. Use when the architect says /review-next, "review the block", "architect review", or wants to go through pending lessons/briefs.
---

# review-next: the architect review session

You are running a structured review session with the architect. The scheduled
`hsai cycle` has already prepared the material; your job is to walk it,
capture decisions, and leave an auditable trail.

## Session flow

1. **Gather the queue.**
   - Open review briefs: `gh issue list --label review --state open`
   - Un-reviewed lessons: files in `knowledge/lessons/` whose frontmatter lacks
     `reviewed: true`, oldest first.
   - Refresh the steering doc: run `hsai brief`; show the architect the
     **Now / Issues Map / Direction** summary from `governance/DIRECTION.md`.

2. **Walk items SEQUENTIALLY, one at a time.** For each lesson or brief:
   - Present a compact summary: what was attempted, outcome, model, PR/ticket
     links (clickable), and what the lesson claims was learned.
   - Ask the architect for their read: agree / disagree / redirect.
   - Record their feedback immediately (do not batch): append an
     `## Architect review` section to the lesson file and set
     `reviewed: true` in its frontmatter.

3. **Encode decisions as ADRs.** When feedback expresses a decision (a rule,
   a direction change, a tradeoff resolution), write
   `docs/adr/NNNN-short-title.md` using `docs/adr/TEMPLATE.md`, numbered
   sequentially. Cite the lessons/PRs that motivated it.

4. **Turn actionable feedback into structured tickets** (schema: Problem /
   Proposal / Acceptance criteria >= 2 checkboxes / Verification plan / size
   label). Refine any `needs-refinement` tickets the architect wants kept;
   close the ones they reject.

5. **Update Architect Notes** in `governance/DIRECTION.md` (between the
   `architect-notes` markers) with anything the architect wants to persist.

6. **Close the loop.** Close reviewed briefs (`gh issue close`). Then create a
   branch `review/<date>-<block>`, commit all review artifacts (lesson
   updates, ADRs, DIRECTION.md), and open a PR titled
   `review: architect session <date>` that references the review issue and
   follows the PR template (model: n/a - human session). Merge on green with
   `gh pr merge --auto --squash`.

## Rules

- Never mark a lesson reviewed without the architect actually seeing it.
- One item at a time - do not dump the whole queue at once.
- Decisions of consequence ALWAYS become ADRs, not just chat.
- The session is not done until the review PR is merged and briefs are closed.
