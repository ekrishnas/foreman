import json
from dataclasses import replace

import pytest

from foreman.config import Scenario
from foreman.engine import RunRefused, ensure_ready, open_session
from foreman.fake import BEHAVIORS
from foreman.github import FakeGitHub
from foreman.observe import run_smoke, verdict
from tests.conftest import ROOT

SLO = {"error_rate_max": 0.01, "p95_ms_max": 200}
STORY = str(ROOT / "scenarios/inputs/feature-url-shortener.md")


def metrics(requests=100, errors=0, p95_ms=50):
    return {"requests": requests, "errors": errors, "p95_ms": p95_ms, "log": ""}


# ---- verdict is pure code, boundaries inclusive ----

@pytest.mark.parametrize("m,ok", [
    (metrics(errors=1), True),
    (metrics(errors=2), False),
    (metrics(p95_ms=200), True),
    (metrics(p95_ms=201), False),
    (metrics(requests=0), False),
])
def test_verdict_boundaries(m, ok):
    assert verdict(m, SLO)[0] is ok


def test_verdict_names_every_breach():
    passed, reason = verdict(metrics(errors=5, p95_ms=500), SLO)
    assert not passed and "error rate" in reason and "p95" in reason


# ---- the deterministic smoke run ----

def with_commands(target_run, **commands):
    target_run.target = replace(target_run.target, commands={**target_run.target.commands, **commands})
    target_run.check_health = False
    return target_run


def test_run_smoke_reads_the_last_json_line(make_run):
    run = with_commands(make_run({}), run="true", stop="true",
                        smoke="echo noise; echo '{\"requests\": 10, \"errors\": 1, \"p95_ms\": 20, \"log\": \"hi\"}'")
    assert run_smoke(run) == {"requests": 10, "errors": 1, "p95_ms": 20, "log": "hi"}


def test_run_smoke_always_stops_the_deployment(make_run):
    run = with_commands(make_run({}), run="false", stop="touch stopped.marker")
    assert run_smoke(run)["errors"] > 0
    assert (run.repo.path / "stopped.marker").exists()


def test_a_failed_deploy_is_a_failing_observation_not_a_crash(make_run):
    run = with_commands(make_run({}), run="echo boom; exit 1", stop="true")
    m = run_smoke(run)
    assert m["requests"] == 0 and "deploy failed" in m["log"]


def test_smoke_output_that_is_not_json_is_a_failing_observation(make_run):
    run = with_commands(make_run({}), run="true", stop="true", smoke="echo not json")
    m = run_smoke(run)
    assert m["requests"] == 0 and "did not print JSON" in m["log"]


def test_deploy_env_reaches_the_smoke_command(make_run):
    run = with_commands(make_run({}), run="true", stop="true", smoke="echo \"{\\\"requests\\\": 1, \\\"errors\\\": 0, \\\"p95_ms\\\": $P95, \\\"log\\\": \\\"\\\"}\"")
    run.deploy_env = {"P95": "321"}
    assert run_smoke(run)["p95_ms"] == 321


# ---- observe stage inside the workflows ----

def feature(tmp_path, **kw):
    scenario = Scenario(**{"name": "o", "workflow": "feature", "input": STORY, "target": "fake", **kw})
    return open_session(scenario, "fake", tmp_path / "runs", run_id="o1")


def events(s, name=None, stage=None):
    return [e for e in s.run.store.audit_events("o1")
            if (name is None or e.event == name) and (stage is None or e.stage == stage)]


def test_passing_observation_completes_the_run_and_comments_on_the_issue(tmp_path):
    s = feature(tmp_path)
    result = s.start()
    assert result.status == "completed" and s.ctx()["observe_ok"] is True
    assert events(s, "observe_passed")
    comments = json.loads((s.run.dir / "github/issues/1.json").read_text())["comments"]
    assert any("Observe passed" in c for c in comments)


def test_slo_breach_opens_a_revert_pr_and_a_bugfix_issue_without_merging_anything(tmp_path):
    s = feature(tmp_path, faults=[{"stage": "observe", "kind": "slo_breach"}])
    result = s.start()
    assert result.status == "observe_failed"
    ctx = s.ctx()
    revert = json.loads((s.run.dir / "github/prs" / f"{ctx['revert_pr']}.json").read_text())
    assert revert["branch"] == "foreman/revert-o1" and revert["labels"] == ["foreman:revert"] and revert["state"] == "open"
    reverted = s.run.repo.git("diff", "--name-only", "main", "foreman/revert-o1").split()
    assert "src/shortener.py" in reverted
    assert (s.run.repo.path / "src/shortener.py").exists()          # main untouched
    bug = s.run.github.get_issue(ctx["bugfix_issue"])
    assert bug.labels == ["foreman:bugfix", "foreman:needs-approval"]
    assert "p95" in bug.body and f"PR #{result.pr}" in bug.body and "Probable cause" in bug.body


def test_observer_runs_in_an_empty_sandbox_and_never_sees_source(tmp_path):
    s = feature(tmp_path, faults=[{"stage": "observe", "kind": "slo_breach"}])
    seen = {}

    def spy(req, fault):
        seen["cwd"], seen["prompt"] = req.cwd, req.prompt
        return BEHAVIORS["observer"](req, fault)

    s.run.worker.behaviors = {**BEHAVIORS, "observer": spy}
    s.start()
    assert seen["cwd"] != s.run.repo.path and list(seen["cwd"].iterdir()) == []
    assert "def resolve" not in seen["prompt"] and "p95_ms" in seen["prompt"]


def test_observe_failure_is_costed_and_audited(tmp_path):
    s = feature(tmp_path, faults=[{"stage": "observe", "kind": "slo_breach"}])
    s.start()
    assert "observer" in {c.agent for c in s.run.store.cost_events(run_id="o1")}
    assert events(s, "observe_failed")


# ---- picking up work: only ready issues ----

def test_issue_awaiting_approval_is_refused_until_a_human_labels_it_ready(tmp_path, repo):
    gh = FakeGitHub(tmp_path / "gh", repo)
    n = gh.create_issue("Observe failure", "body", ["foreman:bugfix", "foreman:needs-approval"])
    with pytest.raises(RunRefused, match="foreman:ready"):
        ensure_ready(gh.get_issue(n))
    n2 = gh.create_issue("Approved", "body", ["foreman:bugfix", "foreman:needs-approval", "foreman:ready"])
    ensure_ready(gh.get_issue(n2))


def test_ordinary_issue_needs_no_label_to_run(tmp_path, repo):
    gh = FakeGitHub(tmp_path / "gh", repo)
    ensure_ready(gh.get_issue(gh.create_issue("Plain", "body", ["foreman:feature"])))
