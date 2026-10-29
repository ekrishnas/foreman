import re
from dataclasses import dataclass

from foreman.config import Target
from foreman.gitops import Change
from foreman.util import glob_match

SECRET_PATTERNS = {
    "aws-access-key": r"AKIA[0-9A-Z]{16}",
    "private-key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "github-token": r"gh[pousr]_[A-Za-z0-9]{36,}",
    "credential-assignment": r"""(?i)(api[_-]?key|secret|token|passw(?:or)?d)\s*[:=]\s*['"][^'"\s]{8,}['"]""",
}
CONFIG_PATTERNS = [".github/**", "Dockerfile*", "**/Dockerfile*", "docker-compose*.yml", "**/docker-compose*.yml"]


@dataclass(frozen=True)
class Finding:
    kind: str  # path | secret | dependency | migration | deleted_test | config
    detail: str
    blocking: bool  # blocking fails the stage; otherwise a human must approve


def resolve_writable(patterns: list[str], target: Target, **values: str) -> list[str]:
    mapping = {f"paths.{k}": v for k, v in target.paths.items()} | values
    out = []
    for p in patterns:
        for key, val in mapping.items():
            p = p.replace("{" + key + "}", str(val))
        out.append(p)
    return out


def _any(path: str, patterns: list[str]) -> bool:
    return any(glob_match(path, p) for p in patterns)


def find_secrets(text: str) -> list[str]:
    """Names of the secret patterns that match `text`."""
    return [name for name, pattern in SECRET_PATTERNS.items() if re.search(pattern, text)]


def check_changes(changes: list[Change], added_text: str, writable: list[str], target: Target) -> list[Finding]:
    findings = []
    for c in changes:
        if not _any(c.path, writable):
            findings.append(Finding("path", f"{c.path} is outside the agent's writable paths", True))
    for name in find_secrets(added_text):
        findings.append(Finding("secret", f"secret ({name}) detected in added content", True))
    for c in changes:
        if _any(c.path, target.dependency_manifests):
            findings.append(Finding("dependency", f"dependency manifest changed: {c.path}", False))
        if _any(c.path, target.protected):
            findings.append(Finding("migration", f"protected path changed: {c.path}", False))
        if c.status == "D" and glob_match(c.path, target.paths["tests"]):
            findings.append(Finding("deleted_test", f"test file deleted: {c.path}", False))
        if _any(c.path, CONFIG_PATTERNS):
            findings.append(Finding("config", f"CI or config file changed: {c.path}", False))
    return findings
