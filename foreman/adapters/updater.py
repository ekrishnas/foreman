"""`foreman update` and `foreman doctor`: keep a Foreman checkout, a product's copy of the skill and its profile current."""
from dataclasses import dataclass
from pathlib import Path

import yaml

from foreman import ROOT
from foreman.adapters.gitops import GitRepo, RepoError
from foreman.core.config import PROFILE

SKILL = "skill/foreman/SKILL.md"  # in the Foreman repo
INSTALLED_SKILL = ".claude/skills/foreman/SKILL.md"  # in a product repo
DEPENDENCY_FILES = {"pyproject.toml", "uv.lock"}
# Profile settings added after the first release, newest last: what a developer may want to switch on.
NEW_SETTINGS = {
    "paths.ci": "lets the implementer write CI workflows and Dependabot config (.github/**); you approve each change",
    "paths.ignore": "lets the implementer and test writer add .gitignore entries; you approve each change",
    "requires": "list docker if the tests need it: a run then checks Docker first (use [] if they do not)",
}


@dataclass
class UpdateResult:
    before: str
    after: str
    commits: list[str]  # "<short sha> <subject>", newest first
    deps_changed: bool  # pyproject.toml or uv.lock changed: `uv sync` is needed


def update_foreman(root: Path = ROOT, remote: str = "origin", branch: str = "main") -> UpdateResult:
    """Fast-forward a Foreman checkout to `remote/branch`. Never discards local edits or unpushed commits."""
    if not (root / ".git").exists():
        raise RepoError(f"{root} is not a git checkout, so it cannot be updated: clone Foreman again from where you got it")
    repo = GitRepo(root)
    if repo.branch() != branch:
        raise RepoError(f"Foreman is on '{repo.branch()}', not {branch}: switch back with `git checkout {branch}`, or update it yourself")
    if changed := repo.changes():
        names = ", ".join(c.path for c in changed[:5]) + (" ..." if len(changed) > 5 else "")
        raise RepoError(f"Foreman has local changes ({names}): commit or stash them, then run update again. Nothing was changed")
    try:
        repo.git("fetch", "-q", remote, branch)
    except RepoError as e:
        raise RepoError(f"could not fetch {remote}/{branch}: {e}") from e
    before, target = repo.head(), repo.git("rev-parse", "FETCH_HEAD").strip()
    if before == target:
        return UpdateResult(before, before, [], False)
    ahead = int(repo.git("rev-list", "--count", f"{target}..HEAD").strip())
    if ahead:
        behind = int(repo.git("rev-list", "--count", f"HEAD..{target}").strip())
        lack = f" (and {remote}/{branch} has {behind} new commit(s) you lack)" if behind else ""
        raise RepoError(f"Foreman has {ahead} commit(s) that are not on {remote}/{branch}{lack}. Update will not discard "
                        "them: push them, or move them to a branch, then run update again")
    commits = repo.git("log", "--format=%h %s", f"{before}..{target}").splitlines()
    files = set(repo.git("diff", "--name-only", before, target).split())
    repo.git("merge", "-q", "--ff-only", target)
    return UpdateResult(before, repo.head(), commits, bool(files & DEPENDENCY_FILES))


def _text(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")  # a Windows checkout may have CRLF


def skill_status(product: Path, root: Path = ROOT) -> str:
    """`current`, `differs` or `not installed`: the product's copy of the skill against this checkout's."""
    installed = product / INSTALLED_SKILL
    if not installed.is_file():
        return "not installed"
    return "current" if _text(installed) == _text(root / SKILL) else "differs"


def install_skill(product: Path, root: Path = ROOT) -> Path:
    dest = product / INSTALLED_SKILL
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_text(root / SKILL), encoding="utf-8", newline="\n")
    return dest


def profile_hints(product: Path) -> list[str]:
    """Newer profile settings the product's `.foreman/target.yaml` does not have, each with what it does."""
    path = product / PROFILE
    if not path.is_file():
        return []
    d = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    paths = d.get("paths") or {}
    return [f"{key}: {why}" for key, why in NEW_SETTINGS.items()
            if (key.split(".", 1)[1] not in paths if key.startswith("paths.") else key not in d)]
