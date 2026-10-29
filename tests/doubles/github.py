"""Offline stand-in for foreman.adapters.github.GitHub, for tests."""
import json
from pathlib import Path

from foreman.adapters.github import Issue, PRState, _pr_body
from foreman.adapters.gitops import GitRepo


class FakeGitHub:
    """Offline stand-in: issues and PRs are JSON files under `root`; merge is a real git merge."""

    def __init__(self, root: Path, repo: GitRepo, on_wait=None):
        self.root = Path(root)
        self.repo = repo
        self.on_wait = on_wait  # stands in for the human who merges (or closes) the PR while the run waits

    def _next(self) -> int:
        nums = [int(p.stem) for kind in ("issues", "prs") for p in (self.root / kind).glob("*.json")]
        return max(nums, default=0) + 1

    def _write(self, kind: str, n: int, data: dict) -> None:
        path = self.root / kind / f"{n}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=1), encoding="utf-8", newline="\n")

    def _read(self, kind: str, n: int) -> dict:
        return json.loads((self.root / kind / f"{n}.json").read_text(encoding="utf-8"))

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
        return Issue(d["number"], d["title"], d["body"], d["labels"], d["comments"])

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
        return PRState(d["state"], d["merge_sha"], d["comments"], d["body"])

    def pr_url(self, pr: int) -> str:
        return f"fake://pr/{pr}"

    def wait_for_merge(self, pr: int, *args, **kwargs) -> str:
        if self.on_wait:
            self.on_wait(pr)
        return self.pr_state(pr).state

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
