"""One SQLite file holds everything Foreman remembers: audit events, cost events and questions for a human.

`foreman run` writes to it while `foreman pending` / `foreman respond` (a different process, usually started
by Claude Code) read and answer it, so it has to be shared storage. WAL mode lets both use it at once.
"""
import json
import sqlite3
import threading
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


SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY, run_id TEXT, workflow TEXT, stage TEXT, event TEXT, attempt INT, model TEXT,
    duration_ms INT, details TEXT, ts TEXT);
CREATE INDEX IF NOT EXISTS audit_run ON audit_events (run_id);
CREATE TABLE IF NOT EXISTS cost_events (
    id INTEGER PRIMARY KEY, run_id TEXT, issue_id INT, workflow TEXT, stage TEXT, agent TEXT, attempt INT,
    model TEXT, input_tokens INT, output_tokens INT, cost_usd REAL, duration_ms INT);
CREATE INDEX IF NOT EXISTS cost_run ON cost_events (run_id);
CREATE INDEX IF NOT EXISTS cost_issue ON cost_events (issue_id);
CREATE TABLE IF NOT EXISTS human_requests (
    id INTEGER PRIMARY KEY, run_id TEXT, kind TEXT, prompt TEXT, status TEXT DEFAULT 'pending',
    approved INT, note TEXT DEFAULT '', answer TEXT DEFAULT '', who TEXT DEFAULT '', created TEXT, answered TEXT);
"""


class Store:
    def __init__(self, path: Path | str):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        # Autocommit, and one lock because parallel graph branches write from several threads.
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.lock = threading.Lock()

    def _run(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, args)

    def _all(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, args).fetchall()

    # ---- audit and cost
    def audit(self, ev: AuditEvent) -> None:
        ev.ts = ev.ts or now()
        self._run("INSERT INTO audit_events (run_id, workflow, stage, event, attempt, model, duration_ms, details, ts) "
                  "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  (ev.run_id, ev.workflow, ev.stage, ev.event, ev.attempt, ev.model, ev.duration_ms,
                   json.dumps(ev.details), ev.ts))

    def cost(self, ev: CostEvent) -> None:
        self._run("INSERT INTO cost_events (run_id, issue_id, workflow, stage, agent, attempt, model, input_tokens, "
                  "output_tokens, cost_usd, duration_ms) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                  tuple(asdict(ev).values()))

    def audit_events(self, run_id: str) -> list[AuditEvent]:
        rows = self._all("SELECT * FROM audit_events WHERE run_id = ? ORDER BY id", (run_id,))
        return [AuditEvent(r["run_id"], r["workflow"], r["stage"], r["event"], r["attempt"], r["model"],
                           r["duration_ms"], json.loads(r["details"]), r["ts"]) for r in rows]

    def runs(self) -> list[dict]:
        """Every run that has recorded anything: id, first and last timestamp, number of events."""
        rows = self._all("SELECT run_id, MIN(ts) AS started, MAX(ts) AS last, COUNT(*) AS events "
                         "FROM audit_events GROUP BY run_id ORDER BY MIN(ts)")
        return [dict(r) for r in rows]

    def cost_events(self, run_id: str | None = None, issue_id: int | None = None) -> list[CostEvent]:
        rows = self._all("SELECT * FROM cost_events WHERE (? IS NULL OR run_id = ?) AND (? IS NULL OR issue_id = ?) "
                         "ORDER BY id", (run_id, run_id, issue_id, issue_id))
        return [CostEvent(*(r[k] for k in CostEvent.__dataclass_fields__)) for r in rows]

    # ---- questions for a human
    def ask(self, run_id: str, kind: str, prompt: str) -> int:
        return self._run("INSERT INTO human_requests (run_id, kind, prompt, created) VALUES (?, ?, ?, ?)",
                         (run_id, kind, prompt, now())).lastrowid

    def get_request(self, request_id: int) -> dict:
        rows = self._all("SELECT * FROM human_requests WHERE id = ?", (request_id,))
        if not rows:
            raise KeyError(f"no request {request_id}")
        return dict(rows[0])

    def pending(self, run_id: str | None = None) -> list[dict]:
        rows = self._all("SELECT * FROM human_requests WHERE status = 'pending' AND (? IS NULL OR run_id = ?) "
                         "ORDER BY id", (run_id, run_id))
        return [dict(r) for r in rows]

    def respond(self, request_id: int, approved: bool | None = None, note: str = "", answer: str = "",
                who: str = "") -> None:
        if self.get_request(request_id)["status"] != "pending":
            raise ValueError(f"request {request_id} was already answered")
        self._run("UPDATE human_requests SET status = 'answered', approved = ?, note = ?, answer = ?, who = ?, "
                  "answered = ? WHERE id = ?", (None if approved is None else int(approved), note, answer, who, now(),
                                                request_id))
