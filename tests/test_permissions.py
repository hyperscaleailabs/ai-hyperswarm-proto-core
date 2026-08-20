import json

from hsai import permissions

ALLOWED = (
    "Bash(ruff check:*)",
    "Bash(pytest:*)",
    "Bash(python -m pytest:*)",
    "Bash(git diff:*)",
    "Bash(git status:*)",
)


def _write_profile(tmp_path, allow):
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"permissions": {"allow": list(allow)}}))
    return path


def test_load_allow_list_reads_the_committed_profile(tmp_path):
    path = _write_profile(tmp_path, ALLOWED)
    assert permissions.load_allow_list(path) == list(ALLOWED)


def test_load_allow_list_is_none_when_the_file_is_absent(tmp_path):
    assert permissions.load_allow_list(tmp_path / "nope.json") is None


def test_load_allow_list_is_none_on_invalid_json(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    assert permissions.load_allow_list(path) is None


def test_is_wildcard_bash_matches_unrestricted_forms():
    assert permissions.is_wildcard_bash("Bash")
    assert permissions.is_wildcard_bash("Bash(*)")
    assert permissions.is_wildcard_bash("Bash(*:*)")
    assert not permissions.is_wildcard_bash("Bash(ruff check:*)")
    assert not permissions.is_wildcard_bash("Bash(git diff:*)")


def test_check_profile_passes_when_the_profile_matches_and_has_no_wildcard(tmp_path):
    path = _write_profile(tmp_path, ALLOWED)
    result = permissions.check_profile(path, ALLOWED)
    assert result.ok is True
    assert "matches core.yaml" in result.message


def test_check_profile_fails_when_the_file_is_missing(tmp_path):
    missing = tmp_path / ".claude" / "settings.json"
    result = permissions.check_profile(missing, ALLOWED)
    assert result.ok is False
    assert "missing or unreadable" in result.message


def test_check_profile_fails_on_a_wildcard_bash_entry(tmp_path):
    path = _write_profile(tmp_path, (*ALLOWED, "Bash(*)"))
    result = permissions.check_profile(path, (*ALLOWED, "Bash(*)"))
    assert result.ok is False
    assert "wildcard Bash" in result.message


def test_check_profile_fails_on_drift_between_profile_and_core_yaml(tmp_path):
    path = _write_profile(tmp_path, ALLOWED)
    result = permissions.check_profile(path, ALLOWED[:-1])  # core.yaml missing one entry
    assert result.ok is False
    assert "drift" in result.message


def test_check_profile_missing_and_wildcard_report_distinct_messages(tmp_path):
    """Verification plan: the two failure modes must be tellable apart."""
    missing = permissions.check_profile(tmp_path / "nope" / "settings.json", ALLOWED)
    wildcard_path = _write_profile(tmp_path, (*ALLOWED, "Bash"))
    wildcard = permissions.check_profile(wildcard_path, (*ALLOWED, "Bash"))
    assert missing.ok is False and wildcard.ok is False
    assert missing.message != wildcard.message
    assert "missing" in missing.message
    assert "wildcard" in wildcard.message
