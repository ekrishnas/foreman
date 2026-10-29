from foreman.workflows.doc_improvement import broken_links
from tests.conftest import ROOT
from tests.doubles.run import fake_session
from tests.doubles.scenario import Scenario

INPUT = str(ROOT / "tests/inputs/improve-docs.md")


def session_for(tmp_path, **kw):
    scenario = Scenario(**{"name": "doc_improvement", "input": INPUT, "request": "work on #1", "start_ref": "v1", **kw})
    return fake_session(scenario, tmp_path / "runs", run_id="d1")


def events(session, name=None, stage=None):
    return [e for e in session.run.store.audit_events("d1")
            if (name is None or e.event == name) and (stage is None or e.stage == stage)]


def test_a_docs_page_is_added_and_nothing_else_changes(tmp_path):
    s = session_for(tmp_path)
    result = s.start()
    assert result.status == "completed", result.reason
    assert s.run.workflow == "doc_improvement"
    assert s.run.github.pr_state(result.pr).state == "merged"
    assert s.run.repo.git("diff", "--name-only", "v1", "main").split() == ["docs/usage.md"]


def test_the_pr_goes_through_the_normal_gates_without_a_proposal(tmp_path):
    s = session_for(tmp_path)
    s.start()
    stages = {e.stage for e in events(s, "stage_passed")}
    assert {"intake", "improve_docs", "validate", "pr_summary", "open_pr", "await_merge"} <= stages
    assert not stages & {"proposal", "approve_proposal", "archive_spec"}


def test_a_page_with_a_broken_relative_link_is_rejected_then_retried(tmp_path):
    s = session_for(tmp_path, faults=[{"stage": "improve_docs", "calls": [0], "kind": "broken_link"}])
    assert s.start().status == "completed"
    reasons = [e.details["reason"] for e in events(s, "attempt_failed", "improve_docs")]
    assert len(reasons) == 1 and "docs/usage.md -> missing.md" in reasons[0]
    assert "(api.md)" in (s.run.repo.path / "docs/usage.md").read_text(encoding="utf-8")


def test_the_improver_cannot_touch_source_files(tmp_path):
    s = session_for(tmp_path, faults=[{"stage": "improve_docs", "all": True, "kind": "bad_path"}])
    assert s.start().status == "safe_stopped"


def test_broken_links_ignores_external_anchors_and_checks_relative_and_rooted_paths(make_run, repo):
    run = make_run({})
    (repo.path / "docs").mkdir()
    (repo.path / "docs/real.md").write_text("ok")
    (repo.path / "docs/page.md").write_text(
        "[a](real.md) [b](/docs/real.md) [c](https://example.com/x) [d](#top) [e](real.md#section) "
        "[f](gone.md) [g](/docs/gone.md) [h](mailto:a@b.c)")
    assert broken_links(run, ["docs/page.md"]) == ["docs/page.md -> gone.md", "docs/page.md -> /docs/gone.md"]
