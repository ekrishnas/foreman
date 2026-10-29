import json
import re

from foreman.cli import main
from foreman.state.store import AuditEvent, Store
from tests.conftest import ROOT
from tests.doubles.run import fake_factory
from tests.doubles.scenario import load_scenario

SCENARIO = ROOT / "tests/scenarios/new_implementation.yaml"
BUDGET_STOP = ROOT / "tests/scenarios/faults/budget-stop.yaml"


def run_cli(tmp_path, capsys, scenario, *args):
    code = main(["run", "1", "--request", scenario.request, "--budget", str(scenario.budget_usd),
                 "--runs-dir", str(tmp_path), *args], factory=fake_factory(scenario))
    return code, capsys.readouterr().out


def cli(tmp_path, capsys, *args, scenario=None):
    code = main([*args, "--runs-dir", str(tmp_path)], factory=fake_factory(scenario or load_scenario(SCENARIO)))
    return code, capsys.readouterr().out


def run_id_of(out):
    return re.search(r"^run: (\S+)", out, re.M).group(1)


def test_run_prints_the_run_id_first_then_type_status_pr_and_cost(tmp_path, capsys):
    code, out = run_cli(tmp_path, capsys, load_scenario(SCENARIO))
    assert code == 0, out
    assert out.splitlines()[0].startswith("run: ")
    assert "type: new_implementation" in out and "status: completed" in out
    assert "PR: #2" in out and "cost: $0.62" in out  # classifier, six agents, and the reviewer


def test_safe_stop_exits_two_with_the_reason_and_how_to_resume(tmp_path, capsys):
    code, out = run_cli(tmp_path, capsys, load_scenario(BUDGET_STOP))
    assert code == 2 and "status: safe_stopped" in out and "budget" in out
    assert f"foreman resume {run_id_of(out)}" in out


def test_resume_continues_a_safe_stopped_run_from_its_checkpoint(tmp_path, capsys):
    scenario = load_scenario(BUDGET_STOP)
    _, out = run_cli(tmp_path, capsys, scenario)
    run_id = run_id_of(out)
    meta_path = tmp_path / run_id / "run.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["config"]["budget_usd"] = 10.0  # the developer raised the budget
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    code = main(["resume", run_id, "--runs-dir", str(tmp_path)], factory=fake_factory(scenario))
    out = capsys.readouterr().out
    assert code == 0 and "status: completed" in out and "type: new_implementation" in out


def test_a_real_run_checks_its_tools_first(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr("shutil.which", lambda tool: None)
    code = main(["run", "1", "--runs-dir", str(tmp_path)])  # default factory: the real one
    assert code == 1 and "missing on PATH" in capsys.readouterr().out


# ---- pending / respond: how Claude Code and the running orchestrator talk

def test_pending_lists_open_questions_with_the_command_that_answers_them(tmp_path, capsys):
    store = Store(tmp_path / "foreman.db")
    store.ask("r1", "proposal", "Change c-1: add the shortener")
    store.ask("r1", "question", "Which database?")
    code, out = cli(tmp_path, capsys, "pending")
    assert code == 0
    assert "request 1 (run r1, proposal)" in out and "foreman respond 1 --approve | --reject" in out
    assert "Change c-1: add the shortener" in out
    assert "foreman respond 2 --answer TEXT" in out


def test_pending_says_so_when_nothing_is_waiting(tmp_path, capsys):
    _, out = cli(tmp_path, capsys, "pending")
    assert "no pending requests" in out


def test_pending_wait_returns_early_when_the_run_has_finished(tmp_path, capsys):
    Store(tmp_path / "foreman.db").audit(AuditEvent("r1", "bugfix", "run", "run_completed"))
    _, out = cli(tmp_path, capsys, "pending", "--run", "r1", "--wait", "30")
    assert "run r1 has finished" in out


def test_respond_approve_and_reject_with_a_note(tmp_path, capsys):
    store = Store(tmp_path / "foreman.db")
    a, b = store.ask("r1", "proposal", "p"), store.ask("r1", "high_impact", "h")
    assert cli(tmp_path, capsys, "respond", str(a), "--approve", "--who", "dev")[0] == 0
    assert cli(tmp_path, capsys, "respond", str(b), "--reject", "--note", "no new dependencies")[0] == 0
    assert store.get_request(a)["approved"] == 1 and store.get_request(a)["who"] == "dev"
    assert store.get_request(b)["approved"] == 0 and store.get_request(b)["note"] == "no new dependencies"


def test_respond_to_a_question_needs_an_answer_and_a_blank_one_is_allowed(tmp_path, capsys):
    store = Store(tmp_path / "foreman.db")
    q = store.ask("r1", "question", "Which database?")
    code, out = cli(tmp_path, capsys, "respond", str(q), "--approve")
    assert code == 1 and "--answer" in out
    assert cli(tmp_path, capsys, "respond", str(q), "--answer", "")[0] == 0
    assert store.get_request(q)["status"] == "answered"


def test_respond_needs_exactly_one_of_approve_or_reject(tmp_path, capsys):
    store = Store(tmp_path / "foreman.db")
    p = store.ask("r1", "proposal", "p")
    assert "exactly one" in cli(tmp_path, capsys, "respond", str(p))[1]
    assert "exactly one" in cli(tmp_path, capsys, "respond", str(p), "--approve", "--reject")[1]


def test_respond_to_an_unknown_or_already_answered_request_is_an_error(tmp_path, capsys):
    store = Store(tmp_path / "foreman.db")
    p = store.ask("r1", "proposal", "p")
    assert cli(tmp_path, capsys, "respond", "99", "--approve")[0] == 1
    cli(tmp_path, capsys, "respond", str(p), "--approve")
    code, out = cli(tmp_path, capsys, "respond", str(p), "--reject")
    assert code == 1 and "already answered" in out


def test_every_agent_has_a_prompt_that_states_its_role():
    from foreman.core.config import load_agents
    agents, _ = load_agents(ROOT / "config/agents.yaml")
    for name in agents:
        text = (ROOT / "prompts" / f"{name}.md").read_text(encoding="utf-8")
        assert text.startswith("# Role:") and "## Inputs" in text, name


def test_the_example_issues_exist_and_the_target_profile_matches_the_real_repo():
    from foreman.core.config import load_target
    assert (ROOT / "examples/new-implementation-url-shortener.md").is_file()
    assert (ROOT / "examples/bugfix-click-counts.md").is_file()
    profile = load_target(ROOT / "examples/target.yaml", repo=ROOT / "my-app")
    assert profile.name == "my-app" and profile.commands["test"] and profile.paths["src"]
