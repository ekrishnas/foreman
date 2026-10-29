import subprocess
from types import SimpleNamespace

import pytest

from foreman.adapters.gitops import GitRepo, RepoError
from foreman.adapters.updater import INSTALLED_SKILL, SKILL, install_skill, profile_hints, skill_status, update_foreman
from foreman.cli import main


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, encoding="utf-8")


def write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def world(tmp_path):
    """A bare 'GitHub' holding Foreman's main, a developer's checkout of it, and a second clone that publishes."""
    bare = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(bare))
    publisher = GitRepo.init(tmp_path / "publisher")
    for rel, text in {".gitignore": "runs/*\n.venv/\n", "pyproject.toml": "v1\n", "uv.lock": "v1\n", SKILL: "skill v1\n"}.items():
        write(publisher.path, rel, text)
    publisher.commit("add the files")
    publisher.git("remote", "add", "origin", str(bare))
    publisher.git("push", "-q", "-u", "origin", "main")
    git(tmp_path, "clone", "-q", str(bare), str(tmp_path / "checkout"))

    def publish(message, **files):
        for rel, text in files.items():
            write(publisher.path, rel.replace("__", "/"), text)
        publisher.commit(message)
        publisher.git("push", "-q", "origin", "main")

    return SimpleNamespace(checkout=tmp_path / "checkout", publish=publish, tmp=tmp_path)


# ---- updating the Foreman checkout ----

def test_a_checkout_that_is_current_stays_as_it_is(world):
    r = update_foreman(world.checkout)
    assert r.before == r.after and r.commits == [] and not r.deps_changed


def test_new_commits_are_fast_forwarded_and_listed_newest_first(world):
    world.publish("feat: first", a="1\n")
    world.publish("feat: second", b="2\n")
    r = update_foreman(world.checkout)
    assert [c.split(" ", 1)[1] for c in r.commits] == ["feat: second", "feat: first"]
    assert r.before != r.after and GitRepo(world.checkout).head() == r.after
    assert (world.checkout / "a").exists() and (world.checkout / "b").exists()


def test_a_change_to_the_dependencies_is_reported_so_uv_sync_can_run(world):
    world.publish("chore: bump a dependency", **{"uv.lock": "v2\n"})
    assert update_foreman(world.checkout).deps_changed
    world.publish("docs: reword", a="x\n")
    assert not update_foreman(world.checkout).deps_changed


def test_run_files_and_the_virtualenv_do_not_count_as_local_changes(world):
    write(world.checkout, "runs/foreman.db", "data")
    write(world.checkout, ".venv/lib/x.py", "x")
    world.publish("feat: something", a="1\n")
    assert update_foreman(world.checkout).commits


def test_local_edits_are_never_overwritten(world):
    write(world.checkout, "pyproject.toml", "my own change\n")
    world.publish("feat: something", a="1\n")
    with pytest.raises(RepoError, match="local changes.*pyproject.toml"):
        update_foreman(world.checkout)
    assert (world.checkout / "pyproject.toml").read_text() == "my own change\n"


def test_unpushed_local_commits_are_never_discarded(world):
    local = GitRepo(world.checkout)
    write(world.checkout, "mine.txt", "m\n")
    mine = local.commit("feat: my own work")
    world.publish("feat: theirs", a="1\n")
    with pytest.raises(RepoError, match=r"1 commit.*not on origin/main.*1 .*you lack"):
        update_foreman(world.checkout)
    assert local.head() == mine


def test_local_commits_with_nothing_new_upstream_are_refused_too(world):
    write(world.checkout, "mine.txt", "m\n")
    GitRepo(world.checkout).commit("feat: my own work")
    with pytest.raises(RepoError, match="not on origin/main"):
        update_foreman(world.checkout)


def test_only_main_is_updated(world):
    GitRepo(world.checkout).git("checkout", "-q", "-b", "experiment")
    with pytest.raises(RepoError, match="on 'experiment', not main"):
        update_foreman(world.checkout)


def test_a_folder_that_is_not_a_git_checkout_says_to_clone_again(tmp_path):
    with pytest.raises(RepoError, match="not a git checkout.*clone"):
        update_foreman(tmp_path)


def test_an_unreachable_remote_is_an_error_not_a_crash(world):
    GitRepo(world.checkout).git("remote", "set-url", "origin", str(world.tmp / "nowhere.git"))
    with pytest.raises(RepoError, match="fetch"):
        update_foreman(world.checkout)


# ---- the product's copy of the skill ----

def test_a_product_without_the_skill_says_so(world, tmp_path):
    assert skill_status(tmp_path / "product", root=world.checkout) == "not installed"


def test_an_identical_copy_is_current_even_with_windows_line_endings(world, tmp_path):
    product = tmp_path / "product"
    (product / INSTALLED_SKILL).parent.mkdir(parents=True)
    (product / INSTALLED_SKILL).write_bytes(b"skill v1\r\n")
    assert skill_status(product, root=world.checkout) == "current"


def test_an_older_copy_differs_and_installing_replaces_it_with_a_unix_copy(world, tmp_path):
    product = tmp_path / "product"
    write(product, INSTALLED_SKILL, "skill v0\n")
    assert skill_status(product, root=world.checkout) == "differs"
    assert install_skill(product, root=world.checkout) == product / INSTALLED_SKILL
    assert (product / INSTALLED_SKILL).read_bytes() == b"skill v1\n"
    assert skill_status(product, root=world.checkout) == "current"


# ---- the product's profile ----

def profile(tmp_path, text):
    write(tmp_path, ".foreman/target.yaml", text)
    return tmp_path


def test_a_profile_missing_the_newer_settings_lists_them(tmp_path):
    product = profile(tmp_path, "paths: {src: 'src/**', tests: 'tests/**', docs: 'docs/**'}\ncommands: {build: b, test: t}\n")
    hints = profile_hints(product)
    assert [h.split(":")[0] for h in hints] == ["paths.ci", "paths.ignore", "requires"]


def test_a_profile_that_has_them_lists_nothing(tmp_path):
    product = profile(tmp_path, "paths: {src: 's', tests: 't', docs: 'd', ci: '.github/**', ignore: '.gitignore'}\n"
                                "commands: {build: b, test: t}\nrequires: []\n")
    assert profile_hints(product) == []


def test_no_profile_means_nothing_to_suggest(tmp_path):
    assert profile_hints(tmp_path) == []


# ---- the commands ----

def test_doctor_reports_the_skill_and_the_profile(tmp_path, capsys):
    product = profile(tmp_path, "paths: {src: 's', tests: 't', docs: 'd'}\ncommands: {build: b, test: t}\n")
    assert main(["doctor", "--repo", str(product)]) == 0
    out = capsys.readouterr().out
    assert "skill: not installed" in out and "profile: you do not have these settings yet" in out and "  paths.ci:" in out


def test_doctor_installs_the_skill_only_when_asked(tmp_path, capsys):
    assert main(["doctor", "--repo", str(tmp_path), "--install-skill"]) == 0
    assert (tmp_path / INSTALLED_SKILL).is_file() and "skill: installed" in capsys.readouterr().out
    assert main(["doctor", "--repo", str(tmp_path)]) == 0
    assert "skill: current" in capsys.readouterr().out


def test_update_prints_what_changed_and_whether_to_sync(world, monkeypatch, capsys):
    monkeypatch.setattr("foreman.cli.ROOT", world.checkout)
    world.publish("fix: something", **{"uv.lock": "v2\n"})
    assert main(["update"]) == 0
    out = capsys.readouterr().out
    assert "foreman: updated" in out and "fix: something" in out and "deps: changed" in out and "uv sync" in out
    assert main(["update"]) == 0
    assert "foreman: up to date" in capsys.readouterr().out


def test_update_failures_are_plain_errors(world, monkeypatch, capsys):
    monkeypatch.setattr("foreman.cli.ROOT", world.checkout)
    write(world.checkout, "pyproject.toml", "edited\n")
    assert main(["update"]) == 1
    assert capsys.readouterr().out.startswith("error: Foreman has local changes")
