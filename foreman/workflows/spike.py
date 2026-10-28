"""spike: intake -> clarify -> frame -> explore(option 1 || 2 || 3) -> analysis -> open_pr -> await_merge
-> handoff   (DESIGN.md section 5.3)"""
from langgraph.graph import END, START, StateGraph

from foreman import nodes as n


def build(run) -> StateGraph:
    g = StateGraph(n.State)
    g.add_node("intake", n.spike_intake(run))
    g.add_node("clarify", n.clarify(run))
    g.add_node("frame", n.agent_node(run, n.FRAME))
    g.add_node("explore", n.explore_node(run))
    g.add_node("analysis", n.analysis_node(run))
    g.add_node("prune_scratch", n.prune_scratch(run))
    g.add_node("open_pr", n.open_pr(run))
    g.add_node("await_merge", n.await_merge(run))
    g.add_node("handoff", n.handoff(run))

    g.add_edge(START, "intake")
    g.add_edge("intake", "clarify")
    g.add_edge("clarify", "frame")
    g.add_conditional_edges("frame", n.fan_out_options, ["explore"])  # dynamic fan-out, one explorer per option
    g.add_edge("explore", "analysis")
    g.add_edge("analysis", "prune_scratch")
    g.add_edge("prune_scratch", "open_pr")
    g.add_edge("open_pr", "await_merge")
    g.add_edge("await_merge", "handoff")
    g.add_edge("handoff", END)
    return g
