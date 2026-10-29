from dataclasses import replace

import pytest

from foreman.config import load_scenario
from foreman.engine import check_expect, open_session
from tests.conftest import ROOT

FAULT_SCENARIOS = sorted((ROOT / "scenarios/faults").glob("*.yaml"))


def run_scenario(name, tmp_path):
    scenario = load_scenario(ROOT / f"scenarios/faults/{name}.yaml")
    session = open_session(scenario, "fake", tmp_path / "runs", run_id="f1")
    return session, session.start()


def events(session, name=None, stage=None):
    return [e for e in session.run.store.audit_events("f1")
            if (name is None or e.event == name) and (stage is None or e.stage == stage)]


@pytest.mark.parametrize("path", FAULT_SCENARIOS, ids=lambda p: p.stem)
def test_every_fault_scenario_meets_its_own_expectations(path, tmp_path):
    session = open_session(load_scenario(path), "fake", tmp_path / "runs", run_id="f1")
    result = session.start()
    assert check_expect(session, result) == []


def test_there_is_a_scenario_for_each_fault_class_in_the_design():
    assert {p.stem for p in FAULT_SCENARIOS} >= {
        "retry-then-success", "fallback", "policy-block", "budget-stop", "rollback"}


def test_retry_then_success_loops_back_to_implement_and_skips_unchanged_stages(tmp_path):
    session, result = run_scenario("retry-then-success", tmp_path)
    assert result.status == "completed"
    implement_rows = [c for c in session.run.store.cost_events(run_id="f1") if c.stage == "implement"]
    tests_rows = [c for c in session.run.store.cost_events(run_id="f1") if c.stage == "tests"]
    assert len(implement_rows) == 2 and len(tests_rows) == 1
    assert [e.stage for e in events(session, "stage_skipped")] == ["tests", "docs"]
    assert session.run.store.cost_events(run_id="f1")[0].attempt == 0


def test_retry_cost_is_visible_separately_in_the_pr_body(tmp_path):
    session, result = run_scenario("retry-then-success", tmp_path)
    body = session.run.github._read("prs", result.pr)["body"]
    assert "retries and re-runs: $0.06" in body


def test_fallback_escalates_to_the_strong_model_exactly_once(tmp_path):
    session, result = run_scenario("fallback", tmp_path)
    rows = [c for c in session.run.store.cost_events(run_id="f1") if c.stage == "tests"]
    assert [c.model for c in rows] == ["claude-sonnet-5-5"] * 3 + ["claude-opus-5-5"]
    assert len(events(session, "fallback")) == 1


def test_policy_block_stops_with_a_clean_tree_and_names_the_path(tmp_path):
    session, result = run_scenario("policy-block", tmp_path)
    assert result.status == "safe_stopped" and "outside" in result.reason
    assert session.run.repo.changes() == []
    assert (session.run.repo.path / "FOREMAN_BAD_PATH.txt").exists() is False


def test_secret_in_agent_output_is_rejected_then_retried(tmp_path):
    session, result = run_scenario("secret-block", tmp_path)
    reasons = [e.details["reason"] for e in events(session, "attempt_failed", "implement")]
    assert len(reasons) == 1 and "secret" in reasons[0]
    assert "AKIA" not in (session.run.repo.path / "src/shortener.py").read_text()


def test_budget_stop_happens_before_the_next_agent_call(tmp_path):
    session, result = run_scenario("budget-stop", tmp_path)
    assert result.status == "safe_stopped" and "budget" in result.reason
    assert {c.agent for c in session.run.store.cost_events(run_id="f1")} == {"requirements_analyst"}


def test_rollback_resets_the_branch_to_the_approved_commit(tmp_path):
    session, result = run_scenario("rollback", tmp_path)
    approved = session.ctx()["approved_commit"]
    assert session.run.repo.head() == approved
    assert not (session.run.repo.path / "src/shortener.py").exists()
    assert len(events(session, "rollback")) == 1


def test_resume_after_budget_stop_reruns_only_the_failed_stage(tmp_path):
    session, result = run_scenario("budget-stop", tmp_path)
    assert result.status == "safe_stopped"
    session.run.scenario = replace(session.run.scenario, budget_usd=10.0)
    resumed = session.resume()
    assert resumed.status == "completed", resumed.reason
    rows = session.run.store.cost_events(run_id="f1")
    assert sum(1 for c in rows if c.agent == "requirements_analyst") == 1
    assert len(events(session, "stage_passed", "requirements")) == 1
    assert session.run.repo.git("rev-list", "--count", "--merges", "main").strip() == "1"


def test_resume_after_a_parallel_branch_fails_does_not_redo_the_others(tmp_path):
    scenario = replace(load_scenario(ROOT / "scenarios/faults/fallback.yaml"),
                       faults=[{"stage": "tests", "all": True, "kind": "no_output"}])
    session = open_session(scenario, "fake", tmp_path / "runs", run_id="f1")
    assert session.start().status == "safe_stopped"
    session.run.worker.faults = []
    assert session.resume().status == "completed"
    rows = session.run.store.cost_events(run_id="f1")
    assert sum(1 for c in rows if c.agent == "implementer") == 1
    assert sum(1 for c in rows if c.agent == "doc_writer") == 1
    commits = session.run.repo.git("log", "--format=%s", "main").splitlines()
    assert commits.count("feat: implement the change") == 1 and commits.count("docs: document the change") == 1


def test_failed_validation_is_reported_on_the_issue_thread(tmp_path):
    session, _ = run_scenario("retry-then-success", tmp_path)
    comments = session.run.github._read("issues", 1)["comments"]
    assert any("Validation failed" in c for c in comments) and any("Validation passed" in c for c in comments)
