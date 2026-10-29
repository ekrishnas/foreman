"""The foreman command line. Claude Code calls these; you can too.

    foreman run 1 --request "let's work on #1"   start a run (blocks until it finishes)
    foreman pending [--run ID] [--wait SECONDS]  questions waiting for a human
    foreman respond 7 --approve                  answer one
    foreman resume RUN_ID                        continue a safe-stopped or crashed run
    foreman logs [RUN_ID] [--problems] [--cost]  what happened: list runs, or one run's timeline
"""
import argparse
import getpass
import sys
import time
from pathlib import Path

from foreman import ROOT
from foreman.adapters.gitops import RepoError
from foreman.core.config import RunConfig
from foreman.engine import DB_NAME, RunRefused, make_checkpointer, make_run, open_session, resume_session
from foreman.state.store import Store

EXIT_CODES = {"completed": 0, "safe_stopped": 2}
FINISHED = ("run_completed", "safe_stop")


class CliError(Exception):
    pass


def open_store(args) -> Store:
    return Store(Path(args.runs_dir) / DB_NAME)


def report(session, result) -> int:
    run = session.run
    run_cost = run.store.cost_events(run_id=run.run_id)
    issue_cost = run.store.cost_events(issue_id=run.issue_id)
    print(f"run: {run.run_id}")
    print(f"type: {run.workflow}")
    print(f"status: {result.status}")
    if result.reason:
        print(f"reason: {result.reason}")
    if result.pr:
        print(f"PR: #{result.pr}")
    print(f"cost: ${sum(c.cost_usd for c in run_cost):.2f}; issue #{run.issue_id} total ${sum(c.cost_usd for c in issue_cost):.2f}")
    if result.status == "safe_stopped":
        print(f"resume with: foreman resume {run.run_id}")
    return EXIT_CODES[result.status]


def cmd_run(args) -> int:
    runs_dir = Path(args.runs_dir)
    config = RunConfig(issue=args.issue, repo=str(Path(args.repo).resolve()), budget_usd=args.budget)
    session = open_session(config, runs_dir, make_checkpointer(runs_dir), store=open_store(args), factory=args.factory,
                           request=args.request)
    print(f"run: {session.run.run_id}", flush=True)  # first line, so whoever started this can find the run
    return report(session, session.start())


def cmd_resume(args) -> int:
    runs_dir = Path(args.runs_dir)
    session = resume_session(args.run_id, runs_dir, make_checkpointer(runs_dir), open_store(args), factory=args.factory)
    return report(session, session.resume())


def run_finished(store: Store, run_id: str) -> bool:
    events = store.audit_events(run_id)
    return bool(events) and events[-1].event in FINISHED


def cmd_pending(args) -> int:
    """List questions waiting for a human; with --wait, block until there is one, the run ends, or time is up."""
    store = open_store(args)
    deadline = time.monotonic() + args.wait
    while True:
        rows = store.pending(args.run)
        finished = bool(args.run) and run_finished(store, args.run)
        if rows or finished or time.monotonic() >= deadline:
            break
        time.sleep(args.poll)
    for r in rows:
        answer_with = ("--answer TEXT" if r["kind"] == "question" else "--approve | --reject --note TEXT")
        print(f"request {r['id']} (run {r['run_id']}, {r['kind']}); answer with: foreman respond {r['id']} {answer_with}")
        print(r["prompt"])
        print("---")
    if not rows:
        print("no pending requests" + (f"; run {args.run} has finished" if finished else ""))
    return 0


def cmd_respond(args) -> int:
    store = open_store(args)
    try:
        request = store.get_request(args.request)
    except KeyError as e:
        raise CliError(str(e).strip("'")) from e
    who = args.who or getpass.getuser()
    if request["kind"] == "question":
        if args.answer is None:
            raise CliError('this is a question: use --answer TEXT (--answer "" to go on with an assumption)')
        store.respond(args.request, answer=args.answer, who=who)
    else:
        if args.approve == args.reject:
            raise CliError("choose exactly one of --approve or --reject")
        store.respond(args.request, approved=args.approve, note=args.note, who=who)
    print(f"request {args.request} answered")
    return 0


PROBLEMS = {"attempt_failed", "retry", "safe_stop", "pr_closed", "rollback"}
QUIET = {"commit", "findings", "results", "comments", "wait_s"}  # details too noisy for the timeline
STATUS = {"run_completed": "completed", "safe_stop": "safe_stopped"}


def describe(details: dict) -> str:
    """One short line from an event's details: type=bugfix; reason=...; url=..."""
    text = "; ".join(f"{k}={str(v).splitlines()[0]}" for k, v in details.items() if v not in ("", None, [], {}) and k not in QUIET)
    return text if len(text) <= 200 else text[:197] + "..."


def cmd_logs(args) -> int:
    """What Foreman did: list the runs, or show one run's timeline."""
    store = open_store(args)
    if not args.run:
        runs = store.runs()
        if not runs:
            print("no runs recorded yet")
        for r in runs:
            last = store.audit_events(r["run_id"])[-1]
            cost = sum(c.cost_usd for c in store.cost_events(run_id=r["run_id"]))
            status = STATUS.get(last.event, "running or interrupted")
            print(f"{r['run_id']}  {r['started'][:19]}  {last.workflow:<18} {status:<22} {r['events']:>3} events  ${cost:.2f}")
        return 0
    events = store.audit_events(args.run)
    if not events:
        raise CliError(f"no events recorded for run {args.run}")
    for e in events:
        if args.problems and e.event not in PROBLEMS:
            continue
        attempt = f" #{e.attempt}" if e.attempt and e.event in PROBLEMS | {"stage_passed"} else ""
        print(f"{e.ts[11:19]}  {e.stage:<18} {e.event + attempt:<18} {describe(e.details)}".rstrip())
    if args.cost:
        rows = store.cost_events(run_id=args.run)
        print("\nstage                agent                  try  tokens in/out   usd")
        for c in rows:
            print(f"{c.stage:<20} {c.agent:<22} {c.attempt:>3}  {c.input_tokens:>6}/{c.output_tokens:<6} {c.cost_usd:>6.2f}")
        print(f"total ${sum(c.cost_usd for c in rows):.2f}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--runs-dir", default=str(ROOT / "runs"), help="where run files and the SQLite database live")

    parser = argparse.ArgumentParser(prog="foreman", description="Governed agentic SDLC orchestrator")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", parents=[common], help="classify a GitHub issue and run the matching graph")
    p.add_argument("issue", type=int, help="GitHub issue number")
    p.add_argument("--request", default="", help="what the developer asked for, in their own words")
    p.add_argument("--repo", default=".", help="the product repo to work on (default: the current folder)")
    p.add_argument("--budget", type=float, default=10.0, help="USD cap for the run")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("resume", parents=[common], help="continue a safe-stopped or crashed run")
    p.add_argument("run_id")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("pending", parents=[common], help="list questions waiting for a human")
    p.add_argument("--run", help="only this run")
    p.add_argument("--wait", type=float, default=0, help="wait up to this many seconds for one to appear")
    p.add_argument("--poll", type=float, default=2, help="seconds between checks while waiting")
    p.set_defaults(func=cmd_pending)

    p = sub.add_parser("respond", parents=[common], help="answer a waiting question")
    p.add_argument("request", type=int)
    p.add_argument("--approve", action="store_true")
    p.add_argument("--reject", action="store_true")
    p.add_argument("--note", default="", help="why, when rejecting")
    p.add_argument("--answer", help="the answer, for a question")
    p.add_argument("--who", default="", help="defaults to the current user")
    p.set_defaults(func=cmd_respond)

    p = sub.add_parser("logs", parents=[common], help="what Foreman did: list runs, or show one run's timeline")
    p.add_argument("run", nargs="?", help="a run id; without one, list all runs")
    p.add_argument("--problems", action="store_true", help="only failed attempts, retries, stops and rollbacks")
    p.add_argument("--cost", action="store_true", help="also show what each agent call cost")
    p.set_defaults(func=cmd_logs)
    return parser


def main(argv: list[str] | None = None, factory=make_run) -> int:
    """`factory` builds each run; tests pass one wired to fakes."""
    for stream in (sys.stdout, sys.stderr):  # a console that cannot show a character must not crash the run
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    args = build_parser().parse_args(argv)
    args.factory = factory
    try:
        return args.func(args)
    except (CliError, RunRefused, RepoError, ValueError, FileNotFoundError, RuntimeError) as e:
        print(f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
