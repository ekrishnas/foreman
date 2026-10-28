"""A throwaway Python target repo for fake-mode runs and tests."""
from pathlib import Path

from foreman.gitops import GitRepo

SMOKE = '''import json
import os

print(json.dumps({
    "requests": 100,
    "errors": int(os.environ.get("FOREMAN_FAKE_ERRORS", "0")),
    "p95_ms": int(os.environ.get("FOREMAN_FAKE_P95", "50")),
    "log": "fake smoke run",
}))
'''

SCAFFOLD = {
    "README.md": "# Fake target\n\nThrowaway project driven by Foreman's fake mode.\n",
    "pytest.ini": "[pytest]\npythonpath = src\ntestpaths = tests\n",
    ".gitignore": "__pycache__/\n.pytest_cache/\n*.pyc\n",
    "scripts/smoke.py": SMOKE,
}

SHORTENER = '''"""In-memory URL shortener (fake target)."""
import secrets


class Shortener:
    def __init__(self):
        self._urls = {}
        self._clicks = {}

    def shorten(self, url):
        if not url.startswith(("http://", "https://")):
            raise ValueError("invalid url")
        code = secrets.token_urlsafe(5)
        self._urls[code] = url
        self._clicks[code] = 0
        return code

    def resolve(self, code):
        url = self._urls[code]
        self._clicks[code] += 1
        return url

    def clicks(self, code):
        return self._clicks[code]
'''

TESTS = '''import pytest
from shortener import Shortener


def test_shorten_then_resolve_round_trips():
    s = Shortener()
    code = s.shorten("https://example.com/a")
    assert s.resolve(code) == "https://example.com/a"


def test_each_resolve_counts_one_click():
    s = Shortener()
    code = s.shorten("https://example.com/a")
    s.resolve(code)
    s.resolve(code)
    assert s.clicks(code) == 2


def test_unknown_code_raises_key_error():
    with pytest.raises(KeyError):
        Shortener().resolve("nope")


def test_rejects_invalid_url():
    with pytest.raises(ValueError):
        Shortener().shorten("not-a-url")
'''


SHORTENER_REGRESSED = SHORTENER.replace(
    "        self._clicks = {}\n", "        self._clicks = {}\n        self._cache = {}\n"
).replace(
    "    def clicks(self, code):\n        return self._clicks[code]\n",
    "    def clicks(self, code):\n        # performance: cache the counter after the first read\n"
    "        if code not in self._cache:\n            self._cache[code] = self._clicks[code]\n"
    "        return self._cache[code]\n",
)

V1_SPEC = """# shortener Specification

## Requirements
### Requirement: Shorten URL
The system SHALL return a short code for a valid URL.

#### Scenario: Valid URL
- **WHEN** a valid URL is submitted
- **THEN** a short code is returned

### Requirement: Count clicks
The system SHALL count one click each time a short code is resolved.

#### Scenario: Resolve twice
- **WHEN** a code is resolved twice
- **THEN** its click count is 2
"""

V1_FILES = {
    "src/shortener.py": SHORTENER,
    "tests/test_shortener.py": TESTS,
    "docs/api.md": "# API\n\n- shorten(url) -> code\n- resolve(code) -> url\n- clicks(code) -> int\n",
    "openspec/specs/shortener/spec.md": V1_SPEC,
}


def write_files(root: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def make_fake_target(path: Path, start_ref: str = "main") -> GitRepo:
    """Create the target repo; `start_ref` picks the fixture (main = empty project)."""
    if start_ref not in ("main", "v1", "v1-regressed"):
        raise ValueError(f"unknown fake start_ref {start_ref!r}")
    repo = GitRepo.init(path)
    write_files(repo.path, SCAFFOLD)
    repo.commit("scaffold")
    if start_ref != "main":
        write_files(repo.path, V1_FILES)
        repo.commit("v1: url shortener")
        repo.git("tag", "v1")
    if start_ref == "v1-regressed":
        write_files(repo.path, {"src/shortener.py": SHORTENER_REGRESSED})
        repo.commit("performance: cache the click counter")
        repo.git("tag", "v1-regressed")
    return repo
