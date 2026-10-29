"""PR body assembled by code (DESIGN.md section 9); the narrative comes from the summarizer agent."""
from foreman.metrics import cost_table
from foreman.util import subsections


def approvals_text(run) -> str:
    rows = [e for e in run.store.audit_events(run.run_id) if e.event == "approval"]
    if not rows:
        return "(none recorded)"
    lines = []
    for e in rows:
        verdict = "approved" if e.details.get("approved") else "rejected"
        note = f" — {e.details['note']}" if e.details.get("note") else ""
        lines.append(f"- {e.details.get('kind')}: {verdict} by {e.details.get('who') or 'unknown'} at {e.ts}{note}")
    return "\n".join(lines)


def gates_text(run) -> str:
    events = run.store.audit_events(run.run_id)
    stages = list(dict.fromkeys(e.stage for e in events if e.event == "stage_passed"))
    rows = ["| Stage | Passed | Retries | Fallbacks |", "|---|---|---|---|"]
    for s in stages:
        retries = sum(1 for e in events if e.stage == s and e.event == "retry")
        fallbacks = sum(1 for e in events if e.stage == s and e.event == "fallback")
        rows.append(f"| {s} | yes | {retries} | {fallbacks} |")
    return "\n".join(rows)


def policy_findings(run) -> list[str]:
    events = run.store.audit_events(run.run_id)
    found = [e.details["reason"] for e in events if e.event == "attempt_failed"
             and str(e.details.get("reason", "")).startswith("policy:")]
    for e in events:
        if e.event == "stage_passed":
            found += [f"{e.stage}: {f} (approved)" for f in e.details.get("findings", [])]
    return found


def risks_text(run, ctx: dict) -> str:
    findings = policy_findings(run)
    lines = [
        f"- Before merge: `git reset --hard {ctx.get('approved_commit', ctx.get('base_commit', '<base>'))}` "
        f"on `{run.branch}` discards this run's implementation.",
        "- After merge: revert this PR (`git revert -m 1 <merge commit>`). If the observe stage fails, "
        "Foreman opens the revert PR and a bugfix issue itself.",
        "- Policy findings: " + ("; ".join(findings) if findings else "none."),
    ]
    return "\n".join(lines)


def on_merge_text(proposed: str) -> str:
    titles = [title for title, _ in subsections(proposed, 3)]
    listed = "\n".join(f"{i}. {title}" for i, title in enumerate(titles, 1))
    plural = "s" if len(titles) != 1 else ""
    return (f"## On merge\nMerging this PR approves the analysis. Foreman then files {len(titles)} issue{plural}, "
            f"labelled `foreman:feature` and `foreman:needs-approval`:\n\n{listed}\n\n"
            "Before merging, answer the open questions by editing `analysis.md` in this PR. "
            "To start a build, settle the decisions and add `foreman:ready` to an issue.")


def build_body(run, ctx: dict, narrative: str) -> str:
    link = f"Linked issue: #{ctx['issue_number']}"
    if ctx.get("change_id") and run.workflow != "spike":
        link += f" · OpenSpec change: `{ctx['change_id']}`"
    sections = [
        f"## Summary\n{narrative.strip() or '(no narrative produced)'}\n\n{link}",
        f"## Files changed\n```\n{run.repo.diff_stat(ctx['base_commit'])}\n```",
        f"## Gates, retries and policy\n{gates_text(run)}",
        f"## Approvals\n{approvals_text(run)}",
    ]
    if run.workflow == "spike" and ctx.get("proposed_issues"):
        sections.insert(1, on_merge_text(ctx["proposed_issues"]))
    if ctx.get("validate_results"):
        sections.append(f"## Test results\n{ctx['validate_results']}")
    sections.append("## Cost\n" + cost_table(run.store.cost_events(run_id=run.run_id),
                                             run.store.cost_events(issue_id=run.issue_id)))
    sections.append(f"## Risks and rollback\n{risks_text(run, ctx)}")
    if ctx.get("rca_path"):
        sections.append(f"## Root cause analysis\n[{ctx['rca_path']}]({ctx['rca_path']})")
    return "\n\n".join(sections)
