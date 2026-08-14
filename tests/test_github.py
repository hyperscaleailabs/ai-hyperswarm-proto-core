import json

from hsai import github
from hsai.proc import Proc


def _fake(stdout: str = ""):
    calls = []

    def runner(cmd, **kwargs):
        calls.append(list(cmd))
        return Proc(cmd, 0, stdout, "")

    runner.calls = calls
    return runner


def test_comment_issue_posts_the_body_via_gh():
    runner = _fake()
    github.comment_issue("o/r", 7, "hello there", runner=runner)
    assert runner.calls[0] == [
        "gh", "issue", "comment", "7", "--repo", "o/r", "--body", "hello there",
    ]


def test_list_issue_comments_returns_bodies_oldest_first():
    payload = json.dumps({
        "comments": [
            {"author": {"login": "hsai-bot"}, "body": "first"},
            {"author": {"login": "hsai-bot"}, "body": "second"},
        ]
    })
    runner = _fake(payload)
    bodies = github.list_issue_comments("o/r", 7, runner=runner)
    assert bodies == ["first", "second"]
    assert runner.calls[0] == [
        "gh", "issue", "view", "7", "--repo", "o/r", "--json", "comments",
    ]


def test_list_issue_comments_tolerates_no_comments():
    runner = _fake(json.dumps({"comments": []}))
    assert github.list_issue_comments("o/r", 7, runner=runner) == []


def test_list_issue_comments_tolerates_unparseable_output():
    runner = _fake("not json")
    assert github.list_issue_comments("o/r", 7, runner=runner) == []
