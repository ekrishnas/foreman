from pathlib import Path

import pytest

from foreman.config import allowed_tools, load_agents, load_scenario, load_target
from foreman.util import glob_match, merge_dicts, section, sh, sha, slug

ROOT = Path(__file__).resolve().parent.parent


def test_glob_double_star_matches_any_depth():
    assert glob_match("src/a/b.py", "src/**")


def test_glob_double_star_prefix_matches_root_file():
    assert glob_match("a.csproj", "**/*.csproj")
    assert glob_match("x/y/A.csproj", "**/*.csproj")


def test_glob_does_not_match_sibling_prefix():
    assert not glob_match("srcx/a.py", "src/**")


def test_glob_single_star_stays_within_segment():
    assert glob_match("docs/a.md", "docs/*.md")
    assert not glob_match("docs/sub/a.md", "docs/*.md")


def test_section_returns_body_under_heading():
    assert section("# T\n## A\nx\ny\n## B\nz", "A") == "x\ny"


def test_section_missing_heading_is_empty():
    assert section("## A\nx", "B") == ""


def test_section_heading_match_is_case_insensitive():
    assert section("## Blocking Questions\n- q", "blocking questions") == "- q"


def test_slug():
    assert slug("Fix: Click Counts!") == "fix-click-counts"


def test_sha_is_12_hex_and_stable():
    assert sha("a") == sha("a") and len(sha("a")) == 12 and sha("a") != sha("b")


def test_merge_dicts_later_wins_and_none_ok():
    assert merge_dicts(None, {"a": 1}) == {"a": 1}
    assert merge_dicts({"a": 1, "b": 2}, {"a": 3}) == {"a": 3, "b": 2}


def test_sh_returns_exit_code_and_output(tmp_path):
    r = sh("echo hi; exit 3", cwd=tmp_path)
    assert r.code == 3 and r.out.strip() == "hi"


def test_sh_timeout_gives_124(tmp_path):
    assert sh("sleep 5", cwd=tmp_path, timeout=1).code == 124


def test_sh_puts_current_python_first_on_path(tmp_path):
    import sys

    r = sh("python -c 'import sys; print(sys.executable)'", cwd=tmp_path)
    assert Path(r.out.strip()).resolve() == Path(sys.executable).resolve()


def test_load_agents_has_the_fourteen_design_agents():
    agents, models = load_agents(ROOT / "config/agents.yaml")
    assert len(agents) == 14
    assert agents["implementer"].tier == "mid"
    assert agents["requirements_analyst"].writable == []
    assert models["strong"] == "claude-opus-5-5"


def test_agent_context_keys_mark_optional_with_question_mark():
    agents, _ = load_agents(ROOT / "config/agents.yaml")
    keys = dict(agents["implementer"].context_keys())
    assert keys["design"] is True and keys["last_failure"] is False


def test_test_writer_never_sees_the_implementation():
    agents, _ = load_agents(ROOT / "config/agents.yaml")
    names = [k for k, _ in agents["test_writer"].context_keys()]
    assert names == ["contract", "acceptance_criteria", "spec_scenarios"]


def test_load_target_matches_design_profile():
    t = load_target(ROOT / "targets/url-shortener.yaml")
    assert t.name == "url-shortener"
    assert t.commands["test"] == "dotnet test"
    assert t.slo == {"error_rate_max": 0.01, "p95_ms_max": 200}
    assert t.dependency_manifests == ["**/*.csproj"]
    assert t.repo == (ROOT / "../url-shortener").resolve()


def test_allowed_tools_expands_command_tools_from_target():
    agents, _ = load_agents(ROOT / "config/agents.yaml")
    t = load_target(ROOT / "targets/url-shortener.yaml")
    tools = allowed_tools(agents["fixer"], t)
    assert "Bash(dotnet build:*)" in tools and "Bash(dotnet test:*)" in tools
    assert "Edit" in tools and "build_cmd" not in tools


def test_load_scenario_applies_defaults(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("name: x\nworkflow: feature\ninput: a.md\ntarget: fake\n")
    s = load_scenario(p)
    assert s.approvals == "scripted" and s.faults == [] and s.budget_usd == 10.0
    assert s.start_ref == "main" and s.input == "a.md"


def test_load_scenario_rejects_unknown_workflow(tmp_path):
    p = tmp_path / "s.yaml"
    p.write_text("name: x\nworkflow: nope\ninput: 1\ntarget: fake\n")
    with pytest.raises(ValueError, match="workflow"):
        load_scenario(p)
