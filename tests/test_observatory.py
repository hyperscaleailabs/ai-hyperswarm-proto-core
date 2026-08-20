"""The reference-set observatory: cached digests, deltas, dossiers, staleness.

Every test here drives `gh` through a fake runner returning canned payloads -
nothing in this module is allowed to touch the network.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hsai import observatory
from hsai.config import load_config
from hsai.knowledge import KnowledgeBase, LessonRecord
from hsai.observatory import (
    ADOPTED_FROM_PROJECT_HEADING,
    DELTA_HEADING,
    DIGEST_HEADING,
    Digest,
    ObservatoryConfig,
    adopted_index,
    diff_digest,
    dossier_name,
    fetch_digest,
    observe,
    read_observation,
    render_section,
    stale_repos,
    staleness_line,
)
from hsai.proc import Proc

REPO = "openai/swarm"
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

FIRST_COMMITS = [("aaa111", "feat: add handoffs"), ("bbb222", "docs: readme")]
SECOND_COMMITS = [("ccc333", "fix: retry the tool loop"), *FIRST_COMMITS]


def _cfg():
    return load_config()


def _runner(
    *,
    commits=FIRST_COMMITS,
    workflows=("ci.yml",),
    readme="# swarm\n\nlightweight orchestration",
    default_branch="main",
    calls=None,
):
    """A fake `gh api` answering the four calls a digest is built from."""

    def runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        if calls is not None:
            calls.append(list(cmd))
        target = cmd[2] if len(cmd) > 2 else ""
        if target.endswith("/readme"):
            return Proc(cmd, 0, readme, "")
        if "/commits" in target:
            return Proc(cmd, 0, "".join(f"{sha}\t{subject}\n" for sha, subject in commits), "")
        if "/contents/.github/workflows" in target:
            return Proc(cmd, 0, "".join(f"{w}\n" for w in workflows), "")
        return Proc(cmd, 0, f"{default_branch}\n", "")

    return runner


def _broken_runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
    return Proc(cmd, 127, "", "gh: command not found")


# --- the cache ------------------------------------------------------------------

def test_first_observation_writes_valid_json_under_knowledge_reference(tmp_path):
    cfg = _cfg()
    [obs] = observe(cfg, tmp_path, repos=[REPO], runner=_runner(), force=True, now=NOW)

    path = tmp_path / "knowledge" / "reference" / "openai__swarm.json"
    assert path.is_file()
    assert obs.path == str(path) and obs.refreshed is True

    payload = json.loads(path.read_text())          # valid JSON, not just a blob
    assert payload["digest"]["repo"] == REPO
    assert payload["digest"]["head_sha"] == "aaa111"
    assert payload["digest"]["default_branch"] == "main"
    assert payload["digest"]["workflows"] == ["ci.yml"]
    assert payload["digest"]["commits"][0] == ["aaa111", "feat: add handoffs"]
    assert payload["delta"]["baseline"] is True


def test_first_observation_is_a_baseline_not_thirty_new_commits(tmp_path):
    """The AC that matters most: "never looked here" != "everything changed"."""
    cfg = _cfg()
    [obs] = observe(cfg, tmp_path, repos=[REPO], runner=_runner(), force=True, now=NOW)

    assert obs.delta.baseline is True
    assert obs.delta.new_commits == ()
    assert obs.delta.added_workflows == () and obs.delta.removed_workflows == ()
    assert obs.delta.readme_changed is False
    assert "Baseline observation" in obs.delta.render()


def test_diff_digest_on_a_first_ever_observation_is_pure_and_baseline_marked():
    fresh = Digest(repo=REPO, head_sha="aaa111", commits=tuple(FIRST_COMMITS))
    delta = diff_digest(None, fresh)
    assert delta.baseline is True and delta.new_commits == ()
    assert delta.changed is False


def test_second_observation_of_an_unchanged_repo_reports_no_change(tmp_path):
    cfg = _cfg()
    observe(cfg, tmp_path, repos=[REPO], runner=_runner(), force=True, now=NOW)
    [obs] = observe(
        cfg, tmp_path, repos=[REPO], runner=_runner(), force=True,
        now=NOW + timedelta(hours=12),
    )

    assert obs.delta.baseline is False
    assert obs.delta.new_commits == ()
    assert obs.delta.added_workflows == () and obs.delta.removed_workflows == ()
    assert obs.delta.readme_changed is False
    assert obs.delta.changed is False
    assert "No new commits" in obs.delta.render()
    assert obs.delta.summary() == "no change since the last observation"


def test_second_observation_reports_only_what_moved(tmp_path):
    cfg = _cfg()
    observe(cfg, tmp_path, repos=[REPO], runner=_runner(), force=True, now=NOW)
    [obs] = observe(
        cfg, tmp_path, repos=[REPO], force=True, now=NOW + timedelta(days=1),
        runner=_runner(
            commits=SECOND_COMMITS,
            workflows=("ci.yml", "stale.yml"),
            readme="# swarm\n\nrewritten",
        ),
    )

    assert obs.delta.new_commits == ("fix: retry the tool loop",)   # only the new one
    assert obs.delta.added_workflows == ("stale.yml",)
    assert obs.delta.removed_workflows == ()
    assert obs.delta.readme_changed is True
    assert obs.delta.previous_head == "aaa111"
    rendered = obs.delta.render()
    assert "fix: retry the tool loop" in rendered
    assert "feat: add handoffs" not in rendered    # already studied last cycle
    assert "`stale.yml`" in rendered


def test_a_removed_workflow_is_reported(tmp_path):
    cfg = _cfg()
    observe(
        cfg, tmp_path, repos=[REPO], force=True, now=NOW,
        runner=_runner(workflows=("ci.yml", "release.yml")),
    )
    [obs] = observe(
        cfg, tmp_path, repos=[REPO], force=True, now=NOW + timedelta(days=1),
        runner=_runner(workflows=("ci.yml",)),
    )
    assert obs.delta.removed_workflows == ("release.yml",)


def test_an_empty_fetch_never_overwrites_a_stored_digest(tmp_path):
    """`gh` missing must not erase the history this module exists to keep."""
    cfg = _cfg()
    observe(cfg, tmp_path, repos=[REPO], runner=_runner(), force=True, now=NOW)
    before = (tmp_path / "knowledge" / "reference" / "openai__swarm.json").read_bytes()

    [obs] = observe(cfg, tmp_path, repos=[REPO], runner=_broken_runner, force=True, now=NOW)

    assert obs.refreshed is False
    assert obs.digest.head_sha == "aaa111"
    assert (tmp_path / "knowledge" / "reference" / "openai__swarm.json").read_bytes() == before


def test_an_empty_first_fetch_stores_nothing(tmp_path):
    cfg = _cfg()
    [obs] = observe(cfg, tmp_path, repos=[REPO], runner=_broken_runner, force=True, now=NOW)
    assert obs.refreshed is False and obs.path == ""
    assert obs.delta.baseline is True
    assert list((tmp_path / "knowledge" / "reference").glob("*.json")) == []


def test_a_corrupt_cache_file_reads_as_never_observed(tmp_path):
    directory = observatory.reference_dir(tmp_path, _cfg())
    (directory / "openai__swarm.json").write_text("{not json")
    assert read_observation(directory, REPO) is None


def test_fetch_digest_caps_the_stored_text():
    """Every stored field is bounded, so the cache cannot bloat the repo."""
    ocfg = ObservatoryConfig(readme_chars=10, subject_chars=5, commits=1)
    digest = fetch_digest(
        REPO, runner=_runner(readme="x" * 5000, commits=SECOND_COMMITS), ocfg=ocfg, now=NOW
    )
    assert len(digest.readme_excerpt) == 10
    assert len(digest.commits) == 1
    assert digest.commits[0][1] == "fix: "
    assert digest.readme_hash


def test_a_readme_change_beyond_the_stored_excerpt_is_still_detected():
    ocfg = ObservatoryConfig(readme_chars=5)
    first = fetch_digest(REPO, runner=_runner(readme="same-" + "a" * 100), ocfg=ocfg, now=NOW)
    second = fetch_digest(REPO, runner=_runner(readme="same-" + "b" * 100), ocfg=ocfg, now=NOW)
    assert first.readme_excerpt == second.readme_excerpt   # excerpts are identical...
    assert diff_digest(first, second).readme_changed is True  # ...the hash is not


# --- refresh policy --------------------------------------------------------------

def test_a_fresh_cache_is_served_without_spending_an_api_call(tmp_path):
    cfg = _cfg()
    observe(cfg, tmp_path, repos=[REPO], runner=_runner(), force=True, now=NOW)

    calls: list[list[str]] = []
    [obs] = observe(
        cfg, tmp_path, repos=[REPO], runner=_runner(calls=calls), now=NOW + timedelta(days=1)
    )
    assert calls == []                       # nothing re-fetched
    assert obs.refreshed is False
    assert obs.digest.head_sha == "aaa111"   # ...and the cached digest still answers


def test_a_stale_cache_is_refetched_without_the_force_flag(tmp_path):
    cfg = _cfg()
    observe(cfg, tmp_path, repos=[REPO], runner=_runner(), force=True, now=NOW)

    calls: list[list[str]] = []
    stale_days = ObservatoryConfig.from_core(cfg).stale_days
    [obs] = observe(
        cfg, tmp_path, repos=[REPO], runner=_runner(calls=calls),
        now=NOW + timedelta(days=stale_days + 1),
    )
    assert calls and obs.refreshed is True


# --- the adopted-practice index ---------------------------------------------------

def _record(note_name: str, title: str, body: str) -> LessonRecord:
    return LessonRecord(
        note_name=note_name, title=title, outcome="pass", kind="implement",
        tags=("lesson",), lesson_text="", body=body,
    )


def test_adopted_index_maps_each_project_to_the_lessons_that_cite_it():
    lessons = [
        _record("2026-01-02-b", "feat: handoffs", "## References\n- `openai/swarm`\n"),
        _record("2026-01-01-a", "feat: routing", "## References\n- `openai/swarm`\n- `microsoft/JARVIS`"),
        _record("2026-01-03-c", "feat: unrelated", "nothing cited here"),
    ]
    index = adopted_index(lessons, ["openai/swarm", "microsoft/JARVIS", "crewAIInc/crewAI"])

    # sorted by note name, so the rendered block never diffs on dict order
    assert [c.note_name for c in index["openai/swarm"]] == ["2026-01-01-a", "2026-01-02-b"]
    assert [c.title for c in index["microsoft/JARVIS"]] == ["feat: routing"]
    assert index["crewAIInc/crewAI"] == ()


def test_adopted_index_is_case_insensitive():
    lessons = [_record("n", "t", "adapted from OpenAI/Swarm's handoff model")]
    index = adopted_index(lessons, ["openai/swarm"])
    assert [c.note_name for c in index["openai/swarm"]] == ["n"]


# --- the rendered context-pack section ---------------------------------------------

def test_render_section_puts_the_delta_first_then_digest_then_adopted(tmp_path):
    cfg = _cfg()
    observe(cfg, tmp_path, repos=[REPO], runner=_runner(), force=True, now=NOW)
    [obs] = observe(
        cfg, tmp_path, repos=[REPO], force=True, now=NOW + timedelta(days=1),
        runner=_runner(commits=SECOND_COMMITS),
    )
    index = adopted_index(
        [_record("2026-01-01-a", "feat: routing", "- `openai/swarm`")], [REPO]
    )

    text = render_section(obs, index)
    assert (
        text.index(DELTA_HEADING)
        < text.index(DIGEST_HEADING)
        < text.index(ADOPTED_FROM_PROJECT_HEADING)
    )
    assert "fix: retry the tool loop" in text
    assert "lightweight orchestration" in text          # baseline README excerpt
    assert "[[2026-01-01-a]] - feat: routing" in text   # citation-grade, resolves in Obsidian


def test_render_section_says_so_when_nothing_has_been_adopted(tmp_path):
    cfg = _cfg()
    [obs] = observe(cfg, tmp_path, repos=[REPO], runner=_runner(), force=True, now=NOW)
    text = render_section(obs, {})
    assert ADOPTED_FROM_PROJECT_HEADING in text
    assert observatory.NOTHING_ADOPTED in text


# --- staleness (surfaced in DIRECTION.md) --------------------------------------------

def test_staleness_counts_projects_that_have_gone_dark(tmp_path):
    cfg = _cfg()
    assert len(stale_repos(cfg, tmp_path, now=NOW)) == len(cfg.reference_top10)

    observe(cfg, tmp_path, repos=[REPO], runner=_runner(), force=True, now=NOW)
    stale = stale_repos(cfg, tmp_path, now=NOW)
    assert REPO not in stale
    assert len(stale) == len(cfg.reference_top10) - 1

    line = staleness_line(cfg, tmp_path, now=NOW)
    assert f"{len(stale)}/{len(cfg.reference_top10)}" in line
    assert "hsai observe --refresh" in line


def test_staleness_line_when_everything_is_current(tmp_path):
    cfg = _cfg()
    repos = [r.repo for r in cfg.reference_top10]
    observe(cfg, tmp_path, repos=repos, runner=_runner(), force=True, now=NOW)
    line = staleness_line(cfg, tmp_path, now=NOW)
    assert "all" in line and "not observed" not in line


def test_an_observation_older_than_the_threshold_is_stale(tmp_path):
    cfg = _cfg()
    stale_days = ObservatoryConfig.from_core(cfg).stale_days
    observe(cfg, tmp_path, repos=[REPO], runner=_runner(), force=True, now=NOW)
    assert REPO not in stale_repos(cfg, tmp_path, now=NOW + timedelta(days=stale_days - 1))
    assert REPO in stale_repos(cfg, tmp_path, now=NOW + timedelta(days=stale_days + 1))


# --- dossiers -------------------------------------------------------------------------

def _seed_citing_lesson(root: Path) -> str:
    lessons = root / "knowledge" / "lessons"
    lessons.mkdir(parents=True, exist_ok=True)
    (lessons / "2026-01-01-handoffs.md").write_text(
        "---\ntags:\n  - lesson\n  - outcome/pass\n  - kind/implement\ncreated: 2026-01-01\n---\n\n"
        "# feat: worker handoffs\n\n## Lesson learned\nSmall cores win.\n\n"
        "## References (reference-set evidence)\n- `openai/swarm`\n"
    )
    return "2026-01-01-handoffs"


def test_reindex_generates_one_dossier_per_project_and_a_reference_moc(tmp_path):
    cfg = _cfg()
    note = _seed_citing_lesson(tmp_path)
    observe(cfg, tmp_path, repos=[REPO], runner=_runner(), force=True, now=NOW)

    written = {p.name for p in KnowledgeBase.from_config(cfg, tmp_path).reindex_mocs()}
    assert "Reference Set MOC.md" in written
    assert len(cfg.reference_top10) == 10
    for ref in cfg.reference_top10:
        assert f"{dossier_name(ref.repo)}.md" in written

    dossier = (tmp_path / "knowledge" / "reference" / "swarm.md").read_text()
    assert "# openai/swarm" in dossier
    assert f"[[{note}]] - feat: worker handoffs" in dossier   # wikilink to the citing lesson
    assert "[[Reference Set MOC]]" in dossier
    assert "aaa111" in dossier                                 # last observed head
    assert "## Open questions" in dossier

    moc = (tmp_path / "knowledge" / "MOCs" / "Reference Set MOC.md").read_text()
    assert "[[swarm]]" in moc and "`openai/swarm`" in moc
    assert "2026-08-20" in moc                                 # last observed date
    for ref in cfg.reference_top10:
        assert f"[[{dossier_name(ref.repo)}]]" in moc
    root_moc = (tmp_path / "knowledge" / "MOCs" / "Knowledge Base MOC.md").read_text()
    assert "[[Reference Set MOC]] - 10 reference project dossier(s)" in root_moc


def test_a_never_observed_project_gets_an_honest_dossier(tmp_path):
    cfg = _cfg()
    KnowledgeBase.from_config(cfg, tmp_path).reindex_mocs()
    dossier = (tmp_path / "knowledge" / "reference" / "swarm.md").read_text()
    assert "_never observed_" in dossier
    assert "Never observed" in dossier                       # and an open question about it
    assert "Nothing adopted from this project yet." in dossier


def test_dossier_regeneration_is_stable_apart_from_the_timestamp(tmp_path, monkeypatch):
    """`hsai reindex` twice on unchanged data must not churn the vault."""
    cfg = _cfg()
    _seed_citing_lesson(tmp_path)
    observe(cfg, tmp_path, repos=[REPO], runner=_runner(), force=True, now=NOW)
    kb = KnowledgeBase.from_config(cfg, tmp_path)
    dossier = tmp_path / "knowledge" / "reference" / "swarm.md"

    monkeypatch.setattr("hsai.knowledge._today", lambda: "2026-08-20")
    kb.reindex_mocs()
    first = dossier.read_text()

    monkeypatch.setattr("hsai.knowledge._today", lambda: "2026-08-21")
    kb.reindex_mocs()
    second = dossier.read_text()

    differing = [
        (a, b) for a, b in zip(first.splitlines(), second.splitlines(), strict=True) if a != b
    ]
    assert differing == [("updated: 2026-08-20", "updated: 2026-08-21")]


def test_observe_touches_nothing_outside_the_observatory_directory(tmp_path):
    """`hsai observe` owns the digest cache and the dossiers - nothing else."""
    cfg = _cfg()
    _seed_citing_lesson(tmp_path)
    (tmp_path / "governance").mkdir()
    (tmp_path / "governance" / "DIRECTION.md").write_text("steering doc\n")
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}

    observe(cfg, tmp_path, runner=_runner(), force=True, now=NOW)
    KnowledgeBase.from_config(cfg, tmp_path).write_reference_dossiers()

    after = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    for path, content in before.items():
        assert after[path] == content, f"{path} was modified"
    reference = tmp_path / "knowledge" / "reference"
    new_files = set(after) - set(before)
    assert new_files, "observe must have written something"
    assert all(p.parent == reference for p in new_files), sorted(str(p) for p in new_files)
    # every pinned project now has both a digest and a dossier
    assert len(list(reference.glob("*.json"))) == len(cfg.reference_top10)
    assert len(list(reference.glob("*.md"))) == len(cfg.reference_top10)


def test_dossier_name_and_digest_filename_are_collision_free():
    """One flat directory holds ten dossiers and ten caches - names must not clash."""
    cfg = _cfg()
    assert dossier_name("openai/swarm") == "swarm"
    assert observatory.digest_filename("openai/swarm") == "openai__swarm.json"
    assert observatory.digest_filename("SWE-agent/SWE-agent") == "SWE-agent__SWE-agent.json"
    assert len({dossier_name(r.repo) for r in cfg.reference_top10}) == len(cfg.reference_top10)
    assert (
        len({observatory.digest_filename(r.repo) for r in cfg.reference_top10})
        == len(cfg.reference_top10)
    )
