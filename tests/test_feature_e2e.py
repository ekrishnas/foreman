import json

from foreman.commits import lint
from foreman.config import Scenario
from foreman.engine import open_session
from tests.conftest import ROOT

INPUT = str(ROOT / "scenarios/inputs/feature-url-shortener.md")
ALL_STAGES = ["intake", "requirements", "proposal", "implement", "tests", "docs", "validate",
              "archive_spec", "pr_summary", "open_pr", "await_merge"]


def scenario(**kw):
    return Scenario(**{"name": "greenfield", "workflow": "feature", "input": INPUT, "target": "fake", **kw})


def session_for(tmp_path, **kw):
    return open_session(scenario(**kw), "fake", tmp_path / "runs", run_id="t1")


def events(session, name=None, stage=None):
    return [e for e in session.run.store.audit_events("t1")
            if (name is None or e.event == name) and (stage is None or e.stage == stage)]


def pr_json(session, n):
    return json.loads((session.run.dir / "github/prs" / f"{n}.json").read_text())


def test_greenfield_feature_runs_end_to_end(tmp_path):
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
    assert pr_json(s, 2)["labels"] == ["foreman:feature"]


def test_issue_thread_is_a_status_trail(tmp_path):
    s = session_for(tmp_path)
    s.start()
    comments = json.loads((s.run.dir / "github/issues/1.json").read_text())["comments"]
    text = "\n".join(comments)
    assert "started" in text and "approved" in text and "Pull request #2 opened" in text and "merged" in text
    assert "ready for approval" in text and "Validation passed" in text and "Observe passed" in text


def test_audit_log_is_exported_as_jsonl(tmp_path):
    s = session_for(tmp_path)
    s.start()
    lines = (tmp_path / "runs/t1/audit.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["run_id"] == "t1" and len(lines) > 10


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
    assert "docs: propose the change" in log and "feat: implement the change" in log and "docs: archive the change spec" in log
    assert s.run.repo.git("branch", "--list", "foreman/t1").strip().endswith("foreman/t1")


def test_the_released_commit_is_tagged_when_the_scenario_asks_for_it(tmp_path):
    s = session_for(tmp_path, tag="v1")
    result = s.start()
    assert result.status == "completed", result.reason
    tagged = s.run.repo.git("rev-list", "-n", "1", "v1").strip()
    assert tagged == s.ctx()["merge_sha"]


def test_a_tag_that_already_points_elsewhere_stops_the_run_instead_of_moving_it(tmp_path):
    s = session_for(tmp_path, tag="v1")
    s.run.repo.git("tag", "v1")            # an earlier release already owns the tag
    result = s.start()
    assert result.status == "safe_stopped" and "v1" in result.reason


def test_every_commit_of_a_run_is_conventional_authored_by_the_developer_and_co_authored_by_foreman(tmp_path, git_config):
    git_config.write_text("[user]\n\tname = Dev Eloper\n\temail = dev@example.com\n", encoding="utf-8")
    s = session_for(tmp_path)
    assert s.start().status == "completed"
    shas = s.run.repo.git("rev-list", "--no-merges", "main").split()
    assert len(shas) >= 5                                      # fixture history plus the stage commits
    for sha in shas:
        author, message = s.run.repo.git("log", "-1", "--format=%an|%B", sha).split("|", 1)
        subject_and_body, trailer = message.strip().rsplit("\n\n", 1)
        assert author == "Dev Eloper", message
        assert trailer == "Co-Authored-By: Foreman <foreman@localhost>", message
        assert lint(subject_and_body) == [], message
