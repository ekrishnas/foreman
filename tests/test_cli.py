import json
import re

from foreman.cli import main
from foreman.state.store import AuditEvent, CostEvent, Store
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


# ---- logs: what Foreman did

RUN = "20261029-101500-ab12"


def seed_run(tmp_path, run=RUN):
    store = Store(tmp_path / "foreman.db")
    store.audit(AuditEvent(run, "route", "classify", "stage_passed", 0, "m", 4200))
    store.audit(AuditEvent(run, "bugfix", "classify", "classified", details={"type": "bugfix", "reason": "stack trace in the issue"}))
    store.audit(AuditEvent(run, "bugfix", "reproduce", "attempt_failed", 0, "m", details={"reason": "No failing test, no fix"}))
    store.audit(AuditEvent(run, "bugfix", "reproduce", "stage_passed", 1, "m", 61000, {"commit": "abc123", "findings": []}))
    store.audit(AuditEvent(run, "bugfix", "run", "safe_stop", details={"reason": "budget exceeded"}))
    store.cost(CostEvent(run, 3, "bugfix", "reproduce", "reproducer", 1, "m", 8000, 900, 0.31, 61000))
    return store


def test_logs_without_a_run_lists_every_run_with_its_type_status_and_cost(tmp_path, capsys):
    seed_run(tmp_path)
    store = Store(tmp_path / "foreman.db")
    store.audit(AuditEvent("20261029-110000-cd34", "greenfield", "run", "run_completed"))
    store.audit(AuditEvent("20261029-120000-ef56", "spike", "frame", "stage_passed"))
    code, out = cli(tmp_path, capsys, "logs")
    lines = {line.split()[0]: line for line in out.splitlines()}
    assert code == 0
    assert "bugfix" in lines[RUN] and "safe_stopped" in lines[RUN] and "5 events" in lines[RUN] and "$0.31" in lines[RUN]
    assert "completed" in lines["20261029-110000-cd34"]
    assert "running or interrupted" in lines["20261029-120000-ef56"]


def test_logs_with_no_runs_says_so(tmp_path, capsys):
    assert "no runs recorded yet" in cli(tmp_path, capsys, "logs")[1]


def test_logs_for_a_run_is_a_timeline_without_the_noise(tmp_path, capsys):
    seed_run(tmp_path)
    code, out = cli(tmp_path, capsys, "logs", RUN)
    assert code == 0 and len(out.splitlines()) == 5
    assert "classified" in out and "type=bugfix" in out and "reason=stack trace in the issue" in out
    assert "reason=No failing test, no fix" in out and "reproduce" in out and "#1" in out  # the second attempt is numbered
    assert "abc123" not in out and "findings" not in out  # commit ids and empty lists are left out


def test_logs_problems_shows_only_failures_and_stops(tmp_path, capsys):
    seed_run(tmp_path)
    out = cli(tmp_path, capsys, "logs", RUN, "--problems")[1]
    assert "attempt_failed" in out and "safe_stop" in out and "budget exceeded" in out
    assert "classified" not in out and "stage_passed" not in out


def test_logs_cost_adds_what_each_agent_call_cost(tmp_path, capsys):
    seed_run(tmp_path)
    out = cli(tmp_path, capsys, "logs", RUN, "--cost")[1]
    assert "reproducer" in out and "0.31" in out and "total $0.31" in out


def test_logs_for_an_unknown_run_is_an_error(tmp_path, capsys):
    code, out = cli(tmp_path, capsys, "logs", "nope")
    assert code == 1 and "no events recorded for run nope" in out


# ---- metrics: how reliable a run was

def seed_metrics_run(tmp_path, run=RUN, extra=()):
    store = Store(tmp_path / "foreman.db")

    def at(minute, second=0):
        return f"2026-10-29T12:{minute:02d}:{second:02d}"

    events = [
        ("intake", "stage_passed", 0, {}, at(0)),
        ("implement", "attempt_failed", 0, {"reason": "build failed"}, at(1)),
        ("implement", "retry", 1, {}, at(1)),
        ("implement", "stage_passed", 1, {}, at(3)),
        ("validate", "attempt_failed", 1, {"reason": "tasks not reported done"}, at(4)),
        ("validate", "retry", 1, {}, at(4)),
        ("validate", "stage_passed", 0, {}, at(5)),
        ("approve_proposal", "approval", 0, {"wait_s": 30}, at(5, 30)),
        *extra,
        ("run", "run_completed", 0, {}, at(10)),
    ]
    for stage, event, attempt, details, ts in events:
        store.audit(AuditEvent(run, "new_implementation", stage, event, attempt, details=details, ts=ts))
    store.cost(CostEvent(run, 24, "new_implementation", "implement", "implementer", 0, "m", 1000, 100, 0.30, 1000))
    store.cost(CostEvent(run, 24, "new_implementation", "implement", "implementer", 1, "m", 1000, 100, 0.20, 1000))
    return store


def test_metrics_for_a_run_reports_its_reliability_numbers(tmp_path, capsys):
    seed_metrics_run(tmp_path)
    code, out = cli(tmp_path, capsys, "metrics", RUN)

    def line(label):
        return next(row for row in out.splitlines() if row.startswith(label))

    assert code == 0 and out.splitlines()[0] == f"run {RUN}"
    assert "10m 00s" in line("end-to-end latency") and "30s" in line("human wait")
    assert "0.67" in line("retry rate") and line("rollbacks").split()[-1] == "0"
    assert "1m 30s" in line("mean time to recover")
    assert "$0.50" in line("cost") and "$0.20" in line("cost")
    assert "50%" in line("  implement") and "100%" in line("  intake") and "50%" in line("  validate")


def test_metrics_counts_rollbacks(tmp_path, capsys):
    seed_metrics_run(tmp_path, extra=[("validate", "rollback", 0, {"to": "abc"}, "2026-10-29T12:06:00")])
    out = cli(tmp_path, capsys, "metrics", RUN)[1]
    assert next(row for row in out.splitlines() if row.startswith("rollbacks")).split()[-1] == "1"


def test_metrics_for_an_unknown_run_is_an_error(tmp_path, capsys):
    code, out = cli(tmp_path, capsys, "metrics", "nope")
    assert code == 1 and "no events recorded for run nope" in out
