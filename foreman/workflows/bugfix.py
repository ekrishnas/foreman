"""bugfix: intake -> triage -> approve -> reproduce -> fix -> validate -> rca -> archive_spec
-> pr_summary -> open_pr -> await_merge   (DESIGN.md section 5.2)"""
from langgraph.graph import END, START, StateGraph

from foreman import nodes as n


def build(run) -> StateGraph:
    g = StateGraph(n.State)
    g.add_node("intake", n.bugfix_intake(run))
    g.add_node("triage", n.agent_node(run, n.TRIAGE))
    g.add_node("approve_proposal", n.approve_proposal(run))
    g.add_node("reproduce", n.agent_node(run, n.REPRODUCE))
    g.add_node("fix", n.agent_node(run, n.FIX))
    g.add_node("validate", n.validate_bugfix(run))
    g.add_node("rca", n.agent_node(run, n.RCA))
    g.add_node("archive_spec", n.archive_spec(run))
    g.add_node("pr_summary", n.agent_node(run, n.PR_SUMMARY))
    g.add_node("open_pr", n.open_pr(run))
    g.add_node("await_merge", n.await_merge(run))

    g.add_edge(START, "intake")
    g.add_edge("intake", "triage")
    g.add_edge("triage", "approve_proposal")
    g.add_conditional_edges(
        "approve_proposal", lambda s: "reproduce" if s["ctx"]["proposal_approved"] else "triage",
        ["reproduce", "triage"])
    g.add_edge("reproduce", "fix")
    g.add_edge("fix", "validate")
    g.add_conditional_edges("validate", lambda s: "rca" if s["ctx"]["validated"] else "fix", ["rca", "fix"])
    g.add_edge("rca", "archive_spec")
    g.add_edge("archive_spec", "pr_summary")
    g.add_edge("pr_summary", "open_pr")
    g.add_edge("open_pr", "await_merge")
    g.add_edge("await_merge", END)
    return g
