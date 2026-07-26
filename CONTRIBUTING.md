# Contributing

This repo is primarily maintained by the `hsai` autonomous loop, but human
contributions are welcome.

## Ground rules (enforced by the loop and CI)

1. **No direct commits to `main`.** Work on a branch, open a PR.
2. **Every PR links a ticket.** Use `Closes #<n>` in the body.
3. **Every PR records the model used** (if produced by an agent) and a
   **lesson learned** in `knowledge/lessons/`.
4. **CI must be green** (`ruff check .` and `pytest`) before merge.

## Local setup

```bash
pip install -e ".[dev]"
ruff check .
pytest
hsai doctor
```

## Priorities

Tickets are prioritized with labels `priority:P0` (highest) … `priority:P3`.
The loop always takes the highest-priority open ticket first.

## Knowledge base

Lessons and whitepapers are Obsidian-ready markdown. After adding notes, run
`hsai reindex` to rebuild the MOCs.
