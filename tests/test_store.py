import json

from foreman.store import AuditEvent, CostEvent, MemoryStore


def cost(run="r1", issue=1, stage="implement", attempt=0, usd=1.0):
    return CostEvent(run, issue, "feature", stage, "implementer", attempt, "claude-sonnet-5-5", 100, 50, usd, 1000)


def test_audit_appends_json_line_per_event(tmp_path):
    s = MemoryStore(runs_dir=tmp_path)
    s.audit(AuditEvent("r1", "feature", "proposal", "stage_passed", details={"k": 1}))
    s.audit(AuditEvent("r1", "feature", "implement", "retry"))
    lines = (tmp_path / "r1" / "audit.jsonl").read_text().splitlines()
    assert [json.loads(x)["event"] for x in lines] == ["stage_passed", "retry"]
    assert json.loads(lines[0])["details"] == {"k": 1}


def test_audit_stamps_timestamp_when_missing():
    s = MemoryStore()
    s.audit(AuditEvent("r1", "feature", "x", "e"))
    assert s.audit_events("r1")[0].ts.startswith("20")


def test_audit_keeps_explicit_timestamp():
    s = MemoryStore()
    s.audit(AuditEvent("r1", "feature", "x", "e", ts="2026-01-01T00:00:00+00:00"))
    assert s.audit_events("r1")[0].ts == "2026-01-01T00:00:00+00:00"


def test_audit_events_are_per_run_and_ordered():
    s = MemoryStore()
    s.audit(AuditEvent("r1", "feature", "a", "one"))
    s.audit(AuditEvent("r2", "feature", "a", "other"))
    s.audit(AuditEvent("r1", "feature", "a", "two"))
    assert [e.event for e in s.audit_events("r1")] == ["one", "two"]


def test_cost_events_by_run():
    s = MemoryStore()
    s.cost(cost(run="r1"))
    s.cost(cost(run="r2"))
    assert len(s.cost_events(run_id="r1")) == 1


def test_cost_events_by_issue_span_runs():
    s = MemoryStore()
    s.cost(cost(run="r1", issue=7))
    s.cost(cost(run="r2", issue=7))
    s.cost(cost(run="r3", issue=8))
    assert len(s.cost_events(issue_id=7)) == 2
