import re

import pytest

from foreman.cli import CliError, create_issue_from_file, handoff_from_file, main, wait_for_merge
from foreman.fake_behaviors import ANALYSIS_DOC
from foreman.github import FakeGitHub
from foreman.gitops import GitRepo
from foreman.human import ScriptedHuman
from tests.conftest import ROOT

SCENARIOS = ROOT / "scenarios"


def cli(tmp_path, capsys, *args):
    code = main([*args, "--runs-dir", str(tmp_path)] if args[0] != "issue" else list(args))
    return code, capsys.readouterr().out


def run_id_of(out):
    return re.search(r"^run: (\S+)", out, re.M).group(1)


def test_run_in_fake_mode_reports_status_pr_and_cost(tmp_path, capsys):
    code, out = cli(tmp_path, capsys, "run", str(SCENARIOS / "greenfield.yaml"), "--mode", "fake")
    assert code == 0, out
    assert "status: completed" in out and "PR: #2" in out and "cost: $0.57" in out
    assert "expect: ok" in out


@pytest.mark.parametrize("name", ["greenfield", "brownfield", "ambiguous"])
def test_each_shipped_scenario_passes_its_own_expectations_in_fake_mode(name, tmp_path, capsys):
    code, out = cli(tmp_path, capsys, "run", str(SCENARIOS / f"{name}.yaml"), "--mode", "fake")
    assert code == 0, out


def test_unmet_expectation_exits_one_and_says_what_failed(tmp_path, capsys):
    scenario = tmp_path / "s.yaml"
    scenario.write_text((SCENARIOS / "greenfield.yaml").read_text().replace("status: completed", "status: safe_stopped"))
    code, out = cli(tmp_path, capsys, "run", str(scenario), "--mode", "fake")
    assert code == 1 and "expected 'safe_stopped'" in out


def test_safe_stop_exits_two_with_the_reason(tmp_path, capsys):
    code, out = cli(tmp_path, capsys, "run", str(SCENARIOS / "faults/budget-stop.yaml"), "--mode", "fake")
    assert code == 2 and "status: safe_stopped" in out and "budget" in out


def test_observe_failure_exits_three(tmp_path, capsys):
    scenario = tmp_path / "s.yaml"
    scenario.write_text((SCENARIOS / "greenfield.yaml").read_text().replace(
        "faults: []", "faults:\n  - {stage: observe, kind: slo_breach}").replace("status: completed", "status: observe_failed"))
    code, out = cli(tmp_path, capsys, "run", str(scenario), "--mode", "fake")
    assert code == 3 and "observe_failed" in out and "revert PR" in out


def test_metrics_command_reads_the_exported_audit_and_cost_files(tmp_path, capsys):
    _, out = cli(tmp_path, capsys, "run", str(SCENARIOS / "faults/retry-then-success.yaml"), "--mode", "fake")
    code, out = cli(tmp_path, capsys, "metrics", run_id_of(out))
    assert code == 0
    for needle in ("stage_success", "retry_rate", "rollbacks", "mttr_s", "latency_s", "human_wait_s", "cost_usd"):
        assert needle in out


def test_cost_for_an_issue_sums_every_run_against_it(tmp_path, capsys):
    cli(tmp_path, capsys, "run", str(SCENARIOS / "greenfield.yaml"), "--mode", "fake")
    cli(tmp_path, capsys, "run", str(SCENARIOS / "greenfield.yaml"), "--mode", "fake")
    code, out = cli(tmp_path, capsys, "cost", "--issue", "1")
    assert code == 0 and "2 runs" in out and "total: $1.14" in out


def test_resume_without_a_durable_store_explains_how_to_get_one(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("FOREMAN_DB_URL", raising=False)
    code, out = cli(tmp_path, capsys, "resume", "some-run")
    assert code == 1 and "FOREMAN_DB_URL" in out


def test_live_mode_requires_a_durable_store(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("FOREMAN_DB_URL", raising=False)
    code, out = cli(tmp_path, capsys, "run", str(SCENARIOS / "greenfield.yaml"), "--mode", "live")
    assert code == 1 and "FOREMAN_DB_URL" in out


def test_fake_mode_cannot_run_an_existing_issue_number(tmp_path, capsys):
    code, out = cli(tmp_path, capsys, "run", str(SCENARIOS / "greenfield.yaml"), "--mode", "fake", "--issue", "5")
    assert code == 1 and "fake mode" in out


def test_record_then_replay_reproduces_the_run_without_a_fake_worker(tmp_path, capsys):
    scenario = tmp_path / "s.yaml"
    scenario.write_text((SCENARIOS / "greenfield.yaml").read_text().replace("target: url-shortener", "target: fake"))
    _, out = cli(tmp_path, capsys, "run", str(scenario), "--mode", "fake", "--record")
    recorded = tmp_path / run_id_of(out) / "recorded"
    assert list(recorded.glob("*.json"))
    code, out = cli(tmp_path, capsys, "run", str(scenario), "--mode", "replay", "--recording", str(recorded))
    assert code == 0 and "status: completed" in out and "cost: $0.57" in out


def test_replay_requires_a_recording(tmp_path, capsys):
    code, out = cli(tmp_path, capsys, "run", str(SCENARIOS / "greenfield.yaml"), "--mode", "replay")
    assert code == 1 and "--recording" in out


def test_issue_create_files_a_labelled_issue_from_a_markdown_file(tmp_path, repo):
    gh = FakeGitHub(tmp_path / "gh", repo)
    n = create_issue_from_file(gh, SCENARIOS / "inputs/spike-analytics.md", "spike")
    issue = gh.get_issue(n)
    assert issue.title == "Link analytics: what should we build?" and issue.labels == ["foreman:spike"]
    assert "## Questions" in issue.body


def test_issue_create_rejects_an_unknown_type(tmp_path, repo):
    with pytest.raises(ValueError, match="type"):
        create_issue_from_file(FakeGitHub(tmp_path / "gh", repo), SCENARIOS / "inputs/spike-analytics.md", "epic")


def test_wait_for_merge_polls_until_the_pr_leaves_the_open_state(tmp_path, repo):
    gh = FakeGitHub(tmp_path / "gh", repo)
    repo.start_run("foreman/r1", "main")
    pr = gh.open_pr("foreman/r1", "T", "b", [], closes=None)
    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 3:
            gh.merge(pr)

    assert wait_for_merge(gh, pr, interval=7, timeout=100, sleep=fake_sleep) == "merged"
    assert sleeps == [7, 7, 7]


def test_wait_for_merge_gives_up_after_the_timeout(tmp_path, repo):
    gh = FakeGitHub(tmp_path / "gh", repo)
    repo.start_run("foreman/r1", "main")
    pr = gh.open_pr("foreman/r1", "T", "b", [], closes=None)
    assert wait_for_merge(gh, pr, interval=10, timeout=25, sleep=lambda s: None) == "open"


def test_every_agent_has_a_prompt_that_states_its_role():
    from foreman.config import load_agents
    agents, _ = load_agents(ROOT / "config/agents.yaml")
    for name in agents:
        text = (ROOT / "prompts" / f"{name}.md").read_text()
        assert text.startswith("# Role:") and "## Inputs" in text, name


# ---- handoff: file the issues of an analysis that is already approved ----

def analysis_file(tmp_path, text=None):
    path = tmp_path / "analysis.md"
    path.write_text(text or ANALYSIS_DOC.format(assumptions="- none"), encoding="utf-8")
    return path


def fake_github(tmp_path):
    github = FakeGitHub(tmp_path / "gh", GitRepo.init(tmp_path / "repo"))
    return github, github.create_issue("Explore", "body", ["foreman:spike"])


def test_handoff_files_the_issues_of_an_approved_analysis_after_the_human_says_yes(tmp_path):
    github, spike = fake_github(tmp_path)
    numbers = handoff_from_file(github, ScriptedHuman(), analysis_file(tmp_path), spike)
    assert [github.get_issue(n).title for n in numbers] == [
        "Count clicks per day for each short link", "Show daily click counts to the link owner"]
    assert all(github.get_issue(n).labels == ["foreman:feature", "foreman:needs-approval"] for n in numbers)


def test_handoff_files_nothing_when_the_human_declines(tmp_path):
    github, spike = fake_github(tmp_path)
    with pytest.raises(CliError, match="not approved: not yet"):
        handoff_from_file(github, ScriptedHuman({"handoff": ["reject: not yet"]}), analysis_file(tmp_path), spike)
    assert github._next() == spike + 1  # no issue was filed


def test_handoff_accepts_an_analysis_that_still_has_a_single_draft_user_story(tmp_path):
    github, spike = fake_github(tmp_path)
    text = ANALYSIS_DOC.format(assumptions="- none").split("## Proposed issues")[0] + (
        "## Draft user story\n### Track clicks\n\nAs a link owner I want clicks.\n")
    numbers = handoff_from_file(github, ScriptedHuman(), analysis_file(tmp_path, text), spike)
    assert [github.get_issue(n).title for n in numbers] == ["Track clicks"]


def test_handoff_refuses_an_analysis_with_no_issues(tmp_path):
    github, spike = fake_github(tmp_path)
    text = ANALYSIS_DOC.format(assumptions="- none").split("## Proposed issues")[0]
    with pytest.raises(CliError, match="Proposed issues"):
        handoff_from_file(github, ScriptedHuman(), analysis_file(tmp_path, text), spike)
