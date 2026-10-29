"""bugfix: something that worked is broken   (DESIGN.md section 5.3)

intake -> [baseline, when the product has no specs yet] -> triage -> APPROVE -> reproduce (the new test must fail)
  -> fix -> validate (+ review) -> rca -> archive_spec -> pr_summary -> open_pr -> AWAIT_MERGE
"""
from langgraph.graph import END, START, StateGraph

from foreman.core.runner import Run, Stage
from foreman.core.util import count_failed, count_tests, count_total, glob_match, section, sh
from foreman.workflows import baseline, review
from foreman.workflows import nodes as n

RCA_SECTIONS = ["Summary", "Impact", "Timeline", "Root cause", "Why it was missed", "Fix", "Prevention"]


def intake(run: Run):
    """Intake with a test baseline, plus any logs the reporter pasted under '## Logs'."""
    base = n.intake_with_baseline(run)

    def node(state: dict) -> dict:
        out = base(state)
        if logs := section(run.github.get_issue(run.issue_id).body, "Logs"):
            out["ctx"]["log_excerpt"] = logs
        return out
    return node


def spec_deltas(run: Run, ctx: dict) -> str:
    return "\n\n".join(p.read_text(encoding="utf-8") for p in sorted((n.change_dir(run, ctx) / "specs").glob("*/spec.md")))


def gate_triage(run: Run, ctx: dict, text: str) -> str | None:
    ok, out = run.spec.validate(ctx["change_id"])
    if not ok:
        return f"openspec validate --strict failed:\n{out}"
    deltas = spec_deltas(run, ctx)
    if any((run.repo.path / "openspec/specs").glob("*/spec.md")):
        if "## MODIFIED Requirements" not in deltas:
            return "triage must modify an existing requirement (## MODIFIED Requirements) and describe the correct behaviour in a scenario"
    elif "## ADDED Requirements" not in deltas:
        # a product with no specs yet has nothing to modify: the first change records the correct behaviour
        return "the repository has no specs yet, so triage must record the correct behaviour with ## ADDED Requirements and a scenario"
    return None


def after_triage(run: Run, ctx: dict, text: str) -> dict:
    d = n.change_dir(run, ctx)
    proposal, design = (d / "proposal.md").read_text(encoding="utf-8"), (d / "design.md").read_text(encoding="utf-8")
    return {"triage": "\n\n".join([proposal, design, spec_deltas(run, ctx)]), "design": design,
            "tasks": (d / "tasks.md").read_text(encoding="utf-8"), "change_summary": proposal}


def gate_reproduce(run: Run, ctx: dict, text: str) -> str | None:
    """No failing test, no fix."""
    r = sh(run.target.commands["test"], cwd=run.repo.path)
    if r.code == 0:
        return "No failing test, no fix: every test passes on the current code. Write a test that fails because of the bug."
    if count_total(r.out) <= ctx["baseline_total"]:
        return "No new test was added: the suite has no more tests than before the change."
    if count_failed(r.out) < 1:
        return f"The suite errored without a failing test (build or collection error?):\n{n.tail(r.out)}"
    if count_tests(r.out) < ctx["baseline_passed"]:
        return f"Existing tests broke: {count_tests(r.out)} pass, {ctx['baseline_passed']} passed before."
    return None


def after_reproduce(run: Run, ctx: dict, text: str) -> dict:
    n.tick_tasks(run, ctx, text)
    r = sh(run.target.commands["test"], cwd=run.repo.path)
    tests_glob = run.target.paths["tests"]
    written = [f"### {path}\n{body}" for path, body in run.repo.snapshot().items()
               if body is not None and glob_match(path, tests_glob)]
    return {"failing_test": "\n\n".join(written), "failure_output": n.tail(r.out, 40)}


def gate_rca(run: Run, ctx: dict, text: str) -> str | None:
    path = run.repo.path / f"docs/rca/{ctx['issue_number']}.md"
    if not path.exists():
        return f"docs/rca/{ctx['issue_number']}.md was not written"
    missing = [h for h in RCA_SECTIONS if not section(path.read_text(encoding="utf-8"), h)]
    return f"the RCA is missing sections: {', '.join(missing)}" if missing else None


def after_rca(run: Run, ctx: dict, text: str) -> dict:
    return {"rca_path": f"docs/rca/{ctx['issue_number']}.md"}


TRIAGE = Stage("triage", "triager", produces="triage_summary", gate=gate_triage, after=after_triage)
REPRODUCE = Stage("reproduce", "reproducer", produces="reproduced", gate=gate_reproduce, after=after_reproduce)
FIX = Stage("fix", "fixer", produces="fixed", gate=n.gate_build_and_observability, after=n.after_code)
RCA = Stage("rca", "rca_writer", produces="rca_summary", gate=gate_rca, after=after_rca)


def validate(run: Run):
    def node(state: dict) -> dict:
        ctx = state["ctx"]
        failures, results = n.validate_feature_checks(run, ctx, min_tests=ctx["baseline_passed"] + 1)
        return review.finish_with_review(run, ctx, failures, results)
    return node


def build(run: Run) -> StateGraph:
    g = StateGraph(n.State)
    g.add_node("intake", intake(run))
    g.add_node("triage", n.agent_node(run, TRIAGE))
    g.add_node("approve_proposal", n.approve_proposal(run))
    g.add_node("reproduce", n.agent_node(run, REPRODUCE))
    g.add_node("fix", n.agent_node(run, FIX))
    g.add_node("validate", validate(run))
    g.add_node("rca", n.agent_node(run, RCA))
    g.add_node("archive_spec", n.archive_spec(run))
    g.add_node("pr_summary", n.agent_node(run, n.PR_SUMMARY))
    g.add_node("open_pr", n.open_pr(run))
    g.add_node("await_merge", n.await_merge(run))

    g.add_edge(START, "intake")
    baseline.wire(g, run, "intake", "triage")
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
