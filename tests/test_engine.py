from dataclasses import replace

from foreman.config import load_target
from foreman.engine import prepare_live_target
from foreman.gitops import GitRepo
from tests.conftest import ROOT


class StubGitHub:
    def __init__(self, remote_adds=True):
        self.created, self.remote_adds = [], remote_adds

    def login(self):
        return "octo"

    def ensure_repo(self, name):
        self.created.append(name)
        if self.remote_adds:                              # `gh repo create --source` adds the origin remote
            self.repo.git("remote", "add", "origin", f"https://github.com/{name}")


def target_at(path):
    return replace(load_target(ROOT / "targets/url-shortener.yaml"), repo=path)


def test_first_greenfield_run_creates_the_local_repo_and_the_github_repo(tmp_path):
    gh = StubGitHub()
    target = target_at(tmp_path / "url-shortener")
    gh.repo = None

    def make_gh(repo):
        gh.repo = repo
        return gh

    repo = prepare_live_target(target, make_gh)
    assert (repo.path / ".git").exists() and repo.branch() == "main"
    assert gh.created == ["octo/url-shortener"]


def test_an_existing_target_with_a_remote_is_left_alone(tmp_path):
    existing = GitRepo.init(tmp_path / "url-shortener")
    existing.git("remote", "add", "origin", "https://github.com/octo/url-shortener")
    gh = StubGitHub()
    prepare_live_target(target_at(existing.path), lambda repo: gh)
    assert gh.created == []


def test_an_existing_local_repo_without_a_remote_gets_its_github_repo(tmp_path):
    existing = GitRepo.init(tmp_path / "url-shortener")
    gh = StubGitHub()

    def make_gh(repo):
        gh.repo = repo
        return gh

    prepare_live_target(target_at(existing.path), make_gh)
    assert gh.created == ["octo/url-shortener"]
