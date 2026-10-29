"""The only code that talks to GitHub. Agents never get GitHub access."""
import json
import time
from dataclasses import dataclass, field
from typing import Callable

from foreman.adapters.gitops import GitRepo
from foreman.core.util import sh

PROTECT_MAIN = {
    "required_status_checks": None,
    "enforce_admins": True,
    "required_pull_request_reviews": {"required_approving_review_count": 0},
    "restrictions": None,
}


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    body: str
    labels: list[str]
    comments: list[str] = field(default_factory=list)  # comment bodies, oldest first


@dataclass(frozen=True)
class PRState:
    state: str  # open | merged | closed
    merge_sha: str | None = None
    comments: list[str] = field(default_factory=list)
    body: str = ""


def _pr_body(body: str, closes: int | None) -> str:
    return f"{body}\n\nCloses #{closes}" if closes else body


class GitHub:
    """Real GitHub through the authenticated `gh` CLI."""

    def __init__(self, repo: GitRepo, runner: Callable = sh):
        self.repo = repo
        self.runner = runner

    def _gh(self, *args: str, input: str | None = None) -> str:
        r = self.runner(["gh", *args], cwd=self.repo.path, input=input)
        if r.code:
            raise RuntimeError(f"gh {' '.join(args[:2])} failed: {r.out.strip()}")
        return r.out

    def _ensure_labels(self, labels: list[str]) -> list[str]:
        for label in labels:
            self._gh("label", "create", label, "--force")
        return [x for label in labels for x in ("--label", label)]

    def login(self) -> str:
        return self._gh("api", "user", "--jq", ".login").strip()

    def ensure_repo(self, name: str) -> None:
        """Create `owner/name` if missing and require PRs on main."""
        if self.runner(["gh", "repo", "view", name], cwd=self.repo.path).code == 0:
            return
        self._gh("repo", "create", name, "--private", "--source", str(self.repo.path), "--remote", "origin", "--push")
        self._gh("api", "-X", "PUT", f"repos/{name}/branches/main/protection", "--input", "-",
                 input=json.dumps(PROTECT_MAIN))

    def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        flags = self._ensure_labels(labels)
        out = self._gh("issue", "create", "--title", title, "--body", body, *flags)
        return int(out.strip().rsplit("/", 1)[1])

    def get_issue(self, n: int) -> Issue:
        d = json.loads(self._gh("issue", "view", str(n), "--json", "number,title,body,labels,comments"))
        return Issue(d["number"], d["title"], d["body"] or "", [x["name"] for x in d["labels"]],
                     [c["body"] for c in d["comments"]])

    def comment(self, n: int, body: str) -> None:
        self._gh("issue", "comment", str(n), "--body", body)

    def push(self, branch: str) -> None:
        self.repo.git("push", "-u", "origin", branch)

    def open_pr(self, branch: str, title: str, body: str, labels: list[str], closes: int | None) -> int:
        flags = self._ensure_labels(labels)
        out = self._gh("pr", "create", "--head", branch, "--base", "main", "--title", title,
                       "--body", _pr_body(body, closes), *flags)
        return int(out.strip().rsplit("/", 1)[1])

    def pr_url(self, pr: int) -> str:
        return self._gh("pr", "view", str(pr), "--json", "url", "--jq", ".url").strip()

    def wait_for_merge(self, pr: int, interval: float = 30, timeout: float = 86400, sleep=time.sleep) -> str:
        """Block until a human merges or closes the PR (or the timeout passes); returns its state."""
        waited = 0.0
        while self.pr_state(pr).state == "open" and waited < timeout:
            sleep(interval)
            waited += interval
        return self.pr_state(pr).state

    def pr_state(self, pr: int) -> PRState:
        d = json.loads(self._gh("pr", "view", str(pr), "--json", "state,mergeCommit,comments,reviews,body"))
        bodies = [c["body"] for c in d["comments"]] + [r["body"] for r in d["reviews"]]
        sha = (d.get("mergeCommit") or {}).get("oid")
        return PRState(d["state"].lower(), sha, [b for b in bodies if b], d.get("body") or "")

    def merged_commit(self, pr: int) -> str:
        st = self.pr_state(pr)
        if st.state != "merged":
            raise RuntimeError(f"PR #{pr} is not merged")
        self.repo.git("fetch", "-q", "origin", "main")
        self.repo.git("checkout", "-q", "main")
        self.repo.git("merge", "-q", "--ff-only", "origin/main")
        return st.merge_sha
