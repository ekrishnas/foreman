import threading

import pytest

from foreman.state.store import AuditEvent, CostEvent, Store


def cost(run="r1", issue=1, stage="implement", attempt=0, usd=1.0):
    return CostEvent(run, issue, "greenfield", stage, "implementer", attempt, "claude-sonnet-5-5", 100, 50, usd, 1000)


def test_audit_events_round_trip_with_their_details():
    s = Store(":memory:")
    s.audit(AuditEvent("r1", "bugfix", "proposal", "stage_passed", details={"k": 1}))
    e = s.audit_events("r1")[0]
    assert (e.stage, e.event, e.workflow, e.details) == ("proposal", "stage_passed", "bugfix", {"k": 1})


def test_audit_stamps_timestamp_when_missing():
    s = Store(":memory:")
    s.audit(AuditEvent("r1", "x", "x", "e"))
    assert s.audit_events("r1")[0].ts.startswith("20")


def test_audit_keeps_explicit_timestamp():
    s = Store(":memory:")
    s.audit(AuditEvent("r1", "x", "x", "e", ts="2026-01-01T00:00:00+00:00"))
    assert s.audit_events("r1")[0].ts == "2026-01-01T00:00:00+00:00"


def test_audit_events_are_per_run_and_ordered():
    s = Store(":memory:")
    s.audit(AuditEvent("r1", "x", "a", "one"))
    s.audit(AuditEvent("r2", "x", "a", "other"))
    s.audit(AuditEvent("r1", "x", "a", "two"))
    assert [e.event for e in s.audit_events("r1")] == ["one", "two"]


def test_cost_events_by_run_and_by_issue_across_runs():
    s = Store(":memory:")
    s.cost(cost(run="r1", issue=7))
    s.cost(cost(run="r2", issue=7))
    s.cost(cost(run="r3", issue=8))
    assert len(s.cost_events(run_id="r1")) == 1
    assert len(s.cost_events(issue_id=7)) == 2
    assert len(s.cost_events()) == 3


def test_a_second_connection_sees_what_the_first_wrote(tmp_path):
    """`foreman run` and `foreman respond` are different processes sharing one file."""
    first, second = Store(tmp_path / "f.db"), Store(tmp_path / "f.db")
    request = first.ask("r1", "proposal", "Approve?")
    second.respond(request, approved=True, who="dev")
    assert first.get_request(request)["status"] == "answered"


def test_a_question_starts_pending_and_is_listed_until_answered():
    s = Store(":memory:")
    a, b = s.ask("r1", "proposal", "Approve?"), s.ask("r2", "question", "Which db?")
    assert [r["id"] for r in s.pending()] == [a, b]
    assert [r["id"] for r in s.pending("r2")] == [b]
    s.respond(a, approved=False, note="needs auth", who="dev")
    assert [r["id"] for r in s.pending()] == [b]
    row = s.get_request(a)
    assert (row["approved"], row["note"], row["who"]) == (0, "needs auth", "dev")


def test_an_answer_to_a_question_is_kept():
    s = Store(":memory:")
    q = s.ask("r1", "question", "Which db?")
    s.respond(q, answer="Postgres", who="dev")
    assert s.get_request(q)["answer"] == "Postgres"


def test_a_request_can_only_be_answered_once():
    s = Store(":memory:")
    q = s.ask("r1", "proposal", "Approve?")
    s.respond(q, approved=True)
    with pytest.raises(ValueError, match="already answered"):
        s.respond(q, approved=False)


def test_an_unknown_request_is_a_key_error():
    with pytest.raises(KeyError):
        Store(":memory:").get_request(99)


def test_parallel_graph_branches_can_write_at_once():
    s = Store(":memory:")
    threads = [threading.Thread(target=lambda i=i: [s.audit(AuditEvent("r1", "x", f"s{i}", "e")) for _ in range(25)])
               for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(s.audit_events("r1")) == 100
