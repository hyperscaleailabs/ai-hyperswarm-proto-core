# bench

`baseline.json` is the committed reference the CI `bench` job checks every PR against:

```
hsai bench --check
```

The gate fails when `pass_rate`, `tier_agreement`, or `recovery_accuracy` drops below the
baseline, when the corpus shrinks (fewer scenarios is coverage loss), or when
`mean_seconds_per_ticket` exceeds the baseline by more than the tolerance in
`hsai.bench.SECONDS_TOLERANCE`.

Regenerate after deliberately changing the corpus or the decision code:

```
hsai bench --write-baseline
```

That refuses to run while any scenario deviates, so a baseline can only ever be written
from a bench you have already made green on purpose. The scenarios themselves live in
`tests/fixtures/trajectories/` — see the README there.
