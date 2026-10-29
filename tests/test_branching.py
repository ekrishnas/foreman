import subprocess

import pytest

from foreman.branching import Branching, choose
from foreman.gitops import GitRepo, RepoError
from foreman.human import ScriptedHuman

DEVELOPER = "[user]\n\tname = Dev Eloper\n\temail = dev@example.com\n"


class Asked(ScriptedHuman):
    """A scripted human that remembers the questions it was asked."""

    def __init__(self, *answers):
        super().__init__({"answers": list(answers)})
        self.questions = []

    def answer(self, question):
        self.questions.append(question)
        return super().answer(question)


def clone(tmp_path, git_config, branches=("main",), default="main"):
    """A developer's clone whose origin (a bare repo) holds exactly `branches`."""
    git_config.write_text(DEVELOPER, encoding="utf-8")
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", default, str(origin)], check=True)
    repo = GitRepo.init(tmp_path / "work")
    repo.git("remote", "add", "origin", str(origin))
    for name in branches:
        repo.git("push", "-q", "origin", f"main:refs/heads/{name}")
    return repo


def test_a_repo_with_only_main_asks_twice_and_blank_answers_take_the_defaults(tmp_path, git_config):
    human = Asked()
    assert choose(clone(tmp_path, git_config), human) == Branching("main", "foreman/")
    assert len(human.questions) == 2 and "main" in human.questions[0] and "foreman/" in human.questions[1]


def test_an_origin_without_a_base_branch_is_refused_with_the_command_to_fix_it(tmp_path, git_config):
    repo = clone(tmp_path, git_config, branches=("foreman/20261029-132454-2eac",))
    with pytest.raises(RepoError, match=r"no base branch.*foreman/2026.*git push -u origin main"):
        choose(repo, Asked())


def test_an_origin_without_any_branch_is_refused(tmp_path, git_config):
    with pytest.raises(RepoError, match="no branches.*git push -u origin main"):
        choose(clone(tmp_path, git_config, branches=()), Asked())


def test_the_developer_can_pick_another_base(tmp_path, git_config):
    human = Asked("develop")
    repo = clone(tmp_path, git_config, branches=("main", "develop"))
    assert choose(repo, human).base == "develop"
    assert "develop, main" in human.questions[0]


def test_the_default_base_is_the_default_branch_of_origin(tmp_path, git_config):
    repo = clone(tmp_path, git_config, branches=("main", "develop"), default="develop")
    assert choose(repo, Asked()).base == "develop"


def test_an_unknown_base_is_refused(tmp_path, git_config):
    with pytest.raises(RepoError, match="'nope' is not a branch on origin"):
        choose(clone(tmp_path, git_config), Asked("nope"))


def test_existing_branch_naming_is_proposed_and_followed_after_confirmation(tmp_path, git_config):
    human = Asked()
    repo = clone(tmp_path, git_config, branches=("main", "feature/a", "feature/b", "fix/c"))
    assert choose(repo, human) == Branching("main", "feature/")
    assert "'feature/a'" in human.questions[1]  # shows what it found, so the developer can confirm it


def test_the_developer_can_overrule_the_proposed_naming(tmp_path, git_config):
    repo = clone(tmp_path, git_config, branches=("main", "feature/a"))
    assert choose(repo, Asked("", "fix")).prefix == "fix/"


def test_foremans_own_branches_are_not_a_naming_convention(tmp_path, git_config):
    repo = clone(tmp_path, git_config, branches=("main", "foreman/r1", "foreman/r2"))
    human = Asked()
    assert choose(repo, human).prefix == "foreman/" and "No existing" in human.questions[1]


def test_a_prefix_with_spaces_or_odd_characters_is_refused(tmp_path, git_config):
    with pytest.raises(RepoError, match="prefix"):
        choose(clone(tmp_path, git_config), Asked("", "my branches"))


def test_a_prefix_cannot_clash_with_a_branch_on_origin(tmp_path, git_config):
    repo = clone(tmp_path, git_config, branches=("main", "feature"))
    with pytest.raises(RepoError, match="'feature' already exists"):
        choose(repo, Asked("", "feature/"))


def test_unpushed_commits_on_the_local_base_are_refused(tmp_path, git_config):
    repo = clone(tmp_path, git_config)
    repo.commit("chore: add a local-only commit")
    with pytest.raises(RepoError, match=r"1 commit.*not on origin/main"):
        choose(repo, Asked())


def test_a_local_base_that_is_behind_origin_is_fine(tmp_path, git_config):
    repo = clone(tmp_path, git_config)
    repo.commit("chore: add a commit")
    repo.git("push", "-q", "origin", "main")
    repo.git("reset", "-q", "--hard", "HEAD~1")
    assert choose(repo, Asked()).base == "main"


def test_a_git_identity_is_required_because_commits_are_authored_as_the_developer(tmp_path, git_config):
    repo = clone(tmp_path, git_config)
    git_config.write_text("", encoding="utf-8")
    fresh = GitRepo(repo.path)  # developer is read once per GitRepo object
    with pytest.raises(RepoError, match="user.name"):
        choose(fresh, Asked())
