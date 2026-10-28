"""Prompt assembly: an agent sees only the context keys listed for it, plus its boundaries."""
from foreman.config import Agent
from foreman.metrics import cost_table
from foreman.pr import approvals_text
from foreman.policy import resolve_writable


class MissingContext(Exception):
    pass


def _repo_tree(run, ctx):
    return run.repo.tree() or "(empty repository)"


def _target_profile(run, ctx):
    t = run.target
    lines = [f"name: {t.name}", "paths: " + ", ".join(f"{k}={v}" for k, v in t.paths.items())]
    lines += [f"{k}: {v}" for k, v in t.commands.items()]
    return "\n".join(lines)


def _existing_specs(run, ctx):
    specs = sorted((run.repo.path / "openspec/specs").glob("*/spec.md"))
    return "\n\n".join(
        f"### openspec/specs/{p.parent.name}/spec.md\n{p.read_text().strip()}" for p in specs
    ) or "(none yet)"


def _diff_stat(run, ctx):
    return run.repo.diff_stat(ctx["base_commit"]) if ctx.get("base_commit") else None


def _cost_table(run, ctx):
    return cost_table(run.store.cost_events(run_id=run.run_id), run.store.cost_events(issue_id=run.issue_id))


def _approvals(run, ctx):
    return approvals_text(run)


# Keys the runner can compute itself when the context bag does not hold them.
PROVIDERS = {
    "repo_tree": _repo_tree,
    "target_profile": _target_profile,
    "existing_specs": _existing_specs,
    "diff_stat": _diff_stat,
    "cost_table": _cost_table,
    "approvals": _approvals,
}


def writable_for(run, agent: Agent, ctx: dict, instance: str = "") -> list[str]:
    return resolve_writable(agent.writable, run.target, run_id=run.run_id,
                            change=ctx.get("change_id", ""), option=instance)


def build_prompt(run, agent: Agent, ctx: dict, failure: str | None = None, instance: str = "") -> str:
    writable = writable_for(run, agent, ctx, instance)
    boundaries = [f"- Branch: {run.branch}"]
    if ctx.get("change_id"):
        boundaries.append(f"- Change id: {ctx['change_id']}")
    boundaries.append(
        f"- You may write only to: {', '.join(writable)}" if writable
        else "- You are read-only: do not create or modify any file."
    )
    boundaries.append("- No network or web access. Changes outside your writable paths are rejected.")

    parts = [(run.prompts_dir / f"{agent.name}.md").read_text().strip(), "## Boundaries\n" + "\n".join(boundaries)]
    for key, required in agent.context_keys():
        value = ctx.get(key)
        if value is None and key in PROVIDERS:
            value = PROVIDERS[key](run, ctx)
        if value is None:
            if required:
                raise MissingContext(key)
            continue
        parts.append(f"## {key}\n{value}")
    if failure:
        parts.append(f"## Previous attempt failed — fix this\n{failure}")
    return "\n\n".join(parts) + "\n"
