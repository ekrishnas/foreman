from foreman.workflows.existing_change import gate_impact, listed_paths
from tests.conftest import ROOT
from tests.doubles.run import fake_session
from tests.doubles.scenario import Scenario

INPUT = str(ROOT / "tests/inputs/existing-change-expiry.md")
GOOD = ("## Impacted files\n- `src/shortener.py`: resolve needs an age check\n## Modules and APIs\n- Shortener\n"
        "## Data flows\n- resolve reads the creation time\n## Risks\n- none\n")


def session_for(tmp_path, **kw):
    scenario = Scenario(**{"name": "existing_change", "input": INPUT, "request": "let's work on #1",
                           "start_ref": "v1", **kw})
    return fake_session(scenario, tmp_path / "runs", run_id="e1")


def events(session, name=None, stage=None):
    return [e for e in session.run.store.audit_events("e1")
            if (name is None or e.event == name) and (stage is None or e.stage == stage)]


def test_a_change_to_existing_code_runs_end_to_end_with_an_impact_analysis_first(tmp_path):
    s = session_for(tmp_path)
    result = s.start()
    assert result.status == "completed", result.reason
    assert s.run.workflow == "existing_change"
    stages = [e.stage for e in events(s, "stage_passed")]
    assert stages.index("impact") < stages.index("requirements") < stages.index("proposal")
    assert s.run.github.pr_state(result.pr).state == "merged"


def test_the_impacted_files_reach_the_architect(tmp_path):
    s = session_for(tmp_path)
    s.start()
    assert "src/shortener.py" in s.ctx()["impacted_files"]
    assert "Data flows" in s.ctx()["impact"]


def test_an_impact_analysis_that_names_a_missing_file_is_rejected_then_retried(tmp_path):
    s = session_for(tmp_path, faults=[{"stage": "impact", "calls": [0], "kind": "missing_file"}])
    assert s.start().status == "completed"
    reasons = [e.details["reason"] for e in events(s, "attempt_failed", "impact")]
    assert len(reasons) == 1 and "src/nope.py" in reasons[0]


def test_the_change_builds_on_the_existing_code_instead_of_replacing_it(tmp_path):
    s = session_for(tmp_path)
    s.start()
    repo = s.run.repo
    assert "docs/api.md" in repo.git("ls-files") and "tests/test_change.py" in repo.git("ls-files")
    assert "changed for this issue" in (repo.path / "src/shortener.py").read_text(encoding="utf-8")


def test_gate_impact_accepts_a_complete_analysis_of_real_files(make_run, repo):
    run = make_run({})
    (repo.path / "src").mkdir()
    (repo.path / "src/shortener.py").write_text("x = 1\n")
    assert gate_impact(run, {}, GOOD) is None


def test_gate_impact_names_missing_sections_and_files(make_run, repo):
    run = make_run({})
    assert "Data flows" in gate_impact(run, {}, "## Impacted files\n- a.py\n## Modules and APIs\n- x\n")
    assert "src/shortener.py" in gate_impact(run, {}, GOOD)  # the file does not exist in this empty repo
    no_bullets = GOOD.replace("- `src/shortener.py`: resolve needs an age check", "the resolve function")
    assert "at least one" in gate_impact(run, {}, no_bullets)
    assert "missing sections: Impacted files" in gate_impact(run, {}, GOOD.replace("- `src/shortener.py`: resolve needs an age check", ""))


def test_listed_paths_strips_backticks_and_trailing_notes():
    assert listed_paths("## Impacted files\n- `src/a.py`: why\n- src/b.py - also\n* docs/c.md\n") == [
        "src/a.py", "src/b.py", "docs/c.md"]
