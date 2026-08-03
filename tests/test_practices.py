import json

from hsai.config import load_config
from hsai.practices import Practice, PracticeRegistry, reconcile_registry
from hsai.proc import Proc


def test_write_and_read_round_trips_a_practice(tmp_path):
    registry = PracticeRegistry(tmp_path)
    practice = Practice(
        title="feat: adaptive budget gate",
        source_repo="openai/swarm, crewAIInc/crewAI",
        summary="Combine cost-aware retries with a warn-then-halt gate.",
        status="proposed",
        ticket=42,
        lessons=("2026-07-01-some-lesson",),
    )
    path = registry.write(practice)
    assert path.exists()

    records = registry.read_all()
    assert len(records) == 1
    rec = records[0]
    assert rec.title == "feat: adaptive budget gate"
    assert rec.source_repo == "openai/swarm, crewAIInc/crewAI"
    assert rec.status == "proposed"
    assert rec.ticket == 42
    assert rec.pr is None
    assert rec.lessons == ("2026-07-01-some-lesson",)
    assert "Combine cost-aware retries" in rec.summary

    text = path.read_text()
    assert "[[Practices MOC]]" in text
    assert "status/proposed" in text


def test_set_status_flips_in_place_and_preserves_fields(tmp_path):
    registry = PracticeRegistry(tmp_path)
    practice = Practice(
        title="chore: refresh reference snapshot",
        source_repo="run-llama/llama_index",
        summary="Adopt issue triage before filing.",
        status="proposed",
        ticket=7,
    )
    registry.write(practice)
    note_name = registry.notes()[0]

    registry.set_status(note_name, "adopted", pr=99)

    records = registry.read_all()
    assert len(records) == 1
    rec = records[0]
    assert rec.status == "adopted"
    assert rec.ticket == 7
    assert rec.pr == 99
    assert rec.title == "chore: refresh reference snapshot"  # unchanged


def test_non_proposed_filters_by_status(tmp_path):
    registry = PracticeRegistry(tmp_path)
    registry.write(Practice(title="a", source_repo="x/y", summary="s", status="proposed", ticket=1))
    registry.write(Practice(title="b", source_repo="x/y", summary="s", status="adopted", ticket=2))
    registry.write(Practice(title="c", source_repo="x/y", summary="s", status="rejected", ticket=3))

    non_proposed = registry.non_proposed()
    assert {r.title for r in non_proposed} == {"b", "c"}


def _reconcile_runner(issues_by_number):
    def runner(cmd, *, cwd=None, env=None, timeout=None, input_text=None):
        cmd = list(cmd)
        if cmd[:3] == ["gh", "issue", "view"]:
            num = int(cmd[3])
            data = issues_by_number.get(num)
            if data is None:
                return Proc(cmd, 1, "", "not found")
            return Proc(cmd, 0, json.dumps(data), "")
        return Proc(cmd, 0, "", "")

    return runner


def test_reconcile_flips_adopted_on_closed_ticket_and_rejected_on_blocked(tmp_path):
    cfg = load_config()
    registry = PracticeRegistry(tmp_path)
    registry.write(
        Practice(
            title="feat: adopted thing", source_repo="a/b", summary="s",
            status="proposed", ticket=10,
        )
    )
    registry.write(
        Practice(
            title="feat: rejected thing", source_repo="a/b", summary="s",
            status="proposed", ticket=11,
        )
    )
    registry.write(
        Practice(
            title="feat: still open thing", source_repo="a/b", summary="s",
            status="proposed", ticket=12,
        )
    )

    runner = _reconcile_runner({
        10: {"number": 10, "title": "feat: adopted thing", "labels": [],
             "assignees": [], "body": "", "state": "CLOSED"},
        11: {"number": 11, "title": "feat: rejected thing",
             "labels": [{"name": "blocked"}], "assignees": [], "body": "",
             "state": "OPEN"},
        12: {"number": 12, "title": "feat: still open thing", "labels": [],
             "assignees": [], "body": "", "state": "OPEN"},
    })

    flipped = reconcile_registry(cfg, tmp_path, runner=runner)
    assert len(flipped) == 2
    assert any("adopted" in line for line in flipped)
    assert any("rejected" in line for line in flipped)

    records = {r.title: r for r in registry.read_all()}
    assert records["feat: adopted thing"].status == "adopted"
    assert records["feat: rejected thing"].status == "rejected"
    assert records["feat: still open thing"].status == "proposed"


def test_reconcile_skips_practices_without_a_ticket(tmp_path):
    cfg = load_config()
    registry = PracticeRegistry(tmp_path)
    registry.write(Practice(title="feat: no ticket yet", source_repo="a/b", summary="s"))
    flipped = reconcile_registry(cfg, tmp_path, runner=_reconcile_runner({}))
    assert flipped == []
    assert registry.read_all()[0].status == "proposed"
