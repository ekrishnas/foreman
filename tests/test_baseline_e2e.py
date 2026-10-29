from foreman.workflows.baseline import baseline_id, gate_baseline, needs_baseline
from tests.conftest import ROOT
from tests.doubles.run import fake_session
from tests.doubles.scenario import Scenario

EXPIRY = str(ROOT / "tests/inputs/existing-change-expiry.md")
BUG = str(ROOT / "examples/bugfix-click-counts.md")


def session_for(tmp_path, input_=EXPIRY, start_ref="v1-nospec", **kw):
    scenario = Scenario(**{"name": "baseline", "input": input_, "request": "work on #1", "start_ref": start_ref, **kw})
    return fake_session(scenario, tmp_path / "runs", run_id="b1")


def events(s, name=None, stage=None):
    return [e for e in s.run.store.audit_events("b1")
            if (name is None or e.event == name) and (stage is None or e.stage == stage)]


def passed(s):
    return [e.stage for e in events(s, "stage_passed")]


def test_a_change_to_a_product_with_no_specs_starts_with_a_baseline_that_is_archived_first(tmp_path):
    s = session_for(tmp_path)
    result = s.start()
    assert result.status == "completed", result.reason
    stages = passed(s)
    assert stages.index("baseline") < stages.index("archive_baseline") < stages.index("impact") < stages.index("requirements")
    log = s.run.repo.git("log", "--format=%s", "main").splitlines()
    assert "foreman: baseline" in log and "foreman: archive_baseline" in log


def test_the_baseline_leaves_living_specs_and_an_archived_change_behind(tmp_path):
    s = session_for(tmp_path)
    s.start()
    spec = (s.run.repo.path / "openspec/specs/shortener/spec.md").read_text(encoding="utf-8")
    assert "Count clicks" in spec and "Shorten URL" in spec
    archived = [p.name for p in (s.run.repo.path / "openspec/changes/archive").iterdir()]
    assert any("baseline-" in name for name in archived)


def test_a_product_that_already_has_specs_gets_no_baseline(tmp_path):
    s = session_for(tmp_path, start_ref="v1")
    assert s.start().status == "completed"
    assert "baseline" not in passed(s) and "archive_baseline" not in passed(s)
    assert not any(c.agent == "baseliner" for c in s.run.store.cost_events(run_id="b1"))


def test_a_bugfix_on_a_product_with_no_specs_gets_a_baseline_so_triage_has_something_to_modify(tmp_path):
    s = session_for(tmp_path, input_=BUG, start_ref="v1-regressed-nospec", request="fix #1")
    result = s.start()
    assert result.status == "completed", result.reason
    assert s.run.workflow == "bugfix"
    stages = passed(s)
    assert stages.index("archive_baseline") < stages.index("triage")


def test_the_baseline_is_kept_when_a_proposal_is_rejected_and_redone(tmp_path):
    s = session_for(tmp_path, script={"proposal": ["reject: add expiry to the docs too", "approve"]})
    result = s.start()
    assert result.status == "completed", result.reason
    assert passed(s).count("baseline") == 1  # not run again after the reset
    assert needs_baseline(s.run) is False


def test_a_baseline_that_modifies_instead_of_adding_is_rejected_then_retried(tmp_path):
    s = session_for(tmp_path, faults=[{"stage": "baseline", "calls": [0], "kind": "modified"}])
    assert s.start().status == "completed"
    reasons = [e.details["reason"] for e in events(s, "attempt_failed", "baseline")]
    assert len(reasons) == 1 and ("ADDED" in reasons[0] or "MODIFIED" in reasons[0])


def test_needs_baseline_is_about_specs_not_code(make_run, repo):
    run = make_run({})
    assert needs_baseline(run) is True
    (repo.path / "openspec/specs/x").mkdir(parents=True)
    (repo.path / "openspec/specs/x/spec.md").write_text("# x\n")
    assert needs_baseline(run) is False


def baseline_files(repo, spec_text, change="baseline-c-1"):
    base = repo.path / "openspec/changes" / change
    for rel, text in {"proposal.md": "p", "design.md": "d", "tasks.md": "t", "specs/x/spec.md": spec_text}.items():
        (base / rel).parent.mkdir(parents=True, exist_ok=True)
        (base / rel).write_text(text, encoding="utf-8")


REQ = "### Requirement: A thing\nThe system SHALL do a thing.\n\n#### Scenario: It does\n- **WHEN** x\n- **THEN** y\n"


def test_gate_baseline_accepts_added_requirements(make_run, repo):
    run = make_run({})
    baseline_files(repo, "## ADDED Requirements\n" + REQ)
    assert gate_baseline(run, {"change_id": "c-1"}, "") is None
    assert baseline_id({"change_id": "c-1"}) == "baseline-c-1"


def test_gate_baseline_refuses_modified_and_removed_blocks(make_run, repo):
    run = make_run({})
    baseline_files(repo, "## ADDED Requirements\n" + REQ + "\n## MODIFIED Requirements\n" + REQ)
    assert "only contain" in gate_baseline(run, {"change_id": "c-1"}, "")
    baseline_files(repo, "## REMOVED Requirements\n### Requirement: A thing\n")
    assert gate_baseline(run, {"change_id": "c-1"}, "") is not None
