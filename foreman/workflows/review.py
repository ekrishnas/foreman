"""The review: OpenSpec's `verify` method, run on a change before it is archived (DESIGN.md section 7.9).

A reviewer agent checks completeness, correctness and coherence of the implementation against the change's
artifacts and reports CRITICAL, WARNING and SUGGESTION issues. Code checks the report's shape and that every
`file:line` it cites exists. A CRITICAL issue fails validation, so the code-writing stages run again with the issue
as context; warnings and suggestions go into the pull request. OpenSpec has no review command; `verify` is its
review step, and it is a skill rather than a CLI command, so Foreman runs its method through its own agent.
"""
import re

from foreman.core.runner import Run, Stage, run_stage
from foreman.core.util import section
from foreman.workflows import nodes as n

SECTIONS = ["Summary", "CRITICAL", "WARNING", "SUGGESTION", "Final Assessment"]
REFERENCE = re.compile(r"`([\w./\\-]+\.\w+):(\d+)(?:-\d+)?`")


def bad_references(run: Run, text: str) -> list[str]:
    """`path:line` references in the report that point at nothing."""
    bad = []
    for path, line in REFERENCE.findall(text):
        f = run.repo.path / path
        if not f.is_file():
            bad.append(f"{path}:{line} (no such file)")
        elif int(line) > len(f.read_text(encoding="utf-8", errors="replace").splitlines()):
            bad.append(f"{path}:{line} (the file is shorter)")
    return bad


def issues(report: str, heading: str) -> list[str]:
    """The bullets under CRITICAL, WARNING or SUGGESTION, leaving out 'None'."""
    return [b for b in n.parse_bullets(section(report, heading)) if b.strip().rstrip(".").lower() != "none"]


def gate_review(run: Run, ctx: dict, text: str) -> str | None:
    missing = [h for h in SECTIONS if not section(text, h)]
    if missing:
        return f"the report is missing sections: {', '.join(missing)}"
    bad = bad_references(run, text)
    return f"these file references do not exist: {', '.join(bad)}" if bad else None


REVIEW = Stage("review", "reviewer", produces="review_report", gate=gate_review, commit=False)


def finish_with_review(run: Run, ctx: dict, failures: list[str], results: str) -> dict:
    """The tail of a validate node: once the deterministic checks pass, review; then pass, or loop back."""
    extra = {}
    if not failures:  # no point paying for a review of code that does not build or pass its tests
        report = run_stage(run, REVIEW, {**ctx, "validate_results": results})["ctx"]["review_report"]
        critical = issues(report, "CRITICAL")
        failures = [f"review: {c}" for c in critical]
        results += "\nreview: " + (f"{len(critical)} critical" if critical else "clean")
        extra = {"review": report}
    out = n.finish_validation(run, ctx, failures, results)
    out["ctx"] |= extra
    return out
