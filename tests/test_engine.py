import json
from dataclasses import replace

from langgraph.checkpoint.memory import MemorySaver

from foreman.branching import Branching
from foreman.config import Scenario, load_scenario, load_target
from foreman.engine import (
    branch_point,
    open_session,
    prepare_live_target,
    resume_session,
)
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


# ---- branching ----

def test_live_runs_start_from_the_confirmed_base_on_origin_unless_the_scenario_names_a_ref():
    main_scenario, tagged = Scenario("s", "feature", "x", "t"), Scenario("s", "feature", "x", "t", start_ref="v1")
    assert branch_point(main_scenario, "live", Branching("develop", "feature/")) == "origin/develop"
    assert branch_point(tagged, "live", Branching("develop", "feature/")) == "v1"
    assert branch_point(main_scenario, "fake", Branching("develop", "feature/")) == "main"


def test_the_confirmed_branching_names_the_run_branch_and_is_saved_for_resume(tmp_path):
    scenario = load_scenario(ROOT / "scenarios/greenfield.yaml")
    session = open_session(scenario, "fake", tmp_path / "runs", run_id="t1", branching=Branching("main", "feature/"))
    assert session.run.repo.branch() == "feature/t1"
    saved = json.loads((tmp_path / "runs/t1/run.json").read_text(encoding="utf-8"))
    assert saved["branching"] == {"base": "main", "prefix": "feature/"}
    assert session.start().status == "completed"          # the reset guard and PR flow accept the custom prefix


def test_resume_reuses_the_saved_branching_instead_of_asking_again(tmp_path):
    scenario = load_scenario(ROOT / "scenarios/greenfield.yaml")
    open_session(scenario, "fake", tmp_path / "runs", run_id="t1", branching=Branching("main", "feature/"))
    assert resume_session("t1", tmp_path / "runs", MemorySaver()).run.branching == Branching("main", "feature/")
