from collections import Counter
from datetime import datetime

from foreman.state.store import AuditEvent, CostEvent


def _t(e: AuditEvent) -> datetime:
    return datetime.fromisoformat(e.ts)


def run_metrics(audit: list[AuditEvent], cost: list[CostEvent]) -> dict:
    passed = Counter(e.stage for e in audit if e.event == "stage_passed")
    failed = Counter(e.stage for e in audit if e.event == "attempt_failed")
    stage_success = {s: passed[s] / (passed[s] + failed[s]) for s in sorted(set(passed) | set(failed))}

    first_failure: dict[str, datetime] = {}
    recoveries = []
    for e in audit:
        if e.event == "attempt_failed":
            first_failure.setdefault(e.stage, _t(e))
        elif e.event == "stage_passed" and e.stage in first_failure:
            recoveries.append((_t(e) - first_failure.pop(e.stage)).total_seconds())

    times = [_t(e) for e in audit]
    total_passed = sum(passed.values())
    return {
        "stage_success": stage_success,
        "retry_rate": sum(e.event == "retry" for e in audit) / total_passed if total_passed else 0,
        "rollbacks": sum(e.event == "rollback" for e in audit),
        "mttr_s": sum(recoveries) / len(recoveries) if recoveries else 0,
        "latency_s": (max(times) - min(times)).total_seconds() if times else 0,
        "human_wait_s": sum(e.details.get("wait_s", 0) for e in audit if e.event in ("approval", "merge_wait", "clarified")),
        "cost_usd": round(sum(c.cost_usd for c in cost), 6),
        "retry_cost_usd": round(sum(c.cost_usd for c in cost if c.attempt > 0), 6),
    }


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
