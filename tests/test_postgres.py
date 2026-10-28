"""Integration tests against a real Postgres. Set FOREMAN_TEST_DB_URL to run them, e.g.
    FOREMAN_TEST_DB_URL=postgresql://foreman:foreman@localhost:5432/foreman_test uv run pytest -m integration
"""
import os
import uuid

import psycopg
import pytest

from foreman.config import Scenario
from foreman.engine import make_checkpointer, open_session, resume_session
from foreman.store import AuditEvent, CostEvent, PostgresStore
from tests.conftest import ROOT

DB_URL = os.environ.get("FOREMAN_TEST_DB_URL")
pytestmark = [pytest.mark.integration, pytest.mark.skipif(not DB_URL, reason="FOREMAN_TEST_DB_URL is not set")]

STORY = str(ROOT / "scenarios/inputs/feature-url-shortener.md")


def ts(minute):
    return f"2026-01-01T10:{minute}:00+00:00"


@pytest.fixture
def store(tmp_path):
    s = PostgresStore(DB_URL, runs_dir=tmp_path)
    s.conn.execute("TRUNCATE audit_events, cost_events")
    return s


def view(store, name, where=""):
    return store.conn.execute(f"SELECT * FROM {name} {where}").fetchall()


def test_audit_and_cost_events_round_trip(store, tmp_path):
    store.audit(AuditEvent("r1", "feature", "implement", "attempt_failed", 0, "m", 5, {"reason": "x"}, ts(0)))
    store.cost(CostEvent("r1", 7, "feature", "implement", "implementer", 0, "claude-sonnet-5-5", 10, 5, 0.25, 900))
    [event] = store.audit_events("r1")
    assert (event.stage, event.event, event.details, event.duration_ms) == ("implement", "attempt_failed", {"reason": "x"}, 5)
    [cost] = store.cost_events(issue_id=7)
    assert cost.cost_usd == 0.25 and cost.model == "claude-sonnet-5-5"
    assert (tmp_path / "r1" / "audit.jsonl").exists()


def test_cost_views_roll_up_per_run_and_per_issue_with_retry_spend_separate(store):
    for run, attempt, usd in (("r1", 0, 1.0), ("r1", 1, 0.5), ("r2", 0, 2.0)):
        store.cost(CostEvent(run, 7, "feature", "implement", "implementer", attempt, "m", 1, 1, usd, 1))
    per_run = {r[0]: r for r in view(store, "v_cost_per_run")}
    assert per_run["r1"][2] == 1.5 and per_run["r1"][3] == 0.5
    [issue] = view(store, "v_cost_per_issue", "WHERE issue_id = 7")
    assert issue[1] == 3.5 and issue[2] == 2


def test_stage_success_retry_rate_mttr_latency_wait_and_rollback_views(store):
    events = [
        ("implement", "attempt_failed", ts("00"), {}), ("implement", "retry", ts("01"), {}),
        ("implement", "stage_passed", ts("30"), {}), ("approve_proposal", "approval", ts("31"), {"wait_s": 40}),
        ("validate", "rollback", ts("59"), {}),
    ]
    for stage, event, t, details in events:
        store.audit(AuditEvent("r1", "feature", stage, event, 0, "", 0, details, t))
    [success] = view(store, "v_stage_success", "WHERE stage = 'implement'")
    assert success[2] == 1 and success[3] == 1 and success[4] == 0.5
    [retry] = view(store, "v_retry_rate", "WHERE run_id = 'r1'")
    assert retry[1] == 1 and retry[2] == 1
    [mttr] = view(store, "v_mttr", "WHERE run_id = 'r1'")
    assert mttr[2].total_seconds() == 30 * 60
    [latency] = view(store, "v_latency", "WHERE run_id = 'r1'")
    assert latency[1].total_seconds() == 59 * 60
    [wait] = view(store, "v_human_wait", "WHERE run_id = 'r1'")
    assert wait[1] == 40
    [rollbacks] = view(store, "v_rollbacks", "WHERE run_id = 'r1'")
    assert rollbacks[1] == 1


def test_a_run_paused_at_the_merge_gate_resumes_from_a_new_connection(tmp_path):
    db = make_checkpointer(DB_URL)
    runs = tmp_path / "runs"
    run_id = f"pg-{uuid.uuid4().hex[:8]}"
    scenario = Scenario("pg", "feature", STORY, "fake")
    first = open_session(scenario, "fake", runs, checkpointer=db, run_id=run_id, store=PostgresStore(DB_URL, runs))
    first.run.github.auto_merge = False                      # the human merges elsewhere, later
    paused = first.start()
    assert paused.status == "waiting" and paused.pr == 2
    del first, db                                            # the first process is gone

    first_pr_state = lambda session: session.run.github.pr_state(2).state
    second = resume_session(run_id, runs, checkpointer=make_checkpointer(DB_URL), store=PostgresStore(DB_URL, runs))
    assert first_pr_state(second) == "open"
    second.run.github.merge(2)                               # the human merges the PR on GitHub
    result = second.resume()
    assert result.status == "completed", result.reason
    assert second.run.store.cost_events(run_id=run_id)       # costs from both processes are in the database


def test_checkpoints_survive_in_postgres_between_connections():
    run_id = f"pg-{uuid.uuid4().hex[:8]}"
    scenario = Scenario("pg", "feature", STORY, "fake")
    import tempfile
    from pathlib import Path
    runs = Path(tempfile.mkdtemp())
    s = open_session(scenario, "fake", runs, checkpointer=make_checkpointer(DB_URL), run_id=run_id)
    s.run.github.auto_merge = False
    s.start()
    with psycopg.connect(DB_URL) as conn:
        count = conn.execute("SELECT count(*) FROM checkpoints WHERE thread_id = %s", (run_id,)).fetchone()[0]
    assert count > 5
