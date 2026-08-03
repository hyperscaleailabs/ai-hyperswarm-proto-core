"""The selection policy is data now - so it gets a strict schema and a test.

A policy file routes real quota. Anything we would not be able to explain in a
PR review (an unknown key, a threshold pair that cannot be satisfied, a weight
with the wrong sign) is rejected at load time rather than silently half-applied.
"""
from __future__ import annotations

import json

import pytest

from hsai import policy as policy_mod
from hsai.policy import PolicyError, SelectionPolicy, default_policy, from_dict, to_dict


def _doc(**overrides) -> dict:
    doc = to_dict(default_policy())
    doc.update(overrides)
    return doc


def test_round_trip_is_lossless():
    p = default_policy()
    assert from_dict(to_dict(p)) == p


def test_render_is_canonical_json_with_trailing_newline():
    text = policy_mod.render(default_policy())
    assert text.endswith("}\n")
    assert json.loads(text) == to_dict(default_policy())


def test_write_then_read_round_trips(tmp_path):
    p = SelectionPolicy(version=4, heavy_threshold=6, notes="calibrated")
    path = policy_mod.write_policy(tmp_path / ".ai-swarm" / "selection-policy.json", p)
    assert path.exists()
    assert policy_mod.read_policy(path) == p


def test_load_policy_falls_back_to_defaults_when_absent(tmp_path):
    assert policy_mod.find_policy_file(tmp_path) is None
    assert policy_mod.load_policy(tmp_path) == default_policy()


def test_load_policy_finds_the_file_walking_upward(tmp_path):
    policy_mod.write_policy(
        tmp_path / ".ai-swarm" / "selection-policy.json",
        SelectionPolicy(version=9),
    )
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert policy_mod.load_policy(nested).version == 9


class TestStrictSchema:
    def test_unknown_key_is_rejected(self):
        with pytest.raises(PolicyError, match="unknown keys"):
            from_dict(_doc(mystery_knob=1))

    def test_missing_key_is_rejected(self):
        doc = _doc()
        del doc["heavy_threshold"]
        with pytest.raises(PolicyError, match="missing required keys"):
            from_dict(doc)

    def test_non_integer_threshold_is_rejected(self):
        with pytest.raises(PolicyError, match="heavy_threshold"):
            from_dict(_doc(heavy_threshold="5"))

    def test_boolean_is_not_accepted_as_an_integer(self):
        with pytest.raises(PolicyError, match="heavy_threshold"):
            from_dict(_doc(heavy_threshold=True))

    def test_threshold_ordering_is_enforced(self):
        with pytest.raises(PolicyError, match="light_threshold must be below"):
            from_dict(_doc(light_threshold=5, heavy_threshold=5))

    def test_signal_weight_signs_are_enforced(self):
        with pytest.raises(PolicyError, match="heavy_signal_weight must be positive"):
            from_dict(_doc(heavy_signal_weight=0))
        with pytest.raises(PolicyError, match="light_signal_weight must be negative"):
            from_dict(_doc(light_signal_weight=1))

    def test_signals_must_be_lowercase_and_unique(self):
        with pytest.raises(PolicyError, match="lowercase"):
            from_dict(_doc(heavy_signals=["Architecture"]))
        with pytest.raises(PolicyError, match="duplicate"):
            from_dict(_doc(light_signals=["typo", "typo"]))

    def test_file_buckets_must_be_descending_and_cover_zero(self):
        with pytest.raises(PolicyError, match="descending"):
            from_dict(_doc(file_buckets=[[0, -1], [8, 3]]))
        with pytest.raises(PolicyError, match="min_files=0"):
            from_dict(_doc(file_buckets=[[8, 3], [4, 1]]))

    def test_version_must_be_positive(self):
        with pytest.raises(PolicyError, match="version must be >= 1"):
            from_dict(_doc(version=0))

    def test_invalid_json_is_a_policy_error(self, tmp_path):
        path = tmp_path / "selection-policy.json"
        path.write_text("{not json")
        with pytest.raises(PolicyError, match="not valid JSON"):
            policy_mod.read_policy(path)

    def test_a_policy_we_would_refuse_to_load_is_never_written(self, tmp_path):
        broken = SelectionPolicy(heavy_threshold=-5, light_threshold=5)
        with pytest.raises(PolicyError):
            policy_mod.write_policy(tmp_path / "selection-policy.json", broken)
        assert not (tmp_path / "selection-policy.json").exists()


def test_file_delta_buckets_match_the_documented_ranges():
    p = default_policy()
    assert [p.file_delta(n) for n in (0, 1, 2, 3, 4, 7, 8, 20)] == [
        -1, -1, 0, 0, 1, 1, 3, 3
    ]


def test_kind_weight_defaults_to_zero_for_unknown_kinds():
    p = default_policy()
    assert (p.kind_weight("heal"), p.kind_weight("improve")) == (2, 1)
    assert p.kind_weight("implement") == 0
    assert p.kind_weight("something-new") == 0


def test_label_is_pr_ready():
    assert SelectionPolicy(version=3).label() == "policy v3"
