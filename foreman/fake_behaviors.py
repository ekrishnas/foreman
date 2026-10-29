"""Deterministic stand-ins for each agent. Like a real agent they read their prompt and write files."""
import re

from foreman.commits import fallback
from foreman.fake import behavior
from foreman.fake_target import SHORTENER, SHORTENER_REGRESSED, TESTS

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
    p.write_text(text, encoding="utf-8", newline="\n")


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
    put(req, "openspec/docs/api.md", "# API\n\n" + prompt_section(req.prompt, "contract") + "\n")
    return "Documented the API.\nDONE: 3.1"


@behavior("pr_summarizer")
def pr_summarizer(req, fault):
    summary = prompt_section(req.prompt, "change_summary").replace("\n", " ")[:200]
    return f"Implements the approved OpenSpec change: {summary}"


MODIFIED_SPEC = """## MODIFIED Requirements
### Requirement: Count clicks
The system SHALL count one click each time a short code is resolved and SHALL report the current count, never a stale one.

#### Scenario: Stats stay current
- **WHEN** a code is resolved, its stats are read, and the code is resolved again
- **THEN** the second stats read reports 2 clicks
"""

REPRO_TEST = """from shortener import Shortener


def test_stats_reflect_clicks_made_after_a_previous_read():
    s = Shortener()
    code = s.shorten("https://example.com/a")
    s.resolve(code)
    assert s.clicks(code) == 1
    s.resolve(code)
    assert s.clicks(code) == 2
"""

RCA = """# RCA: stale click counts

## Summary
The stats endpoint reported stale click counts after the counter had been read once.

## Impact
Anyone watching a link's statistics saw numbers that stopped moving.

## Timeline
- A performance refactor cached the counter on first read.
- A user reported stale counts.
- The bug was reproduced with a failing test and fixed.

## Root cause
`Shortener.clicks` cached the counter on first read and never invalidated the cache.

## Why it was missed
No test read the counter, resolved again, and read it a second time.

## Fix
`clicks` now reads the live counter.

## Prevention
The new test covers reading stats between clicks; performance changes need a read-after-write test.
"""


@behavior("triager")
def triager(req, fault):
    change = re.search(r"- Change id: (\S+)", req.prompt).group(1)
    notes = prompt_section(req.prompt, "reviewer_notes")
    extra = f"\n- Reviewer asked: {notes}" if notes else ""
    base = f"openspec/changes/{change}"
    put(req, f"{base}/proposal.md", (
        "## Why\nThe stats endpoint shows stale click counts once the counter has been read.\n\n"
        f"## What Changes\n- Read the live counter instead of a cached copy{extra}\n\n"
        "## Impact\n- Affected module: src/shortener.py (`Shortener.clicks`)\n\n"
        "## Root cause hypothesis\nThe counter is cached on first read and never invalidated.\n"))
    put(req, f"{base}/design.md", f"## Context\nBug report triaged from the issue.{extra}\n")
    put(req, f"{base}/tasks.md", (
        "## 1. Reproduce\n- [ ] 1.1 Add a failing test for stale counts\n\n"
        "## 2. Fix\n- [ ] 2.1 Read the live counter in clicks()\n"))
    put(req, f"{base}/specs/shortener/spec.md", MODIFIED_SPEC)
    return f"Triaged {change}."


@behavior("reproducer")
def reproducer(req, fault):
    passing = "def test_placeholder():\n    assert True\n"
    put(req, "tests/test_click_counts.py", passing if fault == "passing_test" else REPRO_TEST)
    return "Added a test for the bug.\nDONE: 1.1"


@behavior("fixer")
def fixer(req, fault):
    put(req, "src/shortener.py", SHORTENER_REGRESSED if fault == "break_tests" else SHORTENER)
    if fault == "delete_test":
        (req.cwd / "tests/test_click_counts.py").unlink(missing_ok=True)
    return "Fixed the stale counter.\nDONE: 2.1"


@behavior("rca_writer")
def rca_writer(req, fault):
    issue = re.search(r"- Issue number: (\d+)", req.prompt).group(1)
    put(req, f"openspec/docs/rca/{issue}.md", RCA)
    return "Wrote the RCA."


ANALYSIS_DOC = """# Link analytics spike

## Context
The team wants to understand how short links are used.

## Assumptions
{assumptions}

## Options
- option-1: nightly batch job
- option-2: event stream
- option-3: Postgres counters with rollups

## Trade-off matrix
| Option | Complexity | Cost | Performance | Risk | Operability |
|---|---|---|---|---|---|
| option-1 | low | low | hourly lag | low | easy |
| option-2 | high | high | real time | medium | hard |
| option-3 | medium | low | near real time | low | easy |

## Recommendation
Build option-3: Postgres counters with periodic rollups. It needs no new infrastructure.

## Open questions
- How long must raw clicks be retained?

## Proposed issues
### Count clicks per day for each short link

As a link owner I want clicks counted per link per day, so that I can tell which links are used.

#### Acceptance criteria
- Clicks are counted per link per day

### Show daily click counts to the link owner

Depends on: Count clicks per day for each short link

As a link owner I want to see the daily counts of my links.

#### Acceptance criteria
- The current day is at most one hour stale
"""


@behavior("framer")
def framer(req, fault):
    assumed = prompt_section(req.prompt, "assumptions")
    options = [
        ("Nightly batch job", "Aggregate the click table once a night."),
        ("Event stream", "Publish every click to a stream and aggregate downstream."),
        ("Postgres counters with rollups", "Increment counters in Postgres and roll them up hourly."),
    ]
    if fault == "too_many_options":
        options.append(("A fourth idea", "One option too many."))
    out = ["## Context", "The team wants link analytics but the scope is unclear."]
    if assumed:
        out += ["", "## Assumptions", assumed]
    out += ["", "## Options"]
    for i, (title, body) in enumerate(options, 1):
        out += [f"### Option {i}: {title}", body, ""]
    out += ["## Constraints", "- Must run on the existing Postgres", "- No new infrastructure this quarter"]
    return "\n".join(out)


@behavior("explorer")
def explorer(req, fault):
    run_id, option = re.search(r"spikes/([^/\s]+)/([^/\s]+)/\*\*", req.prompt).groups()
    title = prompt_section(req.prompt, "option").splitlines()[0]
    put(req, f"spikes/{run_id}/{option}/notes.md", f"# Notes for {option}: {title}\n\n- Complexity, cost and risk assessed.\n")
    if fault == "cross_write":
        put(req, f"spikes/{run_id}/other-option/stolen.md", "outside my directory\n")
    return f"Explored {option}."


@behavior("analyst")
def analyst(req, fault):
    run_id = re.search(r"docs/spikes/([^/\s]+)/\*\*", req.prompt).group(1)
    doc = ANALYSIS_DOC.format(assumptions="- Hourly updates are acceptable")
    if fault == "missing_matrix":
        doc = re.sub(r"## Trade-off matrix.*?(?=## Recommendation)", "", doc, flags=re.S)
    if fault == "no_issues":
        doc = doc.split("## Proposed issues")[0] + "## Proposed issues\nTBD\n"
    put(req, f"openspec/docs/spikes/{run_id}/analysis.md", doc)
    return "Wrote the analysis."


@behavior("observer")
def observer(req, fault):
    metrics = prompt_section(req.prompt, "metrics")
    return (f"## Anomalies\n- The smoke run breached the SLO: {metrics}\n\n"
            "## Probable causes\n- Probable cause: a latency regression on the resolve path.\n\n"
            "## Suggested next step\n- Keep the revert PR ready and reproduce under load.")


@behavior("committer")
def committer(req, fault):
    stage = prompt_section(req.prompt, "commit_stage")
    files = re.findall(r"^\+\+\+ b/(.+)$", prompt_section(req.prompt, "commit_diff"), re.M)
    bullets = [f"- update {path}"[:72] for path in files[:5]]
    return "\n".join([fallback(stage), "", f"Record the output of the {stage} stage.", "", *bullets])
