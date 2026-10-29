import sys

import pytest

from foreman.adapters.specs import SpecAdapter
from tests.doubles.run import OPENSPEC

FAKE_CLI = OPENSPEC

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


CURRENT = """# shortener Specification

## Requirements
### Requirement: Count clicks
The system SHALL count one click each time a code is resolved.

#### Scenario: Resolve twice
- **WHEN** a code is resolved twice
- **THEN** its click count is 2
"""


def modified(*scenarios):
    body = "".join(f"\n#### Scenario: {name}\n- **WHEN** x\n- **THEN** y\n" for name in scenarios)
    return f"## MODIFIED Requirements\n### Requirement: Count clicks\nThe system SHALL count clicks, never stale ones.\n{body}"


def living_spec(root):
    (root / "openspec/specs/shortener").mkdir(parents=True)
    (root / "openspec/specs/shortener/spec.md").write_text(CURRENT)


def test_a_modified_requirement_must_keep_every_scenario_the_current_spec_has(tmp_path):
    """The real CLI (1.15.0) refuses a MODIFIED block that drops a scenario; the stand-in must too."""
    living_spec(tmp_path)
    make_change(tmp_path, spec=modified("Stats stay current"))
    ok, out = SpecAdapter(FAKE_CLI, tmp_path).validate("c-1")
    assert not ok and 'omits scenario(s) the current spec still has: "Resolve twice"' in out


def test_a_modified_requirement_that_keeps_them_and_adds_one_passes(tmp_path):
    living_spec(tmp_path)
    make_change(tmp_path, spec=modified("Resolve twice", "Stats stay current"))
    ok, out = SpecAdapter(FAKE_CLI, tmp_path).validate("c-1")
    assert ok, out


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
