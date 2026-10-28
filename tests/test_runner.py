import threading
import time

import pytest

from foreman.config import Agent
from foreman.human import ScriptedHuman
from foreman.runner import SafeStop, Stage, run_stage
from foreman.workers import Recorder, WorkerResult

IMPL = Agent("implementer", "mid", ["Read", "Write"], ["src/**"], ["design"])
STRONG = Agent("architect", "strong", ["Read", "Write"], ["src/**"], ["design"])
CTX = {"design": "the design"}


def writes(path="src/a.py", text="DONE: 1.1"):
    def behavior(req, fault):
        p = req.cwd / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x = 1\n")
        return text
    return behavior


def events(run, name):
    return [e for e in run.store.audit_events("r1") if e.event == name]


def stage(gate=None, **kw):
    return Stage("implement", "implementer", produces="implemented", gate=gate, **kw)


def test_stage_passes_commits_and_records_one_cost_row(make_run):
    run = make_run({"implementer": IMPL}, {"implementer": writes()})
    before = run.repo.head()
    out = run_stage(run, stage(), CTX, {})
    assert out["ctx"]["implemented"] == "DONE: 1.1"
    assert run.repo.head() != before and run.repo.changes() == []
    assert run.repo.git("log", "-1", "--format=%s").strip() == "foreman: implement"
    costs = run.store.cost_events(run_id="r1")
    assert len(costs) == 1 and costs[0].attempt == 0 and costs[0].model == "claude-sonnet-5-5"
    assert len(events(run, "stage_passed")) == 1


def test_stage_records_input_and_output_hashes(make_run):
    run = make_run({"implementer": IMPL}, {"implementer": writes()})
    out = run_stage(run, stage(), CTX, {})
    assert set(out["done"]["implement"]) == {"inputs", "output"}


def test_gate_failure_is_retried_with_the_failure_in_the_prompt(make_run):
    prompts = []

    def behavior(req, fault):
        prompts.append(req.prompt)
        return writes()(req, fault)

    run = make_run({"implementer": IMPL}, {"implementer": behavior})
    gate = lambda run, ctx, text: "tests are missing" if len(prompts) == 1 else None
    run_stage(run, stage(gate), CTX, {})
    assert "tests are missing" not in prompts[0] and "tests are missing" in prompts[1]
    assert [c.attempt for c in run.store.cost_events(run_id="r1")] == [0, 1]
    assert len(events(run, "attempt_failed")) == 1 and len(events(run, "retry")) == 1


def test_exhausted_retries_fall_back_once_to_the_strong_model(make_run):
    calls = []
    run = make_run({"implementer": IMPL}, {"implementer": writes()})
    gate = lambda run, ctx, text: calls.append(1) or (None if len(calls) == 4 else "nope")
    run_stage(run, stage(gate), CTX, {})
    models = [c.model for c in run.store.cost_events(run_id="r1")]
    assert models == ["claude-sonnet-5-5"] * 3 + ["claude-opus-5-5"]
    assert len(events(run, "fallback")) == 1


def test_strong_agent_has_no_fallback_and_safe_stops_after_three_attempts(make_run):
    run = make_run({"architect": STRONG}, {"architect": writes()})
    with pytest.raises(SafeStop, match="nope"):
        run_stage(run, Stage("proposal", "architect", gate=lambda r, c, t: "nope"), CTX, {})
    assert len(run.store.cost_events(run_id="r1")) == 3
    assert run.repo.changes() == []


def test_blocking_policy_finding_fails_every_attempt_and_leaves_tree_clean(make_run):
    run = make_run({"implementer": IMPL}, {"implementer": writes()},
                   faults=[{"stage": "implement", "all": True, "kind": "bad_path"}])
    with pytest.raises(SafeStop, match="outside"):
        run_stage(run, stage(), CTX, {})
    assert run.repo.changes() == [] and len(run.store.cost_events(run_id="r1")) == 4


def test_secret_in_the_diff_blocks_the_attempt(make_run):
    run = make_run({"implementer": IMPL}, {"implementer": writes()},
                   faults=[{"stage": "implement", "calls": [0], "kind": "secret"}])
    run_stage(run, stage(), CTX, {})
    failed = events(run, "attempt_failed")
    assert len(failed) == 1 and "secret" in failed[0].details["reason"]


def test_budget_already_spent_stops_before_calling_the_worker(make_run):
    run = make_run({"implementer": IMPL}, {"implementer": writes()}, budget=0.5)
    from foreman.store import CostEvent
    run.store.cost(CostEvent("r1", 1, "feature", "earlier", "x", 0, "m", 1, 1, 1.0, 1))
    with pytest.raises(SafeStop, match="budget"):
        run_stage(run, stage(), CTX, {})
    assert len(run.store.cost_events(run_id="r1")) == 1


def test_unchanged_inputs_skip_the_stage(make_run):
    run = make_run({"implementer": IMPL}, {"implementer": writes()})
    first = run_stage(run, stage(), CTX, {})
    again = run_stage(run, stage(), CTX, first["done"])
    assert again == {"ctx": {}, "done": {}}
    assert len(run.store.cost_events(run_id="r1")) == 1 and len(events(run, "stage_skipped")) == 1


def test_changed_inputs_rerun_the_stage(make_run):
    run = make_run({"implementer": IMPL}, {"implementer": writes()})
    first = run_stage(run, stage(), CTX, {})
    run_stage(run, stage(), {"design": "a different design"}, first["done"])
    assert len(run.store.cost_events(run_id="r1")) == 2


def test_missing_required_context_fails_the_entry_gate(make_run):
    run = make_run({"implementer": IMPL}, {"implementer": writes()})
    with pytest.raises(SafeStop, match="missing context.*design"):
        run_stage(run, stage(), {}, {})
    assert run.store.cost_events(run_id="r1") == []


def test_high_impact_change_needs_approval_and_rejection_fails_the_attempt(make_run):
    agent = Agent("implementer", "mid", ["Write"], ["src/**", "requirements.txt"], ["design"])
    run = make_run({"implementer": agent}, {"implementer": writes("requirements.txt")},
                   human=ScriptedHuman({"high_impact": ["reject: no new deps"]}))
    run_stage(run, stage(), CTX, {})
    approvals = events(run, "approval")
    assert [a.details["approved"] for a in approvals] == [False, True]
    assert "no new deps" in events(run, "attempt_failed")[0].details["reason"]
    assert approvals[0].details["wait_s"] >= 0


def test_worker_error_counts_as_a_failed_attempt(make_run):
    class Flaky:
        n = 0

        def run(self, req):
            Flaky.n += 1
            if Flaky.n == 1:
                return WorkerResult("", error="claude timed out")
            writes()(req, None)
            return WorkerResult("ok", 1, 1, 0.01, 1)

    run = make_run({"implementer": IMPL}, worker=Flaky())
    run_stage(run, stage(), CTX, {})
    assert "claude timed out" in events(run, "attempt_failed")[0].details["reason"]
    assert len(run.store.cost_events(run_id="r1")) == 2


def test_every_worker_call_is_recorded_when_a_recorder_is_set(make_run, tmp_path):
    run = make_run({"implementer": IMPL}, {"implementer": writes()})
    run.recorder = Recorder(tmp_path / "rec")
    run_stage(run, stage(), CTX, {})
    assert (tmp_path / "rec/implement-0.json").exists()


def test_after_hook_updates_are_returned_and_its_file_edits_are_committed(make_run):
    def after(run, ctx, text):
        (run.repo.path / "notes.md").write_text("ticked")
        return {"extra": "1"}

    run = make_run({"implementer": IMPL}, {"implementer": writes()})
    out = run_stage(run, Stage("implement", "implementer", produces="implemented", after=after), CTX, {})
    assert out["ctx"]["extra"] == "1"
    assert "notes.md" in run.repo.git("show", "--name-only", "--format=", "HEAD")


def test_instances_have_their_own_attempt_counter_and_stage_key(make_run):
    explorer = Agent("explorer", "mid", ["Write"], ["spikes/{run_id}/{option}/**"], [])
    seen = []

    def behavior(req, fault):
        seen.append(req.call)
        option = "opt-1" if "opt-1" in req.key else "opt-2"
        return writes(f"spikes/r1/{option}/n.md")(req, fault)

    run = make_run({"explorer": explorer}, {"explorer": behavior})
    s = Stage("explore", "explorer")
    run_stage(run, s, {}, {}, instance="opt-1")
    run_stage(run, s, {}, {}, instance="opt-2")
    assert seen == [0, 0]
    assert {c.stage for c in run.store.cost_events(run_id="r1")} == {"explore/opt-1", "explore/opt-2"}


def test_sandbox_stage_runs_outside_the_repo_and_needs_no_foreman_branch(make_run):
    watcher = Agent("observer", "small", ["Read"], [], [])
    seen = []

    def behavior(req, fault):
        seen.append(req.cwd)
        return "" if len(seen) == 1 else "summary"

    run = make_run({"observer": watcher}, {"observer": behavior})
    run.repo.git("checkout", "-q", "main")          # post-merge: not on a foreman/ branch
    gate = lambda r, c, text: None if text else "empty summary"
    out = run_stage(run, Stage("observe_summary", "observer", produces="s", gate=gate, commit=False, sandbox=True), {}, {})
    assert out["ctx"]["s"] == "summary"
    assert all(cwd != run.repo.path and cwd.is_dir() for cwd in seen) and len(seen) == 2


def test_concurrent_stages_do_not_mix_their_diffs(make_run):
    a = Agent("a", "mid", ["Write"], ["a/**"], [])
    b = Agent("b", "mid", ["Write"], ["b/**"], [])

    def slow(path):
        def behavior(req, fault):
            writes(path)(req, fault)
            time.sleep(0.2)
            return "ok"
        return behavior

    run = make_run({"a": a, "b": b}, {"a": slow("a/x.py"), "b": slow("b/y.py")})
    errors = []

    def go(s):
        try:
            run_stage(run, s, {}, {})
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=go, args=(Stage(n, n),)) for n in ("a", "b")]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert errors == []
    log = run.repo.git("log", "--name-only", "--format=%s", "-2")
    assert "a/x.py" in log and "b/y.py" in log
    assert run.repo.git("show", "--name-only", "--format=", "HEAD").count("/") == 1
