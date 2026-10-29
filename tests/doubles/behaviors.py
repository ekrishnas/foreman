"""Deterministic stand-ins for each agent. Like a real agent they read their prompt and write files."""
import re

from tests.doubles.target import SHORTENER, SHORTENER_REGRESSED, TESTS
from tests.doubles.worker import behavior

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

OBSERVABILITY = (
    "\n## Observability\n"
    "- Log location: logs/app.log\n"
    "- Success: `INFO shortened url code=`\n"
    "- Errors: `ERROR`, `Traceback`\n"
)


def prompt_section(prompt: str, key: str) -> str:
    m = re.search(rf"^## {key}\n(.*?)(?=^## [a-z_]+\n|\Z)", prompt, re.M | re.S)
    return m.group(1).strip() if m else ""


def put(req, rel: str, text: str) -> None:
    p = req.cwd / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


# ---------- router ----------

@behavior("classifier")
def classifier(req, fault):
    request = prompt_section(req.prompt, "request").lower()
    story = prompt_section(req.prompt, "user_story").lower()
    title = story.splitlines()[0] if story else ""
    has_code = "src/" in prompt_section(req.prompt, "repo_tree")
    if fault == "bad_format":
        return "I think this is a feature."
    if fault == "wrong_type":
        kind = "new_implementation"
    elif "logs" in request and ("deployed" in request or "check" in request):
        kind = "observe"
    elif "steps to reproduce" in story or ("expected" in story and "actual" in story):
        kind = "bugfix"
    elif "tests" in title:
        kind = "test_improvement"
    elif "document" in title:
        kind = "doc_improvement"
    elif "## questions" in story:
        kind = "spike"
    else:
        kind = "existing_change" if has_code else "new_implementation"
    return f"Type: {kind}\n## Rationale\nThe request and the issue point at {kind}."


# ---------- baseline, review ----------

@behavior("baseliner")
def baseliner(req, fault):
    change = re.search(r"- Change id: (\S+)", req.prompt).group(1)
    base = f"openspec/changes/baseline-{change}"
    put(req, f"{base}/proposal.md", (
        "## Why\nThe product has code but no specs, so later changes have nothing to build on or modify.\n\n"
        "## What Changes\n- Record the current behaviour of the shortener as requirements\n\n"
        "## Impact\n- No code changes. Nothing else exists to describe.\n"))
    put(req, f"{base}/design.md", "## Context\nBehaviour read from src/shortener.py.\n")
    put(req, f"{base}/tasks.md", "## 1. Baseline\n- [x] 1.1 Describe the current behaviour\n")
    spec = SPEC.replace("## ADDED Requirements", "## MODIFIED Requirements") if fault == "modified" else SPEC
    put(req, f"{base}/specs/shortener/spec.md", spec)
    return "Described the shortener's current behaviour in two requirements. Left out: nothing."


@behavior("reviewer")
def reviewer(req, fault):
    critical, warning, final = "- None", "- None", "All checks passed. Ready for archive."
    if fault == "critical":
        critical = "- Requirement not found: Count clicks: implement it, `src/shortener.py:1`"
        final = "1 critical issue(s) found. Fix before archiving."
    elif fault == "bad_reference":
        warning = "- Scenario not covered: check `src/nope.py:3`"
    elif fault == "warning":
        warning = "- Scenario not covered: Stats stay current: add a test, `tests/test_shortener.py:5`"
        final = "No critical issues. 1 warning(s) to consider. Ready for archive (with noted improvements)."
    return (
        "## Verification Report: change\n\n### Summary\n| Dimension | Status |\n|---|---|\n"
        "| Completeness | all tasks done |\n| Correctness | requirements covered |\n| Coherence | Followed |\n\n"
        f"### CRITICAL\n{critical}\n\n### WARNING\n{warning}\n\n### SUGGESTION\n- None\n\n### Final Assessment\n{final}\n")


# ---------- new_implementation and existing_change ----------

@behavior("impact_analyst")
def impact_analyst(req, fault):
    path = "src/nope.py" if fault == "missing_file" else "src/shortener.py"
    return (f"## Impacted files\n- {path}: shorten, resolve, clicks\n"
            "## Modules and APIs\n- Shortener: the public API\n"
            "## Data flows\n- url in, short code out; clicks counted on resolve\n"
            "## Risks\n- changing resolve could break click counting\n")


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
    changed = SHORTENER + "\n# changed for this issue\n" if (req.cwd / "src/shortener.py").exists() else SHORTENER
    put(req, "src/shortener.py", BROKEN_SHORTENER if fault == "break_tests" else changed)
    if fault == "no_observability":
        return "Implemented the shortener.\nDONE: 1.1"
    return "Implemented the shortener.\nDONE: 1.1\n" + OBSERVABILITY


@behavior("test_writer")
def test_writer(req, fault):
    # a repo that already has its tests gets a new file, as a real agent adds tests for the change
    name = "tests/test_change.py" if (req.cwd / "tests/test_shortener.py").exists() else "tests/test_shortener.py"
    put(req, name, TESTS)
    return "Wrote tests from the contract.\nDONE: 2.1"


@behavior("doc_writer")
def doc_writer(req, fault):
    put(req, "docs/api.md", "# API\n\n" + prompt_section(req.prompt, "contract") + "\n")
    return "Documented the API.\nDONE: 3.1"


@behavior("pr_summarizer")
def pr_summarizer(req, fault):
    summary = prompt_section(req.prompt, "change_summary").replace("\n", " ")[:200]
    return f"Implements the approved OpenSpec change: {summary}"


# ---------- bugfix ----------

MODIFIED_SPEC = """## MODIFIED Requirements
### Requirement: Count clicks
The system SHALL count one click each time a short code is resolved and SHALL report the current count, never a stale one.

#### Scenario: Resolve twice
- **WHEN** a code is resolved twice
- **THEN** its click count is 2

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
    return "Fixed the stale counter.\nDONE: 2.1\n" + OBSERVABILITY


@behavior("rca_writer")
def rca_writer(req, fault):
    issue = re.search(r"- Issue number: (\d+)", req.prompt).group(1)
    put(req, f"docs/rca/{issue}.md", RCA)
    return "Wrote the RCA."


# ---------- test_improvement and doc_improvement ----------

MORE_TESTS = '''import pytest
from shortener import Shortener


def test_a_new_code_starts_with_no_clicks():
    s = Shortener()
    assert s.clicks(s.shorten("https://example.com/a")) == 0


def test_codes_count_clicks_independently():
    s = Shortener()
    a, b = s.shorten("https://example.com/a"), s.shorten("https://example.com/b")
    s.resolve(a)
    assert (s.clicks(a), s.clicks(b)) == (1, 0)


def test_clicks_for_an_unknown_code_raise_key_error():
    with pytest.raises(KeyError):
        Shortener().clicks("nope")
'''


@behavior("test_improver")
def test_improver(req, fault):
    if fault == "no_new_tests":
        return "Looked at the tests and found nothing to add."
    put(req, "tests/test_shortener_more.py", MORE_TESTS)
    return "Covered click counting edge cases."


@behavior("doc_improver")
def doc_improver(req, fault):
    link = "missing.md" if fault == "broken_link" else "api.md"
    put(req, "docs/usage.md", f"# Using the shortener\n\nSee the [API reference]({link}) for the full contract.\n")
    return "Added a usage page."


# ---------- spike ----------

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

## Draft user story
### Track clicks per day for each short link

As a link owner I want to see clicks per day for each of my links, so that I can tell which links are used.

#### Acceptance criteria
- Clicks are counted per link per day
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
    put(req, f"docs/spikes/{run_id}/analysis.md", doc)
    return "Wrote the analysis."


# ---------- observe ----------

@behavior("observer")
def observer(req, fault):
    lines = [ln for ln in prompt_section(req.prompt, "log_excerpt").splitlines() if ln.strip()]
    errors = [ln for ln in lines if "ERROR" in ln or "Traceback" in ln]
    successes = [ln for ln in lines if "shortened url" in ln]
    if fault == "invented_evidence":
        evidence, verdict = ["`INFO this line is not in the log`"], "working"
    elif errors:
        verdict, evidence = "failing", [f"- `{errors[0]}`: an error was logged"]
    elif successes:
        verdict, evidence = "working", [f"- `{successes[0]}`: the feature handled a request"]
    else:
        verdict, evidence = "inconclusive", ["- (no success or error lines in the excerpt)"]
    if fault == "invented_evidence":
        evidence = [f"- {evidence[0]}: shows it works"]
    return f"Verdict: {verdict}\n## Summary\nChecked {len(lines)} log lines.\n## Evidence\n" + "\n".join(evidence)
