"""Observe stage: deploy the released build, drive smoke traffic, judge against the SLO (in code),
and on failure open a revert PR and a bugfix issue for a human to approve (DESIGN.md section 12)."""
import json
import time
import urllib.request

from foreman.nodes import State, gate_nonempty, passed, tail
from foreman.runner import Run, Stage, run_stage
from foreman.util import sh

OBSERVE = Stage("observe_summary", "observer", produces="observe_summary", gate=gate_nonempty,
                commit=False, sandbox=True)
COMMAND_TIMEOUT = 600
HEALTH_TIMEOUT = 60


def _failed(message: str) -> dict:
    return {"requests": 0, "errors": 1, "p95_ms": 0, "log": message}


def _wait_healthy(url: str, timeout: int) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return True
        except OSError:
            pass
        time.sleep(1)
    return False


def _last_json(output: str) -> dict | None:
    for line in reversed(output.strip().splitlines()):
        try:
            data = json.loads(line)
        except ValueError:
            continue
        return data if isinstance(data, dict) else None
    return None


def run_smoke(run: Run) -> dict:
    """Start the build, run the smoke script, always stop the build. Returns requests/errors/p95_ms/log."""
    commands, cwd, env = run.target.commands, run.repo.path, run.deploy_env
    try:
        up = sh(commands["run"], cwd=cwd, env=env, timeout=COMMAND_TIMEOUT)
        if up.code:
            return _failed(f"deploy failed: {tail(up.out)}")
        if run.check_health and not _wait_healthy(run.target.health_url, HEALTH_TIMEOUT):
            return _failed(f"health check never passed at {run.target.health_url}")
        smoke = sh(commands["smoke"], cwd=cwd, env=env, timeout=COMMAND_TIMEOUT)
        data = _last_json(smoke.out)
        if smoke.code or data is None or not {"requests", "errors", "p95_ms"} <= data.keys():
            return _failed(f"smoke script did not print JSON metrics (requests, errors, p95_ms): {tail(smoke.out)}")
        return {"requests": int(data["requests"]), "errors": int(data["errors"]),
                "p95_ms": float(data["p95_ms"]), "log": str(data.get("log", ""))}
    finally:
        sh(commands["stop"], cwd=cwd, env=env, timeout=120)


def verdict(metrics: dict, slo: dict) -> tuple[bool, str]:
    """Pure comparison against the profile's SLO; limits are inclusive."""
    problems = []
    if metrics["requests"] <= 0:
        problems.append("no requests were served")
    else:
        rate = metrics["errors"] / metrics["requests"]
        if rate > slo["error_rate_max"]:
            problems.append(f"error rate {rate:.2%} exceeds {slo['error_rate_max']:.2%}")
    if metrics["p95_ms"] > slo["p95_ms_max"]:
        problems.append(f"p95 {metrics['p95_ms']:g}ms exceeds {slo['p95_ms_max']:g}ms")
    return not problems, "; ".join(problems) or "within SLO"


def open_revert_pr(run: Run, ctx: dict, reason: str, summary: str) -> int:
    sha, branch = ctx["merge_sha"], f"{run.branching.prefix}revert-{run.run_id}"
    is_merge_commit = len(run.repo.git("rev-list", "--parents", "-n", "1", sha).split()) > 2
    run.repo.git("checkout", "-q", "-b", branch, run.base)
    run.repo.git("revert", "--no-commit", *(["-m", "1"] if is_merge_commit else []), sha)
    run.repo.commit(f"fix: revert PR #{ctx['pr_number']} after observe failed\n\n"
                    f"The observe stage failed for run {run.run_id}.\n\nReverts {sha}.")
    run.github.push(branch)
    body = (f"Automatic revert of PR #{ctx['pr_number']} (issue #{run.issue_id}) after the observe stage failed.\n\n"
            f"**Verdict:** {reason}\n\n{summary}\n")
    pr = run.github.open_pr(branch, f"Revert: {ctx['issue_title']}", body, ["foreman:revert"], closes=None)
    run.repo.git("checkout", "-q", run.base)
    return pr


def observe(run: Run):
    def node(state: State) -> dict:
        ctx = state["ctx"]
        metrics = run_smoke(run)
        ok, reason = verdict(metrics, run.target.slo)
        if ok:
            run.audit("observe", "observe_passed", metrics=metrics)
            run.github.comment(run.issue_id, f"Observe passed: {metrics['requests']} requests, {metrics['errors']} errors, "
                                             f"p95 {metrics['p95_ms']:g}ms ({reason}).")
            passed(run, "observe")
            return {"ctx": {"observe_ok": True}}

        run.audit("observe", "observe_failed", metrics=metrics, reason=reason)
        facts = {**ctx, "log_sample": metrics["log"][-2000:] or "(no log output)",
                 "metrics": json.dumps(metrics), "slo": json.dumps(run.target.slo)}
        out = run_stage(run, OBSERVE, facts, state.get("done", {}))
        summary = out["ctx"]["observe_summary"]
        revert = open_revert_pr(run, ctx, reason, summary)
        bug = run.github.create_issue(
            f"Observe failed after releasing #{run.issue_id}: {reason}"[:120],
            (f"The observe stage failed after PR #{ctx['pr_number']} (issue #{run.issue_id}) was merged.\n\n"
             f"**Verdict:** {reason}\n\n**Metrics:** `{json.dumps(metrics)}`\n\n### Observer summary\n{summary}\n\n"
             f"A revert PR (#{revert}) is open. Label this issue `foreman:ready` to let Foreman start the bugfix workflow."),
            ["foreman:bugfix", "foreman:needs-approval"])
        run.github.comment(run.issue_id, f"Observe failed ({reason}). Revert PR #{revert} and bugfix issue #{bug} opened.")
        return {"ctx": {"observe_ok": False, "observe_summary": f"{reason}\n\n{summary}",
                        "revert_pr": revert, "bugfix_issue": bug}, "done": out["done"]}
    return node
