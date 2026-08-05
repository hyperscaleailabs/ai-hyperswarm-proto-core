"""The cycle journal: append-only step records, lossless reads, replay-once.

These exercise the module directly; ``tests/test_cycle.py`` proves the same
contract end to end through ``run_cycle``.
"""
from __future__ import annotations

import json

import pytest

from hsai import journal


def _payloads() -> dict[str, dict]:
    """One representative payload for every step a block journals."""
    return {
        "synthesis": {"ran": True, "filed": [11, 12], "error": ""},
        "iteration": {"describe": "iteration(kind=implement)", "pr": 501, "merged": True},
        "whitepaper": {"note": "2026-08-05-synthesis-after-3-lessons"},
        "articles": {"paths": ["knowledge/articles/a-cto.md"]},
        "direction": {"path": "governance/DIRECTION.md", "mocs": ["knowledge/MOCs/x.md"]},
        "review_issue": {"number": 940},
        "governance_pr": {"number": 771},
    }


def test_every_step_round_trips_through_one_json_line_per_record(tmp_path):
    path = journal.journal_path(tmp_path, 3)
    payloads = _payloads()
    for step, payload in payloads.items():
        journal.append_record(path, journal.JournalRecord(step=step, key="block", payload=payload))

    lines = path.read_text().splitlines()
    assert len(lines) == len(payloads)          # one JSON line per completed step
    for line in lines:
        json.loads(line)                        # append-only and always parseable

    records = journal.read_records(path)
    assert [r.step for r in records] == list(payloads)
    assert {r.payload["number"] for r in records if r.step.endswith(("issue", "pr"))} == {940, 771}
    for record in records:
        assert record.payload == payloads[record.step]   # no loss, no coercion
        assert record.status == journal.DONE
        assert record.created                             # every record is stamped


def test_read_records_drops_a_torn_trailing_line_so_the_step_is_retried(tmp_path):
    path = journal.journal_path(tmp_path, 4)
    journal.append_record(path, journal.JournalRecord(step="synthesis", key="block", payload={}))
    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"step": "whitepaper", "key": "blo')   # killed mid-write

    records = journal.read_records(path)
    assert [r.step for r in records] == ["synthesis"]


def test_once_runs_the_callable_once_and_replays_the_payload_thereafter(tmp_path):
    jr = journal.open_journal(tmp_path, 5)
    calls: list[int] = []

    def work() -> dict:
        calls.append(1)
        return {"number": 42}

    assert journal.once(jr, "review_issue", "block", work) == {"number": 42}
    assert journal.once(jr, "review_issue", "block", work) == {"number": 42}
    assert calls == [1]
    assert jr.replayed == ["review_issue:block"]

    # A fresh process sees the same thing: the record is on disk, not in memory.
    reopened = journal.open_journal(tmp_path, 5)
    assert reopened.resumed
    assert journal.once(reopened, "review_issue", "block", work) == {"number": 42}
    assert calls == [1]


def test_once_without_a_journal_is_a_plain_call(tmp_path):
    calls: list[int] = []
    for _ in range(2):
        journal.once(None, "synthesis", "block", lambda: calls.append(1))
    assert len(calls) == 2
    assert not journal.cycles_dir(tmp_path).exists()


def test_a_crashed_step_leaves_no_record_so_resume_retries_it(tmp_path):
    jr = journal.open_journal(tmp_path, 6)

    def boom() -> dict:
        raise RuntimeError("worker died mid-step")

    with pytest.raises(RuntimeError):
        journal.once(jr, "whitepaper", "block", boom)

    assert journal.read_records(jr.path) == []
    assert journal.open_journal(tmp_path, 6).find("whitepaper", "block") is None


def test_latest_resumable_skips_closed_journals(tmp_path):
    # 1: still open. 2: halted by the budget gate. 3: completed. 4: open, newest.
    journal.append_record(journal.journal_path(tmp_path, 1),
                          journal.JournalRecord(step="synthesis", key="block"))
    journal.append_record(journal.journal_path(tmp_path, 2),
                          journal.JournalRecord(step="budget_halt", key="block",
                                                status=journal.HALTED))
    journal.append_record(journal.journal_path(tmp_path, 3),
                          journal.JournalRecord(step="block", key="complete",
                                                status=journal.COMPLETE))
    journal.append_record(journal.journal_path(tmp_path, 4),
                          journal.JournalRecord(step="synthesis", key="block"))

    assert journal.resumable_indices(tmp_path) == [1, 4]
    assert journal.latest_resumable(tmp_path) == 4
    assert journal.open_journal(tmp_path, 2).terminal().status == journal.HALTED


def test_latest_resumable_is_none_when_nothing_was_ever_journaled(tmp_path):
    assert journal.resumable_indices(tmp_path) == []
    assert journal.latest_resumable(tmp_path) is None


def test_dry_run_journals_into_a_separate_file(tmp_path):
    live = journal.journal_path(tmp_path, 9)
    rehearsal = journal.journal_path(tmp_path, 9, dry_run=True)
    assert live != rehearsal

    journal.append_record(rehearsal, journal.JournalRecord(step="synthesis", key="block"))
    assert not live.exists()
    assert journal.latest_resumable(tmp_path) is None            # live: nothing to resume
    assert journal.latest_resumable(tmp_path, dry_run=True) == 9


def test_summary_is_one_line_naming_the_replayed_steps(tmp_path):
    jr = journal.open_journal(tmp_path, 12)
    for step in ("synthesis", "iteration", "whitepaper"):
        journal.once(jr, step, "block", lambda: {})
    replayed = journal.open_journal(tmp_path, 12)
    for step in ("synthesis", "iteration", "whitepaper"):
        journal.once(replayed, step, "block", lambda: {})

    line = replayed.summary()
    assert "\n" not in line
    assert line.startswith("resume: replayed 3 recorded step(s) from block 12")
    assert "whitepaper:block" in line
