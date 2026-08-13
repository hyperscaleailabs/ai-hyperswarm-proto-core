"""GitHub operations via the `gh` CLI: labels, issues (tickets), and PRs.

Priority is expressed with labels ``priority:P0`` .. ``priority:P3`` (P0 highest).
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .proc import Proc, Runner, run

PRIORITY_LABELS = ["priority:P0", "priority:P1", "priority:P2", "priority:P3"]
STANDARD_LABELS = {
    "priority:P0": ("b60205", "Critical / broken main"),
    "priority:P1": ("d93f0b", "High priority"),
    "priority:P2": ("fbca04", "Normal priority"),
    "priority:P3": ("0e8a16", "Low priority / nice to have"),
    "hsai": ("5319e7", "Filed or handled by the hsai loop"),
    "ci": ("1d76db", "Continuous integration / build health"),
    "self-improve": ("0052cc", "Improvement toward core.yaml goals"),
    "skill": ("bfd4f2", "A learnable orchestrator capability"),
    "blocked": ("000000", "Exhausted auto-retries; needs a human"),
    "review": ("e99695", "Block review brief for the architect"),
    "needs-refinement": ("f9d0c4", "Ticket lacks acceptance criteria / verification plan"),
    "size:S": ("c5def5", "Small, mechanical change"),
    "size:M": ("76c7f0", "Substantial feature or refactor"),
    "size:L": ("1f77b4", "Large, multi-step change"),
    "attempts:1": ("ededed", "hsai retry counter"),
    "attempts:2": ("d4c5f9", "hsai retry counter"),
    "attempts:3": ("c2a3f5", "hsai retry counter"),
}


@dataclass
class Issue:
    number: int
    title: str
    labels: tuple[str, ...]
    assignees: tuple[str, ...]
    body: str = ""

    def priority_rank(self) -> int:
        for i, lbl in enumerate(PRIORITY_LABELS):
            if lbl in self.labels:
                return i
        return len(PRIORITY_LABELS)  # unlabeled sorts last

    @property
    def is_blocked(self) -> bool:
        return "blocked" in self.labels

    def attempts(self) -> int:
        """Read the current retry count from an ``attempts:N`` label (0 if none)."""
        best = 0
        for lbl in self.labels:
            if lbl.startswith("attempts:"):
                try:
                    best = max(best, int(lbl.split(":", 1)[1]))
                except ValueError:
                    continue
        return best


def _gh(args: list[str], *, cwd: str | None = None, runner: Runner = run) -> Proc:
    return runner(["gh", *args], cwd=cwd)


def current_login(*, runner: Runner = run) -> str:
    p = _gh(["api", "user", "--jq", ".login"], runner=runner)
    return p.stdout.strip()


def ensure_labels(repo: str, *, runner: Runner = run) -> None:
    """Create the standard label set (idempotent)."""
    for name, (color, desc) in STANDARD_LABELS.items():
        _gh(
            [
                "label", "create", name,
                "--repo", repo,
                "--color", color,
                "--description", desc,
                "--force",
            ],
            runner=runner,
        )


def create_issue(
    repo: str,
    title: str,
    body: str,
    labels: list[str],
    *,
    assignee: str | None = None,
    runner: Runner = run,
) -> int:
    """Create an issue and return its number (0 if it could not be parsed)."""
    args = ["issue", "create", "--repo", repo, "--title", title, "--body", body]
    for lbl in labels:
        args += ["--label", lbl]
    if assignee:
        args += ["--assignee", assignee]
    p = _gh(args, runner=runner)
    return _parse_issue_number(p.stdout)


def comment_issue(repo: str, number: int, body: str, *, runner: Runner = run) -> Proc:
    return _gh(
        ["issue", "comment", str(number), "--repo", repo, "--body", body],
        runner=runner,
    )


def list_open_issues(repo: str, *, runner: Runner = run) -> list[Issue]:
    """List open issues, highest priority first."""
    p = _gh(
        [
            "issue", "list", "--repo", repo, "--state", "open",
            "--limit", "100",
            "--json", "number,title,labels,assignees,body",
        ],
        runner=runner,
    )
    try:
        data = json.loads(p.stdout or "[]")
    except json.JSONDecodeError:
        return []
    issues = [
        Issue(
            number=item["number"],
            title=item.get("title", ""),
            labels=tuple(lb["name"] for lb in item.get("labels", [])),
            assignees=tuple(a["login"] for a in item.get("assignees", [])),
            body=item.get("body", "") or "",
        )
        for item in data
    ]
    issues.sort(key=lambda i: (i.priority_rank(), i.number))
    return issues


def list_closed_issues(repo: str, *, limit: int = 15, runner: Runner = run) -> list[Issue]:
    """Most recently closed issues, newest-closed first (capped).

    Read by synthesis memory only - never assigned/claimed like open issues,
    so `assignees` is always empty here.
    """
    p = _gh(
        [
            "issue", "list", "--repo", repo, "--state", "closed",
            "--limit", str(limit),
            "--json", "number,title,labels,closedAt",
        ],
        runner=runner,
    )
    try:
        data = json.loads(p.stdout or "[]")
    except json.JSONDecodeError:
        return []
    data.sort(key=lambda item: item.get("closedAt") or "", reverse=True)
    return [
        Issue(
            number=item["number"],
            title=item.get("title", ""),
            labels=tuple(lb["name"] for lb in item.get("labels", [])),
            assignees=(),
        )
        for item in data
    ]


def next_ticket(repo: str, *, runner: Runner = run) -> Issue | None:
    issues = list_open_issues(repo, runner=runner)
    return issues[0] if issues else None


def assign(repo: str, number: int, login: str, *, runner: Runner = run) -> Proc:
    return _gh(
        ["issue", "edit", str(number), "--repo", repo, "--add-assignee", login],
        runner=runner,
    )


def unassign(repo: str, number: int, login: str, *, runner: Runner = run) -> Proc:
    return _gh(
        ["issue", "edit", str(number), "--repo", repo, "--remove-assignee", login],
        runner=runner,
    )


def edit_labels(
    repo: str,
    number: int,
    *,
    add: list[str] | None = None,
    remove: list[str] | None = None,
    runner: Runner = run,
) -> Proc:
    args = ["issue", "edit", str(number), "--repo", repo]
    for lbl in add or []:
        args += ["--add-label", lbl]
    for lbl in remove or []:
        args += ["--remove-label", lbl]
    return _gh(args, runner=runner)


def get_issue(repo: str, number: int, *, runner: Runner = run) -> Issue | None:
    p = _gh(
        [
            "issue", "view", str(number), "--repo", repo,
            "--json", "number,title,labels,assignees,body",
        ],
        runner=runner,
    )
    try:
        item = json.loads(p.stdout or "{}")
    except json.JSONDecodeError:
        return None
    if not item:
        return None
    return Issue(
        number=item["number"],
        title=item.get("title", ""),
        labels=tuple(lb["name"] for lb in item.get("labels", [])),
        assignees=tuple(a["login"] for a in item.get("assignees", [])),
        body=item.get("body", "") or "",
    )


@dataclass
class Pr:
    number: int
    title: str
    body: str
    head_ref: str = ""


def list_open_prs(repo: str, *, runner: Runner = run) -> list[Pr]:
    """List open PRs (used by the hygiene watchdog to spot abandoned claims)."""
    p = _gh(
        [
            "pr", "list", "--repo", repo, "--state", "open",
            "--limit", "100",
            "--json", "number,title,body,headRefName",
        ],
        runner=runner,
    )
    try:
        data = json.loads(p.stdout or "[]")
    except json.JSONDecodeError:
        return []
    return [
        Pr(
            number=item["number"],
            title=item.get("title", ""),
            body=item.get("body", "") or "",
            head_ref=item.get("headRefName", ""),
        )
        for item in data
    ]


def close_pr(
    repo: str,
    number: int,
    *,
    comment: str | None = None,
    delete_branch: bool = True,
    runner: Runner = run,
) -> Proc:
    args = ["pr", "close", str(number), "--repo", repo]
    if delete_branch:
        args.append("--delete-branch")
    if comment:
        args += ["--comment", comment]
    return _gh(args, runner=runner)


def create_pr(
    repo: str,
    head: str,
    title: str,
    body: str,
    *,
    base: str = "main",
    runner: Runner = run,
) -> int:
    p = _gh(
        [
            "pr", "create", "--repo", repo,
            "--head", head, "--base", base,
            "--title", title, "--body", body,
        ],
        runner=runner,
    )
    return _parse_pr_number(p.stdout)


def merge_pr(
    repo: str,
    number: int,
    *,
    auto: bool = True,
    method: str = "squash",
    runner: Runner = run,
) -> Proc:
    """Merge a PR. With ``auto`` the merge waits for required checks to pass."""
    args = ["pr", "merge", str(number), "--repo", repo, f"--{method}", "--delete-branch"]
    if auto:
        args.append("--auto")
    return _gh(args, runner=runner)


# --- parsing helpers ----------------------------------------------------------
def _parse_issue_number(text: str) -> int:
    return _trailing_number(text)


def _parse_pr_number(text: str) -> int:
    return _trailing_number(text)


def _trailing_number(text: str) -> int:
    """gh prints the created issue/PR URL; the number is the last path segment."""
    for token in reversed(text.strip().split()):
        tail = token.rstrip("/").split("/")[-1]
        if tail.isdigit():
            return int(tail)
    return 0
