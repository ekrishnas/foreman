"""feature: intake -> requirements -> [clarify] -> proposal -> approve -> implement || tests || docs
-> validate -> archive_spec -> pr_summary -> open_pr -> await_merge -> observe   (DESIGN.md section 5.1)"""
from langgraph.graph import END, START, StateGraph

from foreman import nodes as n
from foreman.observe import observe

PARALLEL = ["implement", "tests", "docs"]


def build(run) -> StateGraph:
    g = StateGraph(n.State)
    g.add_node("intake", n.intake(run))
    g.add_node("requirements", n.agent_node(run, n.REQUIREMENTS))
    g.add_node("clarify", n.clarify(run))
    g.add_node("proposal", n.agent_node(run, n.PROPOSAL))
    g.add_node("approve_proposal", n.approve_proposal(run))
    g.add_node("implement", n.agent_node(run, n.IMPLEMENT))
    g.add_node("tests", n.agent_node(run, n.TESTS))
    g.add_node("docs", n.agent_node(run, n.DOCS))
    g.add_node("validate", n.validate_feature(run))
    g.add_node("archive_spec", n.archive_spec(run))
    g.add_node("pr_summary", n.agent_node(run, n.PR_SUMMARY))
    g.add_node("open_pr", n.open_pr(run))
    g.add_node("await_merge", n.await_merge(run))
    g.add_node("observe", observe(run))

    g.add_edge(START, "intake")
    g.add_edge("intake", "requirements")
    g.add_conditional_edges(
        "requirements", lambda s: "clarify" if s["ctx"].get("blocking_questions") else "proposal",
        ["clarify", "proposal"])
    g.add_edge("clarify", "proposal")
    g.add_edge("proposal", "approve_proposal")
    g.add_conditional_edges(
        "approve_proposal", lambda s: PARALLEL if s["ctx"]["proposal_approved"] else "proposal",
        [*PARALLEL, "proposal"])
    g.add_edge(PARALLEL, "validate")
    # A failed validation re-enters the fan-out: stages whose inputs did not change are skipped.
    g.add_conditional_edges(
        "validate", lambda s: "archive_spec" if s["ctx"]["validated"] else PARALLEL, ["archive_spec", *PARALLEL])
    g.add_edge("archive_spec", "pr_summary")
    g.add_edge("pr_summary", "open_pr")
    g.add_edge("open_pr", "await_merge")
    g.add_edge("await_merge", "observe")
    g.add_edge("observe", END)
    return g
