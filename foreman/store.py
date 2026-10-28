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


def export_cost(runs_dir: Path | None, ev: CostEvent) -> None:
    if runs_dir is None:
        return
    path = Path(runs_dir) / ev.run_id / "cost.jsonl"
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
        export_cost(self.runs_dir, ev)

    def audit_events(self, run_id: str) -> list[AuditEvent]:
        return [e for e in self._audit if e.run_id == run_id]

    def cost_events(self, run_id: str | None = None, issue_id: int | None = None) -> list[CostEvent]:
        return [
            c for c in self._cost
            if (run_id is None or c.run_id == run_id) and (issue_id is None or c.issue_id == issue_id)
        ]


class FileStore(MemoryStore):
    """Read-only view over the jsonl exports in `runs_dir`, for use when no database is configured."""

    def __init__(self, runs_dir: Path):
        super().__init__(runs_dir=None)
        for path in sorted(Path(runs_dir).glob("*/audit.jsonl")):
            self._audit += [AuditEvent(**json.loads(line)) for line in path.read_text().splitlines() if line]
        for path in sorted(Path(runs_dir).glob("*/cost.jsonl")):
            self._cost += [CostEvent(**json.loads(line)) for line in path.read_text().splitlines() if line]


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS audit_events (
    id bigserial PRIMARY KEY, run_id text NOT NULL, workflow text NOT NULL, stage text NOT NULL,
    event text NOT NULL, attempt int NOT NULL DEFAULT 0, model text NOT NULL DEFAULT '',
    duration_ms int NOT NULL DEFAULT 0, details jsonb NOT NULL DEFAULT '{}', ts timestamptz NOT NULL);
CREATE INDEX IF NOT EXISTS audit_events_run ON audit_events (run_id);
CREATE TABLE IF NOT EXISTS cost_events (
    id bigserial PRIMARY KEY, run_id text NOT NULL, issue_id int NOT NULL, workflow text NOT NULL,
    stage text NOT NULL, agent text NOT NULL, attempt int NOT NULL, model text NOT NULL,
    input_tokens int NOT NULL, output_tokens int NOT NULL, cost_usd double precision NOT NULL,
    duration_ms int NOT NULL);
CREATE INDEX IF NOT EXISTS cost_events_run ON cost_events (run_id);
CREATE INDEX IF NOT EXISTS cost_events_issue ON cost_events (issue_id);

CREATE OR REPLACE VIEW v_cost_per_run AS
SELECT run_id, issue_id, SUM(cost_usd) AS cost_usd,
       COALESCE(SUM(cost_usd) FILTER (WHERE attempt > 0), 0) AS retry_cost_usd
FROM cost_events GROUP BY run_id, issue_id;

CREATE OR REPLACE VIEW v_cost_per_issue AS
SELECT issue_id, SUM(cost_usd) AS cost_usd, COUNT(DISTINCT run_id) AS runs,
       COALESCE(SUM(cost_usd) FILTER (WHERE attempt > 0), 0) AS retry_cost_usd
FROM cost_events GROUP BY issue_id;

CREATE OR REPLACE VIEW v_stage_success AS
SELECT workflow, stage,
       COUNT(*) FILTER (WHERE event = 'stage_passed') AS passed,
       COUNT(*) FILTER (WHERE event = 'attempt_failed') AS failed,
       COUNT(*) FILTER (WHERE event = 'stage_passed')::float / NULLIF(COUNT(*), 0) AS success_rate
FROM audit_events WHERE event IN ('stage_passed', 'attempt_failed') GROUP BY workflow, stage;

CREATE OR REPLACE VIEW v_retry_rate AS
SELECT run_id,
       COUNT(*) FILTER (WHERE event = 'retry') AS retries,
       COUNT(*) FILTER (WHERE event = 'stage_passed') AS passed_stages,
       COALESCE(COUNT(*) FILTER (WHERE event = 'retry')::float
                / NULLIF(COUNT(*) FILTER (WHERE event = 'stage_passed'), 0), 0) AS retry_rate
FROM audit_events GROUP BY run_id;

CREATE OR REPLACE VIEW v_rollbacks AS
SELECT run_id, COUNT(*) AS rollbacks FROM audit_events WHERE event = 'rollback' GROUP BY run_id;

CREATE OR REPLACE VIEW v_mttr AS
SELECT f.run_id, f.stage, MIN(p.ts) - f.ts AS recovery
FROM (SELECT run_id, stage, MIN(ts) AS ts FROM audit_events WHERE event = 'attempt_failed' GROUP BY run_id, stage) f
JOIN audit_events p ON p.run_id = f.run_id AND p.stage = f.stage AND p.event = 'stage_passed' AND p.ts > f.ts
GROUP BY f.run_id, f.stage, f.ts;

CREATE OR REPLACE VIEW v_latency AS
SELECT run_id, MAX(ts) - MIN(ts) AS latency FROM audit_events GROUP BY run_id;

CREATE OR REPLACE VIEW v_human_wait AS
SELECT run_id, COALESCE(SUM((details->>'wait_s')::float), 0) AS wait_s
FROM audit_events WHERE event IN ('approval', 'merge_wait', 'clarified') GROUP BY run_id;
"""


class PostgresStore:
    """Audit and cost events in Postgres (DESIGN.md sections 8 and 11); also exports audit.jsonl."""

    def __init__(self, conninfo: str, runs_dir: Path | None = None):
        import psycopg
        self.runs_dir = Path(runs_dir) if runs_dir else None
        self.conn = psycopg.connect(conninfo, autocommit=True)
        self.conn.execute(SCHEMA_SQL)

    def audit(self, ev: AuditEvent) -> None:
        from psycopg.types.json import Jsonb
        ev.ts = ev.ts or now()
        self.conn.execute(
            "INSERT INTO audit_events (run_id, workflow, stage, event, attempt, model, duration_ms, details, ts) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (ev.run_id, ev.workflow, ev.stage, ev.event, ev.attempt, ev.model, ev.duration_ms, Jsonb(ev.details), ev.ts))
        export_audit(self.runs_dir, ev)

    def cost(self, ev: CostEvent) -> None:
        self.conn.execute(
            "INSERT INTO cost_events (run_id, issue_id, workflow, stage, agent, attempt, model, input_tokens, "
            "output_tokens, cost_usd, duration_ms) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            tuple(asdict(ev).values()))
        export_cost(self.runs_dir, ev)

    def audit_events(self, run_id: str) -> list[AuditEvent]:
        rows = self.conn.execute(
            "SELECT run_id, workflow, stage, event, attempt, model, duration_ms, details, ts "
            "FROM audit_events WHERE run_id = %s ORDER BY id", (run_id,)).fetchall()
        return [AuditEvent(r[0], r[1], r[2], r[3], r[4], r[5], r[6], r[7], r[8].isoformat()) for r in rows]

    def cost_events(self, run_id: str | None = None, issue_id: int | None = None) -> list[CostEvent]:
        rows = self.conn.execute(
            "SELECT run_id, issue_id, workflow, stage, agent, attempt, model, input_tokens, output_tokens, "
            "cost_usd, duration_ms FROM cost_events WHERE (%s::text IS NULL OR run_id = %s) "
            "AND (%s::int IS NULL OR issue_id = %s) ORDER BY id", (run_id, run_id, issue_id, issue_id)).fetchall()
        return [CostEvent(*r) for r in rows]


def open_store(runs_dir: Path, db_url: str | None = None):
    """Store for a run that writes events: Postgres when configured, otherwise memory plus jsonl export."""
    return PostgresStore(db_url, runs_dir) if db_url else MemoryStore(runs_dir=runs_dir)


def open_reader(runs_dir: Path, db_url: str | None = None):
    """Store for reporting commands: Postgres when configured, otherwise the jsonl files."""
    return PostgresStore(db_url, runs_dir) if db_url else FileStore(runs_dir)
