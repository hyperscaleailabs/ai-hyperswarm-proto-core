# Bench corpus

Hand-authored iterations for `hsai bench` (see `src/hsai/bench.py`). Each file
is one scenario: the inputs an iteration faced, and the decisions the loop must
make from them. `hsai bench` replays every one through the real decision code -
no model, no network, no quota - and fails when a decision drifts.

These are fixtures, not recordings: they were written from behaviours the loop
has actually had to handle, so a scenario failing means the harness changed
shape, and the right response is to argue about which shape is correct - not to
edit the expectation until it passes.

## Scenario shape

```jsonc
{
  "schema_version": 1,
  "name": "implement-green",
  "description": "why this scenario exists",
  "given": {
    "ci_green": true,              // local CI before the iteration
    "has_tickets": true,           // backlog non-empty -> implement, else improve
    "ticket": {"number": 41, "title": "feat: ...", "body": "...",
               "labels": ["size:M"], "prior_attempts": 0},
    "prior_iterations": [          // this block's spend so far, for the budget gate
      {"tier": "heavy", "seconds": 900, "outcome": "merged"}
    ],
    "budget": {...},               // optional override of core.yaml's ceilings
    "agent": {"ok": true, "timed_out": false},
    "changed_paths": ["src/...", "tests/..."],
    "repro": {"fix_passes": true, "parent_passes": false},
    "ci_after_green": true,
    "review_approve": true,
    "remote_ci": "SUCCESS"
  },
  "expect": {"kind": "implement", "tier": "standard", "outcome": "merged",
             "recovered": false}
}
```

Every key in `expect` must name an observable (see `bench.Replay.actual`); a
typo is a scenario failure, never a silently skipped assertion.
