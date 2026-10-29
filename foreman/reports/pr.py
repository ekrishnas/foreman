"""PR body and cost table, assembled by code (DESIGN.md section 9); the narrative comes from the summarizer agent."""
from foreman.core.util import section
from foreman.state.store import CostEvent


def cost_table(run_cost: list[CostEvent], issue_cost: list[CostEvent]) -> str:
    groups: dict[tuple[str, str], list[CostEvent]] = {}
    for c in run_cost:
        groups.setdefault((c.agent, c.model), []).append(c)
    lines = [
        "| Agent | Model | Attempts | Tokens (in / out) | Cost (USD) | Duration |",
        "|---|---|---|---|---|---|",
    ]
    for (agent, model), rows in groups.items():
        lines.append(
            f"| {agent} | {model} | {len(rows)} "
            f"| {sum(r.input_tokens for r in rows):,} / {sum(r.output_tokens for r in rows):,} "
            f"| ${sum(r.cost_usd for r in rows):.4f} | {sum(r.duration_ms for r in rows) / 1000:.1f}s |"
        )
    retry = sum(c.cost_usd for c in run_cost if c.attempt > 0)
    lines += [
        "",
        f"**Run total:** ${sum(c.cost_usd for c in run_cost):.2f} (retries and re-runs: ${retry:.2f})  ",
        f"**Issue total (all runs):** ${sum(c.cost_usd for c in issue_cost):.2f}",
        "",
        "_Costs are notional (API-equivalent) when run on a Claude subscription._",
    ]
    return "\n".join(lines)


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
    rows = ["| Stage | Passed | Retries |", "|---|---|---|"]
    for s in stages:
        retries = sum(1 for e in events if e.stage == s and e.event == "retry")
        rows.append(f"| {s} | yes | {retries} |")
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
        "- After merge: revert this PR (`git revert -m 1 <merge commit>`).",
        "- Policy findings: " + ("; ".join(findings) if findings else "none."),
    ]
    return "\n".join(lines)


def build_body(run, ctx: dict, narrative: str) -> str:
    link = f"Linked issue: #{ctx['issue_number']} · Type: {run.workflow}"
    if ctx.get("change_id") and run.workflow != "spike":  # a spike's change id is only a name; it has no OpenSpec change
        link += f" · OpenSpec change: `{ctx['change_id']}`"
    sections = [
        f"## Summary\n{narrative.strip() or '(no narrative produced)'}\n\n{link}",
        f"## Files changed\n```\n{run.repo.diff_stat(ctx['base_commit'])}\n```",
        f"## Gates, retries and policy\n{gates_text(run)}",
        f"## Approvals\n{approvals_text(run)}",
    ]
    if ctx.get("validate_results"):
        sections.append(f"## Test results\n{ctx['validate_results']}")
    if ctx.get("review"):  # OpenSpec's verify method: what the reviewer found short of blocking
        sections.append("## Review (OpenSpec verify)\n" + "\n\n".join(
            f"### {h}\n{section(ctx['review'], h)}" for h in ("Summary", "WARNING", "SUGGESTION", "Final Assessment")))
    if ctx.get("observability"):
        sections.append(f"## Observability\n{ctx['observability']}")
    sections.append("## Cost\n" + cost_table(run.store.cost_events(run_id=run.run_id),
                                             run.store.cost_events(issue_id=run.issue_id)))
    sections.append(f"## Risks and rollback\n{risks_text(run, ctx)}")
    if ctx.get("rca_path"):
        sections.append(f"## Root cause analysis\n[{ctx['rca_path']}]({ctx['rca_path']})")
    return "\n\n".join(sections)
