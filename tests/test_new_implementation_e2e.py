import json

from tests.conftest import ROOT
from tests.doubles.run import fake_session
from tests.doubles.scenario import Scenario

INPUT = str(ROOT / "examples/new-implementation-url-shortener.md")
ALL_STAGES = ["classify", "intake", "requirements", "proposal", "implement", "tests", "docs", "validate",
              "archive_spec", "pr_summary", "open_pr", "await_merge"]


def scenario(**kw):
    return Scenario(**{"name": "new_implementation", "input": INPUT, "request": "let's work on #1", **kw})


def session_for(tmp_path, **kw):
    return fake_session(scenario(**kw), tmp_path / "runs", run_id="t1")


def events(session, name=None, stage=None):
    return [e for e in session.run.store.audit_events("t1")
            if (name is None or e.event == name) and (stage is None or e.stage == stage)]


def pr_json(session, n):
    return json.loads((session.run.dir / "github/prs" / f"{n}.json").read_text())


def test_new_implementation_feature_runs_end_to_end(tmp_path):
    s = session_for(tmp_path)
    result = s.start()
    assert result.status == "completed", result.reason
    assert result.pr == 2 and s.run.github.pr_state(2).state == "merged"
    passed = {e.stage for e in events(s, "stage_passed")}
    assert set(ALL_STAGES) <= passed


def test_work_product_lands_on_main_and_spec_is_archived(tmp_path):
    s = session_for(tmp_path)
    s.start()
    root = s.run.repo.path
    assert s.run.repo.branch() == "main"
    for rel in ("src/shortener.py", "tests/test_shortener.py", "docs/api.md", "openspec/specs/shortener/spec.md"):
        assert (root / rel).exists(), rel
    archived = list((root / "openspec/changes/archive").glob("*/tasks.md"))
    assert archived and "- [ ]" not in archived[0].read_text()


def test_pr_body_has_cost_gates_approvals_and_closes_line(tmp_path):
    s = session_for(tmp_path)
    s.start()
    body = pr_json(s, 2)["body"]
    for needle in ("Closes #1", "Run total", "Issue total", "## Gates, retries and policy",
                   "## Approvals", "proposal: approved", "tests: ", "OpenSpec change", "## Risks and rollback"):
        assert needle in body, needle
    assert pr_json(s, 2)["labels"] == ["foreman:new_implementation"]


def test_issue_thread_is_a_status_trail(tmp_path):
    s = session_for(tmp_path)
    s.start()
    comments = json.loads((s.run.dir / "github/issues/1.json").read_text())["comments"]
    text = "\n".join(comments)
    assert "started" in text and "approved" in text and "Pull request #2 opened" in text and "merged" in text
    assert "ready for approval" in text and "Validation passed" in text and "classified this request" in text


def test_the_whole_run_is_in_the_audit_trail_from_classification_to_completion(tmp_path):
    s = session_for(tmp_path)
    s.start()
    trail = s.run.store.audit_events("t1")
    assert trail[0].stage == "classify" and trail[0].workflow == "route"
    assert trail[-1].event == "run_completed" and trail[-1].workflow == "new_implementation"
    assert len(trail) >= 15  # every stage that passed, plus approvals and the PR events


def test_the_type_the_classifier_chose_is_saved_so_a_resume_does_not_ask_again(tmp_path):
    s = session_for(tmp_path)
    s.start()
    saved = json.loads((s.run.dir / "run.json").read_text(encoding="utf-8"))
    assert saved["workflow"] == "new_implementation" and saved["request"] == "let's work on #1"


def test_the_observability_notes_are_in_the_pr_and_on_the_issue(tmp_path):
    s = session_for(tmp_path)
    s.start()
    assert "## Observability" in pr_json(s, 2)["body"] and "Log location: logs/app.log" in pr_json(s, 2)["body"]
    assert any(c.startswith("**Observability notes**") for c in s.run.github.get_issue(1).comments)


def test_an_implementer_that_does_not_say_how_it_logs_is_rejected_then_retried(tmp_path):
    s = session_for(tmp_path, faults=[{"stage": "implement", "calls": [0], "kind": "no_observability"}])
    assert s.start().status == "completed"
    reasons = [e.details["reason"] for e in events(s, "attempt_failed", "implement")]
    assert len(reasons) == 1 and "## Observability" in reasons[0]


def test_every_agent_call_has_a_cost_row_and_the_run_has_a_total(tmp_path):
    s = session_for(tmp_path)
    s.start()
    agents = {c.agent for c in s.run.store.cost_events(run_id="t1")}
    assert {"requirements_analyst", "architect", "implementer", "test_writer", "doc_writer", "pr_summarizer"} <= agents


def test_rejected_proposal_is_replanned_with_the_reviewer_note(tmp_path):
    s = session_for(tmp_path, script={"proposal": ["reject: add rate limiting", "approve"]})
    result = s.start()
    assert result.status == "completed", result.reason
    approvals = [e.details["approved"] for e in events(s, "approval", "approve_proposal")]
    assert approvals == [False, True]
    assert sum(1 for c in s.run.store.cost_events(run_id="t1") if c.stage == "proposal") == 2
    assert "rate limiting" in s.ctx()["design"]


def test_blocking_questions_are_asked_and_unanswered_ones_become_assumptions(tmp_path):
    story = tmp_path / "story.md"
    story.write_text("# Short links\n\nShorten URLs.\n\nQUESTION: Which database?\nQUESTION: Should links expire?\n")
    s = session_for(tmp_path, input=str(story), script={"answers": ["Postgres"]})
    result = s.start()
    assert result.status == "completed", result.reason
    ctx = s.ctx()
    assert "Postgres" in ctx["clarifications"] and "Should links expire?" in ctx["assumptions"]
    assert "Should links expire?" not in ctx["clarifications"]


def test_architect_output_that_fails_openspec_validation_is_retried(tmp_path):
    s = session_for(tmp_path, faults=[{"stage": "proposal", "calls": [0], "kind": "break_spec"}])
    result = s.start()
    assert result.status == "completed", result.reason
    failed = events(s, "attempt_failed", "proposal")
    assert len(failed) == 1 and "Scenario" in failed[0].details["reason"]


def test_closed_pr_safe_stops_and_records_the_reviewer_comment(tmp_path):
    s = session_for(tmp_path, script={"release": ["reject: not ready"]})
    result = s.start()
    assert result.status == "safe_stopped"
    assert "closed without merge" in result.reason and "not ready" in result.reason
    assert s.run.github.pr_state(2).state == "closed"


def test_run_branch_and_stage_commits(tmp_path):
    s = session_for(tmp_path)
    s.start()
    log = s.run.repo.git("log", "--format=%s", "main").splitlines()
    assert "foreman: proposal" in log and "foreman: implement" in log and "foreman: archive_spec" in log
    assert s.run.repo.git("branch", "--list", "foreman/t1").strip().endswith("foreman/t1")
