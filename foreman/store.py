import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class AuditEvent:
    run_id: str
    workflow: str
    stage: str
    event: str
    attempt: int = 0
    model: str = ""
    duration_ms: int = 0
    details: dict = field(default_factory=dict)
    ts: str = ""


@dataclass
class CostEvent:
    run_id: str
    issue_id: int
    workflow: str
    stage: str
    agent: str
    attempt: int
    model: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    duration_ms: int


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def export_audit(runs_dir: Path | None, ev: AuditEvent) -> None:
    if runs_dir is None:
        return
    path = Path(runs_dir) / ev.run_id / "audit.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(asdict(ev)) + "\n")


class MemoryStore:
    def __init__(self, runs_dir: Path | None = None):
        self.runs_dir = Path(runs_dir) if runs_dir else None
        self._audit: list[AuditEvent] = []
        self._cost: list[CostEvent] = []

    def audit(self, ev: AuditEvent) -> None:
        ev.ts = ev.ts or now()
        self._audit.append(ev)
        export_audit(self.runs_dir, ev)

    def cost(self, ev: CostEvent) -> None:
        self._cost.append(ev)

    def audit_events(self, run_id: str) -> list[AuditEvent]:
        return [e for e in self._audit if e.run_id == run_id]

    def cost_events(self, run_id: str | None = None, issue_id: int | None = None) -> list[CostEvent]:
        return [
            c for c in self._cost
            if (run_id is None or c.run_id == run_id) and (issue_id is None or c.issue_id == issue_id)
        ]
