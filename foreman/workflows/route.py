"""The router: before any graph runs, a classifier agent reads the request and the issue and picks the graph.

The classifier proposes; code checks. `new_implementation` is refused when the repo already has application code, and
the types that change existing code are refused when it has none. A refused answer goes back to the agent.
"""
import re

from foreman.core.config import WORKFLOWS
from foreman.core.runner import Run, Stage, run_stage
from foreman.core.util import section
from foreman.workflows.nodes import has_source

NEEDS_EXISTING_CODE = ("existing_change", "bugfix", "test_improvement")


def parse_type(text: str) -> str:
    m = re.search(r"^Type:\s*([\w-]+)", text, re.I | re.M)
    return m.group(1).lower().replace("-", "_") if m else ""


def gate_classify(run: Run, ctx: dict, text: str) -> str | None:
    kind = parse_type(text)
    if kind not in WORKFLOWS:
        return f"the first line must be 'Type: <one of {', '.join(WORKFLOWS)}>'"
    if not section(text, "Rationale"):
        return "add a '## Rationale' section explaining the choice"
    source = has_source(run)
    if kind == "new_implementation" and source:
        return (f"the repository already has application code under {run.target.paths['src']}, so this cannot be "
                "new_implementation; pick existing_change (a new feature) or another type")
    if kind in NEEDS_EXISTING_CODE and not source:
        return f"the repository has no application code yet, so this cannot be {kind}; pick new_implementation"
    return None


CLASSIFY = Stage("classify", "classifier", produces="classification", gate=gate_classify, commit=False, sandbox=True)


def classify(run: Run) -> str:
    """Pick the graph for this run. Raises SafeStop if the classifier cannot produce an acceptable answer."""
    issue = run.github.get_issue(run.issue_id)
    ctx = {"request": run.request, "user_story": f"# {issue.title}\n\n{issue.body}".strip()}
    text = run_stage(run, CLASSIFY, ctx)["ctx"]["classification"]
    kind = parse_type(text)
    reason = section(text, "Rationale").splitlines()[0]
    run.workflow = kind
    run.audit("classify", "classified", type=kind, reason=reason)
    run.github.comment(run.issue_id, f"Foreman classified this request as **{kind}**: {reason}")
    return kind
