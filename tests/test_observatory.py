"""The reference-set observatory: cache, delta, adoption index, dossiers.

Every test here injects a fake `gh` runner returning canned API payloads - no
test in this file touches the network.
"""
import hashlib
import json
from datetime import datetime, timedelta, timezone

from hsai import observatory
from hsai import practices as practices_mod
from hsai.config import load_config
from hsai.knowledge import KnowledgeBase, Lesson
from hsai.observatory import (
    ADOPTED_FROM_HEADING,
    BASELINE_HEADING,
    DELTA_HEADING,
    REFERENCE_DIR_DEFAULT,
    Adoption,
    ObservatoryConfig,
    build_adoption_index,
    fetch_digest,
    observe,
    observe_all,
    read_digest,
    render_section,
    stale_report,
)
from hsai.proc import Proc

README_V1 = "# llama_index\n\nA data framework for LLM applications.\n"
README_V2 = "# llama_index\n\nAn agent AND data framework for LLM applications.\n"

FIRST_COMMITS = [("aaa1", "feat: add sync-docs workflow"), ("aaa0", "fix: pin the mirror")]


def _fake_gh(*, commits=(), workflows=(), readme=README_V1, branch="main", fail=False):
    """A `gh` that answers the observatory's four API calls from canned payloads."""
    calls: list[list[str]] = []

    def runner(cmd, *, cwd=None, env=None, env_remove=None, timeout=None, input_text=None):
        calls.append(list(cmd))
        if fail:
            return Proc(cmd, 1, "", "gh: command not found")
        path = cmd[2] if len(cmd) > 2 else ""
        if path.endswith("/readme"):
            return Proc(cmd, 0, readme, "")
        if "/commits" in path:
            return Proc(cmd, 0, "".join(f"{sha}\t{subject}\n" for sha, subject in commits), "")
        if path.endswith("/workflows"):
            return Proc(cmd, 0, "".join(f"{name}\n" for name in workflows), "")
        return Proc(cmd, 0, f"{branch}\n", "")

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


# --- the digest cache ---------------------------------------------------------

def test_first_observation_writes_a_valid_json_cache_entry(tmp_path):
    runner = _fake_gh(commits=FIRST_COMMITS, workflows=["ci.yml", "stale_bot.yml"])

    digest = observe(tmp_path, "run-llama/llama_index", runner=runner)

    path = tmp_path / "run-llama__llama_index.json"
    assert path.is_file()
    payload = json.loads(path.read_text())          # valid JSON, not a blob of prose
    assert payload["repo"] == "run-llama/llama_index"
    assert payload["default_branch"] == "main"
    assert payload["head_sha"] == "aaa1"
    assert [c["subject"] for c in payload["commits"]] == [s for _, s in FIRST_COMMITS]
    assert payload["workflows"] == ["ci.yml", "stale_bot.yml"]
    assert payload["readme_hash"] and payload["readme_excerpt"].startswith("# llama_index")
    assert payload["schema_version"] == observatory.SCHEMA_VERSION
    # and it round-trips back through the reader
    assert read_digest(tmp_path, "run-llama/llama_index") == digest


def test_observation_only_ever_shells_out_to_gh(tmp_path):
    runner = _fake_gh(commits=FIRST_COMMITS, workflows=["ci.yml"])
    observe(tmp_path, "o/r", runner=runner)
    assert runner.calls
    assert all(c[:2] == ["gh", "api"] for c in runner.calls)


def test_read_digest_tolerates_a_missing_or_corrupt_entry(tmp_path):
    assert read_digest(tmp_path, "o/r") is None
    (tmp_path / "o__r.json").write_text("{ not json")
    assert read_digest(tmp_path, "o/r") is None


def test_stored_readme_is_capped_so_the_vault_cannot_bloat():
    big = "x" * 10_000
    digest = fetch_digest(
        "o/r",
        runner=_fake_gh(commits=FIRST_COMMITS, workflows=[], readme=big),
        readme_bytes=100,
    )
    assert len(digest.readme_excerpt) == 100
    # the hash still covers the WHOLE readme, so truncation cannot hide a change
    assert digest.readme_hash == hashlib.sha256(big.encode()).hexdigest()


def test_stored_commit_window_is_capped():
    many = [(f"sha{i:03d}", f"feat: change {i}") for i in range(50)]
    digest = fetch_digest("o/r", runner=_fake_gh(commits=many, workflows=[]), commits=5)
    assert len(digest.commits) == 5
    assert digest.head_sha == "sha000"


# --- baseline vs delta --------------------------------------------------------

def test_first_observation_is_a_baseline_not_a_wall_of_new_commits(tmp_path):
    digest = observe(
        tmp_path, "o/r", runner=_fake_gh(commits=FIRST_COMMITS, workflows=["ci.yml"])
    )

    delta = digest.delta
    assert delta.baseline is True
    assert delta.new_commits == ()          # "new since last cycle" is undefined at baseline
    assert delta.added_workflows == () and delta.removed_workflows == ()
    assert delta.readme_changed is False
    assert delta.changed is False
    assert "BASELINE" in delta.render()
    assert delta.summary() == "baseline (first observation)"


def test_second_observation_of_an_unchanged_repo_reports_no_change(tmp_path):
    observe(tmp_path, "o/r", runner=_fake_gh(commits=FIRST_COMMITS, workflows=["ci.yml"]))

    again = observe(tmp_path, "o/r", runner=_fake_gh(commits=FIRST_COMMITS, workflows=["ci.yml"]))

    delta = again.delta
    assert delta.baseline is False
    assert delta.new_commits == ()
    assert delta.added_workflows == () and delta.removed_workflows == ()
    assert delta.readme_changed is False
    assert delta.changed is False
    assert delta.summary() == "no change"
    assert "New commits: none" in delta.render()


def test_delta_reports_new_commits_added_workflows_and_a_moved_readme(tmp_path):
    observe(tmp_path, "o/r", runner=_fake_gh(commits=FIRST_COMMITS, workflows=["ci.yml"]))

    moved = [("bbb3", "feat: retain sources"), ("bbb2", "chore: refresh skill refs")]
    digest = observe(
        tmp_path, "o/r",
        runner=_fake_gh(
            commits=moved + FIRST_COMMITS,
            workflows=["ci.yml", "sync-docs.yml"],
            readme=README_V2,
        ),
    )

    delta = digest.delta
    assert delta.new_commits == ("feat: retain sources", "chore: refresh skill refs")
    assert delta.added_workflows == ("sync-docs.yml",)
    assert delta.removed_workflows == ()
    assert delta.readme_changed is True
    assert delta.window_incomplete is False
    assert delta.previous_head == "aaa1"
    rendered = delta.render()
    assert "feat: retain sources" in rendered
    assert "sync-docs.yml" in rendered
    assert "README: CHANGED" in rendered


def test_delta_reports_a_removed_workflow(tmp_path):
    observe(tmp_path, "o/r", runner=_fake_gh(commits=FIRST_COMMITS, workflows=["ci.yml", "old.yml"]))
    digest = observe(tmp_path, "o/r", runner=_fake_gh(commits=FIRST_COMMITS, workflows=["ci.yml"]))
    assert digest.delta.removed_workflows == ("old.yml",)
    assert digest.delta.changed is True


def test_delta_flags_a_previous_head_outside_the_fetched_window(tmp_path):
    observe(tmp_path, "o/r", runner=_fake_gh(commits=[("old1", "feat: old")], workflows=[]))

    digest = observe(
        tmp_path, "o/r",
        runner=_fake_gh(commits=[("new2", "feat: newest"), ("new1", "feat: newer")], workflows=[]),
    )

    assert digest.delta.window_incomplete is True
    assert digest.delta.new_commits == ("feat: newest", "feat: newer")
    assert "there may be more" in digest.delta.render()


def test_a_failed_fetch_never_overwrites_a_good_observation(tmp_path):
    good = observe(tmp_path, "o/r", runner=_fake_gh(commits=FIRST_COMMITS, workflows=["ci.yml"]))
    before = (tmp_path / "o__r.json").read_text()

    degraded = observe(tmp_path, "o/r", runner=_fake_gh(fail=True))

    assert (tmp_path / "o__r.json").read_text() == before
    assert degraded.head_sha == good.head_sha
    assert degraded.delta.fetch_failed is True
    assert "last good observation" in degraded.delta.render()


def test_a_failed_first_fetch_writes_nothing_at_all(tmp_path):
    digest = observe(tmp_path, "o/r", runner=_fake_gh(fail=True))
    assert list(tmp_path.glob("*.json")) == []
    assert digest.fetched_at == ""
    assert digest.delta.baseline is True and digest.delta.fetch_failed is True


def test_observe_all_skips_fresh_projects_unless_refresh_is_asked_for(tmp_path):
    first = _fake_gh(commits=FIRST_COMMITS, workflows=["ci.yml"])
    observe_all(tmp_path, ["o/r"], runner=first)
    assert first.calls

    cached = _fake_gh(commits=FIRST_COMMITS, workflows=["ci.yml"])
    digests = observe_all(tmp_path, ["o/r"], runner=cached)
    assert cached.calls == []                       # fresh enough: no network at all
    assert digests[0].head_sha == "aaa1"

    forced = _fake_gh(commits=FIRST_COMMITS, workflows=["ci.yml"])
    observe_all(tmp_path, ["o/r"], runner=forced, refresh=True)
    assert forced.calls


# --- staleness ----------------------------------------------------------------

def test_stale_report_counts_never_observed_and_expired_entries(tmp_path):
    observe(tmp_path, "o/fresh", runner=_fake_gh(commits=FIRST_COMMITS, workflows=[]))

    now_report = stale_report(tmp_path, ["o/fresh", "o/never"], stale_after_days=7)
    assert now_report.total == 2
    assert now_report.stale == ("o/never",)
    assert "1 of 2" in now_report.line()
    assert "hsai observe --refresh" in now_report.line()

    later = datetime.now(timezone.utc) + timedelta(days=30)
    aged = stale_report(tmp_path, ["o/fresh", "o/never"], stale_after_days=7, now=later)
    assert set(aged.stale) == {"o/fresh", "o/never"}
    assert "2 of 2" in aged.line()

    assert "all 1" in stale_report(tmp_path, ["o/fresh"], stale_after_days=7).line()
    assert stale_report(tmp_path, [], stale_after_days=7).line() == "no reference projects configured"


# --- the adoption index -------------------------------------------------------

def _lesson(title: str, references: tuple[str, ...]) -> Lesson:
    return Lesson(
        title=title, outcome="pass", kind="implement", context="c",
        what_happened="w", lesson="l", references=references,
    )


def test_adoption_index_maps_projects_to_what_this_repo_already_took(tmp_path):
    kb = KnowledgeBase(tmp_path)
    kb.write_lesson(_lesson("implement: a hard numeric CI gate", ("run-llama/llama_index",)))
    kb.write_lesson(
        _lesson("implement: keep the concurrency core tiny", ("openai/swarm", "run-llama/llama_index"))
    )
    practices_mod.append(
        tmp_path,
        practices_mod.build_practice(
            title="mirror an external source instead of re-reading it",
            source_project="run-llama/llama_index",
            source_artifact="ci_cd", evidence="PR #299",
        ),
    )

    index = build_adoption_index(kb.read_lessons(), kb.read_practices())

    titles = {a.title for a in index["run-llama/llama_index"]}
    assert titles == {
        "implement: a hard numeric CI gate",
        "implement: keep the concurrency core tiny",
        "mirror an external source instead of re-reading it",
    }
    assert {a.source for a in index["run-llama/llama_index"]} == {"lesson", "practice"}
    assert {a.title for a in index["openai/swarm"]} == {"implement: keep the concurrency core tiny"}
    assert "microsoft/jarvis" not in index

    # lookup is case-insensitive on the slug, as the citations in the vault are
    assert observatory.adoptions_for(index, "run-llama/LLAMA_index")


def test_lessons_that_cite_nothing_contribute_nothing(tmp_path):
    kb = KnowledgeBase(tmp_path)
    kb.write_lesson(_lesson("implement: something", ()))
    assert build_adoption_index(kb.read_lessons()) == {}


def test_render_adopted_states_open_ground_explicitly():
    text = observatory.render_adopted({}, "o/r")
    assert "nothing adopted from `o/r` yet" in text


# --- the context-pack section -------------------------------------------------

def test_render_section_puts_the_delta_first_then_the_digest_then_adoptions(tmp_path):
    digest = observe(tmp_path, "o/r", runner=_fake_gh(commits=FIRST_COMMITS, workflows=["ci.yml"]))
    index = {"o/r": (Adoption(repo="o/r", note_name="2026-01-01-gate", title="feat: gate"),)}

    text = render_section(digest, index)

    assert text.index(DELTA_HEADING) < text.index(BASELINE_HEADING) < text.index(ADOPTED_FROM_HEADING)
    assert "feat: add sync-docs workflow" in text        # the baseline digest
    assert "[[2026-01-01-gate]]" in text                  # the adoption, as a wikilink
    assert "ci.yml" in text


# --- dossiers + the Reference Set MOC ----------------------------------------

def _seeded_kb(tmp_path):
    """A knowledge base with one cited lesson and one observed project."""
    cfg = load_config()
    kb = KnowledgeBase.from_config(cfg, tmp_path)
    lesson = _lesson("implement: mirror the reference set", ("run-llama/llama_index",))
    kb.write_lesson(lesson)
    observe(
        kb.reference_dir, "run-llama/llama_index",
        runner=_fake_gh(commits=FIRST_COMMITS, workflows=["ci.yml", "stale_bot.yml"]),
    )
    return cfg, kb, lesson


def test_reindex_writes_one_dossier_per_reference_project_plus_a_moc(tmp_path):
    cfg, kb, lesson = _seeded_kb(tmp_path)

    written = kb.reindex_mocs()

    names = {p.name for p in written}
    assert "Reference Set MOC.md" in names
    assert "llama_index.md" in names
    assert len([p for p in written if p.parent == kb.reference_dir]) == len(cfg.reference_top10)

    dossier = (kb.reference_dir / "llama_index.md").read_text()
    assert "# run-llama/llama_index" in dossier
    assert f"[[{lesson.note_name()}]]" in dossier            # wikilink to the citing lesson
    assert "## What we have adopted" in dossier
    assert "## What changed last cycle" in dossier
    assert "## Open questions" in dossier
    assert "[[Reference Set MOC]]" in dossier

    moc = (kb.mocs_dir / "Reference Set MOC.md").read_text()
    assert "[[llama_index]]" in moc
    assert f"Total: **{len(cfg.reference_top10)}**" in moc
    assert "`run-llama/llama_index`" in moc

    root_moc = (kb.mocs_dir / "Knowledge Base MOC.md").read_text()
    assert f"[[Reference Set MOC]] - {len(cfg.reference_top10)} reference project(s)" in root_moc


def test_a_never_observed_project_says_so_in_its_dossier(tmp_path):
    _, kb, _ = _seeded_kb(tmp_path)
    kb.reindex_mocs()
    dossier = (kb.reference_dir / "swarm.md").read_text()
    assert "_never_" in dossier
    assert "never been observed" in dossier


def _without_timestamp(text: str) -> list[str]:
    return [line for line in text.splitlines() if not line.startswith("updated:")]


def test_dossier_regeneration_differs_only_in_the_timestamp_line(tmp_path, monkeypatch):
    """Idempotence: no new data must mean no diff beyond the stamp."""
    _, kb, _ = _seeded_kb(tmp_path)
    kb.write_reference_dossiers()
    first = (kb.reference_dir / "llama_index.md").read_text()

    # Same day, same data: byte-for-byte identical.
    kb.write_reference_dossiers()
    assert (kb.reference_dir / "llama_index.md").read_text() == first

    # A later day moves the timestamp line, and nothing else.
    monkeypatch.setattr(observatory, "_now", lambda: datetime(2099, 1, 1, tzinfo=timezone.utc))
    kb.write_reference_dossiers()
    second = (kb.reference_dir / "llama_index.md").read_text()

    assert second != first
    assert _without_timestamp(second) == _without_timestamp(first)
    assert "updated: 2099-01-01" in second


# --- config -------------------------------------------------------------------

def test_observatory_config_reads_core_yaml():
    ocfg = ObservatoryConfig.from_core(load_config())
    assert ocfg.dir == REFERENCE_DIR_DEFAULT
    assert ocfg.commits >= 1
    assert ocfg.readme_bytes >= 1000
    assert ocfg.stale_after_days >= 1
    # and it degrades to documented defaults with no config at all
    assert ObservatoryConfig.from_core(None) == ObservatoryConfig()


def test_knowledge_base_reference_dir_matches_the_module_default(tmp_path):
    """The literal in hsai.knowledge and the constant here must not drift."""
    assert KnowledgeBase(tmp_path).reference_dir == tmp_path / REFERENCE_DIR_DEFAULT
