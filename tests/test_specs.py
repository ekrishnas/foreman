import sys

import pytest

from foreman.specs import SpecAdapter

FAKE_CLI = [sys.executable, "-m", "foreman.fake_openspec"]

SPEC = """## ADDED Requirements
### Requirement: Shorten URL
The system SHALL return a short code for a valid URL.

#### Scenario: Valid URL
- **WHEN** a valid URL is submitted
- **THEN** a code is returned
"""


def make_change(root, name="c-1", skip=(), spec=SPEC):
    base = root / "openspec/changes" / name
    files = {
        "proposal.md": "## Why\nx\n",
        "design.md": "## Context\nx\n",
        "tasks.md": "- [ ] 1.1 do it\n",
        "specs/shortener/spec.md": spec,
    }
    for rel, text in files.items():
        if rel not in skip:
            (base / rel).parent.mkdir(parents=True, exist_ok=True)
            (base / rel).write_text(text)


def test_valid_change_passes(tmp_path):
    make_change(tmp_path)
    ok, out = SpecAdapter(FAKE_CLI, tmp_path).validate("c-1")
    assert ok, out


def test_missing_design_fails_and_names_the_file(tmp_path):
    make_change(tmp_path, skip=("design.md",))
    ok, out = SpecAdapter(FAKE_CLI, tmp_path).validate("c-1")
    assert not ok and "design.md" in out


def test_requirement_without_scenario_fails(tmp_path):
    make_change(tmp_path, spec="## ADDED Requirements\n### Requirement: X\nThe system SHALL x.\n")
    ok, out = SpecAdapter(FAKE_CLI, tmp_path).validate("c-1")
    assert not ok and "Scenario" in out


def test_unknown_change_fails(tmp_path):
    ok, out = SpecAdapter(FAKE_CLI, tmp_path).validate("nope")
    assert not ok and "nope" in out


def test_archive_moves_change_and_updates_living_spec(tmp_path):
    make_change(tmp_path)
    SpecAdapter(FAKE_CLI, tmp_path).archive("c-1")
    assert not (tmp_path / "openspec/changes/c-1").exists()
    assert list((tmp_path / "openspec/changes/archive").glob("*-c-1"))
    assert "Shorten URL" in (tmp_path / "openspec/specs/shortener/spec.md").read_text()


def test_archive_modified_requirement_replaces_the_old_one(tmp_path):
    make_change(tmp_path)
    adapter = SpecAdapter(FAKE_CLI, tmp_path)
    adapter.archive("c-1")
    make_change(tmp_path, name="c-2", spec=SPEC.replace("ADDED", "MODIFIED").replace("a valid URL", "any URL"))
    adapter.archive("c-2")
    text = (tmp_path / "openspec/specs/shortener/spec.md").read_text()
    assert text.count("### Requirement: Shorten URL") == 1 and "any URL" in text


def test_archive_failure_raises(tmp_path):
    with pytest.raises(RuntimeError, match="nope"):
        SpecAdapter(FAKE_CLI, tmp_path).archive("nope")


def test_adapter_uses_the_documented_cli_arguments(tmp_path):
    log = tmp_path / "argv.log"
    script = tmp_path / "openspec-stub"
    script.write_text(f"#!{sys.executable}\nimport sys\nopen({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + '\\n')\n")
    script.chmod(0o755)
    adapter = SpecAdapter([str(script)], tmp_path)
    adapter.validate("c-1")
    adapter.archive("c-1")
    assert log.read_text().splitlines() == ["validate c-1 --strict", "archive c-1 --yes"]
