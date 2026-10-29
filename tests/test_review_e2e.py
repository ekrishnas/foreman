import json

from foreman.workflows.review import bad_references, finish_with_review, gate_review, issues
from tests.conftest import ROOT
from tests.doubles.run import fake_session
from tests.doubles.scenario import Scenario

INPUT = str(ROOT / "examples/new-implementation-url-shortener.md")
BUG = str(ROOT / "examples/bugfix-click-counts.md")

CLEAN = ("## Verification Report: c\n\n### Summary\n| Dimension | Status |\n|---|---|\n| Completeness | 3/3 |\n\n"
         "### CRITICAL\n- None\n\n### WARNING\n- None\n\n### SUGGESTION\n- None\n\n### Final Assessment\nAll checks passed.\n")


def session_for(tmp_path, **kw):
    scenario = Scenario(**{"name": "review", "input": INPUT, "request": "let's work on #1", **kw})
    return fake_session(scenario, tmp_path / "runs", run_id="r1")


def events(s, name=None, stage=None):
    return [e for e in s.run.store.audit_events("r1")
            if (name is None or e.event == name) and (stage is None or e.stage == stage)]


def reviews(s):
    return [c for c in s.run.store.cost_events(run_id="r1") if c.agent == "reviewer"]


def pr_body(s, pr=2):
    return json.loads((s.run.dir / "github/prs" / f"{pr}.json").read_text(encoding="utf-8"))["body"]


def test_a_clean_review_lets_validation_pass_and_the_report_is_kept(tmp_path):
    s = session_for(tmp_path)
    assert s.start().status == "completed"
    assert len(reviews(s)) == 1
    assert "Ready for archive" in s.ctx()["review"] or "All checks passed" in s.ctx()["review"]
    assert "review: clean" in s.ctx()["validate_results"]


def test_the_review_runs_before_the_change_is_archived(tmp_path):
    s = session_for(tmp_path)
    s.start()
    stages = [e.stage for e in events(s, "stage_passed")]
    assert stages.index("validate") < stages.index("archive_spec")
    assert stages.index("review") < stages.index("validate")


def test_a_critical_issue_fails_validation_and_sends_the_code_stages_round_again(tmp_path):
    s = session_for(tmp_path, faults=[{"stage": "review", "calls": [0], "kind": "critical"}])
    result = s.start()
    assert result.status == "completed", result.reason
    reasons = [e.details["reason"] for e in events(s, "attempt_failed", "validate")]
    assert len(reasons) == 1 and "review: Requirement not found" in reasons[0]
    rows = s.run.store.cost_events(run_id="r1")
    assert sum(1 for c in rows if c.stage == "implement") == 2 and len(reviews(s)) == 2


def test_warnings_do_not_block_but_reach_the_pull_request(tmp_path):
    s = session_for(tmp_path, faults=[{"stage": "review", "calls": [0], "kind": "warning"}])
    assert s.start().status == "completed"
    body = pr_body(s)
    assert "## Review (OpenSpec verify)" in body and "Scenario not covered: Stats stay current" in body
    assert len(reviews(s)) == 1


def test_a_report_citing_a_file_that_does_not_exist_is_rejected_then_retried(tmp_path):
    s = session_for(tmp_path, faults=[{"stage": "review", "calls": [0], "kind": "bad_reference"}])
    assert s.start().status == "completed"
    reasons = [e.details["reason"] for e in events(s, "attempt_failed", "review")]
    assert len(reasons) == 1 and "src/nope.py:3" in reasons[0]


def test_no_review_is_paid_for_while_the_code_does_not_pass_its_checks(tmp_path):
    s = session_for(tmp_path, faults=[{"stage": "implement", "calls": [0], "kind": "break_tests"}])
    assert s.start().status == "completed"
    assert len(reviews(s)) == 1  # only after the second, passing validation


def test_a_bugfix_is_reviewed_too(tmp_path):
    s = session_for(tmp_path, input=BUG, request="fix #1", start_ref="v1-regressed")
    assert s.start().status == "completed"
    assert s.run.workflow == "bugfix" and len(reviews(s)) == 1


def test_test_only_and_docs_only_work_is_not_reviewed(tmp_path):
    s = session_for(tmp_path, input=str(ROOT / "tests/inputs/improve-docs.md"), start_ref="v1")
    assert s.start().status == "completed"
    assert s.run.workflow == "doc_improvement" and reviews(s) == []


def test_the_reviewer_cannot_change_anything_or_archive(tmp_path):
    from foreman.core.config import allowed_tools, load_agents, load_target
    agents, _ = load_agents(ROOT / "config/agents.yaml")
    tools = allowed_tools(agents["reviewer"], load_target(ROOT / "examples/target.yaml"))
    assert agents["reviewer"].writable == [] and "Write" not in tools and "Edit" not in tools
    assert not any("archive" in t or "apply" in t or "new change" in t for t in tools)


# ---- the report checks

def test_issues_lists_bullets_and_ignores_none():
    report = CLEAN.replace("### CRITICAL\n- None", "### CRITICAL\n- Requirement not found: X\n- None\n- Another")
    assert issues(report, "CRITICAL") == ["Requirement not found: X", "Another"]
    assert issues(CLEAN, "WARNING") == []


def test_gate_review_needs_every_section(make_run):
    run = make_run({})
    assert gate_review(run, {}, CLEAN) is None
    assert "Final Assessment" in gate_review(run, {}, CLEAN.replace("### Final Assessment", "### Verdict"))


def test_bad_references_checks_the_file_and_the_line(make_run, repo):
    run = make_run({})
    (repo.path / "src").mkdir()
    (repo.path / "src/a.py").write_text("one\ntwo\nthree\n")
    text = "ok `src/a.py:3`, range `src/a.py:1-2`, too far `src/a.py:9`, missing `src/b.py:1`, not a ref `x = 1`"
    assert bad_references(run, text) == ["src/a.py:9 (the file is shorter)", "src/b.py:1 (no such file)"]


def test_finish_with_review_skips_the_reviewer_when_checks_already_failed(make_run, tmp_path):
    run = make_run({})
    run.github.create_issue("t", "b", [])  # validation comments on the issue
    out = finish_with_review(run, {"base_commit": "x"}, ["build failed"], "build: FAIL")
    assert out["ctx"]["validated"] is False and "review" not in out["ctx"]
    assert run.store.cost_events(run_id="r1") == []
