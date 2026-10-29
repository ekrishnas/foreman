"""Build a Run from a scenario and drive its graph to completion, a pause, or a safe-stop."""
import importlib
import json
import secrets
import sys
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from foreman.branching import Branching, choose
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

ROOT = Path(__file__).resolve().parent.parent
FAKE_OPENSPEC = [sys.executable, "-m", "foreman.fake_openspec"]
MODES = ("fake", "live", "replay")


class RunRefused(Exception):
    pass


def ensure_ready(issue) -> None:
    """Issues Foreman filed itself (e.g. after a failed observe) wait for a human to label them ready."""
    if "foreman:needs-approval" in issue.labels and "foreman:ready" not in issue.labels:
        raise RunRefused(f"issue #{issue.number} awaits human approval: label it foreman:ready before Foreman picks it up")


@dataclass
class Result:
    status: str  # completed | safe_stopped | waiting | observe_failed
    reason: str = ""
    pr: int | None = None


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def issue_from_file(path: Path) -> tuple[str, str]:
    """Title is the first `# ` heading, body the rest."""
    lines = path.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.startswith("# "):
            return line[2:].strip(), "\n".join(lines[i + 1:]).strip()
    return path.stem, path.read_text(encoding="utf-8").strip()


def make_checkpointer(db_url: str | None = None):
    """Postgres checkpoints when a database is configured (durable, resumable); otherwise in memory."""
    if not db_url:
        return MemorySaver()
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver
    from psycopg.rows import dict_row
    conn = psycopg.connect(db_url, autocommit=True, prepare_threshold=0, row_factory=dict_row)
    saver = PostgresSaver(conn)
    saver.setup()
    return saver


def prepare_live_target(target, make_github=GitHub) -> GitRepo:
    """First greenfield run: create the local repo and the GitHub repo (PRs required on main) if missing."""
    repo = GitRepo(target.repo) if (target.repo / ".git").exists() else GitRepo.init(target.repo)
    if "origin" not in repo.git("remote").split():
        gh = make_github(repo)
        gh.ensure_repo(f"{gh.login()}/{target.name}")
    return repo


def branch_point(scenario: Scenario, mode: str, branching: Branching) -> str:
    """Where the run branch starts: the confirmed base on origin for live runs, else the scenario's own ref."""
    return f"origin/{branching.base}" if mode == "live" and scenario.start_ref == "main" else scenario.start_ref


def make_run(scenario: Scenario, mode: str, runs_dir: Path, run_id: str | None = None, store=None,
             recording: Path | None = None, issue: int | None = None, record: bool = False,
             branching: Branching | None = None) -> Run:
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")
    run_id = run_id or new_run_id()
    runs_dir = Path(runs_dir)
    agents, models = load_agents(ROOT / "config/agents.yaml")
    use_fake_target = mode == "fake" or scenario.target == "fake"
    target = load_target(ROOT / "targets" / f"{'fake' if use_fake_target else scenario.target}.yaml")
    human = ScriptedHuman(scenario.script) if scenario.approvals == "scripted" else TerminalHuman()

    if use_fake_target:
        target_dir = runs_dir / run_id / "target"
        repo = GitRepo(target_dir) if (target_dir / ".git").exists() else make_fake_target(target_dir, scenario.start_ref)
        target = replace(target, repo=repo.path)
    elif mode == "live":
        repo = prepare_live_target(target)
        branching = branching or choose(repo, human)  # fails fast, before an issue is filed or any model is paid for
    else:
        repo = GitRepo(target.repo)
    branching = branching or Branching()

    if mode == "live":
        github, worker, spec = GitHub(repo, base=branching.base), ClaudeCodeWorker(), SpecAdapter(["openspec"], repo.path)
    else:
        github = FakeGitHub(runs_dir / run_id / "github", repo)
        worker = ReplayWorker(recording) if mode == "replay" else FakeWorker(scenario.faults)
        spec = SpecAdapter(FAKE_OPENSPEC, repo.path)
    recorder = Recorder(runs_dir / run_id / "recorded") if mode == "live" or record else None

    if issue is not None:
        pass  # resuming: the issue already exists
    elif isinstance(scenario.input, int):
        issue = scenario.input
        ensure_ready(github.get_issue(issue))
    else:
        path = Path(scenario.input)
        title, body = issue_from_file(path if path.is_absolute() else ROOT / path)
        issue = github.create_issue(title, body, [f"foreman:{scenario.workflow}"])

    deploy_env = {}
    if use_fake_target:
        effects = {"slo_breach": {"FOREMAN_FAKE_P95": "999"}, "errors": {"FOREMAN_FAKE_ERRORS": "30"}}
        for f in scenario.faults:
            if f.get("stage") == "observe":
                deploy_env |= effects.get(f["kind"], {})

    run = Run(
        deploy_env=deploy_env, check_health=not use_fake_target, branching=branching,
        run_id=run_id, issue_id=issue, scenario=scenario, target=target, agents=agents, models=models,
        repo=repo, worker=worker, store=store or MemoryStore(runs_dir=runs_dir), github=github,
        human=human, spec=spec, runs_dir=runs_dir, recorder=recorder,
    )
    run.repo.guard = run.branch  # reset_hard may only touch this run's branch
    return run


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
                 store=None, recording: Path | None = None, record: bool = False,
                 branching: Branching | None = None) -> Session:
    run = make_run(scenario, mode, runs_dir, run_id, store, recording, record=record, branching=branching)
    run.repo.start_run(run.branch, branch_point(scenario, mode, run.branching))
    run.dir.mkdir(parents=True, exist_ok=True)
    (run.dir / "run.json").write_text(json.dumps({
        "run_id": run.run_id, "mode": mode, "issue": run.issue_id, "scenario": asdict(scenario),
        "recording": str(recording) if recording else None, "record": record,
        "branching": asdict(run.branching)}, indent=1), encoding="utf-8", newline="\n")
    return build_session(run, checkpointer)


def resume_session(run_id: str, runs_dir: Path, checkpointer, store=None) -> Session:
    """Rebuild a run from its saved metadata; the checkpointer supplies the graph state."""
    meta = json.loads((Path(runs_dir) / run_id / "run.json").read_text(encoding="utf-8"))
    run = make_run(Scenario(**meta["scenario"]), meta["mode"], runs_dir, run_id, store,
                   Path(meta["recording"]) if meta["recording"] else None, issue=meta["issue"], record=meta["record"],
                   branching=Branching(**meta.get("branching", {})))  # runs saved before branching existed use the defaults
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
        if not run.github.auto_merge:
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
