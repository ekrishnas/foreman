import sys
from dataclasses import replace
from pathlib import Path

import pytest

from foreman.config import Scenario, load_target
from foreman.fake import FakeWorker
from foreman.github import FakeGitHub
from foreman.gitops import GitRepo
from foreman.human import ScriptedHuman
from foreman.runner import Run
from foreman.specs import SpecAdapter
from foreman.store import MemoryStore

ROOT = Path(__file__).resolve().parent.parent
MODELS = {"small": "claude-haiku-5-5", "mid": "claude-sonnet-5-5", "strong": "claude-opus-5-5"}


@pytest.fixture
def git_config(tmp_path, monkeypatch):
    """A private, empty global git config: the machine's own identity never leaks into a test.

    Write [user] settings into the returned file before creating a repo to give it a developer identity.
    """
    cfg = tmp_path / "gitconfig"
    cfg.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    for var in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(var, raising=False)
    return cfg


@pytest.fixture
def repo(tmp_path):
    return GitRepo.init(tmp_path / "target")


@pytest.fixture
def make_run(tmp_path, repo):
    """Build a Run on branch foreman/r1 of a tmp git repo, wired to fakes."""

    def factory(agents=None, behaviors=None, faults=None, budget=10.0, human=None, worker=None, workflow="feature"):
        agents = agents or {}
        prompts = tmp_path / "prompts"
        prompts.mkdir(exist_ok=True)
        for name in agents:
            (prompts / f"{name}.md").write_text(f"# {name}\nYou are the {name}.\n")
        repo.start_run("foreman/r1", "main")
        runs_dir = tmp_path / "runs"
        return Run(
            run_id="r1", issue_id=1,
            scenario=Scenario("t", workflow, "x", "fake", budget_usd=budget, faults=faults or []),
            target=replace(load_target(ROOT / "targets/fake.yaml"), repo=repo.path),
            agents=agents, models=MODELS, repo=repo,
            worker=worker or FakeWorker(faults or [], behaviors=behaviors),
            store=MemoryStore(runs_dir=runs_dir),
            github=FakeGitHub(tmp_path / "gh", repo),
            human=human or ScriptedHuman(),
            spec=SpecAdapter([sys.executable, "-m", "foreman.fake_openspec"], repo.path),
            runs_dir=runs_dir, prompts_dir=prompts,
        )

    return factory
