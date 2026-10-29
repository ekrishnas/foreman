from pathlib import Path

import pytest

from foreman.core.config import WORKFLOWS, allowed_tools, load_agents, load_target
from foreman.core.util import glob_match, merge_dicts, section, sh, sha, slug

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


@pytest.mark.parametrize("out,expected", [
    ("....\n3 passed in 0.10s", 3),
    ("FAILED x\n1 failed, 2 passed in 0.10s", 2),
    ("Passed!  - Failed:     0, Passed:    12, Skipped:     0, Total:    12", 12),
    ("no tests ran", 0),
])
def test_count_tests_reads_pytest_and_dotnet_summaries(out, expected):
    from foreman.core.util import count_tests
    assert count_tests(out) == expected


@pytest.mark.parametrize("out,failed,total", [
    ("1 failed, 4 passed in 0.2s", 1, 5),
    ("4 passed in 0.2s", 0, 4),
    ("1 failed, 3 passed, 1 skipped in 1s", 1, 5),
    ("Failed!  - Failed:     2, Passed:    12, Skipped:     0, Total:    14", 2, 14),
    ("garbage", 0, 0),
])
def test_count_failed_and_total(out, failed, total):
    from foreman.core.util import count_failed, count_total
    assert (count_failed(out), count_total(out)) == (failed, total)


def test_sh_accepts_an_argv_list_without_a_shell(tmp_path):
    assert sh(["echo", "a b;c"], cwd=tmp_path).out.strip() == "a b;c"


def test_sh_feeds_stdin(tmp_path):
    assert sh("cat", cwd=tmp_path, input="hello").out == "hello"


def test_sh_env_allowlist_drops_everything_else(tmp_path, monkeypatch):
    monkeypatch.setenv("SECRET_X", "1")
    out = sh("env", cwd=tmp_path, keep_env=["HOME"]).out
    assert "SECRET_X" not in out and "PATH=" in out


def test_sh_missing_executable_gives_127(tmp_path):
    assert sh(["/definitely/not/here"], cwd=tmp_path).code == 127


def test_openspec_helpers_expand_to_scoped_commands_and_never_to_archive_or_apply():
    agents, _ = load_agents(ROOT / "config/agents.yaml")
    target = load_target(ROOT / "examples/target.yaml")
    architect = allowed_tools(agents["architect"], target)
    assert "Bash(openspec instructions:*)" in architect and "Bash(openspec new change:*)" in architect
    assert "openspec_author" not in architect
    assert "Bash(openspec new change:*)" not in allowed_tools(agents["reviewer"], target)
    for name, agent in agents.items():
        joined = " ".join(allowed_tools(agent, target))
        assert "openspec archive" not in joined and "openspec apply" not in joined, name


def test_load_agents_has_all_twenty_agents():
    agents, models = load_agents(ROOT / "config/agents.yaml")
    assert len(agents) == 20
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
    assert names == ["contract", "acceptance_criteria", "spec_scenarios", "target_profile"]  # not design or code


def test_the_agents_that_create_projects_are_told_the_product_profile():
    """The profile's commands and paths decide where projects must live, so the agents that plan and create them see it."""
    agents, _ = load_agents(ROOT / "config/agents.yaml")
    for name in ("architect", "implementer", "test_writer"):
        assert "target_profile" in [k for k, _ in agents[name].context_keys()], name


def test_no_prompt_tells_an_agent_to_edit_a_file_outside_its_writable_paths():
    """A past prompt told the implementer to edit .gitignore, which sits at the repo root and is always refused."""
    for name in ("implementer", "fixer"):
        text = (ROOT / "prompts" / f"{name}.md").read_text(encoding="utf-8")
        assert "Add the log directory to `.gitignore`" not in text


def test_the_example_profile_loads_and_the_repo_is_where_the_profile_lives(tmp_path):
    t = load_target(ROOT / "examples/target.yaml")
    assert t.name == "my-app" and t.commands["test"] == "dotnet test"
    assert t.dependency_manifests == ["**/*.csproj"] and t.protected == ["**/Migrations/**"]
    assert t.repo == ROOT  # examples/target.yaml sits two levels below ROOT/examples/.. (a real one: <repo>/.foreman/)


def test_a_profile_inside_dot_foreman_belongs_to_the_repo_around_it(tmp_path):
    (tmp_path / ".foreman").mkdir()
    (tmp_path / ".foreman/target.yaml").write_text("paths: {src: 'a/**', tests: 't/**', docs: 'd/**'}\ncommands: {build: b, test: t}\n")
    t = load_target(tmp_path / ".foreman/target.yaml")
    assert t.repo == tmp_path.resolve() and t.name == tmp_path.name  # the name defaults to the folder name
    assert t.dependency_manifests == [] and t.protected == []


def test_a_profile_missing_its_commands_is_an_error(tmp_path):
    (tmp_path / "t.yaml").write_text("paths: {src: 'a/**'}\n")
    with pytest.raises(KeyError):
        load_target(tmp_path / "t.yaml")


def test_allowed_tools_expands_command_tools_from_target():
    agents, _ = load_agents(ROOT / "config/agents.yaml")
    t = load_target(ROOT / "examples/target.yaml")
    tools = allowed_tools(agents["fixer"], t)
    assert "Bash(dotnet build:*)" in tools and "Bash(dotnet test:*)" in tools
    assert "Edit" in tools and "build_cmd" not in tools


def test_every_workflow_the_router_can_choose_has_a_graph_and_the_graphs_are_all_choosable():
    from foreman.workflows import GRAPHS
    assert set(GRAPHS) == set(WORKFLOWS)


def test_every_agent_has_a_prompt_file_and_every_prompt_file_an_agent():
    agents, _ = load_agents(ROOT / "config/agents.yaml")
    assert {p.stem for p in (ROOT / "prompts").glob("*.md")} == set(agents)


def test_ci_files_and_ignore_entries_are_written_only_by_the_agents_that_need_them():
    agents, _ = load_agents(ROOT / "config/agents.yaml")
    assert {"{paths.ci}", "{paths.ignore}"} <= set(agents["implementer"].writable)
    assert "{paths.ignore}" in agents["test_writer"].writable and "{paths.ci}" not in agents["test_writer"].writable
    others = [a for n, a in agents.items() if n not in ("implementer", "test_writer")]
    assert not [a.name for a in others if {"{paths.ci}", "{paths.ignore}"} & set(a.writable)]


def test_the_example_profile_lets_agents_write_ci_files_and_ignore_entries():
    t = load_target(ROOT / "examples/target.yaml")
    assert t.paths["ci"] == ".github/**" and t.paths["ignore"] == ".gitignore"
    assert t.requires == []  # docker is an opt-in line, commented out in the template


def test_a_profile_without_requirements_requires_nothing(tmp_path):
    (tmp_path / ".foreman").mkdir()
    (tmp_path / ".foreman/target.yaml").write_text(
        "paths: {src: 'src/**', tests: 'tests/**', docs: 'docs/**'}\ncommands: {build: b, test: t}\n", encoding="utf-8")
    assert load_target(tmp_path / ".foreman/target.yaml").requires == []
