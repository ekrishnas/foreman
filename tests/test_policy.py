from foreman.config import load_target
from foreman.gitops import Change
from foreman.policy import check_changes, find_secrets, resolve_writable
from tests.conftest import ROOT

TARGET = load_target(ROOT / "targets/url-shortener.yaml")
SRC = ["src/**"]


def kinds(findings):
    return sorted((f.kind, f.blocking) for f in findings)


def test_resolve_writable_fills_placeholders():
    out = resolve_writable(
        ["{paths.src}", "spikes/{run_id}/{option}/**", "openspec/changes/{change}/**"],
        TARGET, run_id="r1", option="o1", change="c-1",
    )
    assert out == ["src/**", "spikes/r1/o1/**", "openspec/changes/c-1/**"]


def test_write_inside_writable_is_clean():
    assert check_changes([Change("A", "src/a.cs")], "", SRC, TARGET) == []


def test_write_outside_writable_is_blocking_path():
    f = check_changes([Change("A", "tests/x.py")], "", SRC, TARGET)
    assert kinds(f) == [("path", True)]
    assert "tests/x.py" in f[0].detail


def test_agent_with_no_writable_paths_cannot_change_anything():
    assert kinds(check_changes([Change("M", "src/a.cs")], "", [], TARGET)) == [("path", True)]


def test_aws_key_is_blocking_secret_and_not_echoed():
    f = check_changes([Change("A", "src/a.cs")], "k = AKIAABCDEFGHIJKLMNOP", SRC, TARGET)
    assert kinds(f) == [("secret", True)]
    assert "AKIAABCDEFGHIJKLMNOP" not in f[0].detail


def test_private_key_header_is_secret():
    text = "-----BEGIN RSA PRIVATE KEY-----"
    assert kinds(check_changes([Change("A", "src/k")], text, SRC, TARGET)) == [("secret", True)]


def test_password_assignment_is_secret():
    text = 'password = "hunter2hunter2"'
    assert kinds(check_changes([Change("A", "src/a")], text, SRC, TARGET)) == [("secret", True)]


def test_ordinary_code_is_not_secret():
    text = 'token = request.token\npassword = ""\nkey = "abc"\n'
    assert check_changes([Change("A", "src/a")], text, SRC, TARGET) == []


def test_dependency_manifest_needs_approval_not_block():
    f = check_changes([Change("M", "src/App/App.csproj")], "", SRC, TARGET)
    assert kinds(f) == [("dependency", False)]


def test_protected_path_is_migration_approval():
    f = check_changes([Change("A", "src/Db/Migrations/001.cs")], "", SRC, TARGET)
    assert kinds(f) == [("migration", False)]


def test_deleted_test_needs_approval():
    f = check_changes([Change("D", "tests/t.cs")], "", ["tests/**"], TARGET)
    assert kinds(f) == [("deleted_test", False)]


def test_ci_and_docker_config_need_approval():
    ch = [Change("M", ".github/workflows/ci.yml"), Change("M", "Dockerfile")]
    f = check_changes(ch, "", [".github/**", "Dockerfile"], TARGET)
    assert kinds(f) == [("config", False), ("config", False)]


def test_find_secrets_names_every_kind_found():
    assert find_secrets('AWS_KEY = "AKIAABCDEFGHIJKLMNOP"') == ["aws-access-key"]
    assert find_secrets('-----BEGIN RSA PRIVATE KEY-----\npassword = "hunter2hunter2"') == ["private-key", "credential-assignment"]
    assert find_secrets("no secrets here") == []
