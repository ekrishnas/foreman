"""Foreman must run on Windows too: no POSIX-only commands in the fake profile, no reliance on a UTF-8
default encoding, no hard dependency on os.killpg."""
import json
import os
import subprocess
import sys

from foreman.config import load_target
from foreman.util import sh
from tests.conftest import ROOT


def test_fake_target_commands_only_call_python():
    # cmd.exe has no `true`, `false` or `touch`; the fake profile must not depend on a POSIX shell.
    commands = load_target(ROOT / "targets/fake.yaml").commands
    assert all(c.startswith("python") for c in commands.values()), commands


def test_sh_timeout_works_without_killpg(tmp_path, monkeypatch):
    monkeypatch.delattr(os, "killpg")
    assert sh("sleep 2", cwd=tmp_path, timeout=1).code == 124


def cli_with_ascii_default_encoding(tmp_path, scenario):
    env = {**os.environ, "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0", "LC_ALL": "C", "LANG": "C"}
    env.pop("PYTHONIOENCODING", None)
    return subprocess.run(
        [sys.executable, "-m", "foreman.cli", "run", str(scenario), "--mode", "fake", "--runs-dir", str(tmp_path)],
        capture_output=True, text=True, env=env, cwd=ROOT)


def test_fake_run_works_when_the_default_text_encoding_is_not_utf8(tmp_path):
    r = cli_with_ascii_default_encoding(tmp_path, ROOT / "scenarios/greenfield.yaml")
    assert r.returncode == 0, r.stdout + r.stderr


def test_non_ascii_issue_text_survives_a_non_utf8_default_encoding(tmp_path):
    story = tmp_path / "story.md"
    story.write_text("# Käse-Kürzer — café\n\nWir brauchen “Links”.\n", encoding="utf-8")
    scenario = tmp_path / "s.yaml"
    scenario.write_text(f"name: u\nworkflow: feature\ninput: {story.as_posix()}\ntarget: fake\n", encoding="utf-8")
    r = cli_with_ascii_default_encoding(tmp_path, scenario)
    assert r.returncode == 0, r.stdout + r.stderr
    pr = next(tmp_path.glob("*/github/prs/2.json"))
    assert "Käse-Kürzer — café" in json.loads(pr.read_text(encoding="utf-8"))["title"]
