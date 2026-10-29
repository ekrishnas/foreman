from pathlib import Path

import pytest

from foreman.core.config import load_agents
from foreman.core.runner import SafeStop
from foreman.workflows.new_implementation import gate_requirements
from foreman.workflows.nodes import (
    gate_observability,
    gate_wrote,
    parse_bullets,
    tick_tasks,
    validate_feature_checks,
)

AGENTS, _ = load_agents(Path(__file__).resolve().parent.parent / "config/agents.yaml")

GOOD_REQ = """## Normalized requirement
Shorten URLs.
## Acceptance criteria
- returns a code
## Impacted files
- src/shortener.py
## Blocking questions
- Which database?
- Expiry?
"""


def test_parse_bullets_returns_each_bullet_text():
    assert parse_bullets("- one\n* two\n\n  - three\nnot a bullet") == ["one", "two", "three"]


def test_requirements_gate_accepts_complete_output(make_run):
    run = make_run({})
    assert gate_requirements(run, {}, GOOD_REQ) is None


def test_requirements_gate_names_missing_sections(make_run):
    run = make_run({})
    assert "Acceptance criteria" in gate_requirements(run, {}, "## Normalized requirement\nx")


def test_requirements_gate_does_not_need_impacted_files(make_run):
    run = make_run({})
    assert gate_requirements(run, {}, "## Normalized requirement\nx\n## Acceptance criteria\n- y\n") is None


GOOD_NOTES = """Done.
## Observability
- Log location: logs/app.log
- Success: `INFO shortened url`
- Errors: `ERROR`
"""


def test_observability_gate_accepts_a_complete_section(make_run):
    assert gate_observability(make_run({}), {}, GOOD_NOTES) is None


def test_observability_gate_names_what_is_missing(make_run):
    msg = gate_observability(make_run({}), {}, "Done.\n## Observability\n- Log location: logs/app.log\n")
    assert "Success" in msg and "Error" in msg and "Log location" not in msg.split("missing:")[1]


def test_observability_gate_demands_the_section(make_run):
    assert "## Observability" in gate_observability(make_run({}), {}, "Implemented it.\nDONE: 1.1")


def test_gate_wrote_counts_files_already_committed_on_the_branch(make_run, repo):
    run = make_run({})
    base = repo.head()
    (repo.path / "tests").mkdir()
    (repo.path / "tests/test_a.py").write_text("x")
    repo.commit("earlier attempt")
    assert gate_wrote("tests")(run, {"base_commit": base}, "") is None  # nothing new now, but the branch has the file


def test_gate_wrote_fails_when_nothing_under_that_path_exists(make_run, repo):
    run = make_run({})
    (repo.path / "src").mkdir()
    (repo.path / "src/a.py").write_text("x")
    assert "no files under tests/**" in gate_wrote("tests")(run, {"base_commit": repo.head()}, "")


def write_tasks(repo, text):
    p = repo.path / "openspec/changes/c-1/tasks.md"
    p.parent.mkdir(parents=True)
    p.write_text(text)
    return p


def test_tick_tasks_checks_only_reported_ids(make_run, repo):
    run = make_run({})
    p = write_tasks(repo, "- [ ] 1.1 impl\n- [ ] 2.1 tests\n")
    tick_tasks(run, {"change_id": "c-1"}, "all done\nDONE: 1.1\n")
    assert p.read_text() == "- [x] 1.1 impl\n- [ ] 2.1 tests\n"


def test_tick_tasks_ignores_unknown_ids_and_missing_file(make_run, repo):
    run = make_run({})
    tick_tasks(run, {"change_id": "nope"}, "DONE: 9.9")
    p = write_tasks(repo, "- [ ] 1.1 impl\n")
    tick_tasks(run, {"change_id": "c-1"}, "DONE: 9.9")
    assert p.read_text() == "- [ ] 1.1 impl\n"


def make_valid_change(repo):
    base = repo.path / "openspec/changes/c-1"
    (base / "specs/shortener").mkdir(parents=True)
    (base / "proposal.md").write_text("## Why\nx")
    (base / "design.md").write_text("## API contract\nPOST /shorten")
    (base / "tasks.md").write_text("- [x] 1.1 impl\n")
    (base / "specs/shortener/spec.md").write_text(
        "## ADDED Requirements\n### Requirement: A\nThe system SHALL a.\n\n#### Scenario: s\n- **WHEN** x\n- **THEN** y\n")
    (repo.path / "src").mkdir()
    (repo.path / "src/shortener.py").write_text("x = 1\n")
    (repo.path / "tests").mkdir()
    (repo.path / "tests/test_a.py").write_text("def test_a():\n    assert True\n")


def test_validate_checks_pass_on_a_healthy_branch(make_run, repo):
    run = make_run(AGENTS)
    make_valid_change(repo)
    repo.commit("work")
    ctx = {"change_id": "c-1", "base_commit": repo.git("rev-parse", "HEAD~1").strip()}
    failures, results = validate_feature_checks(run, ctx)
    assert failures == [] and "tests: 1 passed" in results and "tasks: 1/1" in results


def test_validate_reports_each_problem(make_run, repo):
    run = make_run(AGENTS)
    make_valid_change(repo)
    (repo.path / "openspec/changes/c-1/tasks.md").write_text("- [x] 1.1 a\n- [ ] 2.1 b\n")
    (repo.path / "tests/test_a.py").write_text("def test_a():\n    assert False\n")
    repo.commit("work")
    ctx = {"change_id": "c-1", "base_commit": repo.git("rev-parse", "HEAD~1").strip()}
    failures, _ = validate_feature_checks(run, ctx)
    text = "\n".join(failures)
    assert "tests failed" in text and "unchecked tasks: 2.1" in text


def test_validate_flags_a_secret_anywhere_on_the_branch(make_run, repo):
    run = make_run(AGENTS)
    make_valid_change(repo)
    (repo.path / "src/shortener.py").write_text('KEY = "AKIAABCDEFGHIJKLMNOP"\n')
    repo.commit("work")
    ctx = {"change_id": "c-1", "base_commit": repo.git("rev-parse", "HEAD~1").strip()}
    failures, _ = validate_feature_checks(run, ctx)
    assert any("secret" in f for f in failures)


def test_safe_stop_carries_rollback_flag_and_stage():
    s = SafeStop("boom", rollback=True, stage="validate")
    assert (s.reason, s.rollback, s.stage) == ("boom", True, "validate")
    with pytest.raises(SafeStop):
        raise s


def test_validate_enforces_a_minimum_test_count(make_run, repo):
    run = make_run(AGENTS)
    make_valid_change(repo)
    repo.commit("work")
    ctx = {"change_id": "c-1", "base_commit": repo.git("rev-parse", "HEAD~1").strip()}
    failures, results = validate_feature_checks(run, ctx, min_tests=3)
    assert any("test count" in f for f in failures) and "tests: 1 passed (need 3)" in results
