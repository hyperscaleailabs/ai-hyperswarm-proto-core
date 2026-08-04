---
tags:
  - eval
  - moc/knowledge
created: 2026-08-04
---

# Selection benchmark

> Part of [[Knowledge Base MOC]]

The harness's model-selection decision, graded against a fixed set of labeled
tasks. `hsai replay` runs a registered strategy over
[`selection-corpus.jsonl`](selection-corpus.jsonl) and prints a confusion
matrix plus over-/under-provision rates; CI fails the build if the configured
strategy scores below `models.replay_min_score`.

Everything here is offline. Scoring makes no model call and no network call, so
the benchmark runs on every PR at zero quota cost.

## Why this exists

`models._score` combined hand-chosen keyword weights with thresholds of `>= 5`
and `<= -3` under comments claiming they were "calibrated from observed
patterns" and "over multiple iterations". No calibration artifact, dataset, or
measurement existed anywhere in the repo. This directory is that artifact.

## The corpus

One JSON object per line. The first line is a version-stamped header; every
subsequent line is a `LabeledTask` (see `src/hsai/replay.py`).

| field | meaning |
| --- | --- |
| `id` | stable instance key |
| `kind` / `title` / `body` / `labels` / `est_files` | exactly what `models.select` is given |
| `correct_tier` | **the label** - the tier a human judged correct. `null` = unlabeled draft |
| `observed_tier` / `observed_outcome` / `attempts` / `wall_clock_seconds` | what actually happened when the loop ran it, where a ledger record or lesson exists |
| `source` | `lesson:<note>`, `ledger:<iteration>`, `issue:<n>`, `pr:<n>`, or `adversarial` |
| `note` | why this label. Every row must justify itself to a reviewer |

## Labeling rubric

- **light** (haiku) - the diff is determined by the request. Typo fixes, prose,
  dependency bumps, pure renames, regenerating derived indexes. No design
  decision is being delegated.
- **standard** (sonnet) - ordinary engineering inside one subsystem: a feature,
  a bugfix, tests, a contained refactor. One or two real decisions.
- **heavy** (opus) - cross-cutting or correctness-critical: a new subsystem, a
  concurrency or secrets invariant, a data migration, or anything the planner
  sized `size:L`.

Evidence rules, applied in order:

1. If a run **merged on tier T at the first attempt** and the change is
   T-shaped, label T.
2. If a run **failed or needed a retry at tier T**, label at least one tier
   above T - the tier was insufficient in practice.
3. If a run **merged on tier T but a written retrospective judged the tier
   wrong**, follow the retrospective. A merge is not proof the price was right.
4. Adversarial rows carry no observed outcome; their label is the rubric
   applied to the described work, and the `note` must say which failure mode
   the row exists to pin down.

## Known limitations

Stated plainly, because a benchmark that oversells itself is worse than none.

- **Small and in-sample.** 34 instances, of which 17 come from this repo's own
  history. `heuristic-v2` was designed against this corpus, so its delta is an
  in-sample fit and will overstate real-world gains. Re-measure as the corpus
  grows.
- **Historical rows have empty bodies.** GitHub issue text was not available
  offline when the corpus was seeded, so those rows are scored on their titles
  and labels alone. `hsai corpus-build` fills `body` in when `gh` is
  authenticated; re-running it and re-measuring is the fix.
- **Two rows are not separable from their titles.** `ticket-004` ("chore:
  reference-set miner - extract one practice") and `ticket-048` ("chore:
  refresh reference-set snapshot and extract one practice") read almost
  identically but carry different labels, because one builds a module and one
  refreshes a data file. No title-only strategy can split them; `ticket-048`
  is a permanent over-provision until bodies are populated.
- **`est_files` is inert in production.** `orchestrator.run_once` constructs
  its `Task` without `est_files`, so every real routing decision is made with
  the default of 1 and the file-count buckets in `_score` never fire.
  `ticket-004` records the measured file count; `adv-unknown-file-count`
  records the same task as the router actually saw it. Populating `est_files`
  from the ticket is follow-up work, and the corpus keeps both rows so the gap
  stays visible.

## Adding to the corpus

```
hsai corpus-build                 # mine closed issues + ledger + lessons -> draft
hsai corpus-build --no-github     # local artifacts only (fully offline)
```

The draft lands in `selection-corpus.draft.jsonl` with `correct_tier: null` on
every row. A human sets the labels and moves the rows into
`selection-corpus.jsonl`. The loop proposes; it never grades its own homework.

## Records

- [[2026-08-04-selection-strategy-v1-vs-v2|heuristic-v1 vs heuristic-v2]] - the
  measured delta that justifies every constant changed in v2.
