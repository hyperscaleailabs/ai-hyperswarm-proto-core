# Bench baseline

`baseline.json` is the committed floor `hsai bench --check` gates on, and the
`bench` job in `.github/workflows/ci.yml` runs that check on every PR.

```
hsai bench                  # replay the corpus, human-readable
hsai bench --json           # the full report, including each replayed trajectory
hsai bench --check          # additionally fail on regression against this file
hsai bench --update-baseline  # move the floor (deliberate, never in CI)
```

Gated: `pass_rate`, `tier_agreement`, `recovery_accuracy`, and the corpus size
(so deleting an inconvenient scenario is a regression, not a fix). *Not* gated:
wall-clock, which measures the CI runner rather than the harness and would only
buy flaky failures - `hsai bench` reports mean seconds/ticket either way.

Raising a number here means the loop got better and the new floor is being
locked in. Lowering one is a deliberate, reviewable admission that it got worse,
and belongs in a PR that says why.
