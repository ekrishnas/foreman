from dataclasses import replace
from pathlib import Path

import pytest

from foreman.adapters.gitops import GitRepo
from foreman.adapters.specs import SpecAdapter
from foreman.core.config import RunConfig, load_target
from foreman.core.runner import Run
from foreman.state.store import Store
from tests.doubles.github import FakeGitHub
from tests.doubles.human import ScriptedHuman
from tests.doubles.run import OPENSPEC, TARGET_PROFILE
from tests.doubles.worker import FakeWorker

ROOT = Path(__file__).resolve().parent.parent
MODELS = {"small": "claude-haiku-5-5", "mid": "claude-sonnet-5-5", "strong": "claude-opus-5-5"}


@pytest.fixture
def repo(tmp_path):
    return GitRepo.init(tmp_path / "target")


@pytest.fixture
def make_run(tmp_path, repo):
    """Build a Run on branch foreman/r1 of a tmp git repo, wired to fakes."""

    def factory(agents=None, behaviors=None, faults=None, budget=10.0, human=None, worker=None,
                workflow="new_implementation"):
        agents = agents or {}
        prompts = tmp_path / "prompts"
        prompts.mkdir(exist_ok=True)
        for name in agents:
            (prompts / f"{name}.md").write_text(f"# {name}\nYou are the {name}.\n")
        repo.start_run("foreman/r1", "main")
        runs_dir = tmp_path / "runs"
        return Run(
            run_id="r1", issue_id=1, config=RunConfig(issue=1, budget_usd=budget),
            target=replace(load_target(TARGET_PROFILE), repo=repo.path),
            agents=agents, models=MODELS, repo=repo,
            worker=worker or FakeWorker(faults or [], behaviors=behaviors),
            store=Store(":memory:"),
            github=FakeGitHub(tmp_path / "gh", repo),
            human=human or ScriptedHuman(),
            spec=SpecAdapter(OPENSPEC, repo.path),
            runs_dir=runs_dir, prompts_dir=prompts, workflow=workflow,
        )

    return factory
