import pytest

from foreman.core.config import Agent
from foreman.core.context import MissingContext, build_prompt
from foreman.state.store import CostEvent

IMPL = Agent("implementer", "mid", ["Read"], ["src/**"], ["design", "tasks", "last_failure?"])
READONLY = Agent("analyst", "strong", ["Read"], [], ["repo_tree"])


def prompt(run, agent, ctx, failure=None):
    return build_prompt(run, agent, ctx, failure)


def test_prompt_contains_only_the_listed_context_keys(make_run):
    run = make_run({"implementer": IMPL})
    text = prompt(run, IMPL, {"design": "D", "tasks": "T", "unrelated": "LEAKED-SECRET"})
    assert "## design\nD" in text and "## tasks\nT" in text
    assert "LEAKED-SECRET" not in text and "unrelated" not in text


def test_prompt_starts_with_the_agent_prompt_file(make_run):
    run = make_run({"implementer": IMPL})
    assert prompt(run, IMPL, {"design": "D", "tasks": "T"}).startswith("# implementer\nYou are the implementer.")


def test_optional_key_is_omitted_when_absent_and_shown_when_present(make_run):
    run = make_run({"implementer": IMPL})
    assert "last_failure" not in prompt(run, IMPL, {"design": "D", "tasks": "T"})
    assert "## last_failure\nboom" in prompt(run, IMPL, {"design": "D", "tasks": "T", "last_failure": "boom"})


def test_missing_required_key_raises_naming_it(make_run):
    run = make_run({"implementer": IMPL})
    with pytest.raises(MissingContext, match="tasks"):
        prompt(run, IMPL, {"design": "D"})


def test_boundaries_list_writable_paths_branch_and_change(make_run):
    run = make_run({"implementer": IMPL})
    text = prompt(run, IMPL, {"design": "D", "tasks": "T", "change_id": "add-x-1"})
    assert "## Boundaries" in text and "src/**" in text and "foreman/r1" in text and "add-x-1" in text


def test_read_only_agent_is_told_it_cannot_write(make_run):
    run = make_run({"analyst": READONLY})
    assert "read-only" in prompt(run, READONLY, {})


def test_retry_failure_is_fed_back(make_run):
    run = make_run({"implementer": IMPL})
    text = prompt(run, IMPL, {"design": "D", "tasks": "T"}, failure="gate said: no tests")
    assert "Previous attempt failed" in text and "gate said: no tests" in text


def test_repo_tree_provider_fills_missing_key(make_run, repo):
    run = make_run({"analyst": READONLY})
    (repo.path / "src").mkdir()
    (repo.path / "src/a.py").write_text("x")
    repo.commit("a")
    assert "src/a.py" in prompt(run, READONLY, {})


def test_existing_specs_and_target_profile_providers(make_run, repo):
    agent = Agent("framer", "strong", ["Read"], [], ["existing_specs", "target_profile"])
    run = make_run({"framer": agent})
    text = prompt(run, agent, {})
    assert "(none yet)" in text and "python -m pytest -q" in text
    (repo.path / "openspec/specs/shortener").mkdir(parents=True)
    (repo.path / "openspec/specs/shortener/spec.md").write_text("### Requirement: Shorten")
    assert "### Requirement: Shorten" in prompt(run, agent, {})


def test_cost_table_provider_uses_run_and_issue_costs(make_run):
    agent = Agent("pr_summarizer", "small", ["Read"], [], ["cost_table"])
    run = make_run({"pr_summarizer": agent})
    run.store.cost(CostEvent("r1", 1, "feature", "x", "implementer", 0, "m", 1, 1, 2.0, 1))
    run.store.cost(CostEvent("old", 1, "feature", "x", "implementer", 0, "m", 1, 1, 3.0, 1))
    text = prompt(run, agent, {})
    assert "$2.00" in text and "$5.00" in text


def test_boundaries_name_the_issue_number(make_run):
    run = make_run({"implementer": IMPL})
    text = prompt(run, IMPL, {"design": "D", "tasks": "T", "issue_number": 42})
    assert "Issue number: 42" in text
