import json

from foreman.core.util import count_tests, sh
from tests.conftest import ROOT
from tests.doubles.run import fake_session
from tests.doubles.scenario import Scenario

INPUT = str(ROOT / "examples/bugfix-click-counts.md")
STAGES = ["classify", "intake", "triage", "reproduce", "fix", "validate", "rca", "archive_spec", "pr_summary",
          "open_pr", "await_merge"]


def scenario(**kw):
    return Scenario(**{"name": "bugfix", "input": INPUT, "request": "fix #1",
                       "start_ref": "v1-regressed", **kw})


def session_for(tmp_path, **kw):
    return fake_session(scenario(**kw), tmp_path / "runs", run_id="b1")


def events(session, name=None, stage=None):
    return [e for e in session.run.store.audit_events("b1")
            if (name is None or e.event == name) and (stage is None or e.stage == stage)]


def test_brownfield_bugfix_runs_end_to_end(tmp_path):
    s = session_for(tmp_path)
    result = s.start()
    assert result.status == "completed", result.reason
    assert s.run.github.pr_state(result.pr).state == "merged"
    assert set(STAGES) <= {e.stage for e in events(s, "stage_passed")}


def test_the_new_test_failed_before_the_fix_and_passes_after(tmp_path):
    s = session_for(tmp_path)
    s.start()
    ctx = s.ctx()
    assert "failed" in ctx["failure_output"] and "test_click_counts" in ctx["failing_test"]
    final = sh(s.run.target.commands["test"], cwd=s.run.repo.path)
    assert final.code == 0 and count_tests(final.out) == ctx["baseline_passed"] + 1


def test_reproduce_is_committed_before_fix(tmp_path):
    s = session_for(tmp_path)
    s.start()
    log = s.run.repo.git("log", "--reverse", "--format=%s", "--first-parent", "foreman/b1").splitlines()
    assert log.index("foreman: reproduce") < log.index("foreman: fix") < log.index("foreman: rca")


def test_rca_document_has_every_required_section_and_pr_links_it(tmp_path):
    s = session_for(tmp_path)
    result = s.start()
    rca = (s.run.repo.path / "docs/rca/1.md").read_text()
    for heading in ("Summary", "Impact", "Timeline", "Root cause", "Why it was missed", "Fix", "Prevention"):
        assert f"## {heading}" in rca
    body = json.loads((s.run.dir / "github/prs" / f"{result.pr}.json").read_text())["body"]
    assert "## Root cause analysis" in body and "docs/rca/1.md" in body and "Closes #1" in body


def test_living_spec_requirement_is_modified_by_archive(tmp_path):
    s = session_for(tmp_path)
    s.start()
    spec = (s.run.repo.path / "openspec/specs/shortener/spec.md").read_text()
    assert spec.count("### Requirement: Count clicks") == 1 and "stale" in spec


def test_a_reproduction_test_that_passes_is_rejected_then_retried(tmp_path):
    s = session_for(tmp_path, faults=[{"stage": "reproduce", "calls": [0], "kind": "passing_test"}])
    result = s.start()
    assert result.status == "completed", result.reason
    reasons = [e.details["reason"] for e in events(s, "attempt_failed", "reproduce")]
    assert len(reasons) == 1 and "No failing test, no fix" in reasons[0]


def test_no_failing_test_means_no_fix_attempt_is_ever_made(tmp_path):
    s = session_for(tmp_path, faults=[{"stage": "reproduce", "all": True, "kind": "passing_test"}])
    result = s.start()
    assert result.status == "safe_stopped" and "No failing test, no fix" in result.reason
    assert "fixer" not in {c.agent for c in s.run.store.cost_events(run_id="b1")}


def test_fixer_cannot_delete_the_failing_test(tmp_path):
    s = session_for(tmp_path, faults=[{"stage": "fix", "calls": [0], "kind": "delete_test"}])
    result = s.start()
    assert result.status == "completed", result.reason
    reasons = [e.details["reason"] for e in events(s, "attempt_failed", "fix")]
    assert len(reasons) == 1 and reasons[0].startswith("policy:")
    assert (s.run.repo.path / "tests/test_click_counts.py").exists()


def test_a_fix_that_does_not_fix_fails_validation_and_loops_back(tmp_path):
    s = session_for(tmp_path, faults=[{"stage": "fix", "calls": [0], "kind": "break_tests"}])
    result = s.start()
    assert result.status == "completed", result.reason
    assert len(events(s, "attempt_failed", "validate")) == 1
    assert sum(1 for c in s.run.store.cost_events(run_id="b1") if c.stage == "fix") == 2


def test_triage_rejection_is_replanned(tmp_path):
    s = session_for(tmp_path, script={"proposal": ["reject: also cover stats endpoint", "approve"]})
    result = s.start()
    assert result.status == "completed", result.reason
    assert "stats endpoint" in s.ctx()["triage"]
    assert [e.details["approved"] for e in events(s, "approval", "approve_proposal")] == [False, True]


def test_baseline_suite_is_recorded_at_intake(tmp_path):
    s = session_for(tmp_path)
    s.start()
    assert s.ctx()["baseline_passed"] == 4 and s.ctx()["baseline_total"] == 4


# ---- the triage gate before and after a product has specs

ADDED = ("## ADDED Requirements\n### Requirement: Count clicks\nThe system SHALL count clicks.\n\n"
         "#### Scenario: Resolve twice\n- **WHEN** x\n- **THEN** y\n")
MODIFIED = ADDED.replace("ADDED", "MODIFIED")


def triage_change(repo, spec_text):
    base = repo.path / "openspec/changes/c-1"
    for rel, text in {"proposal.md": "p", "design.md": "d", "tasks.md": "t", "specs/shortener/spec.md": spec_text}.items():
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / rel).write_text(text, encoding="utf-8")


def test_triage_on_a_product_with_no_specs_must_record_the_behaviour_as_added(make_run, repo):
    from foreman.workflows.bugfix import gate_triage
    run = make_run({})
    triage_change(repo, ADDED)
    assert gate_triage(run, {"change_id": "c-1"}, "") is None
    triage_change(repo, MODIFIED)
    assert "no specs yet" in gate_triage(run, {"change_id": "c-1"}, "")


def test_triage_on_a_product_with_specs_must_modify_one_of_them(make_run, repo):
    from foreman.workflows.bugfix import gate_triage
    run = make_run({})
    (repo.path / "openspec/specs/shortener").mkdir(parents=True)
    (repo.path / "openspec/specs/shortener/spec.md").write_text(MODIFIED.replace("MODIFIED", "x").replace("## x Requirements\n", "## Requirements\n"), encoding="utf-8")
    triage_change(repo, ADDED)
    assert "must modify an existing requirement" in gate_triage(run, {"change_id": "c-1"}, "")
