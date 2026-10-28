"""Deterministic stand-ins for each agent. Like a real agent they read their prompt and write files."""
import re

from foreman.fake import behavior
from foreman.fake_target import SHORTENER, TESTS

BROKEN_SHORTENER = SHORTENER.replace("        self._clicks[code] += 1\n", "")

SPEC = """## ADDED Requirements
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
SPEC_WITHOUT_SCENARIO = "## ADDED Requirements\n### Requirement: Shorten URL\nThe system SHALL return a short code.\n"


def prompt_section(prompt: str, key: str) -> str:
    m = re.search(rf"^## {key}\n(.*?)(?=^## [a-z_]+\n|\Z)", prompt, re.M | re.S)
    return m.group(1).strip() if m else ""


def put(req, rel: str, text: str) -> None:
    p = req.cwd / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


@behavior("requirements_analyst")
def requirements_analyst(req, fault):
    story = prompt_section(req.prompt, "user_story")
    title = story.splitlines()[0].lstrip("# ").strip() if story else "Feature"
    out = [
        "## Normalized requirement", f"{title}: shorten URLs, resolve short codes and count clicks.", "",
        "## Acceptance criteria",
        "- Shortening a valid URL returns a short code",
        "- Resolving a code returns the original URL and counts one click",
        "- Unknown codes and invalid URLs are rejected", "",
    ]
    if "src/" in prompt_section(req.prompt, "repo_tree"):
        out += ["## Impacted files", "- src/shortener.py: shorten, resolve, clicks", ""]
    questions = re.findall(r"^QUESTION:\s*(.+)$", story, re.M)
    if questions:
        out += ["## Blocking questions", *[f"- {q}" for q in questions], ""]
    return "\n".join(out)


@behavior("architect")
def architect(req, fault):
    change = re.search(r"- Change id: (\S+)", req.prompt).group(1)
    notes = prompt_section(req.prompt, "reviewer_notes")
    extra = f"\n\nAddressed review feedback: {notes}" if notes else ""
    base = f"openspec/changes/{change}"
    put(req, f"{base}/proposal.md", f"## Why\nPeople need short links.\n\n## What Changes\n- Add a URL shortener{extra}\n\n## Impact\n- New src/ and tests/\n")
    put(req, f"{base}/design.md", (
        f"## Context\nA small in-memory shortener.{extra}\n\n"
        "## API contract\n"
        "- `Shortener.shorten(url) -> code`; raises ValueError for invalid URLs\n"
        "- `Shortener.resolve(code) -> url`; counts one click; raises KeyError for unknown codes\n"
        "- `Shortener.clicks(code) -> int`\n"))
    put(req, f"{base}/tasks.md", (
        "## 1. Implementation\n- [ ] 1.1 Implement shorten, resolve and click counting\n\n"
        "## 2. Tests\n- [ ] 2.1 Test shorten, resolve and click counting\n\n"
        "## 3. Documentation\n- [ ] 3.1 Document the API contract\n"))
    put(req, f"{base}/specs/shortener/spec.md", SPEC_WITHOUT_SCENARIO if fault == "break_spec" else SPEC)
    return f"Proposed change {change}."


@behavior("implementer")
def implementer(req, fault):
    put(req, "src/shortener.py", BROKEN_SHORTENER if fault == "break_tests" else SHORTENER)
    return "Implemented the shortener.\nDONE: 1.1"


@behavior("test_writer")
def test_writer(req, fault):
    put(req, "tests/test_shortener.py", TESTS)
    return "Wrote tests from the contract.\nDONE: 2.1"


@behavior("doc_writer")
def doc_writer(req, fault):
    put(req, "docs/api.md", "# API\n\n" + prompt_section(req.prompt, "contract") + "\n")
    return "Documented the API.\nDONE: 3.1"


@behavior("pr_summarizer")
def pr_summarizer(req, fault):
    summary = prompt_section(req.prompt, "change_summary").replace("\n", " ")[:200]
    return f"Implements the approved OpenSpec change: {summary}"
