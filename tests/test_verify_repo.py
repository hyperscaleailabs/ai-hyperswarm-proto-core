"""The knowledge/audit-trail integrity gate, run against the real repo.

This is the enforcement point: `hsai.verify` cannot ship as a new CI workflow
(`run_once` reverts any worker edit under `.github/workflows/`), so this test
is what makes the existing `ruff + pytest` CI job the gate instead. A failure
here means an error-severity finding survived `hsai.verify.LEGACY_ALLOWLIST`
- either a genuinely new violation, or one that needs its own allowlist entry
with a comment explaining why it is pre-existing and understood.
"""
from pathlib import Path

from hsai.config import load_config
from hsai.verify import verify_repo

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_verify_repo_passes_against_the_real_repo():
    cfg = load_config()
    report = verify_repo(REPO_ROOT, cfg)
    assert report.ok, f"hsai verify found error-severity findings:\n{report.render()}"


def test_verify_repo_is_deterministic():
    """Two runs over the same (unchanged) repo must report identically -
    otherwise the gate is noise, not a signal."""
    cfg = load_config()
    first = verify_repo(REPO_ROOT, cfg)
    second = verify_repo(REPO_ROOT, cfg)
    assert first.findings == second.findings
