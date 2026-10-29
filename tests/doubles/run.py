"""Build a Run wired to the doubles: FakeWorker, FakeGitHub, the OpenSpec stub and a throwaway Python target."""
import sys
from dataclasses import replace
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver

from foreman import ROOT
from foreman.core.config import load_agents, load_target
from foreman.engine import Session, new_run_id, open_session
from foreman.adapters.gitops import GitRepo
from foreman.adapters.specs import SpecAdapter
from foreman.core.runner import Run
from foreman.state.store import Store
from tests.doubles.github import FakeGitHub
from tests.doubles.human import ScriptedHuman
from tests.doubles.scenario import Scenario
from tests.doubles.target import make_fake_target
from tests.doubles.worker import FakeWorker

HERE = Path(__file__).parent
TARGET_PROFILE = HERE / "target.yaml"
OPENSPEC = [sys.executable, str(HERE / "openspec.py")]


def issue_from_file(path: Path) -> tuple[str, str]:
    """Title is the first `# ` heading, body the rest."""
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.startswith("# "):
            return line[2:].strip(), "\n".join(lines[i + 1:]).strip()
    return path.stem, path.read_text(encoding="utf-8").strip()


def releaser(run: Run):
    """Stand in for the human on GitHub: the scripted `release` decision merges (or closes) the PR."""

    def release(pr: int) -> None:
        decision = run.human.approve("release", f"Merge PR #{pr}?")
        run.audit("release", "approval", kind="release", approved=decision.approved, note=decision.note,
                  who=decision.who, wait_s=0)
        if decision.approved:
            run.github.merge(pr)
        else:
            run.github.close(pr, decision.note or "closed by reviewer")

    return release


def fake_factory(scenario: Scenario, worker=None):
    """A `factory` for engine.open_session / cli.main: same arguments as engine.make_run."""

    def factory(config, runs_dir, run_id, store, request="") -> Run:
        run_id = run_id or new_run_id()
        runs_dir = Path(runs_dir)
        agents, models = load_agents(ROOT / "config/agents.yaml")
        target_dir = runs_dir / run_id / "target"
        repo = GitRepo(target_dir) if (target_dir / ".git").exists() else make_fake_target(target_dir, config.start_ref)
        github = FakeGitHub(runs_dir / run_id / "github", repo)
        existing = sorted((github.root / "issues").glob("*.json"))
        if existing:  # resuming: the issue was filed by the first call
            issue = int(existing[0].stem)
        else:
            path = Path(scenario.input)
            title, body = issue_from_file(path if path.is_absolute() else ROOT / path)
            issue = github.create_issue(title, body, [])
        run = Run(
            run_id=run_id, issue_id=issue, config=config, target=replace(load_target(TARGET_PROFILE), repo=repo.path),
            agents=agents, models=models, repo=repo, worker=worker or FakeWorker(scenario.faults),
            store=store or Store(":memory:"), github=github, human=ScriptedHuman(scenario.script),
            spec=SpecAdapter(OPENSPEC, repo.path), runs_dir=runs_dir, request=request or scenario.request,
        )
        github.on_wait = releaser(run)
        return run

    return factory


def fake_session(scenario: Scenario, runs_dir: Path, run_id: str | None = None, checkpointer=None, store=None,
                 worker=None) -> Session:
    return open_session(scenario.config(), runs_dir, checkpointer or MemorySaver(), run_id=run_id,
                        store=store or Store(":memory:"), factory=fake_factory(scenario, worker),
                        request=scenario.request)
