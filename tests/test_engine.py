import pytest

from foreman.adapters.gitops import GitRepo
from foreman.core.config import PROFILE, RunConfig, load_target
from foreman.core.util import ShResult
from foreman.engine import RunRefused, make_run, preflight, prepare_target
from foreman.state.store import Store
from tests.conftest import ROOT


class StubGitHub:
    def __init__(self):
        self.created, self.repo = [], None

    def login(self):
        return "octo"

    def ensure_repo(self, name):
        self.created.append(name)
        self.repo.git("remote", "add", "origin", f"https://github.com/{name}")  # as `gh repo create --source` does


def stub_factory(gh):
    def make_gh(repo):
        gh.repo = repo
        return gh
    return make_gh


def product(tmp_path, folder="shop", remote=True, profile=True):
    """A product repo with a first commit, optionally a GitHub remote and a committed .foreman/target.yaml."""
    repo = GitRepo.init(tmp_path / folder)
    if remote:
        repo.git("remote", "add", "origin", f"https://github.com/octo/{folder}")
    if profile:
        (repo.path / ".foreman").mkdir()
        (repo.path / PROFILE).write_text((ROOT / "examples/target.yaml").read_text(encoding="utf-8"), encoding="utf-8")
        repo.commit("add the foreman profile")
    return repo


def target_of(repo):
    return load_target(repo.path / PROFILE)


def test_a_product_that_already_has_a_remote_is_left_alone(tmp_path):
    gh = StubGitHub()
    prepare_target(target_of(product(tmp_path)), stub_factory(gh))
    assert gh.created == []


def test_a_product_without_a_remote_gets_a_github_repo_named_after_its_profile(tmp_path):
    gh = StubGitHub()
    prepare_target(target_of(product(tmp_path, remote=False)), stub_factory(gh))
    assert gh.created == ["octo/my-app"]  # the profile's name, not the folder's


def test_a_repo_with_no_commits_gets_one_so_runs_can_branch_from_main(tmp_path):
    path = tmp_path / "shop"
    path.mkdir()
    bare = GitRepo(path)
    bare.git("init", "-q", "-b", "main")
    bare.git("remote", "add", "origin", "https://github.com/octo/shop")
    repo = prepare_target(load_target(ROOT / "examples/target.yaml", repo=path), stub_factory(StubGitHub()))
    assert repo.head() and repo.start_run("foreman/r1", "main")


def test_a_folder_that_is_not_a_git_repository_is_refused(tmp_path):
    (tmp_path / ".foreman").mkdir()
    (tmp_path / PROFILE).write_text((ROOT / "examples/target.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(RunRefused, match="not a git repository"):
        prepare_target(load_target(tmp_path / PROFILE), stub_factory(StubGitHub()))


def test_make_run_reads_the_profile_from_the_product_repo(tmp_path, monkeypatch):
    monkeypatch.setattr("foreman.engine.preflight", lambda: None)
    repo = product(tmp_path)
    run = make_run(RunConfig(issue=5, repo=str(repo.path)), tmp_path / "runs", "r1", Store(":memory:"))
    assert run.target.name == "my-app" and run.target.repo == repo.path.resolve()
    assert run.issue_id == 5 and run.request == "work on issue #5" and run.workflow == ""


def test_make_run_without_a_profile_says_how_to_get_one(tmp_path, monkeypatch):
    monkeypatch.setattr("foreman.engine.preflight", lambda: None)
    repo = product(tmp_path, profile=False)
    with pytest.raises(RunRefused, match=r"target\.yaml not found.*/foreman"):
        make_run(RunConfig(issue=5, repo=str(repo.path)), tmp_path / "runs", "r1", Store(":memory:"))


def test_make_run_checks_the_tools_before_anything_else(tmp_path, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda tool: None)
    with pytest.raises(RunRefused, match="missing on PATH"):
        make_run(RunConfig(issue=5, repo=str(tmp_path)), tmp_path / "runs", "r1", Store(":memory:"))


def test_preflight_names_every_missing_tool(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda tool: None if tool in ("gh", "openspec") else "/bin/" + tool)
    with pytest.raises(RunRefused, match="gh, openspec"):
        preflight()


def test_preflight_requires_a_logged_in_gh(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda tool: "/bin/" + tool)
    monkeypatch.setattr("foreman.engine.sh", lambda *a, **k: ShResult(1, "not logged in"))
    with pytest.raises(RunRefused, match="gh auth login"):
        preflight()
