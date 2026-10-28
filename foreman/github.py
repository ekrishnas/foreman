"""The only code that talks to GitHub. Agents never get GitHub access."""
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from foreman.gitops import GitRepo
from foreman.util import sh

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


@dataclass(frozen=True)
class PRState:
    state: str  # open | merged | closed
    merge_sha: str | None = None
    comments: list[str] = field(default_factory=list)


def _pr_body(body: str, closes: int | None) -> str:
    return f"{body}\n\nCloses #{closes}" if closes else body


class GitHub:
    """Real GitHub through the authenticated `gh` CLI."""

    auto_merge = False  # a human merges on GitHub; Foreman only waits

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
        d = json.loads(self._gh("issue", "view", str(n), "--json", "number,title,body,labels"))
        return Issue(d["number"], d["title"], d["body"] or "", [x["name"] for x in d["labels"]])

    def comment(self, n: int, body: str) -> None:
        self._gh("issue", "comment", str(n), "--body", body)

    def push(self, branch: str) -> None:
        self.repo.git("push", "-u", "origin", branch)

    def open_pr(self, branch: str, title: str, body: str, labels: list[str], closes: int | None) -> int:
        flags = self._ensure_labels(labels)
        out = self._gh("pr", "create", "--head", branch, "--base", "main", "--title", title,
                       "--body", _pr_body(body, closes), *flags)
        return int(out.strip().rsplit("/", 1)[1])

    def pr_state(self, pr: int) -> PRState:
        d = json.loads(self._gh("pr", "view", str(pr), "--json", "state,mergeCommit,comments,reviews"))
        bodies = [c["body"] for c in d["comments"]] + [r["body"] for r in d["reviews"]]
        sha = (d.get("mergeCommit") or {}).get("oid")
        return PRState(d["state"].lower(), sha, [b for b in bodies if b])

    def merged_commit(self, pr: int) -> str:
        st = self.pr_state(pr)
        if st.state != "merged":
            raise RuntimeError(f"PR #{pr} is not merged")
        self.repo.git("fetch", "-q", "origin", "main")
        self.repo.git("checkout", "-q", "main")
        self.repo.git("merge", "-q", "--ff-only", "origin/main")
        return st.merge_sha


class FakeGitHub:
    """Offline stand-in: issues and PRs are JSON files under `root`; merge is a real git merge."""

    auto_merge = True  # the run's `release` decision merges (or closes) the PR in-process

    def __init__(self, root: Path, repo: GitRepo):
        self.root = Path(root)
        self.repo = repo

    def _next(self) -> int:
        nums = [int(p.stem) for kind in ("issues", "prs") for p in (self.root / kind).glob("*.json")]
        return max(nums, default=0) + 1

    def _write(self, kind: str, n: int, data: dict) -> None:
        path = self.root / kind / f"{n}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1))

    def _read(self, kind: str, n: int) -> dict:
        return json.loads((self.root / kind / f"{n}.json").read_text())

    def ensure_repo(self, name: str) -> None:
        pass

    def push(self, branch: str) -> None:
        pass

    def create_issue(self, title: str, body: str, labels: list[str]) -> int:
        n = self._next()
        self._write("issues", n, {"number": n, "title": title, "body": body, "labels": labels, "comments": []})
        return n

    def get_issue(self, n: int) -> Issue:
        d = self._read("issues", n)
        return Issue(d["number"], d["title"], d["body"], d["labels"])

    def comment(self, n: int, body: str) -> None:
        d = self._read("issues", n)
        d["comments"].append(body)
        self._write("issues", n, d)

    def open_pr(self, branch: str, title: str, body: str, labels: list[str], closes: int | None) -> int:
        n = self._next()
        self._write("prs", n, {
            "number": n, "branch": branch, "title": title, "body": _pr_body(body, closes),
            "labels": labels, "closes": closes, "state": "open", "merge_sha": None, "comments": [],
        })
        return n

    def pr_state(self, pr: int) -> PRState:
        d = self._read("prs", pr)
        return PRState(d["state"], d["merge_sha"], d["comments"])

    def merged_commit(self, pr: int) -> str:
        st = self.pr_state(pr)
        if st.state != "merged":
            raise RuntimeError(f"PR #{pr} is not merged")
        return st.merge_sha

    def merge(self, pr: int) -> None:
        """What the human does on GitHub: merge the PR into main."""
        d = self._read("prs", pr)
        self.repo.git("checkout", "-q", "main")
        self.repo.git("merge", "--no-ff", "-q", d["branch"], "-m", f"Merge PR #{pr}")
        d.update(state="merged", merge_sha=self.repo.head())
        self._write("prs", pr, d)

    def close(self, pr: int, comment: str) -> None:
        d = self._read("prs", pr)
        d["state"] = "closed"
        d["comments"].append(comment)
        self._write("prs", pr, d)
