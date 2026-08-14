"""The decision-core benchmark gate.

This file IS the enforcement mechanism. Workers may not add or edit
`.github/workflows` (`run_once` reverts such diffs), so the committed baseline
is enforced here, inside pytest - the only gate a self-improving worker can
legitimately strengthen.
"""
import json

import pytest
import yaml

from hsai import evals
from hsai.config import CoreConfig, load_config
from hsai.models import ModelChoice, Task


def _cfg():
    return load_config()


def _root():
    return evals.repo_root()


def _cases():
    return evals.load_cases(evals.cases_path(_root()))


def _card():
    return evals.run_suite(_cfg(), _cases())


def _baseline():
    return evals.load_baseline(evals.baseline_path(_root()))


class TestCaseFile:
    """The suite is only evidence if every case traces back to real history."""

    def test_at_least_25_labeled_cases(self):
        assert len(_cases()) >= 25

    def test_every_case_has_an_id_a_source_and_expected_verdicts(self):
        for case in _cases():
            assert case.id and case.source, case
            assert case.probes
            for name, spec in case.probes.items():
                assert name in evals.FUNCTIONS
                assert "expect" in spec

    def test_every_decision_function_is_probed(self):
        probed = {name for case in _cases() for name in case.probes}
        assert probed == set(evals.FUNCTIONS)

    def test_a_malformed_case_file_is_rejected_not_silently_skipped(self, tmp_path):
        bad = tmp_path / "cases.yaml"
        bad.write_text(yaml.safe_dump({"cases": [{"id": "x", "source": "s", "probes": {}}]}))
        with pytest.raises(evals.EvalError):
            evals.load_cases(bad)

    def test_an_unknown_probe_name_is_rejected(self, tmp_path):
        bad = tmp_path / "cases.yaml"
        bad.write_text(yaml.safe_dump({"cases": [
            {"id": "x", "source": "s", "probes": {"models.pick": {"expect": "heavy"}}}
        ]}))
        with pytest.raises(evals.EvalError):
            evals.load_cases(bad)


class TestScorecard:
    def test_scorecard_reports_per_function_accuracy_and_tier_cost(self):
        card = _card()
        assert card.cases == len(_cases())
        assert set(card.scores) == set(evals.FUNCTIONS)
        for score in card.scores.values():
            assert score.total > 0
            assert 0.0 <= score.accuracy <= 1.0
        # heavy=3 / standard=2 / light=1 summed over selections, so the proxy sits
        # between "everything light" and "everything heavy".
        selections = card.scores["models.select"].total
        assert selections <= card.tier_cost <= 3 * selections

    def test_mismatches_name_the_case_ids(self):
        card = _card()
        ids = {c.id for c in _cases()}
        for m in card.mismatches:
            assert m.case_id in ids
            assert m.function in evals.FUNCTIONS

    def test_scorecard_is_machine_readable(self):
        data = _card().to_dict()
        assert json.loads(json.dumps(data)) == data
        assert set(data) >= {"accuracy", "counts", "tier_cost", "mismatches", "probes"}


class TestBaselineGate:
    """The regression gate itself: it must pass today and bite on a regression."""

    def test_committed_baseline_exists_and_matches_the_suite_shape(self):
        baseline = _baseline()
        assert baseline is not None, "evals/baseline.json must be committed"
        card = _card()
        for name, counts in baseline["counts"].items():
            assert card.scores[name].total == counts["total"], name

    def test_current_scorecard_does_not_regress_against_the_baseline(self):
        assert evals.compare(_card(), _baseline()) == []

    def test_a_degraded_selector_fails_the_gate(self):
        """Inject a heuristic that routes everything light - accuracy must drop."""

        def always_light(task: Task, cfg: CoreConfig) -> ModelChoice:
            return ModelChoice(tier="light", model=cfg.tiers["light"].model, rationale="degraded")

        degraded = evals.run_suite(_cfg(), _cases(), selector=always_light)
        baseline = _baseline()
        assert degraded.accuracy("models.select") < baseline["accuracy"]["models.select"]
        regressions = evals.compare(degraded, baseline)
        assert any(r.kind == "accuracy" and "models.select" in r.detail for r in regressions)

    def test_a_stricter_baseline_reports_a_regression(self):
        # Deliberately unreachable: one point above whatever the suite scores
        # today, so this stays a real gate even if the heuristic reaches 100%.
        stricter = dict(_baseline())
        stricter["accuracy"] = {
            **stricter["accuracy"], "models.select": _card().accuracy("models.select") + 0.01
        }
        assert evals.compare(_card(), stricter)

    def test_deleting_a_hard_case_is_a_coverage_regression(self):
        """Accuracy must not be improvable by dropping the cases that fail."""
        dropped = "select-lesson-reference-set-miner-must-not-route-light"
        kept = [c for c in _cases() if c.id != dropped]
        assert len(kept) == len(_cases()) - 1, f"{dropped} is no longer in the suite"
        trimmed = evals.run_suite(_cfg(), kept)
        assert any(r.kind == "coverage" for r in evals.compare(trimmed, _baseline()))

    def test_spending_more_quota_for_no_accuracy_gain_is_a_regression(self):
        baseline = dict(_baseline())
        baseline["tier_cost"] = _card().tier_cost - 1
        regressions = evals.compare(_card(), baseline)
        assert any(r.kind == "tier-cost" for r in regressions)

    def test_a_tier_cost_rise_is_allowed_when_accuracy_improves(self):
        baseline = dict(_baseline())
        baseline["tier_cost"] = _card().tier_cost - 1
        baseline["accuracy"] = {**baseline["accuracy"], "models.select": 0.0}
        assert evals.compare(_card(), baseline) == []

    def test_update_baseline_round_trips(self, tmp_path):
        card = _card()
        path = evals.write_baseline(tmp_path / "baseline.json", card)
        assert evals.compare(card, evals.load_baseline(path)) == []


class TestCli:
    def test_eval_command_prints_a_scorecard_and_exits_zero(self, capsys):
        from hsai.cli import main

        assert main(["eval"]) == 0
        out = capsys.readouterr().out
        assert "decision-core scorecard" in out
        assert "models.select" in out
        assert "tier-cost proxy" in out
        assert "no regression" in out

    def test_eval_exits_non_zero_against_a_stricter_baseline(self, tmp_path, capsys):
        card = _card()
        strict = dict(_baseline())
        strict["accuracy"] = {k: card.accuracy(k) + 0.01 for k in strict["accuracy"]}
        path = tmp_path / "strict.json"
        path.write_text(json.dumps(strict))

        from hsai.cli import main

        assert main(["eval", "--baseline", str(path)]) == 1
        assert "REGRESSION" in capsys.readouterr().out

    def test_eval_json_is_machine_readable_and_matches_the_committed_baseline(self, capsys):
        from hsai.cli import main

        assert main(["eval", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        baseline = _baseline()
        for name, expected in baseline["accuracy"].items():
            assert payload["scorecard"]["accuracy"][name] >= expected
        assert payload["regressions"] == []

    def test_update_baseline_rewrites_the_file(self, tmp_path, capsys):
        from hsai.cli import main

        path = tmp_path / "baseline.json"
        assert main(["eval", "--baseline", str(path), "--update-baseline"]) == 0
        assert "baseline rewritten" in capsys.readouterr().out
        assert json.loads(path.read_text())["cases"] == _card().cases

    def test_update_baseline_produces_a_visible_diff_when_the_score_moved(self, tmp_path):
        """The point of --update-baseline: the new numbers land in `git diff`."""
        from hsai.cli import main

        path = tmp_path / "baseline.json"
        stale = {**_baseline(), "tier_cost": 1, "cases": 1}
        path.write_text(json.dumps(stale, indent=2, sort_keys=True) + "\n")
        before = path.read_text()

        assert main(["eval", "--baseline", str(path), "--update-baseline"]) == 0
        after = path.read_text()
        assert after != before
        assert json.loads(after) == _card().to_dict()

    def test_the_committed_baseline_is_exactly_what_update_baseline_would_write(
        self, tmp_path
    ):
        """A stale baseline silently passes its own gate; this keeps it honest.

        Regenerate with `hsai eval --update-baseline` whenever a heuristic moves.
        """
        regenerated = evals.write_baseline(tmp_path / "baseline.json", _card())
        committed = evals.baseline_path(_root())
        assert json.loads(regenerated.read_text()) == json.loads(committed.read_text())
        assert regenerated.read_text() == committed.read_text()  # formatting too


class TestNoWorkflowFile:
    """The gate must live in pytest, not in a workflow the loop would revert."""

    def test_the_suite_ships_no_github_workflow(self):
        assert not list((_root() / ".github" / "workflows").glob("*eval*"))
