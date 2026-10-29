import json

from tests.conftest import ROOT
from tests.doubles.run import fake_session
from tests.doubles.scenario import Scenario

INPUT = str(ROOT / "tests/inputs/spike-analytics.md")
ANALYSIS = "docs/spikes/s1/analysis.md"


def scenario(**kw):
    return Scenario(**{"name": "spike", "input": INPUT, "request": "let's work on #1",
                       "script": {"answers": ["Total clicks and referrers", "Hourly is fine"]}, **kw})


def session_for(tmp_path, **kw):
    return fake_session(scenario(**kw), tmp_path / "runs", run_id="s1")


def events(session, name=None, stage=None):
    return [e for e in session.run.store.audit_events("s1")
            if (name is None or e.event == name) and (stage is None or e.stage == stage)]


def test_spike_runs_end_to_end_and_hands_off(tmp_path):
    s = session_for(tmp_path)
    result = s.start()
    assert result.status == "completed", result.reason
    assert s.run.github.pr_state(result.pr).state == "merged"
    passed = {e.stage for e in events(s, "stage_passed")}
    assert {"intake", "clarify", "frame", "analysis", "open_pr", "await_merge", "handoff"} <= passed


def test_answered_questions_are_clarifications_and_the_rest_are_assumptions(tmp_path):
    s = session_for(tmp_path)
    s.start()
    ctx = s.ctx()
    assert "Total clicks and referrers" in ctx["clarifications"] and "Hourly is fine" in ctx["clarifications"]
    assert "expected click volume" in ctx["assumptions"] and "expected click volume" not in ctx["clarifications"]


def test_one_explorer_per_option_each_confined_to_its_own_directory(tmp_path):
    s = session_for(tmp_path)
    s.start()
    stages = sorted({c.stage for c in s.run.store.cost_events(run_id="s1") if c.agent == "explorer"})
    assert stages == ["explore/option-1", "explore/option-2", "explore/option-3"]
    for option in ("option-1", "option-2", "option-3"):
        assert (s.run.dir / "spikes" / option / "notes.md").exists()


def test_only_the_analysis_document_is_merged(tmp_path):
    s = session_for(tmp_path)
    s.start()
    base = s.ctx()["base_commit"]
    changed = s.run.repo.git("diff", "--name-only", base, "main").split()
    assert changed == [ANALYSIS]
    assert not (s.run.repo.path / "spikes").exists()


def test_analysis_has_the_required_sections_and_trade_off_criteria(tmp_path):
    s = session_for(tmp_path)
    s.start()
    text = (s.run.repo.path / ANALYSIS).read_text()
    for heading in ("Context", "Assumptions", "Options", "Trade-off matrix", "Recommendation",
                    "Open questions", "Draft user story"):
        assert f"## {heading}" in text
    for criterion in ("Complexity", "Cost", "Performance", "Risk", "Operability"):
        assert criterion in text


def test_handoff_files_a_feature_issue_from_the_draft_story_linked_to_the_spike(tmp_path):
    s = session_for(tmp_path)
    result = s.start()
    new = s.ctx()["handoff_issue"]
    issue = s.run.github.get_issue(new)
    assert issue.labels == ["foreman:from-spike"] and issue.title == "Track clicks per day for each short link"
    assert "As a link owner" in issue.body and "spike #1" in issue.body and f"PR #{result.pr}" in issue.body
    spike_comments = json.loads((s.run.dir / "github/issues/1.json").read_text())["comments"]
    assert any(f"#{new}" in c for c in spike_comments)


def test_pr_body_has_cost_but_no_test_results_or_openspec_change(tmp_path):
    s = session_for(tmp_path)
    result = s.start()
    body = json.loads((s.run.dir / "github/prs" / f"{result.pr}.json").read_text())["body"]
    assert "Run total" in body and "Closes #1" in body
    assert "## Test results" not in body and "OpenSpec change" not in body


def test_a_framing_with_more_than_three_options_is_rejected_and_retried(tmp_path):
    s = session_for(tmp_path, faults=[{"stage": "frame", "calls": [0], "kind": "too_many_options"}])
    result = s.start()
    assert result.status == "completed", result.reason
    reasons = [e.details["reason"] for e in events(s, "attempt_failed", "frame")]
    assert len(reasons) == 1 and "between 1 and 3" in reasons[0]


def test_an_explorer_that_writes_outside_its_directory_is_blocked(tmp_path):
    s = session_for(tmp_path, faults=[{"stage": "explore", "calls": [0], "kind": "cross_write"}])
    result = s.start()
    assert result.status == "completed", result.reason
    failed = events(s, "attempt_failed")
    assert failed and all(e.details["reason"].startswith("policy:") for e in failed)
    assert not (s.run.repo.path / "spikes/s1/other-option").exists()


def test_an_analysis_without_a_trade_off_matrix_is_rejected_and_retried(tmp_path):
    s = session_for(tmp_path, faults=[{"stage": "analysis", "calls": [0], "kind": "missing_matrix"}])
    result = s.start()
    assert result.status == "completed", result.reason
    reasons = [e.details["reason"] for e in events(s, "attempt_failed", "analysis")]
    assert len(reasons) == 1 and "Trade-off matrix" in reasons[0]


def test_a_spike_pr_closed_without_merge_stops_before_handoff(tmp_path):
    s = session_for(tmp_path, script={"release": ["reject: not convincing"], "answers": []})
    result = s.start()
    assert result.status == "safe_stopped" and "not convincing" in result.reason
    assert "handoff_issue" not in s.ctx()
