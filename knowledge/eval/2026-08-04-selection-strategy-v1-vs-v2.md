---
tags:
  - eval
  - measurement
  - model-selection
created: 2026-08-04
corpus: knowledge/eval/selection-corpus.jsonl
corpus_version: 1
instances: 34
---

# heuristic-v1 vs heuristic-v2 on selection-corpus v1

> Part of [[Knowledge Base MOC]] - see [[README|the selection benchmark]]

Reproduce with:

```
hsai replay --strategy heuristic-v1
hsai replay --strategy heuristic-v2 --compare heuristic-v1
```

Every number below is asserted by `tests/test_replay.py`, so this record cannot
drift away from the code without turning CI red.

## Headline

| metric | heuristic-v1 | heuristic-v2 | delta |
| --- | ---: | ---: | ---: |
| instances | 34 | 34 | - |
| correct | 22 | 32 | +10 |
| accuracy | 64.7% | 94.1% | +29.4pp |
| over-provision rate | 14.7% (5) | 2.9% (1) | -11.8pp |
| under-provision rate | 20.6% (7) | 2.9% (1) | -17.6pp |
| **score** | **0.7206** | **0.9559** | **+0.2353** |
| quota units spent | 306 | 378 | +72 |
| oracle floor | 394 | 394 | - |
| all-heavy ceiling | 850 | 850 | - |
| quota saved vs. all-heavy | 64.0% | 55.5% | -8.5pp |

`score = 1 - (1.0 x under_rate + 0.5 x over_rate)`. Under-provisioning is
weighted twice as heavily as over-provisioning because the harness has already
paid for it: ticket #4 burned two haiku attempts (one off-spec PR, one 1200s
timeout) and delivered no code. An over-provisioned ticket only costs quota.

## Read the quota row carefully

v2 spends **more** quota than v1 (378 vs 306 units) and therefore "saves" less
against the all-heavy ceiling. That is not a regression. v1's apparent saving
was partly counterfeit: it under-spent even a perfect router (306 vs an oracle
floor of 394) by routing seven tasks below the tier they needed. Quota it did
not spend on the right tier, it spent again on retries. v2 lands at 378 - still
below the oracle, but only by the single instance it still under-provisions.

**Over-provision rate is where quota is genuinely wasted**, and v2 cuts it from
five instances to one.

## Confusion matrices

Rows = labeled correct, columns = chosen.

heuristic-v1:

| | light | standard | heavy | total |
| --- | ---: | ---: | ---: | ---: |
| **light** | 4 | 5 | 0 | 9 |
| **standard** | 2 | 10 | 0 | 12 |
| **heavy** | 0 | 5 | 8 | 13 |

heuristic-v2:

| | light | standard | heavy | total |
| --- | ---: | ---: | ---: | ---: |
| **light** | 8 | 1 | 0 | 9 |
| **standard** | 0 | 12 | 0 | 12 |
| **heavy** | 0 | 1 | 12 | 13 |

## Every changed constant, and the instances that justify it

v2 inherits `_score`, the keyword weights, the file-count buckets, and the
`>= 5` heavy threshold from v1 **unchanged**. The delta is entirely in the
routing rules, so the comparison isolates them.

### 1. Correctness before economy (+4 instances)

A `migration` anywhere in the text, or a `heal` whose text names a fragile
invariant (concurrency, secrets, data loss), routes heavy regardless of size.

| instance | v1 | v2 | why v1 missed it |
| --- | --- | --- | --- |
| `hist-parallel-safety` | standard | heavy | 3 files, score 2. The change is a concurrency invariant; v1's only routes to heavy were keyword accumulation and file count, and it had neither |
| `adv-quiet-concurrency-bug` | standard | heavy | describes a race in plain English - no keyword in `_HEAVY_SIGNALS` appears |
| `adv-security-heal` | standard | heavy | scored 4, one short of the threshold, for a secrets-leak fix |
| `adv-schema-migration` | standard | heavy | scored 4; a data migration with a compatibility window |

`flaky` is deliberately **not** a fragile-invariant word: `adv-flaky-test`
(labeled standard) must stay standard, and it does.

### 2. A docs prefix is decided by the prefix, not the nouns (+3 instances)

| instance | v1 | v2 | why v1 missed it |
| --- | --- | --- | --- |
| `ticket-006` | standard | light | "ARCHITECTURE.md" in the title scored +2 for architecture; the diff is a mermaid block |
| `adv-docs-scary-nouns` | standard | light | "security" and "concurrency" are the subject, not the work; scored +2 net |
| `adv-docs-design-noun` | standard | light | same via "architecture" and "design" |

No instance in the corpus that carries a `docs`-shaped prefix is labeled above
light, so the rule costs nothing.

### 3. Mechanical operations are named explicitly (+1 instance)

`typo, whitespace, reformat, formatting, bump, rename, reindex` in the *title*,
on a title that is not build-shaped.

| instance | v1 | v2 | why v1 missed it |
| --- | --- | --- | --- |
| `adv-mechanical-rename` | standard | light | 9 files triggered the `+3` bucket; the diff is a rename with zero judgement |

Bare `format` is excluded from the list on purpose - `--format` is a flag on a
real feature (`adv-code-free-diff`). A `chore:` prefix is likewise not an
operation: the incident that produced a code-free diff was labeled `chore:`.

### 4. The light tier is opt-in, never a residue (+2 instances)

v1 reached light whenever keyword arithmetic fell to `<= -3` with `est_files
<= 2`. v2 routes light only on a positive signal from rule 2 or 3.

| instance | v1 | v2 | why v1 missed it |
| --- | --- | --- | --- |
| `adv-code-free-diff` | light | standard | "format" + "README" summed to -4 on a ticket whose acceptance criteria require new code and a test. This is the shape that produced the documented code-free diff |
| `adv-unknown-file-count` | light | standard | ticket #4 as the router actually saw it: `chore:` plus the default `est_files=1` lands on exactly -3 |

This is the single most important change. Every documented incident in this
repo's history where a worker "completed" a ticket without writing code came in
through the light tier by keyword subtraction.

## The two instances v2 still gets wrong

Both are honest limits, not oversights.

- `ticket-044` (**under**, standard vs heavy) - "feat: quota/cost telemetry
  ledger with a warn-then-halt per-block budget gate", 10 files. Its shape is
  indistinguishable from `ticket-043` ("feat: reproduce-before-fix regression
  guard", 10 files, correctly standard). Separating them needs semantics no
  keyword rule has. Fixing it by file count alone would misroute `ticket-043`,
  trading one miss for another.
- `ticket-048` (**over**, standard vs light) - "chore: refresh reference-set
  snapshot and extract one practice" reads almost identically to `ticket-004`
  ("chore: reference-set miner - extract one practice"), which is labeled
  standard. One refreshes a data file, one builds a module. With empty bodies
  on both rows, no title-only rule can split them, and v2 deliberately errs
  toward the more expensive tier for the pair.

## Honest caveats

- **v2 was designed against this corpus.** The +0.2353 is an in-sample fit and
  will overstate the gain on unseen tickets. The mitigation is that all four
  rules are stated as general principles with rationales that do not reference
  a specific instance, and there are only four of them - but the number itself
  should be treated as an upper bound.
- **34 instances is small**, and 17 of them come from a single repository's
  history. Widen the corpus (`hsai corpus-build`) before treating any of these
  rates as stable.
- **v2 costs more quota.** Promoting it would move benchmark routing from 8
  heavy instances to 12. The budget gate already contains that, but it is a
  steering decision with a real price, not a free win.

## Decision

**v2 ships registered and selectable; production stays on `heuristic-v1` for
now.** `models.selection_strategy` is unchanged and `models.replay_min_score`
is set to `0.70` - just under v1's measured 0.7206, so any regression to the
current configured strategy turns CI red immediately.

Promotion is the architect's call, because it raises heavy-tier routing by half
and the delta above is in-sample. When it happens, `replay_min_score` should
ratchet to `0.90` in the same change, so the new floor is defended the moment
it is claimed.
