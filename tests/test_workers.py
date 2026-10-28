import json
import sys

from foreman.config import Agent
from foreman.fake import FakeWorker
from foreman.workers import ClaudeCodeWorker, Recorder, ReplayWorker, WorkerRequest, WorkerResult

AGENT = Agent("implementer", "mid", ["Read", "Write"], ["src/**"], ["design"])


def make_req(cwd, call=0, stage="implement", model="claude-sonnet-5-5"):
    return WorkerRequest(
        agent=AGENT, model=model, prompt="do it", cwd=cwd, key=f"{stage}-{call}",
        stage=stage, call=call, tools=["Read", "Write"],
    )


def writes_a(req, fault):
    (req.cwd / "src").mkdir(exist_ok=True)
    (req.cwd / "src/a.py").write_text("x = 1\n")
    return "DONE: 1.1"


def fake(faults=None, behavior=writes_a):
    return FakeWorker(faults or [], behaviors={"implementer": behavior})


# ---- FakeWorker ----

def test_fake_runs_behavior_and_reports_deterministic_cost(tmp_path):
    r = fake().run(make_req(tmp_path))
    assert r.text == "DONE: 1.1" and (tmp_path / "src/a.py").exists()
    assert r.cost_usd == 0.05 and r.input_tokens > 0 and r.output_tokens > 0


def test_fake_cost_follows_model_tier(tmp_path):
    assert fake().run(make_req(tmp_path, model="claude-opus-5-5")).cost_usd == 0.20
    assert fake().run(make_req(tmp_path, model="claude-haiku-5-5")).cost_usd == 0.01


def test_fault_no_output_hits_only_listed_calls(tmp_path):
    w = fake([{"stage": "implement", "calls": [0], "kind": "no_output"}])
    first = w.run(make_req(tmp_path, call=0))
    assert first.text == "" and not (tmp_path / "src/a.py").exists()
    assert w.run(make_req(tmp_path, call=1)).text == "DONE: 1.1"


def test_fault_all_applies_to_every_call(tmp_path):
    w = fake([{"stage": "implement", "all": True, "kind": "bad_path"}])
    w.run(make_req(tmp_path, call=5))
    assert (tmp_path / "FOREMAN_BAD_PATH.txt").exists()


def test_fault_for_another_stage_is_ignored(tmp_path):
    w = fake([{"stage": "tests", "all": True, "kind": "no_output"}])
    assert w.run(make_req(tmp_path)).text == "DONE: 1.1"


def test_fault_secret_lands_in_a_file_the_agent_wrote(tmp_path, repo):
    w = fake([{"stage": "implement", "all": True, "kind": "secret"}])
    w.run(make_req(repo.path))
    assert "AKIA" in (repo.path / "src/a.py").read_text()


def test_fault_cost_overrides_usd(tmp_path):
    w = fake([{"stage": "implement", "all": True, "kind": "cost", "usd": 3.5}])
    assert w.run(make_req(tmp_path)).cost_usd == 3.5


def test_behavior_sees_the_fault_kind(tmp_path):
    seen = []
    w = fake([{"stage": "implement", "all": True, "kind": "break_tests"}],
             behavior=lambda req, fault: seen.append(fault) or "ok")
    w.run(make_req(tmp_path))
    assert seen == ["break_tests"]


# ---- ClaudeCodeWorker (against a stub `claude` executable) ----

def stub(tmp_path, body):
    path = tmp_path / "claude-stub"
    path.write_text(f"#!{sys.executable}\n{body}")
    path.chmod(0o755)
    return str(path)


OK_BODY = """
import json, os, sys
seen = {"argv": sys.argv[1:], "stdin": sys.stdin.read(), "env": sorted(os.environ)}
json.dump(seen, open("seen.json", "w"))
print(json.dumps({"result": "hello", "is_error": False, "total_cost_usd": 0.12, "duration_ms": 1500,
                  "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100}}))
"""


def test_claude_worker_parses_json_result(tmp_path):
    r = ClaudeCodeWorker(claude_bin=stub(tmp_path, OK_BODY)).run(make_req(tmp_path))
    assert (r.text, r.cost_usd, r.duration_ms, r.error) == ("hello", 0.12, 1500, None)
    assert (r.input_tokens, r.output_tokens) == (110, 5)


def test_claude_worker_passes_model_tools_and_prompt_on_stdin(tmp_path):
    ClaudeCodeWorker(claude_bin=stub(tmp_path, OK_BODY)).run(make_req(tmp_path))
    seen = json.loads((tmp_path / "seen.json").read_text())
    argv = seen["argv"]
    assert argv[0] == "-p" and argv[argv.index("--model") + 1] == "claude-sonnet-5-5"
    assert argv[argv.index("--allowedTools") + 1] == "Read,Write"
    assert argv[argv.index("--output-format") + 1] == "json"
    assert "WebFetch" in argv[argv.index("--disallowedTools") + 1]
    assert seen["stdin"] == "do it"


def test_claude_worker_hides_api_keys_and_secrets(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret")
    monkeypatch.setenv("GH_TOKEN", "ghp_secret")
    ClaudeCodeWorker(claude_bin=stub(tmp_path, OK_BODY)).run(make_req(tmp_path))
    env = json.loads((tmp_path / "seen.json").read_text())["env"]
    assert "ANTHROPIC_API_KEY" not in env and "GH_TOKEN" not in env and "PATH" in env


def test_claude_worker_non_json_output_is_an_error_not_a_crash(tmp_path):
    r = ClaudeCodeWorker(claude_bin=stub(tmp_path, "print('oops')")).run(make_req(tmp_path))
    assert "non-JSON" in r.error and r.cost_usd == 0


def test_claude_worker_is_error_flag(tmp_path):
    body = "import json; print(json.dumps({'result': 'boom', 'is_error': True}))"
    r = ClaudeCodeWorker(claude_bin=stub(tmp_path, body)).run(make_req(tmp_path))
    assert r.error == "boom"


def test_claude_worker_nonzero_exit(tmp_path):
    r = ClaudeCodeWorker(claude_bin=stub(tmp_path, "import sys; sys.exit(2)")).run(make_req(tmp_path))
    assert "exit code 2" in r.error


def test_claude_worker_timeout(tmp_path):
    r = ClaudeCodeWorker(claude_bin=stub(tmp_path, "import time; time.sleep(5)"), timeout=1).run(make_req(tmp_path))
    assert "timed out" in r.error


def test_claude_worker_missing_binary(tmp_path):
    r = ClaudeCodeWorker(claude_bin="/definitely/not/claude").run(make_req(tmp_path))
    assert "not found" in r.error


# ---- Recorder / ReplayWorker ----

def test_replay_reproduces_recorded_files_and_result(tmp_path):
    rec = Recorder(tmp_path / "rec")
    rec.save("implement-0", WorkerResult("DONE", 1, 2, 0.5, 10), {"src/a.py": "x = 1\n", "old.py": None})
    work = tmp_path / "work"
    (work / "src").mkdir(parents=True)
    (work / "old.py").write_text("gone soon")
    r = ReplayWorker(tmp_path / "rec").run(make_req(work))
    assert r.text == "DONE" and r.cost_usd == 0.5
    assert (work / "src/a.py").read_text() == "x = 1\n" and not (work / "old.py").exists()


def test_replay_without_recording_is_an_error(tmp_path):
    r = ReplayWorker(tmp_path / "rec").run(make_req(tmp_path))
    assert "no recording" in r.error
