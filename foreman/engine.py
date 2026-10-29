"""Build a Run, let the classifier pick its graph, then drive that graph to completion or a safe-stop."""
import json
import secrets
import shutil
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

from foreman import ROOT
from foreman.adapters.github import GitHub
from foreman.adapters.gitops import GitRepo
from foreman.adapters.specs import SpecAdapter
from foreman.adapters.workers import ClaudeCodeWorker
from foreman.core.config import PROFILE, RunConfig, load_agents, load_target
from foreman.core.human import StoreHuman
from foreman.core.runner import Run, SafeStop
from foreman.core.util import sh
from foreman.workflows import GRAPHS
from foreman.workflows.route import classify

TOOLS = ("git", "gh", "claude", "openspec")
DB_NAME = "foreman.db"  # inside the runs directory


class RunRefused(Exception):
    pass


@dataclass
class Result:
    status: str  # completed | safe_stopped
    reason: str = ""
    pr: int | None = None


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def make_checkpointer(runs_dir: Path) -> SqliteSaver:
    """Graph state is checkpointed to the same SQLite file, so a crashed run can be resumed."""
    Path(runs_dir).mkdir(parents=True, exist_ok=True)
    return SqliteSaver(sqlite3.connect(Path(runs_dir) / DB_NAME, check_same_thread=False, timeout=30))


def preflight() -> None:
    """Fail early, with a fix, when a tool a run needs is missing."""
    missing = [t for t in TOOLS if not shutil.which(t)]
    if missing:
        raise RunRefused(f"missing on PATH: {', '.join(missing)} (see 'Setup' in the README)")
    if sh(["gh", "auth", "status"], cwd=ROOT).code:
        raise RunRefused("gh is not logged in: run `gh auth login`")


def check_requirements(target) -> None:
    """Fail early when this machine lacks something the product's tests need (the profile's `requires`)."""
    for need in target.requires:
        if need != "docker":
            raise RunRefused(f"unknown requirement '{need}' in the profile (known: docker)")
        r = sh(["docker", "info"], cwd=ROOT, timeout=30)
        if r.code == 127:
            raise RunRefused("Docker is not installed, and the profile says the tests need it. Install Docker, or remove "
                             "`requires: [docker]` from .foreman/target.yaml if the tests can run without it")
        if r.code:
            raise RunRefused("Docker is not running, and the profile says the tests need it: start Docker Desktop "
                             "(or the Docker daemon) and run again")


def prepare_target(target, make_github=GitHub) -> GitRepo:
    """Open the product repo. Give it a first commit if it has none, and create its GitHub repo (PRs required on
    main) if it has no remote."""
    if not (target.repo / ".git").exists():
        raise RunRefused(f"{target.repo} is not a git repository: run `git init` there, or open Claude Code in your product repo")
    repo = GitRepo(target.repo)
    if not repo.git("rev-parse", "--verify", "--quiet", "HEAD", check=False).strip():
        repo.git("commit", "-q", "--allow-empty", "-m", "initial commit")
    if "origin" not in repo.git("remote").split():
        gh = make_github(repo)
        gh.ensure_repo(f"{gh.login()}/{target.name}")
    return repo


def make_run(config: RunConfig, runs_dir: Path, run_id: str | None, store, request: str = "") -> Run:
    """A real run: `claude -p` workers, GitHub through `gh`, the OpenSpec CLI, questions answered through the store."""
    preflight()
    run_id = run_id or new_run_id()
    agents, models = load_agents(ROOT / "config/agents.yaml")
    profile = Path(config.repo).resolve() / PROFILE
    if not profile.is_file():
        raise RunRefused(f"{profile} not found. It describes the product (build and test commands, paths): run "
                         "/foreman in Claude Code, which creates it, or copy examples/target.yaml there and edit it")
    target = load_target(profile)
    check_requirements(target)
    repo = prepare_target(target)
    return Run(
        run_id=run_id, issue_id=config.issue, config=config, target=target, agents=agents, models=models, repo=repo,
        worker=ClaudeCodeWorker(), store=store, github=GitHub(repo), human=StoreHuman(store, run_id),
        spec=SpecAdapter([shutil.which("openspec")], repo.path),  # full path: npm installs a .cmd shim on Windows
        runs_dir=Path(runs_dir), request=request or f"work on issue #{config.issue}",
        progress=lambda message: print(message, flush=True),
    )


@dataclass
class Session:
    run: Run
    checkpointer: object
    graph: object = None  # built once the classifier has chosen the type

    @property
    def config(self) -> dict:
        return {"configurable": {"thread_id": self.run.run_id}, "recursion_limit": 100}

    def ctx(self) -> dict:
        return self.graph.get_state(self.config).values.get("ctx", {}) if self.graph else {}

    def save(self) -> None:
        self.run.dir.mkdir(parents=True, exist_ok=True)
        (self.run.dir / "run.json").write_text(json.dumps({
            "run_id": self.run.run_id, "config": asdict(self.run.config), "request": self.run.request,
            "workflow": self.run.workflow if self.graph else ""}, indent=1), encoding="utf-8", newline="\n")

    def compile(self) -> None:
        self.graph = GRAPHS[self.run.workflow](self.run).compile(checkpointer=self.checkpointer)

    def start(self) -> Result:
        run = self.run
        run.workflow = "route"  # what audit and cost rows say until the classifier has answered
        try:
            kind = classify(run)
            if kind != "observe":  # observing changes nothing, so it needs no branch (and the repo may be dirty with logs)
                run.repo.start_run(run.branch, run.config.start_ref)
        except SafeStop as stop:
            return safe_stop(self, stop)
        self.compile()
        self.save()
        return drive(self, {"ctx": {}})

    def resume(self) -> Result:
        if not self.graph:
            return self.start()  # classification never finished
        if self.run.workflow != "observe":  # observe has no branch
            self.run.repo.switch_to(self.run.branch)
        return drive(self, None)


def open_session(config: RunConfig, runs_dir: Path, checkpointer, run_id: str | None = None, store=None,
                 factory=make_run, request: str = "") -> Session:
    """`factory` builds the Run; tests pass one that wires in fakes."""
    session = Session(factory(config, runs_dir, run_id, store, request), checkpointer)
    session.save()
    return session


def resume_session(run_id: str, runs_dir: Path, checkpointer, store=None, factory=make_run) -> Session:
    """Rebuild a run from its saved metadata; the checkpointer supplies the graph state."""
    meta = json.loads((Path(runs_dir) / run_id / "run.json").read_text(encoding="utf-8"))
    run = factory(RunConfig(**meta["config"]), runs_dir, run_id, store, meta["request"])
    session = Session(run, checkpointer)
    if meta["workflow"]:
        run.workflow = meta["workflow"]
        session.compile()
    return session


def drive(session: Session, input_) -> Result:
    run = session.run
    try:
        session.graph.invoke(input_, session.config)
    except SafeStop as stop:
        return safe_stop(session, stop)
    ctx = session.ctx()
    run.audit("run", "run_completed")
    return Result("completed", ctx.get("summary", ""), ctx.get("pr_number"))


def restart_from_approval(session: Session, approved_commit: str) -> None:
    """After a rollback, make `resume` carry on from the checkpoint taken when the proposal was approved. Its state
    matches the rolled-back tree, so the code-writing stages run again with a fresh count of validation attempts.
    (Resuming into `validate` would only check a tree with no code in it, and fail at once.)"""
    since_approval = [s for s in session.graph.get_state_history(session.config)
                      if s.values.get("ctx", {}).get("approved_commit") == approved_commit]
    if since_approval:  # newest first, so the last one is right after the approval
        session.graph.update_state(since_approval[-1].config, None, as_node="__copy__")


def safe_stop(session: Session, stop: SafeStop) -> Result:
    run = session.run
    run.audit(stop.stage or "run", "safe_stop", reason=stop.reason)
    ctx = session.ctx()
    if stop.rollback and ctx.get("approved_commit"):
        run.repo.reset_hard(ctx["approved_commit"])
        run.audit(stop.stage or "run", "rollback", to=ctx["approved_commit"])
        restart_from_approval(session, ctx["approved_commit"])
    run.github.comment(run.issue_id, f"Foreman stopped the run: {stop.reason}")
    return Result("safe_stopped", stop.reason, ctx.get("pr_number"))
