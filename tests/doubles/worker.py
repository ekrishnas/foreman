"""Deterministic fake worker, for tests."""
from typing import Callable

from foreman.adapters.gitops import GitRepo
from foreman.adapters.workers import WorkerRequest, WorkerResult

# model family -> (input tokens, output tokens, usd)
COSTS = {"haiku": (1000, 300, 0.01), "sonnet": (3000, 1000, 0.05), "opus": (5000, 1500, 0.20)}
SECRET_LINE = '\nAWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n'

Behavior = Callable[[WorkerRequest, "str | None"], str]
BEHAVIORS: dict[str, Behavior] = {}


def behavior(agent_name: str):
    def register(fn: Behavior) -> Behavior:
        BEHAVIORS[agent_name] = fn
        return fn
    return register


def _plant_secret(cwd) -> None:
    for change in GitRepo(cwd).changes():
        if change.status in ("A", "M"):
            with (cwd / change.path).open("a", encoding="utf-8", newline="\n") as f:
                f.write(SECRET_LINE)
            return


class FakeWorker:
    """Runs a per-agent behaviour. Faults: {stage, calls:[n]|all:true, kind, usd?}.

    kinds: no_output, bad_path, secret, break_tests (passed to the behaviour), cost.
    """

    def __init__(self, faults: list[dict] | None = None, behaviors: dict[str, Behavior] | None = None):
        self.faults = faults or []
        if behaviors is None:
            import tests.doubles.behaviors  # noqa: F401  (registers the default behaviours)
            behaviors = BEHAVIORS
        self.behaviors = behaviors

    def _fault(self, req: WorkerRequest) -> dict | None:
        for f in self.faults:
            if f["stage"] == req.stage and (f.get("all") or req.call in f.get("calls", [])):
                return f
        return None

    def run(self, req: WorkerRequest) -> WorkerResult:
        fault = self._fault(req)
        kind = fault["kind"] if fault else None
        text = ""
        if kind != "no_output":
            text = self.behaviors[req.agent.name](req, kind)
            if kind == "bad_path":
                (req.cwd / "FOREMAN_BAD_PATH.txt").write_text("outside every writable path\n", encoding="utf-8", newline="\n")
            elif kind == "secret":
                _plant_secret(req.cwd)
        tokens_in, tokens_out, usd = next(v for family, v in COSTS.items() if family in req.model)
        if kind == "cost":
            usd = fault["usd"]
        return WorkerResult(text, tokens_in, tokens_out, usd, 1000)
