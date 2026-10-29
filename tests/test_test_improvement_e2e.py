from foreman.core.util import count_tests, sh
from tests.conftest import ROOT
from tests.doubles.run import fake_session
from tests.doubles.scenario import Scenario

INPUT = str(ROOT / "tests/inputs/improve-tests.md")


def session_for(tmp_path, **kw):
    scenario = Scenario(**{"name": "test_improvement", "input": INPUT, "request": "work on #1", "start_ref": "v1", **kw})
    return fake_session(scenario, tmp_path / "runs", run_id="t1")


def events(session, name=None, stage=None):
    return [e for e in session.run.store.audit_events("t1")
            if (name is None or e.event == name) and (stage is None or e.stage == stage)]


def test_tests_are_added_and_nothing_else_changes(tmp_path):
    s = session_for(tmp_path)
    result = s.start()
    assert result.status == "completed", result.reason
    assert s.run.workflow == "test_improvement"
    assert s.run.github.pr_state(result.pr).state == "merged"
    changed = s.run.repo.git("diff", "--name-only", "v1", "main").split()
    assert changed == ["tests/test_shortener_more.py"]


def test_the_suite_is_bigger_and_still_green_afterwards(tmp_path):
    s = session_for(tmp_path)
    s.start()
    final = sh(s.run.target.commands["test"], cwd=s.run.repo.path)
    assert final.code == 0 and count_tests(final.out) > s.ctx()["baseline_passed"]


def test_it_needs_no_openspec_change_and_no_approval(tmp_path):
    s = session_for(tmp_path)
    s.start()
    stages = {e.stage for e in events(s, "stage_passed")}
    assert {"intake", "improve_tests", "validate", "pr_summary", "open_pr", "await_merge"} <= stages
    assert not stages & {"proposal", "approve_proposal", "archive_spec"}
    assert events(s, "approval", "approve_proposal") == []


def test_an_attempt_that_adds_no_tests_is_rejected_then_retried(tmp_path):
    s = session_for(tmp_path, faults=[{"stage": "improve_tests", "calls": [0], "kind": "no_new_tests"}])
    assert s.start().status == "completed"
    reasons = [e.details["reason"] for e in events(s, "attempt_failed", "improve_tests")]
    assert len(reasons) == 1 and "no new tests were added" in reasons[0]


def test_the_improver_cannot_touch_source_files(tmp_path):
    s = session_for(tmp_path, faults=[{"stage": "improve_tests", "all": True, "kind": "bad_path"}])
    result = s.start()
    assert result.status == "safe_stopped" and "outside" in result.reason
