"""The foreman command line: run, resume, metrics, cost, issue create."""
import argparse
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

from foreman.config import WORKFLOWS, Scenario, load_scenario, load_target
from foreman.engine import (
    ROOT, RunRefused, check_expect, ensure_ready, issue_from_file, make_checkpointer, open_session, resume_session,
)
from foreman.github import GitHub
from foreman.gitops import GitRepo, RepoError
from foreman.human import TerminalHuman
from foreman.metrics import cost_table, run_metrics
from foreman.nodes import file_proposed_issues, split_issues
from foreman.store import open_reader, open_store
from foreman.util import section

EXIT_CODES = {"completed": 0, "waiting": 0, "safe_stopped": 2, "observe_failed": 3}
NEEDS_DB = "set FOREMAN_DB_URL to a Postgres URL (docker compose up -d starts one, see README)"


class CliError(Exception):
    pass


def create_issue_from_file(github, path: Path, issue_type: str) -> int:
    if issue_type not in WORKFLOWS:
        raise ValueError(f"issue type must be one of {WORKFLOWS}, got {issue_type!r}")
    title, body = issue_from_file(Path(path))
    return github.create_issue(title, body, [f"foreman:{issue_type}"])


def handoff_from_file(github, human, path: Path, spike_issue: int, analysis: str | None = None) -> list[int]:
    """File the issues of a spike analysis that is already approved, after the human confirms the list."""
    text = Path(path).read_text(encoding="utf-8")
    proposed = section(text, "Proposed issues") or section(text, "Draft user story")  # older analyses have one story
    if not proposed:
        raise CliError(f"{path} has no '## Proposed issues' section")
    issues = split_issues(proposed)
    listed = "\n".join(f"{i}. {title}" for i, (title, _) in enumerate(issues, 1))
    decision = human.approve("handoff", f"File {len(issues)} issue(s) from {Path(path).name} for spike #{spike_issue}, "
                                        f"labelled foreman:feature and foreman:needs-approval:\n{listed}")
    if not decision.approved:
        raise CliError(f"handoff not approved: {decision.note or 'no reason given'}")
    return file_proposed_issues(github, proposed, spike_issue, analysis or Path(path).name)


def wait_for_merge(github, pr: int, interval: float = 30, timeout: float = 86400, sleep=time.sleep) -> str:
    """Poll the PR until a human merges or closes it (or the timeout passes); returns its state."""
    waited = 0.0
    while github.pr_state(pr).state == "open" and waited < timeout:
        sleep(interval)
        waited += interval
    return github.pr_state(pr).state


def adhoc_scenario(args) -> Scenario:
    """`foreman run --issue N`: the workflow comes from the issue's foreman:<workflow> label."""
    target = load_target(ROOT / "targets" / f"{args.target}.yaml")
    issue = GitHub(GitRepo(target.repo)).get_issue(args.issue)
    ensure_ready(issue)
    found = [w for w in WORKFLOWS if f"foreman:{w}" in issue.labels]
    if len(found) != 1:
        raise CliError(f"issue #{issue.number} needs exactly one label of {[f'foreman:{w}' for w in WORKFLOWS]}")
    return Scenario(name=f"issue-{issue.number}", workflow=found[0], input=issue.number, target=args.target,
                    approvals="interactive", budget_usd=args.budget)


def report(session, result) -> int:
    run = session.run
    problems = check_expect(session, result)
    run_cost = run.store.cost_events(run_id=run.run_id)
    issue_cost = run.store.cost_events(issue_id=run.issue_id)
    retry = sum(c.cost_usd for c in run_cost if c.attempt > 0)
    print(f"run: {run.run_id}")
    print(f"workflow: {run.workflow}")
    print(f"status: {result.status}")
    if result.reason:
        print(f"reason: {result.reason}")
    if result.pr:
        print(f"PR: #{result.pr}")
    ctx = session.ctx()
    if result.status == "observe_failed":
        print(f"revert PR: #{ctx.get('revert_pr')}  bugfix issue: #{ctx.get('bugfix_issue')} (label foreman:ready to start it)")
    print(f"cost: ${sum(c.cost_usd for c in run_cost):.2f} (retries and re-runs ${retry:.2f}); "
          f"issue #{run.issue_id} total ${sum(c.cost_usd for c in issue_cost):.2f}")
    print(f"audit: {run.dir / 'audit.jsonl'}")
    if result.status == "safe_stopped":
        print(f"resume with: foreman resume {run.run_id}")
    elif result.status == "waiting":
        print(f"merge the PR, then: foreman resume {run.run_id}")
    if run.scenario.expect:
        print("expect: ok" if not problems else "expect: FAILED\n" + "\n".join(f"  - {p}" for p in problems))
    return 1 if problems else EXIT_CODES[result.status]


def cmd_run(args) -> int:
    db = os.environ.get("FOREMAN_DB_URL")
    if args.mode == "live" and not db:
        raise CliError(f"live runs need a durable store: {NEEDS_DB}")
    if args.mode == "replay" and not args.recording:
        raise CliError("replay mode needs --recording <dir> (a runs/<id>/recorded directory)")
    if args.issue and args.mode == "fake":
        raise CliError("fake mode files its own issue from the scenario input; --issue needs live mode")
    if args.scenario:
        scenario = load_scenario(args.scenario)
        if args.issue:
            scenario = replace(scenario, input=args.issue)
    elif args.issue:
        scenario = adhoc_scenario(args)
    else:
        raise CliError("give a scenario file or --issue <n>")
    runs_dir = Path(args.runs_dir)
    session = open_session(scenario, args.mode, runs_dir, checkpointer=make_checkpointer(db),
                           store=open_store(runs_dir, db), record=args.record,
                           recording=Path(args.recording) if args.recording else None)
    return report(session, session.start())


def cmd_resume(args) -> int:
    db = os.environ.get("FOREMAN_DB_URL")
    if not db:
        raise CliError(f"resume needs the durable store that holds the checkpoints: {NEEDS_DB}")
    runs_dir = Path(args.runs_dir)
    session = resume_session(args.thread, runs_dir, make_checkpointer(db), open_store(runs_dir, db))
    if args.poll and (pr := session.ctx().get("pr_number")):
        print(f"waiting for PR #{pr} to be merged or closed...")
        print(f"PR #{pr} is {wait_for_merge(session.run.github, pr, args.interval, args.timeout)}")
    return report(session, session.resume())


def cmd_metrics(args) -> int:
    store = open_reader(Path(args.runs_dir), os.environ.get("FOREMAN_DB_URL"))
    audit, cost = store.audit_events(args.run), store.cost_events(run_id=args.run)
    if not audit:
        raise CliError(f"no audit events found for run {args.run}")
    m = run_metrics(audit, cost)
    print(f"run: {args.run}")
    print("stage_success:")
    for stage, rate in m["stage_success"].items():
        print(f"  {stage}: {rate:.2f}")
    for key in ("retry_rate", "rollbacks", "mttr_s", "latency_s", "human_wait_s", "cost_usd", "retry_cost_usd"):
        print(f"{key}: {m[key]:.2f}" if isinstance(m[key], float) else f"{key}: {m[key]}")
    print()
    print(cost_table(cost, store.cost_events(issue_id=cost[0].issue_id) if cost else []))
    return 0


def cmd_cost(args) -> int:
    store = open_reader(Path(args.runs_dir), os.environ.get("FOREMAN_DB_URL"))
    events = store.cost_events(issue_id=args.issue)
    if not events:
        raise CliError(f"no cost recorded for issue #{args.issue}")
    runs = {}
    for c in events:
        runs.setdefault(c.run_id, []).append(c)
    print(f"issue #{args.issue}: {len(runs)} runs")
    for run_id, rows in runs.items():
        retry = sum(c.cost_usd for c in rows if c.attempt > 0)
        print(f"  {run_id}  ${sum(c.cost_usd for c in rows):.2f} (retries and re-runs ${retry:.2f})")
    retry_total = sum(c.cost_usd for c in events if c.attempt > 0)
    print(f"total: ${sum(c.cost_usd for c in events):.2f} (retries and re-runs ${retry_total:.2f})")
    print("costs are notional (API-equivalent) on a Claude subscription")
    return 0


def cmd_issue_create(args) -> int:
    target = load_target(ROOT / "targets" / f"{args.target}.yaml")
    n = create_issue_from_file(GitHub(GitRepo(target.repo)), Path(args.file), args.type)
    print(f"issue #{n} created with label foreman:{args.type}; start it with: foreman run --issue {n}")
    return 0


def cmd_handoff(args) -> int:
    target = load_target(ROOT / "targets" / f"{args.target}.yaml")
    path = Path(args.analysis).resolve()
    try:
        analysis = path.relative_to(target.repo).as_posix()  # the path the issues should link to
    except ValueError:
        analysis = path.name
    numbers = handoff_from_file(GitHub(GitRepo(target.repo)), TerminalHuman(), path, args.issue, analysis)
    print("filed " + ", ".join(f"#{n}" for n in numbers))
    print("settle each issue's open decisions, add the foreman:ready label, then: foreman run --issue <n>")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--runs-dir", default=str(ROOT / "runs"), help="where run logs and fake runs live")

    parser = argparse.ArgumentParser(prog="foreman", description="Governed agentic SDLC orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", parents=[common], help="run a scenario, or an existing issue with --issue")
    p.add_argument("scenario", nargs="?", help="scenario yaml")
    p.add_argument("--mode", choices=["fake", "live", "replay"], default="fake")
    p.add_argument("--issue", type=int, help="existing GitHub issue number (live mode)")
    p.add_argument("--target", default="url-shortener", help="target profile for --issue without a scenario")
    p.add_argument("--budget", type=float, default=10.0, help="USD cap for --issue without a scenario")
    p.add_argument("--recording", help="recorded worker outputs to replay (runs/<id>/recorded)")
    p.add_argument("--record", action="store_true", help="record worker outputs (always on in live mode)")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("resume", parents=[common], help="continue a paused or safe-stopped run")
    p.add_argument("thread", help="the run id")
    p.add_argument("--poll", action="store_true", help="wait for the PR to be merged or closed before resuming")
    p.add_argument("--interval", type=float, default=30, help="seconds between polls")
    p.add_argument("--timeout", type=float, default=86400, help="give up polling after this many seconds")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("metrics", parents=[common], help="stage success, retries, MTTR, latency, cost for a run")
    p.add_argument("run")
    p.set_defaults(func=cmd_metrics)

    p = sub.add_parser("cost", parents=[common], help="cumulative cost of an issue across all runs")
    p.add_argument("--issue", type=int, required=True)
    p.set_defaults(func=cmd_cost)

    p = sub.add_parser("handoff", help="file the issues of an approved spike analysis")
    p.add_argument("analysis", help="path to analysis.md")
    p.add_argument("--issue", type=int, required=True, help="the spike issue number")
    p.add_argument("--target", default="url-shortener")
    p.set_defaults(func=cmd_handoff)

    p = sub.add_parser("issue", help="issue helpers")
    issue_sub = p.add_subparsers(dest="issue_command", required=True)
    c = issue_sub.add_parser("create", help="file a markdown file as a labelled GitHub issue")
    c.add_argument("file")
    c.add_argument("--type", required=True, choices=list(WORKFLOWS))
    c.add_argument("--target", default="url-shortener")
    c.set_defaults(func=cmd_issue_create)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # a console that cannot show a character must not crash the run
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (CliError, RunRefused, RepoError, ValueError, FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
