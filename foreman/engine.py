"""Build a Run from a scenario and drive its graph to completion, a pause, or a safe-stop."""
import importlib
import secrets
import time
from dataclasses import dataclass, replace
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from foreman.config import Scenario, load_agents, load_target
from foreman.fake import FakeWorker
from foreman.fake_target import make_fake_target
from foreman.github import FakeGitHub, GitHub
from foreman.gitops import GitRepo
from foreman.human import ScriptedHuman, TerminalHuman
from foreman.runner import Run, SafeStop
from foreman.specs import SpecAdapter
from foreman.store import MemoryStore
from foreman.workers import ClaudeCodeWorker, Recorder, ReplayWorker
import sys

ROOT = Path(__file__).resolve().parent.parent
FAKE_OPENSPEC = [sys.executable, "-m", "foreman.fake_openspec"]
MODES = ("fake", "live", "replay")


@dataclass
class Result:
    status: str  # completed | safe_stopped | waiting | observe_failed
    reason: str = ""
    pr: int | None = None


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def issue_from_file(path: Path) -> tuple[str, str]:
    """Title is the first `# ` heading, body the rest."""
    lines = path.read_text().splitlines()
    for i, line in enumerate(lines):
        if line.startswith("# "):
            return line[2:].strip(), "\n".join(lines[i + 1:]).strip()
    return path.stem, path.read_text().strip()


def make_run(scenario: Scenario, mode: str, runs_dir: Path, run_id: str | None = None,
             store=None, recording: Path | None = None, resume: bool = False) -> Run:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    run_id = run_id or new_run_id()
    runs_dir = Path(runs_dir)
    agents, models = load_agents(ROOT / "config/agents.yaml")
    target = load_target(ROOT / "targets" / f"{'fake' if mode == 'fake' else scenario.target}.yaml")

    if mode == "fake":
        target_dir = runs_dir / run_id / "target"
        repo = GitRepo(target_dir) if resume else make_fake_target(target_dir, scenario.start_ref)
        target = replace(target, repo=repo.path)
    else:
        repo = GitRepo(target.repo)

    if mode == "live":
        github, worker, spec = GitHub(repo), ClaudeCodeWorker(), SpecAdapter(["openspec"], repo.path)
        recorder = Recorder(runs_dir / run_id / "recorded")
    else:
        github = FakeGitHub(runs_dir / run_id / "github", repo)
        worker = ReplayWorker(recording) if mode == "replay" else FakeWorker(scenario.faults)
        spec, recorder = SpecAdapter(FAKE_OPENSPEC, repo.path), None

    human = ScriptedHuman(scenario.script) if scenario.approvals == "scripted" else TerminalHuman()
    if isinstance(scenario.input, int):
        issue = scenario.input
    else:
        path = Path(scenario.input)
        title, body = issue_from_file(path if path.is_absolute() else ROOT / path)
        issue = github.create_issue(title, body, [f"foreman:{scenario.workflow}"])

    return Run(
        run_id=run_id, issue_id=issue, scenario=scenario, target=target, agents=agents, models=models,
        repo=repo, worker=worker, store=store or MemoryStore(runs_dir=runs_dir), github=github,
        human=human, spec=spec, runs_dir=runs_dir, recorder=recorder,
    )


@dataclass
class Session:
    run: Run
    graph: object
    config: dict

    def ctx(self) -> dict:
        return self.graph.get_state(self.config).values.get("ctx", {})

    def start(self) -> Result:
        return drive(self, {"ctx": {}, "done": {}})

    def resume(self) -> Result:
        return drive(self, None)


def open_session(scenario: Scenario, mode: str, runs_dir: Path, checkpointer=None, run_id: str | None = None,
                 store=None, recording: Path | None = None) -> Session:
    run = make_run(scenario, mode, runs_dir, run_id, store, recording)
    run.repo.start_run(run.branch, scenario.start_ref)
    return build_session(run, checkpointer)


def build_session(run: Run, checkpointer=None) -> Session:
    module = importlib.import_module(f"foreman.workflows.{run.scenario.workflow}")
    graph = module.build(run).compile(checkpointer=checkpointer or MemorySaver())
    return Session(run, graph, {"configurable": {"thread_id": run.run_id}, "recursion_limit": 100})


def drive(session: Session, input_) -> Result:
    run = session.run
    while True:
        try:
            session.graph.invoke(input_, session.config)
        except SafeStop as stop:
            return safe_stop(session, stop)
        ctx = session.ctx()
        if not session.graph.get_state(session.config).next:
            status = "observe_failed" if ctx.get("observe_ok") is False else "completed"
            return Result(status, ctx.get("observe_summary", ""), ctx.get("pr_number"))
        pr = ctx["pr_number"]
        if not isinstance(run.github, FakeGitHub):
            return Result("waiting", f"waiting for the merge of PR #{pr}; run `foreman resume {run.run_id}` after merging", pr)
        # Offline, the human's release decision is the merge.
        decision = run.human.approve("release", f"Merge PR #{pr}?")
        run.audit("release", "approval", kind="release", approved=decision.approved, note=decision.note,
                  who=decision.who, wait_s=0)
        if decision.approved:
            run.github.merge(pr)
        else:
            run.github.close(pr, decision.note or "closed by reviewer")
        input_ = Command(resume=True)


def safe_stop(session: Session, stop: SafeStop) -> Result:
    run = session.run
    run.audit(stop.stage or "run", "safe_stop", reason=stop.reason)
    ctx = session.ctx()
    if stop.rollback and ctx.get("approved_commit"):
        run.repo.reset_hard(ctx["approved_commit"])
        run.audit(stop.stage or "run", "rollback", to=ctx["approved_commit"])
    run.github.comment(run.issue_id, f"Foreman stopped the run: {stop.reason}")
    return Result("safe_stopped", stop.reason, ctx.get("pr_number"))


def check_expect(session: Session, result: Result) -> list[str]:
    """Evaluate the scenario's `expect` block; returns human-readable failures."""
    run, expect, problems = session.run, session.run.scenario.expect, []
    events = {e.event for e in run.store.audit_events(run.run_id)}
    if "status" in expect and result.status != expect["status"]:
        problems.append(f"status is {result.status!r}, expected {expect['status']!r} ({result.reason})")
    if "pr_state" in expect:
        state = run.github.pr_state(result.pr).state if result.pr else "none"
        if state != expect["pr_state"]:
            problems.append(f"PR state is {state!r}, expected {expect['pr_state']!r}")
    for event in expect.get("audit", []):
        if event not in events:
            problems.append(f"audit event {event!r} never happened")
    for rel in expect.get("files", []):
        if not (run.repo.path / rel).exists():
            problems.append(f"expected file {rel} is missing")
    if result.pr and isinstance(run.github, FakeGitHub):
        body = run.github._read("prs", result.pr)["body"]
        problems += [f"PR body lacks {needle!r}" for needle in expect.get("pr_body_contains", []) if needle not in body]
    if "max_cost_usd" in expect and run.run_cost() > expect["max_cost_usd"]:
        problems.append(f"cost ${run.run_cost():.2f} exceeds ${expect['max_cost_usd']:.2f}")
    return problems
