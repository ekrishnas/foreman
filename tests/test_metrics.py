from foreman.metrics import cost_table, run_metrics
from foreman.store import AuditEvent, CostEvent


def ev(stage, event, t, **details):
    return AuditEvent("r1", "feature", stage, event, ts=f"2026-01-01T10:{t}+00:00", details=details)


def cost(stage="implement", attempt=0, usd=1.0, agent="implementer"):
    return CostEvent("r1", 1, "feature", stage, agent, attempt, "claude-sonnet-5-5", 1000, 500, usd, 12000)


FAIL_THEN_PASS = [
    ev("implement", "attempt_failed", "00:00"),
    ev("implement", "retry", "00:01"),
    ev("implement", "stage_passed", "00:30"),
]


def test_mttr_is_failure_to_next_pass_for_same_stage():
    assert run_metrics(FAIL_THEN_PASS, [])["mttr_s"] == 30


def test_retry_rate_is_retries_per_passed_stage():
    assert run_metrics(FAIL_THEN_PASS, [])["retry_rate"] == 1.0


def test_stage_success_rate_counts_failed_attempts():
    assert run_metrics(FAIL_THEN_PASS, [])["stage_success"] == {"implement": 0.5}


def test_rollbacks_are_counted():
    assert run_metrics([ev("validate", "rollback", "01:00")], [])["rollbacks"] == 1


def test_latency_spans_first_to_last_event():
    events = [ev("a", "stage_passed", "00:00"), ev("b", "stage_passed", "10:00")]
    assert run_metrics(events, [])["latency_s"] == 600


def test_human_wait_sums_approvals_and_merge_wait():
    events = [
        ev("proposal", "approval", "00:00", wait_s=30),
        ev("await_merge", "merge_wait", "05:00", wait_s=90),
        ev("x", "stage_passed", "06:00"),
    ]
    assert run_metrics(events, [])["human_wait_s"] == 120


def test_cost_totals_split_out_retry_spend():
    m = run_metrics([], [cost(attempt=0, usd=1.0), cost(attempt=1, usd=0.5)])
    assert m["cost_usd"] == 1.5 and m["retry_cost_usd"] == 0.5


def test_no_events_gives_zeroes():
    m = run_metrics([], [])
    assert m["latency_s"] == 0 and m["mttr_s"] == 0 and m["retry_rate"] == 0


def test_cost_table_has_rows_run_total_issue_total_and_notional_label():
    run = [cost(attempt=0, usd=1.0), cost(attempt=1, usd=0.5)]
    issue = run + [cost(usd=2.5)]
    table = cost_table(run, issue)
    assert "| implementer | claude-sonnet-5-5 | 2 |" in table
    assert "Run total" in table and "$1.50" in table
    assert "retries and re-runs" in table and "$0.50" in table
    assert "Issue total" in table and "$4.00" in table
    assert "notional" in table
