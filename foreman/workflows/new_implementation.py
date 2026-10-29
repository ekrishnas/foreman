"""new_implementation: a new feature in a repo that has no application code yet   (DESIGN.md section 5.1)

intake -> requirements -> [clarify if blocking questions] -> proposal -> APPROVE
  -> implement || tests || docs -> validate (+ review) -> archive_spec -> pr_summary -> open_pr -> AWAIT_MERGE

existing_change.py reuses this graph and adds a baseline (when the product has no specs yet) and an
impact-analysis step in front of `requirements`.
"""
from langgraph.graph import END, START, StateGraph

from foreman.core.runner import Run, Stage
from foreman.core.util import section
from foreman.workflows import baseline, review
from foreman.workflows import nodes as n

PARALLEL = ["implement", "tests", "docs"]


# ---------- agent stages ----------

def gate_requirements(run: Run, ctx: dict, text: str) -> str | None:
    missing = [h for h in ("Normalized requirement", "Acceptance criteria") if not section(text, h)]
    return f"requirements output is missing sections: {', '.join(missing)}" if missing else None


def after_requirements(run: Run, ctx: dict, text: str) -> dict:
    return {
        "acceptance_criteria": section(text, "Acceptance criteria"),
        "blocking_questions": n.parse_bullets(section(text, "Blocking questions")),
    }


def gate_proposal(run: Run, ctx: dict, text: str) -> str | None:
    ok, out = run.spec.validate(ctx["change_id"])
    if not ok:
        return f"openspec validate --strict failed:\n{out}"
    design = (n.change_dir(run, ctx) / "design.md").read_text(encoding="utf-8")
    if not section(design, "API contract"):
        return "design.md must contain an '## API contract' section"
    return None


def after_proposal(run: Run, ctx: dict, text: str) -> dict:
    d = n.change_dir(run, ctx)
    design = (d / "design.md").read_text(encoding="utf-8")
    return {
        "design": design,
        "tasks": (d / "tasks.md").read_text(encoding="utf-8"),
        "contract": section(design, "API contract"),
        "spec_scenarios": "\n\n".join(p.read_text(encoding="utf-8") for p in sorted((d / "specs").glob("*/spec.md"))),
        "change_summary": (d / "proposal.md").read_text(encoding="utf-8"),
    }


REQUIREMENTS = Stage("requirements", "requirements_analyst", produces="requirements",
                     gate=gate_requirements, after=after_requirements, commit=False)
PROPOSAL = Stage("proposal", "architect", produces="proposal_summary", gate=gate_proposal, after=after_proposal)
IMPLEMENT = Stage("implement", "implementer", produces="implemented", gate=n.gate_build_and_observability,
                  after=n.after_code)
TESTS = Stage("tests", "test_writer", produces="tests_written", gate=n.gate_wrote("tests"), after=n.tick_tasks)
DOCS = Stage("docs", "doc_writer", produces="docs_written", gate=n.gate_wrote("docs"), after=n.tick_tasks)


def validate(run: Run):
    def node(state: dict) -> dict:
        ctx = state["ctx"]
        failures, results = n.validate_feature_checks(run, ctx)
        return review.finish_with_review(run, ctx, failures, results)
    return node


# ---------- the graph ----------

def build(run: Run, impact: Stage | None = None, with_baseline: bool = False) -> StateGraph:
    """`impact` and `with_baseline` are existing_change's extra first stages."""
    g = StateGraph(n.State)
    g.add_node("intake", n.intake(run))
    g.add_node("requirements", n.agent_node(run, REQUIREMENTS))
    g.add_node("clarify", n.clarify(run))
    g.add_node("proposal", n.agent_node(run, PROPOSAL))
    g.add_node("approve_proposal", n.approve_proposal(run))
    g.add_node("implement", n.agent_node(run, IMPLEMENT))
    g.add_node("tests", n.agent_node(run, TESTS))
    g.add_node("docs", n.agent_node(run, DOCS))
    g.add_node("validate", validate(run))
    g.add_node("archive_spec", n.archive_spec(run))
    g.add_node("pr_summary", n.agent_node(run, n.PR_SUMMARY))
    g.add_node("open_pr", n.open_pr(run))
    g.add_node("await_merge", n.await_merge(run))

    g.add_edge(START, "intake")
    first = "requirements"
    if impact:
        g.add_node("impact", n.agent_node(run, impact))
        g.add_edge("impact", "requirements")
        first = "impact"
    if with_baseline:
        baseline.wire(g, run, "intake", first)
    else:
        g.add_edge("intake", first)
    g.add_conditional_edges(
        "requirements", lambda s: "clarify" if s["ctx"].get("blocking_questions") else "proposal",
        ["clarify", "proposal"])
    g.add_edge("clarify", "proposal")
    g.add_edge("proposal", "approve_proposal")
    g.add_conditional_edges(
        "approve_proposal", lambda s: PARALLEL if s["ctx"]["proposal_approved"] else "proposal",
        [*PARALLEL, "proposal"])
    g.add_edge(PARALLEL, "validate")
    # A failed validation sends the three parallel stages round again, with the failure as context.
    g.add_conditional_edges(
        "validate", lambda s: "archive_spec" if s["ctx"]["validated"] else PARALLEL, ["archive_spec", *PARALLEL])
    g.add_edge("archive_spec", "pr_summary")
    g.add_edge("pr_summary", "open_pr")
    g.add_edge("open_pr", "await_merge")
    g.add_edge("await_merge", END)
    return g
