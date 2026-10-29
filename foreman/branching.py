"""Branching for live runs: where a run starts and what its branch is called. Asked once, kept with the run."""
import re
from collections import Counter
from dataclasses import dataclass

from foreman.gitops import GitRepo, RepoError

BASES = ("main", "master", "develop", "trunk")  # names that look like a base branch
PREFIX = re.compile(r"[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*/")


@dataclass(frozen=True)
class Branching:
    base: str = "main"  # the pull request targets it and the run branch starts from origin/<base>
    prefix: str = "foreman/"  # the run branch is <prefix><run id>


def _convention(heads: list[str]) -> str | None:
    """The most common `prefix/` among origin's branches, ignoring Foreman's own."""
    counts = Counter(h.split("/", 1)[0] + "/" for h in heads if "/" in h and not h.startswith("foreman/"))
    return counts.most_common(1)[0][0] if counts else None


def choose(repo: GitRepo, human) -> Branching:
    """Check that origin has a base branch, then let the developer confirm the base and the branch naming.

    Foreman never commits or pushes to the base: a run starts from origin/<base> and ends in a pull request.
    """
    if not repo.developer:
        raise RepoError("git user.name and user.email are not set. Foreman commits as you, with itself as co-author: "
                        "set them with git config user.name '...' and git config user.email '...'")
    heads = repo.remote_branches()
    if not heads:
        raise RepoError("origin has no branches, so there is nothing to start from or open a pull request against. "
                        "Push your base branch first: git push -u origin main")
    head = repo.remote_default()
    bases = [b for b in BASES if b in heads]
    if head in heads and head not in bases:
        bases.insert(0, head)
    if not bases:
        raise RepoError(f"origin has no base branch (looked for {', '.join(BASES)}; it has: {', '.join(heads)}). "
                        "Push your base branch first: git push -u origin main")
    default = head if head in bases else bases[0]

    base = (human.answer(
        f"Which branch should this run start from and open its pull request against? "
        f"Base branches on origin: {', '.join(sorted(bases))}. Press enter for '{default}'.") or default).strip()
    if base not in heads:
        raise RepoError(f"'{base}' is not a branch on origin (it has: {', '.join(heads)})")
    repo.git("fetch", "-q", "origin", base)
    ahead = repo.git("rev-list", "--count", f"origin/{base}..{base}", check=False).strip()
    if ahead not in ("", "0"):
        raise RepoError(f"local '{base}' has {ahead} commit(s) that are not on origin/{base}. Runs start from "
                        f"origin/{base}, so those commits would be left out. Land them through a branch and pull request first")

    found = _convention(heads)
    if found:
        example = next(h for h in heads if h.startswith(found))
        question = (f"Branches on origin are named like '{example}'. Name this run's branch '{found}<run id>' too? "
                    "Press enter to agree, or type another prefix.")
    else:
        question = ("No existing branch naming found on origin. Press enter to name this run's branch "
                    "'foreman/<run id>', or type a prefix such as 'feature/'.")
    prefix = (human.answer(question) or found or "foreman/").strip()
    if not prefix.endswith("/"):
        prefix += "/"
    if not PREFIX.fullmatch(prefix):
        raise RepoError(f"'{prefix}' is not a usable branch prefix: use lowercase letters, digits, '.', '_' and '-', ending in '/'")
    if clash := [h for h in heads if prefix.startswith(h + "/")]:
        raise RepoError(f"a branch named '{clash[0]}' already exists on origin, so branches named '{prefix}…' cannot be created")
    return Branching(base, prefix)
