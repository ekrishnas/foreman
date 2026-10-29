"""doc_improvement: fix or extend documentation for code that already exists   (DESIGN.md section 5.5)

intake -> improve_docs -> validate -> pr_summary -> open_pr -> AWAIT_MERGE

The doc improver may only write under the docs path and README.md. Code checks that every relative link
in the markdown it touched points at a file that exists.
"""
import re

from langgraph.graph import END, START, StateGraph

from foreman.core.runner import Run, Stage
from foreman.workflows import nodes as n

EXTERNAL = ("http://", "https://", "mailto:", "#")


def broken_links(run: Run, pages: list[str]) -> list[str]:
    broken = []
    for path in pages:
        if not path.endswith(".md"):
            continue
        page = run.repo.path / path
        for link in re.findall(r"\]\(([^)\s]+)\)", page.read_text(encoding="utf-8", errors="replace")):
            target = link.split("#")[0]
            if link.startswith(EXTERNAL) or not target:
                continue
            base = run.repo.path if target.startswith("/") else page.parent
            if not (base / target.lstrip("/")).exists():
                broken.append(f"{path} -> {link}")
    return broken


def doc_files(run: Run, ctx: dict) -> list[str]:
    """The documentation this run's branch adds or changes: pages under docs/, and README.md."""
    changed = {c.path for c in [*run.repo.branch_changes(ctx["base_commit"]), *run.repo.changes()] if c.status != "D"}
    return n.branch_files(run, ctx, "docs") + (["README.md"] if "README.md" in changed else [])


def gate_docs(run: Run, ctx: dict, text: str) -> str | None:
    pages = doc_files(run, ctx)
    if not pages:
        return "the agent wrote no documentation (pages under docs/ or README.md)"
    broken = broken_links(run, pages)
    return f"broken relative links: {'; '.join(broken)}" if broken else None


IMPROVE = Stage("improve_docs", "doc_improver", produces="docs_improved", gate=gate_docs)


def validate(run: Run):
    def node(state: dict) -> dict:
        ctx = state["ctx"]
        failures, results = n.check_build_and_tests(run, min_tests=0)
        policy_failures, policy_results = n.check_branch_policy(run, ctx)
        return n.finish_validation(run, ctx, failures + policy_failures, "\n".join(results + policy_results))
    return node


def build(run: Run) -> StateGraph:
    g = StateGraph(n.State)
    g.add_node("intake", n.intake(run))
    g.add_node("improve_docs", n.agent_node(run, IMPROVE))
    g.add_node("validate", validate(run))
    g.add_node("pr_summary", n.agent_node(run, n.PR_SUMMARY))
    g.add_node("open_pr", n.open_pr(run))
    g.add_node("await_merge", n.await_merge(run))

    g.add_edge(START, "intake")
    g.add_edge("intake", "improve_docs")
    g.add_edge("improve_docs", "validate")
    g.add_conditional_edges("validate", lambda s: "pr_summary" if s["ctx"]["validated"] else "improve_docs",
                            ["pr_summary", "improve_docs"])
    g.add_edge("pr_summary", "open_pr")
    g.add_edge("open_pr", "await_merge")
    g.add_edge("await_merge", END)
    return g
