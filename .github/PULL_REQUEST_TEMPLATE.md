Closes #<!-- ticket number - REQUIRED; no PR merges without a linked ticket -->

## Model used
- **model**: `<!-- sonnet / opus / haiku / n/a for human -->`
- **selection**: <!-- rationale, or "human change" -->

## CI
<!-- local pre-flight result; remote CI is the merge gate -->

## Acceptance review
<!-- REQUIRED when the ticket has acceptance criteria: one row per criterion.
| id | criterion | status | evidence |
| --- | --- | --- | --- |
| AC1 | ... | **met** | path/to/file.py:42 |
-->

## Lesson learned
<!-- REQUIRED, pass or fail - link the lesson note in knowledge/lessons/ -->

## SDLC evidence
- [ ] Plan: ticket has acceptance criteria + verification plan
- [ ] Implement: change is scoped to the ticket
- [ ] Verify: `ruff check .` + `pytest` green locally; code ticket => code diff
- [ ] Review: every acceptance criterion met, with evidence cited above
- [ ] QA: remote CI green
- [ ] Integrate: lesson recorded
