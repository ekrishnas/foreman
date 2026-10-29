import pytest

from foreman.workflows.nodes import OBSERVABILITY_MARK
from foreman.workflows.observe import LOCATION_MARK, location_in_notes, paths_in
from tests.conftest import ROOT
from tests.doubles.run import fake_session
from tests.doubles.scenario import Scenario

INPUT = str(ROOT / "examples/new-implementation-url-shortener.md")
NOTES = f"{OBSERVABILITY_MARK}\n- Log location: logs/app.log\n- Success: `INFO shortened url code=`\n- Errors: `ERROR`\n"
GOOD_LOG = "INFO server started\nINFO shortened url code=abc123\nINFO shortened url code=def456\n"
BAD_LOG = "INFO server started\nINFO shortened url code=abc123\nERROR resolve failed: KeyError('zzz')\n"
REQUEST = "issue #1 is deployed to qa, check the logs if it's working"


def session_for(tmp_path, log=None, comments=(), **kw):
    scenario = Scenario(**{"name": "observe", "input": INPUT, "request": REQUEST, "start_ref": "v1", **kw})
    s = fake_session(scenario, tmp_path / "runs", run_id="o1")
    for comment in comments:
        s.run.github.comment(1, comment)
    if log is not None:
        (s.run.repo.path / "logs").mkdir(exist_ok=True)
        (s.run.repo.path / "logs/app.log").write_text(log, encoding="utf-8")
    return s


def comments(s):
    return s.run.github.get_issue(1).comments


def events(s, name=None, stage=None):
    return [e for e in s.run.store.audit_events("o1")
            if (name is None or e.event == name) and (stage is None or e.stage == stage)]


def test_logs_found_through_the_implementers_notes_confirm_the_work(tmp_path):
    s = session_for(tmp_path, GOOD_LOG, comments=[NOTES])
    result = s.start()
    assert result.status == "completed", result.reason
    assert s.run.workflow == "observe"
    assert result.reason.startswith("working; logs checked:") and "app.log" in result.reason
    report = comments(s)[-1]
    assert "Observe result for #1: **working**" in report and "Logs checked: `" in report
    assert "`INFO shortened url code=abc123`" in report


def test_errors_in_the_logs_make_the_verdict_failing_and_quote_the_error(tmp_path):
    s = session_for(tmp_path, BAD_LOG, comments=[NOTES])
    result = s.start()
    assert result.status == "completed" and result.reason.startswith("failing")
    assert "ERROR resolve failed: KeyError('zzz')" in comments(s)[-1]


def test_observing_changes_nothing_in_the_repository(tmp_path):
    s = session_for(tmp_path, GOOD_LOG, comments=[NOTES])
    head = s.run.repo.head()
    s.start()
    assert s.run.repo.branch() == "main"
    assert s.run.repo.head() == head  # no branch, no commits, even though logs/ is untracked


def test_a_location_in_the_developers_message_wins_and_is_saved_on_the_issue(tmp_path):
    s = session_for(tmp_path, GOOD_LOG, request="issue #1 is deployed, check logs/app.log")
    assert s.start().status == "completed"
    assert f"{LOCATION_MARK} logs/app.log" in comments(s)


def test_a_location_saved_earlier_is_used_without_asking(tmp_path):
    s = session_for(tmp_path, GOOD_LOG, comments=[f"{LOCATION_MARK} logs/app.log"], script={"answers": ["SHOULD-NOT-BE-ASKED"]})
    assert s.start().status == "completed"
    assert s.run.human.script["answers"] == ["SHOULD-NOT-BE-ASKED"]  # nobody was asked


def test_with_no_location_anywhere_it_asks_and_remembers_the_answer(tmp_path):
    s = session_for(tmp_path, GOOD_LOG, script={"answers": ["logs/app.log"]})
    assert s.start().status == "completed"
    assert s.run.human.script["answers"] == []  # the one answer was consumed
    assert f"{LOCATION_MARK} logs/app.log" in comments(s)


def test_a_location_that_does_not_exist_is_asked_again_then_gives_up(tmp_path):
    s = session_for(tmp_path, GOOD_LOG, script={"answers": ["nowhere/a.log", "still/nowhere.log"]})
    result = s.start()
    assert result.status == "safe_stopped" and "no usable log location" in result.reason


def test_declining_to_give_a_location_stops_the_run(tmp_path):
    s = session_for(tmp_path, GOOD_LOG)  # no notes, no saved location, and the scripted human has no answers
    assert s.start().status == "safe_stopped"


def test_a_folder_means_its_newest_file(tmp_path):
    s = session_for(tmp_path, GOOD_LOG, request="issue #1 is deployed, check the logs in ./logs")
    (s.run.repo.path / "logs/older.log").write_text("ERROR old\n", encoding="utf-8")
    import os
    os.utime(s.run.repo.path / "logs/older.log", (1, 1))
    result = s.start()
    assert result.reason.startswith("working") and "app.log" in result.reason


def test_an_empty_log_file_stops_the_run(tmp_path):
    s = session_for(tmp_path, "", comments=[NOTES])
    result = s.start()
    assert result.status == "safe_stopped" and "is empty" in result.reason


def test_evidence_that_is_not_in_the_log_is_rejected_then_retried(tmp_path):
    s = session_for(tmp_path, GOOD_LOG, comments=[NOTES], faults=[{"stage": "observe", "calls": [0], "kind": "invented_evidence"}])
    assert s.start().status == "completed"
    reasons = [e.details["reason"] for e in events(s, "attempt_failed", "observe")]
    assert len(reasons) == 1 and "not in the log excerpt" in reasons[0]


def test_the_observer_never_sees_the_source_only_the_excerpt(tmp_path):
    prompts = []

    class Spy:
        def run(self, req):
            prompts.append((req.agent.name, req.cwd, req.prompt))
            from tests.doubles.worker import FakeWorker
            return FakeWorker().run(req)

    s = session_for(tmp_path, GOOD_LOG, comments=[NOTES])
    s.run.worker = Spy()
    s.start()
    name, cwd, prompt = next(p for p in prompts if p[0] == "observer")
    assert cwd != s.run.repo.path and "INFO shortened url code=abc123" in prompt and "Log location: logs/app.log" in prompt


@pytest.mark.parametrize("text,expected", [
    ("check logs/app.log please", ["logs/app.log"]),
    ("look at C:\\logs\\app.log now", ["C:\\logs\\app.log"]),
    ("see app.log", ["app.log"]),
    ("is it working?", []),
])
def test_paths_in_finds_things_that_look_like_paths(text, expected):
    assert paths_in(text) == expected


def test_location_in_notes_reads_the_log_location_line():
    assert location_in_notes(NOTES) == "logs/app.log"
    assert location_in_notes("- Log location (relative): `var/log/x.log`") == "var/log/x.log"
    assert location_in_notes("nothing here") == ""
