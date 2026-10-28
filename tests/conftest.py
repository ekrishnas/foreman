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
