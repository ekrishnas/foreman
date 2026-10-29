"""Shared building blocks for the graphs: gates, deterministic nodes and the stages more than one graph uses.

A graph (see the other files in this folder) is a list of these nodes plus its own agent stages.
"""
import re
import time
from typing import Annotated, TypedDict

from foreman.core.context import writable_for
from foreman.core.policy import check_changes
from foreman.core.runner import Run, SafeStop, Stage, run_stage
from foreman.core.util import count_tests, count_total, glob_match, merge_dicts, section, sh, slug
from foreman.reports.pr import build_body

OBSERVABILITY_MARK = "**Observability notes**"  # starts the issue comment the observe graph looks for


class State(TypedDict, total=False):
    ctx: Annotated[dict, merge_dicts]  # context bag: everything agents may be shown


# ---------- small helpers ----------

def parse_bullets(text: str) -> list[str]:
    return re.findall(r"^\s*[-*]\s+(.*\S)\s*$", text, re.M)


def tail(text: str, lines: int = 30) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def change_dir(run: Run, ctx: dict):
    return run.repo.path / "openspec/changes" / ctx.get("change_id", "")


def passed(run: Run, name: str, **details) -> None:
    run.audit(name, "stage_passed", **details)


def has_source(run: Run) -> bool:
    """True when the target repo already contains application code."""
    return any(glob_match(f, run.target.paths["src"]) for f in run.repo.tree().splitlines())


def agent_node(run: Run, stage: Stage):
    def node(state: State) -> dict:
        return run_stage(run, stage, state["ctx"])
    return node


# ---------- gates and after-hooks shared by several graphs ----------

def gate_build(run: Run, ctx: dict, text: str) -> str | None:
    r = sh(run.target.commands["build"], cwd=run.repo.path)
    return None if r.code == 0 else f"build failed:\n{tail(r.out)}"


def branch_files(run: Run, ctx: dict, path_key: str) -> list[str]:
    """Files under the target's `path_key` path (src, tests, docs) that this run's branch adds or changes,
    committed or not. A re-run that writes the same content still counts."""
    pattern = run.target.paths[path_key]
    changes = [*run.repo.branch_changes(ctx["base_commit"]), *run.repo.changes()]
    return sorted({c.path for c in changes if c.status != "D" and glob_match(c.path, pattern)})


def gate_wrote(path_key: str):
    def gate(run: Run, ctx: dict, text: str) -> str | None:
        if branch_files(run, ctx, path_key):
            return None
        return f"the agent wrote no files under {run.target.paths[path_key]}"
    return gate


def gate_nonempty(run: Run, ctx: dict, text: str) -> str | None:
    return None if text.strip() else "the agent produced no output"


def gate_observability(run: Run, ctx: dict, text: str) -> str | None:
    """Code agents must say how their code logs, so the observe graph knows what to look for later."""
    notes = section(text, "Observability")
    missing = [label for label in ("Log location", "Success", "Error") if label.lower() not in notes.lower()]
    if missing:
        return ("the output must end with an '## Observability' section listing: Log location (file path), "
                f"Success (an example line) and Errors (patterns to search for); missing: {', '.join(missing)}")
    return None


def gate_build_and_observability(run: Run, ctx: dict, text: str) -> str | None:
    return gate_build(run, ctx, text) or gate_observability(run, ctx, text)


def tick_tasks(run: Run, ctx: dict, text: str) -> dict:
    """Agents report `DONE: <task id>`; only Foreman edits tasks.md."""
    path = change_dir(run, ctx) / "tasks.md"
    ids = re.findall(r"^DONE:\s*([\w.]+)", text, re.M)
    if path.exists() and ids:
        content = path.read_text(encoding="utf-8")
        for task_id in ids:
            content = re.sub(rf"^(\s*- )\[ \]( {re.escape(task_id)}\b)", r"\1[x]\2", content, flags=re.M)
        path.write_text(content, encoding="utf-8", newline="\n")
    return {}


def after_code(run: Run, ctx: dict, text: str) -> dict:
    """After the implementer or fixer: tick finished tasks and keep the observability notes for the PR."""
    tick_tasks(run, ctx, text)
    return {"observability": section(text, "Observability")}


PR_SUMMARY = Stage("pr_summary", "pr_summarizer", produces="pr_narrative", gate=gate_nonempty, commit=False)


# ---------- deterministic nodes ----------

def intake(run: Run):
    def node(state: State) -> dict:
        issue = run.github.get_issue(run.issue_id)
        run.github.comment(run.issue_id, f"Foreman run `{run.run_id}` started ({run.workflow}) on branch `{run.branch}`.")
        passed(run, "intake", issue=run.issue_id)
        return {"ctx": {
            "issue_number": run.issue_id,
            "issue_title": issue.title,
            "user_story": f"# {issue.title}\n\n{issue.body}".strip(),
            "change_id": f"{slug(issue.title)[:40]}-{run.issue_id}",
            "base_commit": run.repo.head(),
        }}
    return node


def intake_with_baseline(run: Run):
    """Normal intake, plus a baseline of the current suite so new or changed tests can be recognised."""
    base = intake(run)

    def node(state: State) -> dict:
        out = base(state)
        r = sh(run.target.commands["test"], cwd=run.repo.path)
        out["ctx"]["baseline_passed"] = count_tests(r.out)
        out["ctx"]["baseline_total"] = count_total(r.out)
        return out
    return node


def clarify(run: Run):
    """Ask the human the blocking questions; unanswered ones become stated assumptions."""
    def node(state: State) -> dict:
        started = time.monotonic()
        answers, assumptions = [], []
        for q in state["ctx"].get("blocking_questions", []):
            a = run.human.answer(q)
            if a:
                answers.append(f"- Q: {q}\n  A: {a}")
            else:
                assumptions.append(f"- {q} (unanswered; assumed the simplest reasonable answer)")
        run.audit("clarify", "clarified", answered=len(answers), assumed=len(assumptions),
                  wait_s=round(time.monotonic() - started, 3))
        if answers or assumptions:
            run.github.comment(run.issue_id, "Clarification round:\n" + "\n".join(answers + assumptions))
        passed(run, "clarify")
        return {"ctx": {k: v for k, v in {"clarifications": "\n".join(answers),
                                         "assumptions": "\n".join(assumptions)}.items() if v}}
    return node


MAX_REJECTIONS = 3


def approve_proposal(run: Run):
    def node(state: State) -> dict:
        ctx = state["ctx"]
        summary = f"Change {ctx['change_id']}\n\n{ctx['change_summary']}\n\nTasks:\n{ctx['tasks']}"
        run.github.comment(run.issue_id, f"Proposal `{ctx['change_id']}` is ready for approval.\n\nTasks:\n{ctx['tasks']}")
        started = time.monotonic()
        d = run.human.approve("proposal", summary)
        run.audit("approve_proposal", "approval", kind="proposal", approved=d.approved, note=d.note,
                  who=d.who, wait_s=round(time.monotonic() - started, 3))
        if d.approved:
            run.github.comment(run.issue_id, f"Proposal `{ctx['change_id']}` approved by {d.who}.")
            return {"ctx": {"proposal_approved": True, "approved_commit": run.repo.head()}}
        rejections = ctx.get("proposal_rejections", 0) + 1
        run.github.comment(run.issue_id, f"Proposal `{ctx['change_id']}` rejected: {d.note or 'no reason given'}")
        if rejections >= MAX_REJECTIONS:
            raise SafeStop(f"proposal rejected {rejections} times; last note: {d.note}", stage="approve_proposal")
        run.repo.reset_hard(ctx.get("planning_base", ctx["base_commit"]))  # keep an archived baseline
        return {"ctx": {"proposal_approved": False, "reviewer_notes": d.note, "proposal_rejections": rejections}}
    return node


# ---------- validation ----------

def check_build_and_tests(run: Run, min_tests: int = 1) -> tuple[list[str], list[str]]:
    """Build and run the full suite. Returns (failures, result lines)."""
    failures, results = [], []
    with run.lock:
        build = sh(run.target.commands["build"], cwd=run.repo.path)
        tests = sh(run.target.commands["test"], cwd=run.repo.path)
    results.append(f"build: {'pass' if build.code == 0 else 'FAIL'}")
    if build.code:
        failures.append(f"build failed:\n{tail(build.out)}")
    n = count_tests(tests.out)
    if tests.code:
        results.append("tests: FAIL")
        failures.append(f"tests failed:\n{tail(tests.out)}")
    elif n < min_tests:
        results.append(f"tests: {n} passed (need {min_tests})")
        failures.append(f"test count too low: {n} passed, need at least {min_tests} (tests must not be removed)")
    else:
        results.append(f"tests: {n} passed")
    return failures, results


FOREMAN_OWNED = ["openspec/specs/**", "openspec/changes/archive/**"]  # only Foreman's own code writes these (archiving)


def check_branch_policy(run: Run, ctx: dict) -> tuple[list[str], list[str]]:
    """Policy over the whole branch, not just the last stage."""
    writable = [*FOREMAN_OWNED, *(w for a in run.agents.values() for w in writable_for(run, a, ctx))]
    findings = check_changes(run.repo.branch_changes(ctx["base_commit"]),
                             run.repo.branch_added_text(ctx["base_commit"]), writable, run.target)
    blocking = [f.detail for f in findings if f.blocking]
    return [f"policy: {d}" for d in blocking], [f"policy: {'clean' if not blocking else 'FAIL'}"]


TASK_RULE = ("A task is ticked only when its agent ends a reply with `DONE: <task id>`; "
             "Foreman does not run a task's own verification step.")


def unchecked_tasks(text: str) -> list[tuple[str, str, str]]:
    """(id, group, title) of each unchecked task in a tasks.md; the group is its `## 1. Implementation` heading."""
    found, group = [], ""
    for line in text.splitlines():
        if heading := re.match(r"^##\s+(?:\d+\.\s*)?(.*\S)\s*$", line):
            group = heading.group(1)
        elif task := re.match(r"^\s*- \[ \]\s+(\S+)\s*(.*)$", line):
            found.append((task.group(1), group, task.group(2)))
    return found


def unreported_tasks(tasks: list[tuple[str, str, str]]) -> str:
    """The failure for tasks nobody reported done: the first line names them, the rest says what each one is."""
    named = ", ".join(f"{i} ({g})" if g else i for i, g, _ in tasks)
    return "\n".join([f"tasks not reported done: {named}", *(f"- {i} {title}".rstrip() for i, _, title in tasks), TASK_RULE])


def validate_feature_checks(run: Run, ctx: dict, min_tests: int = 1) -> tuple[list[str], str]:
    """Deterministic validation: build, full tests, tasks ticked, OpenSpec, branch-wide policy."""
    failures, results = check_build_and_tests(run, min_tests)

    tasks_file = change_dir(run, ctx) / "tasks.md"
    text = tasks_file.read_text(encoding="utf-8") if tasks_file.exists() else ""
    unchecked = unchecked_tasks(text)
    total = len(unchecked) + len(re.findall(r"^\s*- \[x\]", text, re.M | re.I))
    results.append(f"tasks: {total - len(unchecked)}/{total}")
    if not text:
        failures.append("tasks.md is missing")
    elif unchecked:
        failures.append(unreported_tasks(unchecked))

    ok, out = run.spec.validate(ctx["change_id"])
    results.append(f"openspec: {'valid' if ok else 'INVALID'}")
    if not ok:
        failures.append(f"openspec validate failed:\n{out}")

    policy_failures, policy_results = check_branch_policy(run, ctx)
    return failures + policy_failures, "\n".join(results + policy_results)


DETAIL_LINES = 15  # most lines of one failure that go into the issue comment (the end of a long build or test output)


def failure_detail(failures: list[str]) -> str:
    """What each failed check said beyond its first line, so the issue thread shows the error and not just its name."""
    blocks = [[lines[0], *lines[1:][-DETAIL_LINES:]] for f in failures if len(lines := f.splitlines()) > 1]
    return "\n\n".join("\n".join(block) for block in blocks)


def finish_validation(run: Run, ctx: dict, failures: list[str], results: str) -> dict:
    """Shared tail of every validate node: pass, or loop back (max_retries times), or safe-stop with a rollback."""
    if not failures:
        run.github.comment(run.issue_id, f"Validation passed:\n```\n{results}\n```")
        passed(run, "validate", results=results)
        return {"ctx": {"validated": True, "validate_results": results}}
    loops = ctx.get("validate_loops", 0) + 1
    reason = "; ".join(f.splitlines()[0] for f in failures)
    detail = failure_detail(failures)
    run.github.comment(
        run.issue_id,
        f"Validation failed (attempt {loops} of {run.max_retries + 1}): {reason}\n\n"
        + (f"{detail}\n\n" if detail else "") + f"```\n{results}\n```")
    run.audit("validate", "attempt_failed", attempt=loops, reason=reason)
    if loops > run.max_retries:
        checks = "; ".join(results.splitlines())
        raise SafeStop(f"validation failed {loops} times: {reason} (last checks: {checks})", rollback=True, stage="validate")
    run.audit("validate", "retry", attempt=loops)
    return {"ctx": {"validated": False, "validate_loops": loops, "validate_results": results,
                    "last_failure": f"(validation attempt {loops})\n" + "\n".join(failures)}}


def archive_spec(run: Run):
    def node(state: State) -> dict:
        try:
            run.spec.archive(state["ctx"]["change_id"])
        except RuntimeError as e:
            raise SafeStop(str(e), stage="archive_spec") from e
        run.repo.commit("foreman: archive_spec")
        passed(run, "archive_spec")
        return {}
    return node


# ---------- pull request ----------

def open_pr(run: Run):
    def node(state: State) -> dict:
        ctx = state["ctx"]
        body = build_body(run, ctx, ctx.get("pr_narrative", ""))
        title = f"[foreman/{run.workflow}] {ctx['issue_title']}"
        run.github.push(run.branch)
        pr = run.github.open_pr(run.branch, title, body, [f"foreman:{run.workflow}"], closes=run.issue_id)
        url = run.github.pr_url(pr)
        run.github.comment(run.issue_id, f"Pull request #{pr} opened: {url}. Review and merge it to release.")
        if ctx.get("observability"):  # where the observe graph finds it later
            run.github.comment(run.issue_id, f"{OBSERVABILITY_MARK}\n{ctx['observability']}")
        run.audit("open_pr", "pr_opened", pr=pr, url=url)
        passed(run, "open_pr", pr=pr)
        return {"ctx": {"pr_number": pr}}
    return node


def await_merge(run: Run):
    """The release gate: a human merges the PR. The run waits here, in this process."""
    def node(state: State) -> dict:
        pr = state["ctx"]["pr_number"]
        started = time.monotonic()
        run.github.wait_for_merge(pr)
        st = run.github.pr_state(pr)
        if st.state == "closed":
            run.audit("await_merge", "pr_closed", pr=pr, comments=st.comments)
            raise SafeStop(f"PR #{pr} was closed without merge; reviewer comments: {st.comments}", stage="await_merge")
        if st.state != "merged":
            raise SafeStop(f"PR #{pr} is still open; merge it on GitHub, then `foreman resume {run.run_id}`",
                           stage="await_merge")
        run.audit("await_merge", "merge_wait", wait_s=round(time.monotonic() - started, 3))
        sha = run.github.merged_commit(pr)
        run.github.comment(run.issue_id, f"PR #{pr} merged ({sha[:8]}).")
        passed(run, "await_merge", merge_sha=sha)
        return {"ctx": {"merge_sha": sha}}
    return node
