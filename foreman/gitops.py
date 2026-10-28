import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

IDENTITY = {
    "GIT_AUTHOR_NAME": "Foreman", "GIT_AUTHOR_EMAIL": "foreman@localhost",
    "GIT_COMMITTER_NAME": "Foreman", "GIT_COMMITTER_EMAIL": "foreman@localhost",
}
MAX_SCAN_BYTES = 1_000_000


class RepoError(Exception):
    pass


@dataclass(frozen=True)
class Change:
    status: str  # A | M | D
    path: str


class GitRepo:
    def __init__(self, path: Path):
        self.path = Path(path)

    @classmethod
    def init(cls, path: Path) -> "GitRepo":
        path = Path(path)
        path.mkdir(parents=True, exist_ok=True)
        repo = cls(path)
        repo.git("init", "-q", "-b", "main")
        repo.git("commit", "-q", "--allow-empty", "-m", "initial commit")
        return repo

    def git(self, *args: str, check: bool = True) -> str:
        p = subprocess.run(
            ["git", *args], cwd=self.path, env={**os.environ, **IDENTITY},
            capture_output=True, text=True, errors="replace",
        )
        if check and p.returncode:
            raise RepoError(f"git {' '.join(args)}: {p.stderr.strip()}")
        return p.stdout

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").strip()

    def branch(self) -> str:
        return self.git("rev-parse", "--abbrev-ref", "HEAD").strip()

    def start_run(self, branch: str, start_ref: str) -> str:
        if self.git("status", "--porcelain").strip():
            raise RepoError("target repo has a dirty working tree; commit or stash first")
        if self.git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).strip():
            raise RepoError(f"branch {branch} already exists")
        self.git("checkout", "-q", "-b", branch, start_ref)
        return self.head()

    def changes(self) -> list[Change]:
        parts = self.git("status", "--porcelain=v1", "-z", "-uall").split("\0")
        out, i = [], 0
        while i < len(parts):
            entry = parts[i]
            i += 1
            if not entry:
                continue
            xy, path = entry[:2], entry[3:]
            if xy[0] in "RC":
                i += 1  # skip the original path of a rename/copy
            if xy == "??" or xy[0] in "RC":
                status = "A"
            elif "D" in xy:
                status = "D"
            elif "A" in xy:
                status = "A"
            else:
                status = "M"
            out.append(Change(status, path))
        return out

    def added_text(self) -> str:
        """Added lines of tracked changes plus contents of new text files (binaries skipped)."""
        diff = self.git("diff", "HEAD", "-U0", "--no-color")
        parts = [ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++")]
        for rel in self.git("ls-files", "--others", "--exclude-standard", "-z").split("\0"):
            p = self.path / rel
            if not rel or not p.is_file():
                continue
            data = p.read_bytes()[:MAX_SCAN_BYTES]
            if b"\0" in data:
                continue
            try:
                parts.append(data.decode())
            except UnicodeDecodeError:
                continue
        return "\n".join(parts)

    def branch_changes(self, base: str) -> list[Change]:
        """Net changes from `base` to HEAD."""
        out = self.git("diff", "--name-status", "--no-renames", base, "HEAD")
        return [Change(line[0], line.split("\t", 1)[1]) for line in out.splitlines() if "\t" in line]

    def branch_added_text(self, base: str) -> str:
        diff = self.git("diff", "-U0", "--no-color", base, "HEAD")
        return "\n".join(ln[1:] for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))

    def commit(self, message: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message)
        return self.head()

    def reset_hard(self, sha: str | None = None) -> None:
        if not self.branch().startswith("foreman/"):
            raise RepoError("refusing to reset: not on a foreman/ branch")
        self.git("reset", "-q", "--hard", sha or "HEAD")
        self.git("clean", "-fdq")

    def diff_stat(self, base: str) -> str:
        return self.git("diff", "--stat", "--no-color", base, "HEAD").strip()

    def diff_text(self, base: str, paths: list[str] | None = None) -> str:
        args = ["diff", "--no-color", base, "HEAD"]
        if paths:
            args += ["--", *paths]
        return self.git(*args)

    def tree(self) -> str:
        return self.git("ls-files").strip()

    def snapshot(self) -> dict[str, str | None]:
        """Current uncommitted changes as {path: content}; None marks a deletion."""
        snap = {}
        for c in self.changes():
            snap[c.path] = None if c.status == "D" else (self.path / c.path).read_text(errors="replace")
        return snap

    def restore(self, snap: dict[str, str | None]) -> None:
        for rel, content in snap.items():
            p = self.path / rel
            if content is None:
                p.unlink(missing_ok=True)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(content)
