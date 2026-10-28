"""The runner: every agent stage goes through run_stage."""
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from foreman.config import Agent, Scenario, Target, allowed_tools
from foreman.context import PROVIDERS, MissingContext, build_prompt, writable_for
from foreman.gitops import GitRepo, RepoError
from foreman.policy import check_changes
from foreman.specs import SpecAdapter
from foreman.store import AuditEvent, CostEvent
from foreman.util import sha, slug
from foreman.workers import Recorder, Worker, WorkerRequest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class SafeStop(Exception):
    """Stop the run: state is persisted, the reason recorded, the run is resumable."""

    def __init__(self, reason: str, rollback: bool = False, stage: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.rollback = rollback
        self.stage = stage


@dataclass(eq=False)
class Run:
    run_id: str
    issue_id: int
    scenario: Scenario
    target: Target
    agents: dict[str, Agent]
    models: dict[str, str]
    repo: GitRepo
    worker: Worker
    store: object
    github: object
    human: object
    spec: SpecAdapter
    runs_dir: Path
    prompts_dir: Path = PROJECT_ROOT / "prompts"
    recorder: Recorder | None = None
    max_retries: int = 2
    deploy_env: dict = field(default_factory=dict)  # extra env for the deploy/smoke/stop commands
    check_health: bool = True  # wait for the target's health_url before smoke traffic
    # One lock around worker call, diff check and commit, so parallel graph branches
    # never see each other's uncommitted changes.
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def workflow(self) -> str:
        return self.scenario.workflow

    @property
    def branch(self) -> str:
        return f"foreman/{self.run_id}"

    @property
    def dir(self) -> Path:
        return self.runs_dir / self.run_id

    def audit(self, stage: str, event: str, attempt: int = 0, model: str = "", duration_ms: int = 0, **details):
        self.store.audit(AuditEvent(self.run_id, self.workflow, stage, event, attempt, model, duration_ms, details))

    def run_cost(self) -> float:
        return sum(c.cost_usd for c in self.store.cost_events(run_id=self.run_id))


@dataclass
class Stage:
    name: str
    agent: str
    produces: str | None = None  # ctx key that receives the worker's text
    gate: Callable[[Run, dict, str], str | None] | None = None  # exit gate: failure message or None
    after: Callable[[Run, dict, str], dict] | None = None  # runs before the commit; returns ctx updates
    commit: bool = True
    sandbox: bool = False  # run the worker in an empty directory: it never sees the repository


def _check(run: Run, stage: Stage, ctx: dict, text: str, writable: list[str], name: str) -> tuple[str | None, list]:
    """Policy, exit gate, then human approval for high-impact changes. Returns (failure, findings)."""
    findings = [] if stage.sandbox else check_changes(run.repo.changes(), run.repo.added_text(), writable, run.target)
    blocking = [f for f in findings if f.blocking]
    if blocking:
        return "policy: " + "; ".join(f.detail for f in blocking), findings
    if stage.gate and (message := stage.gate(run, ctx, text)):
        return message, findings
    pending = [f for f in findings if not f.blocking]
    if pending:
        started = time.monotonic()
        decision = run.human.approve("high_impact", "\n".join(f"- {f.kind}: {f.detail}" for f in pending))
        run.audit(name, "approval", kind="high_impact", approved=decision.approved, note=decision.note,
                  who=decision.who, wait_s=round(time.monotonic() - started, 3))
        if not decision.approved:
            return f"human rejected high-impact change: {decision.note}", findings
    return None, findings


def run_stage(run: Run, stage: Stage, ctx: dict, done: dict, instance: str = "") -> dict:
    name = f"{stage.name}/{instance}" if instance else stage.name
    try:
        return _run_stage(run, stage, ctx, done, instance, name)
    except SafeStop as e:
        e.stage = e.stage or name
        raise


def _run_stage(run: Run, stage: Stage, ctx: dict, done: dict, instance: str, name: str) -> dict:
    agent = run.agents[stage.agent]
    keys = agent.context_keys()

    missing = [k for k, required in keys if required and k not in ctx and k not in PROVIDERS]
    if missing:
        raise SafeStop(f"{name}: entry gate failed, missing context: {', '.join(missing)}")

    inputs = sha(json.dumps({k: ctx.get(k) for k, _ in keys}, sort_keys=True, default=str))
    if done.get(name, {}).get("inputs") == inputs:
        run.audit(name, "stage_skipped", inputs=inputs)
        return {"ctx": {}, "done": {}}

    writable = writable_for(run, agent, ctx, instance)
    tools = allowed_tools(agent, run.target)
    cwd = run.dir / "sandbox" if stage.sandbox else run.repo.path
    cwd.mkdir(parents=True, exist_ok=True)
    plan = [agent.tier] * (1 + run.max_retries)
    if agent.tier != "strong":
        plan.append("strong")  # one fallback attempt on the strongest tier

    failure = None
    for n, tier in enumerate(plan):
        model = run.models[tier]
        if n:
            run.audit(name, "fallback" if tier != agent.tier else "retry", attempt=n, model=model, reason=failure)
        with run.lock:
            if run.run_cost() >= run.scenario.budget_usd:
                raise SafeStop(f"budget exceeded: ${run.run_cost():.2f} of ${run.scenario.budget_usd:.2f}")
            call = sum(1 for c in run.store.cost_events(run_id=run.run_id) if c.stage == name)
            try:
                prompt = build_prompt(run, agent, ctx, failure, instance)
            except MissingContext as e:
                raise SafeStop(f"{name}: entry gate failed, missing context: {e}") from e
            key = slug(f"{name}-{call}")
            result = run.worker.run(WorkerRequest(agent, model, prompt, cwd, key, stage.name, call, tools))
            run.store.cost(CostEvent(run.run_id, run.issue_id, run.workflow, name, agent.name, call, model,
                                     result.input_tokens, result.output_tokens, result.cost_usd, result.duration_ms))
            if run.recorder:
                run.recorder.save(key, result, {} if stage.sandbox else run.repo.snapshot())

            findings = []
            failure = f"worker error: {result.error}" if result.error else None
            if not failure:
                failure, findings = _check(run, stage, ctx, result.text, writable, name)
            if not failure:
                updates = {stage.produces: result.text} if stage.produces else {}
                if stage.after:
                    updates |= stage.after(run, {**ctx, **updates}, result.text) or {}
                output = sha(result.text)
                commit = run.repo.commit(f"foreman: {name}") if stage.commit else ""
                run.audit(name, "stage_passed", attempt=call, model=model, duration_ms=result.duration_ms,
                          inputs=inputs, output=output, commit=commit, findings=[f.detail for f in findings])
                return {"ctx": updates, "done": {name: {"inputs": inputs, "output": output}}}

            if not stage.sandbox:
                try:
                    run.repo.reset_hard()
                except RepoError as e:
                    raise SafeStop(f"{name}: {e}") from e
            run.audit(name, "attempt_failed", attempt=call, model=model, reason=failure)
    raise SafeStop(f"{name} failed after {len(plan)} attempts: {failure}")
