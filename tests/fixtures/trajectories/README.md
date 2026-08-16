# Bench corpus

Hand-authored iteration scenarios replayed by `hsai bench` (see `src/hsai/bench.py`).
Each `*.json` file is one scenario: the world an iteration ran in (`given`) and how
the orchestration is expected to end (`expect`).

These are **not** recorded `.hsai/trajectories/` artifacts. A real trajectory is raw,
local, gitignored telemetry; a scenario is a curated, committed fixture with an
asserted outcome. They share a vocabulary (`kind`, `tier`, `outcome`) on purpose, so a
real trajectory can be hand-lifted into a new scenario when the loop does something
surprising.

## Adding a scenario

1. Copy the closest existing file, give it a new `id` and a filename that sorts sensibly.
2. Fill in `given`; leave out anything the scenario does not exercise.
3. Run `hsai bench` and read the replay line it prints for your scenario.
4. Only once you agree the replayed behaviour is *correct*, write it into `expect`.
5. Regenerate the committed baseline: `hsai bench --write-baseline`.

Never edit `expect` to make a red bench go green without first deciding the new
behaviour is the behaviour you want - that inverts the whole point of the gate.

## `given` keys

| key | meaning |
| --- | --- |
| `ci_green` | was local CI green before the iteration started |
| `open_tickets` | number of claimable tickets in the backlog |
| `ticket` | `{number, title, body, labels}` - drives tier selection and the guards |
| `attempts` | this ticket's attempt number, including the one being replayed |
| `changed_paths` | what the agent left in the worktree |
| `agent` | `{ok, timed_out, seconds}` |
| `repro` | `{fix_pytest_ok, parent_pytest_ok}` for the reproduce-before-fix guard |
| `local_ci_after_green` | local CI after the agent ran (defaults to `agent.ok`) |
| `review_output` | what the independent reviewer replied (parsed fail-closed) |
| `remote_ci` | the source-of-truth remote conclusion |
| `block_spend` | the block's spend so far, fed to the budget gate |
| `budget` | optional ceiling override (default: `budget:` from `core.yaml`) |

## `expect` keys

Any of `kind`, `tier`, `outcome`, `recovery`, `guard`. Omitted keys are not checked.
