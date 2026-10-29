"""The runner: every agent stage goes through run_stage."""
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from foreman import ROOT
from foreman.adapters.gitops import GitRepo, RepoError
from foreman.adapters.specs import SpecAdapter
from foreman.adapters.workers import Worker, WorkerRequest
from foreman.core.config import Agent, RunConfig, Target, allowed_tools
from foreman.core.context import PROVIDERS, MissingContext, build_prompt, writable_for
from foreman.core.policy import check_changes
from foreman.core.util import slug
from foreman.state.store import AuditEvent, CostEvent

# Audit events worth showing live while a run is going (the rest are only recorded).
SHOWN = {"classified", "stage_passed", "attempt_failed", "retry", "approval", "pr_opened", "safe_stop", "rollback"}


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
    config: RunConfig
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
    request: str = ""  # what the developer asked for, in their own words
    workflow: str = ""  # the graph the classifier chose
    prompts_dir: Path = ROOT / "prompts"
    max_retries: int = 2
    progress: Callable[[str], None] | None = None
    # One lock around worker call, diff check and commit, so parallel graph branches
    # never see each other's uncommitted changes.
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def branch(self) -> str:
        return f"foreman/{self.run_id}"

    @property
    def dir(self) -> Path:
        return self.runs_dir / self.run_id

    def audit(self, stage: str, event: str, attempt: int = 0, model: str = "", duration_ms: int = 0, **details):
        self.store.audit(AuditEvent(self.run_id, self.workflow, stage, event, attempt, model, duration_ms, details))
        if self.progress and event in SHOWN:
            extra = details.get("reason") or details.get("url")
            self.progress(f"[{stage}] {event}" + (f": {extra}" if extra else ""))

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
    sandbox: bool = False  # run the worker in an empty directory: it sees only the prompt, never the repository


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


def run_stage(run: Run, stage: Stage, ctx: dict, instance: str = "") -> dict:
    """Run one agent: entry gate, prompt, worker, policy, exit gate. Up to 1 + max_retries attempts, then safe-stop."""
    name = f"{stage.name}/{instance}" if instance else stage.name
    try:
        return _run_stage(run, stage, ctx, instance, name)
    except SafeStop as e:
        e.stage = e.stage or name
        raise


def _run_stage(run: Run, stage: Stage, ctx: dict, instance: str, name: str) -> dict:
    agent = run.agents[stage.agent]
    missing = [k for k, required in agent.context_keys() if required and k not in ctx and k not in PROVIDERS]
    if missing:
        raise SafeStop(f"{name}: entry gate failed, missing context: {', '.join(missing)}")

    writable = writable_for(run, agent, ctx, instance)
    tools = allowed_tools(agent, run.target)
    cwd = run.dir / "sandbox" if stage.sandbox else run.repo.path
    cwd.mkdir(parents=True, exist_ok=True)
    model = run.models[agent.tier]

    failure = None
    for attempt in range(1 + run.max_retries):
        if attempt:
            run.audit(name, "retry", attempt=attempt, model=model, reason=failure)
        with run.lock:
            if run.run_cost() >= run.config.budget_usd:
                raise SafeStop(f"budget exceeded: ${run.run_cost():.2f} of ${run.config.budget_usd:.2f}")
            call = sum(1 for c in run.store.cost_events(run_id=run.run_id) if c.stage == name)
            try:
                prompt = build_prompt(run, agent, ctx, failure, instance)
            except MissingContext as e:
                raise SafeStop(f"{name}: entry gate failed, missing context: {e}") from e
            key = slug(f"{name}-{call}")
            result = run.worker.run(WorkerRequest(agent, model, prompt, cwd, key, stage.name, call, tools))
            run.store.cost(CostEvent(run.run_id, run.issue_id, run.workflow, name, agent.name, call, model,
                                     result.input_tokens, result.output_tokens, result.cost_usd, result.duration_ms))

            findings = []
            failure = f"worker error: {result.error}" if result.error else None
            if not failure:
                failure, findings = _check(run, stage, ctx, result.text, writable, name)
            if not failure:
                updates = {stage.produces: result.text} if stage.produces else {}
                if stage.after:
                    updates |= stage.after(run, {**ctx, **updates}, result.text) or {}
                commit = run.repo.commit(f"foreman: {name}") if stage.commit else ""
                run.audit(name, "stage_passed", attempt=call, model=model, duration_ms=result.duration_ms,
                          commit=commit, findings=[f.detail for f in findings])
                return {"ctx": updates}

            if not stage.sandbox:
                try:
                    run.repo.reset_hard()
                except RepoError as e:
                    raise SafeStop(f"{name}: {e}") from e
            run.audit(name, "attempt_failed", attempt=call, model=model, reason=failure)
    raise SafeStop(f"{name} failed after {1 + run.max_retries} attempts: {failure}")
