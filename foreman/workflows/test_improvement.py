"""test_improvement: add or strengthen tests for code that already exists, without changing the code   (DESIGN.md 5.4)

intake (records the test baseline) -> improve_tests -> validate -> pr_summary -> open_pr -> AWAIT_MERGE

The test improver may only write under the tests path, so source changes are blocked by policy.
"""
from langgraph.graph import END, START, StateGraph

from foreman.core.runner import Run, Stage
from foreman.core.util import count_tests, count_total, sh
from foreman.workflows import nodes as n


def gate_tests_improved(run: Run, ctx: dict, text: str) -> str | None:
    r = sh(run.target.commands["test"], cwd=run.repo.path)
    if r.code:
        return f"the suite fails with the new tests:\n{n.tail(r.out)}"
    if count_total(r.out) <= ctx["baseline_total"]:
        return "no new tests were added: the suite is the same size as before"
    if count_tests(r.out) < ctx["baseline_passed"]:
        return f"existing tests were lost: {count_tests(r.out)} pass, {ctx['baseline_passed']} passed before"
    return None


IMPROVE = Stage("improve_tests", "test_improver", produces="tests_improved", gate=gate_tests_improved)


def validate(run: Run):
    def node(state: dict) -> dict:
        ctx = state["ctx"]
        failures, results = n.check_build_and_tests(run, min_tests=ctx["baseline_passed"] + 1)
        policy_failures, policy_results = n.check_branch_policy(run, ctx)
        return n.finish_validation(run, ctx, failures + policy_failures, "\n".join(results + policy_results))
    return node


def build(run: Run) -> StateGraph:
    g = StateGraph(n.State)
    g.add_node("intake", n.intake_with_baseline(run))
    g.add_node("improve_tests", n.agent_node(run, IMPROVE))
    g.add_node("validate", validate(run))
    g.add_node("pr_summary", n.agent_node(run, n.PR_SUMMARY))
    g.add_node("open_pr", n.open_pr(run))
    g.add_node("await_merge", n.await_merge(run))

    g.add_edge(START, "intake")
    g.add_edge("intake", "improve_tests")
    g.add_edge("improve_tests", "validate")
    g.add_conditional_edges("validate", lambda s: "pr_summary" if s["ctx"]["validated"] else "improve_tests",
                            ["pr_summary", "improve_tests"])
    g.add_edge("pr_summary", "open_pr")
    g.add_edge("open_pr", "await_merge")
    g.add_edge("await_merge", END)
    return g
