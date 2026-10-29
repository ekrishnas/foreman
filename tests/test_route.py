import pytest

from foreman.workflows.route import classify, gate_classify, parse_type
from tests.conftest import ROOT
from tests.doubles.run import fake_session
from tests.doubles.scenario import Scenario

FEATURE = str(ROOT / "examples/new-implementation-url-shortener.md")
BUG = str(ROOT / "examples/bugfix-click-counts.md")
INPUTS = ROOT / "tests/inputs"

CASES = [
    # (what the developer typed, issue file, repo fixture, expected type)
    ("let's work on #1", FEATURE, "main", "new_implementation"),
    ("let's work on #1", str(INPUTS / "existing-change-expiry.md"), "v1", "existing_change"),
    ("fix #1", BUG, "v1-regressed", "bugfix"),
    ("work on #1", str(INPUTS / "improve-tests.md"), "v1", "test_improvement"),
    ("work on #1", str(INPUTS / "improve-docs.md"), "v1", "doc_improvement"),
    ("let's work on #1", str(INPUTS / "spike-analytics.md"), "v1", "spike"),
    ("issue #1 is deployed to qa, check the logs if it's working", FEATURE, "v1", "observe"),
]


@pytest.mark.parametrize("request_text,issue_file,repo_state,expected", CASES, ids=[c[3] for c in CASES])
def test_the_classifier_picks_the_graph_for_each_kind_of_request(tmp_path, request_text, issue_file, repo_state, expected):
    scenario = Scenario("route", issue_file, request=request_text, start_ref=repo_state)
    session = fake_session(scenario, tmp_path / "runs", run_id="c1")
    session.run.workflow = "route"
    assert classify(session.run) == expected
    assert session.run.workflow == expected


def test_the_decision_is_audited_and_posted_on_the_issue(tmp_path):
    session = fake_session(Scenario("route", FEATURE, request="let's work on #1"), tmp_path / "runs", run_id="c1")
    session.run.workflow = "route"
    classify(session.run)
    event = next(e for e in session.run.store.audit_events("c1") if e.event == "classified")
    assert event.details["type"] == "new_implementation"
    comments = session.run.github.get_issue(1).comments
    assert any("classified this request as **new_implementation**" in c for c in comments)


def test_the_classifier_costs_one_cheap_sandboxed_call(tmp_path):
    session = fake_session(Scenario("route", FEATURE), tmp_path / "runs", run_id="c1")
    session.run.workflow = "route"
    classify(session.run)
    rows = session.run.store.cost_events(run_id="c1")
    assert [(c.agent, c.stage, c.workflow) for c in rows] == [("classifier", "classify", "route")]
    assert (session.run.dir / "sandbox").is_dir()  # it ran outside the repository


def test_parse_type_reads_the_first_type_line_and_normalises_dashes():
    assert parse_type("Type: bugfix\n## Rationale\nx") == "bugfix"
    assert parse_type("type: Test-Improvement\n## Rationale\nx") == "test_improvement"
    assert parse_type("It is a bugfix.") == ""


def answer(kind, rationale="because"):
    return f"Type: {kind}\n## Rationale\n{rationale}\n"


def test_gate_accepts_a_known_type_with_a_rationale(make_run):
    assert gate_classify(make_run({}), {}, answer("new_implementation")) is None


def test_gate_rejects_an_unknown_type_and_a_missing_rationale(make_run):
    run = make_run({})
    assert "one of" in gate_classify(run, {}, answer("feature"))
    assert "Rationale" in gate_classify(run, {}, "Type: spike\n")


def test_gate_refuses_new_implementation_when_the_repo_already_has_code(make_run, repo):
    run = make_run({})
    (repo.path / "src").mkdir()
    (repo.path / "src/old.py").write_text("x = 1\n")
    repo.commit("existing code")
    assert "already has application code" in gate_classify(run, {}, answer("new_implementation"))
    assert gate_classify(run, {}, answer("existing_change")) is None


@pytest.mark.parametrize("kind", ["existing_change", "bugfix", "test_improvement"])
def test_gate_refuses_types_that_change_code_when_there_is_none(make_run, kind):
    assert "no application code" in gate_classify(make_run({}), {}, answer(kind))


@pytest.mark.parametrize("kind", ["spike", "doc_improvement", "observe"])
def test_gate_lets_the_other_types_through_either_way(make_run, kind):
    assert gate_classify(make_run({}), {}, answer(kind)) is None
