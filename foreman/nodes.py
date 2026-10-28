"""Shared node library: agent-stage wrappers, gates and deterministic nodes."""
import re
import time
from typing import Annotated, TypedDict

from langgraph.types import interrupt

from foreman.pr import build_body
from foreman.policy import check_changes
from foreman.context import writable_for
from foreman.runner import Run, SafeStop, Stage, run_stage
from foreman.util import count_failed, count_tests, count_total, glob_match, merge_dicts, section, sh, slug


class State(TypedDict, total=False):
    ctx: Annotated[dict, merge_dicts]  # context bag: everything agents may be shown
    done: Annotated[dict, merge_dicts]  # stage -> {inputs, output} hashes, for re-planning


# ---------- small helpers ----------

def parse_bullets(text: str) -> list[str]:
    return re.findall(r"^\s*[-*]\s+(.*\S)\s*$", text, re.M)


def tail(text: str, lines: int = 30) -> str:
    return "\n".join(text.strip().splitlines()[-lines:])


def change_dir(run: Run, ctx: dict):
    return run.repo.path / "openspec/changes" / ctx.get("change_id", "")


def passed(run: Run, name: str, **details) -> None:
    run.audit(name, "stage_passed", **details)


def agent_node(run: Run, stage: Stage):
    def node(state: State) -> dict:
        return run_stage(run, stage, state["ctx"], state.get("done", {}))
    return node


# ---------- gates and after-hooks for agent stages ----------

def is_brownfield(run: Run) -> bool:
    return any(glob_match(f, run.target.paths["src"]) for f in run.repo.tree().splitlines())


def gate_requirements(run: Run, ctx: dict, text: str) -> str | None:
    missing = [h for h in ("Normalized requirement", "Acceptance criteria") if not section(text, h)]
    if is_brownfield(run) and not section(text, "Impacted files"):
        missing.append("Impacted files")
    return f"requirements output is missing sections: {', '.join(missing)}" if missing else None


def after_requirements(run: Run, ctx: dict, text: str) -> dict:
    out = {
        "acceptance_criteria": section(text, "Acceptance criteria"),
        "blocking_questions": parse_bullets(section(text, "Blocking questions")),
    }
    if impacted := section(text, "Impacted files"):
        out["impacted_files"] = impacted
    return out


def gate_proposal(run: Run, ctx: dict, text: str) -> str | None:
    ok, out = run.spec.validate(ctx["change_id"])
    if not ok:
        return f"openspec validate --strict failed:\n{out}"
    design = (change_dir(run, ctx) / "design.md").read_text()
    if not section(design, "API contract"):
        return "design.md must contain an '## API contract' section"
    return None


def after_proposal(run: Run, ctx: dict, text: str) -> dict:
    d = change_dir(run, ctx)
    design = (d / "design.md").read_text()
    return {
        "design": design,
        "tasks": (d / "tasks.md").read_text(),
        "contract": section(design, "API contract"),
        "spec_scenarios": "\n\n".join(p.read_text() for p in sorted((d / "specs").glob("*/spec.md"))),
        "change_summary": (d / "proposal.md").read_text(),
    }


def gate_build(run: Run, ctx: dict, text: str) -> str | None:
    r = sh(run.target.commands["build"], cwd=run.repo.path)
    return None if r.code == 0 else f"build failed:\n{tail(r.out)}"


def gate_wrote_files(run: Run, ctx: dict, text: str) -> str | None:
    return None if run.repo.changes() else "the agent wrote no files"


def gate_nonempty(run: Run, ctx: dict, text: str) -> str | None:
    return None if text.strip() else "the agent produced no output"


def tick_tasks(run: Run, ctx: dict, text: str) -> dict:
    """Agents report `DONE: <task id>`; only Foreman edits tasks.md."""
    path = change_dir(run, ctx) / "tasks.md"
    ids = re.findall(r"^DONE:\s*([\w.]+)", text, re.M)
    if path.exists() and ids:
        content = path.read_text()
        for task_id in ids:
            content = re.sub(rf"^(\s*- )\[ \]( {re.escape(task_id)}\b)", r"\1[x]\2", content, flags=re.M)
        path.write_text(content)
    return {}


REQUIREMENTS = Stage("requirements", "requirements_analyst", produces="requirements",
                     gate=gate_requirements, after=after_requirements, commit=False)
PROPOSAL = Stage("proposal", "architect", produces="proposal_summary", gate=gate_proposal, after=after_proposal)
IMPLEMENT = Stage("implement", "implementer", produces="implemented", gate=gate_build, after=tick_tasks)
TESTS = Stage("tests", "test_writer", produces="tests_written", gate=gate_wrote_files, after=tick_tasks)
DOCS = Stage("docs", "doc_writer", produces="docs_written", gate=gate_wrote_files, after=tick_tasks)
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
        run.repo.reset_hard(ctx["base_commit"])
        return {"ctx": {"proposal_approved": False, "reviewer_notes": d.note, "proposal_rejections": rejections}}
    return node


def validate_feature_checks(run: Run, ctx: dict, min_tests: int = 1) -> tuple[list[str], str]:
    """Deterministic validation: build, full tests, tasks ticked, OpenSpec, branch-wide policy."""
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

    tasks_file = change_dir(run, ctx) / "tasks.md"
    text = tasks_file.read_text() if tasks_file.exists() else ""
    open_tasks = re.findall(r"^\s*- \[ \]\s+(\S+)", text, re.M)
    total = len(open_tasks) + len(re.findall(r"^\s*- \[x\]", text, re.M | re.I))
    results.append(f"tasks: {total - len(open_tasks)}/{total}")
    if not text:
        failures.append("tasks.md is missing")
    elif open_tasks:
        failures.append(f"unchecked tasks: {', '.join(open_tasks)}")

    ok, out = run.spec.validate(ctx["change_id"])
    results.append(f"openspec: {'valid' if ok else 'INVALID'}")
    if not ok:
        failures.append(f"openspec validate failed:\n{out}")

    writable = [w for a in run.agents.values() for w in writable_for(run, a, ctx)]
    findings = check_changes(run.repo.branch_changes(ctx["base_commit"]),
                             run.repo.branch_added_text(ctx["base_commit"]), writable, run.target)
    blocking = [f.detail for f in findings if f.blocking]
    results.append(f"policy: {'clean' if not blocking else 'FAIL'}")
    failures += [f"policy: {d}" for d in blocking]
    return failures, "\n".join(results)


def finish_validation(run: Run, ctx: dict, failures: list[str], results: str) -> dict:
    if not failures:
        passed(run, "validate", results=results)
        return {"ctx": {"validated": True, "validate_results": results}}
    loops = ctx.get("validate_loops", 0) + 1
    reason = "; ".join(f.splitlines()[0] for f in failures)
    run.audit("validate", "attempt_failed", attempt=loops, reason=reason)
    if loops > run.max_retries:
        raise SafeStop(f"validation failed {loops} times: {reason}", rollback=True, stage="validate")
    run.audit("validate", "retry", attempt=loops)
    return {"ctx": {"validated": False, "validate_loops": loops, "validate_results": results,
                    "last_failure": f"(validation attempt {loops})\n" + "\n".join(failures)}}


def validate_feature(run: Run):
    def node(state: State) -> dict:
        ctx = state["ctx"]
        failures, results = validate_feature_checks(run, ctx)
        return finish_validation(run, ctx, failures, results)
    return node


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


def open_pr(run: Run):
    def node(state: State) -> dict:
        ctx = state["ctx"]
        body = build_body(run, ctx, ctx.get("pr_narrative", ""))
        title = f"[foreman/{run.workflow}] {ctx['issue_title']}"
        run.github.push(run.branch)
        pr = run.github.open_pr(run.branch, title, body, [f"foreman:{run.workflow}"], closes=run.issue_id)
        run.github.comment(run.issue_id, f"Pull request #{pr} opened. Review and merge it to release.")
        run.audit("open_pr", "pr_opened", pr=pr)
        passed(run, "open_pr", pr=pr)
        return {"ctx": {"pr_number": pr}}
    return node


def await_merge(run: Run):
    """The release gate: a human merges the PR. Pauses the graph until then."""
    def node(state: State) -> dict:
        pr = state["ctx"]["pr_number"]
        if run.github.pr_state(pr).state == "open":
            interrupt({"waiting_for": f"merge of PR #{pr}"})
        st = run.github.pr_state(pr)
        if st.state == "closed":
            run.audit("await_merge", "pr_closed", pr=pr, comments=st.comments)
            raise SafeStop(f"PR #{pr} was closed without merge; reviewer comments: {st.comments}", stage="await_merge")
        if st.state != "merged":
            raise SafeStop(f"PR #{pr} is still open; merge it on GitHub, then resume", stage="await_merge")
        opened = [e for e in run.store.audit_events(run.run_id) if e.event == "pr_opened"]
        if opened:
            from datetime import datetime, timezone
            waited = (datetime.now(timezone.utc) - datetime.fromisoformat(opened[-1].ts)).total_seconds()
            run.audit("await_merge", "merge_wait", wait_s=round(waited, 3))
        sha = run.github.merged_commit(pr)
        run.github.comment(run.issue_id, f"PR #{pr} merged ({sha[:8]}).")
        passed(run, "await_merge", merge_sha=sha)
        return {"ctx": {"merge_sha": sha}}
    return node


# ---------- bugfix ----------

RCA_SECTIONS = ["Summary", "Impact", "Timeline", "Root cause", "Why it was missed", "Fix", "Prevention"]


def bugfix_intake(run: Run):
    """Normal intake, plus a baseline of the current suite so a new failing test can be recognised."""
    base = intake(run)

    def node(state: State) -> dict:
        out = base(state)
        r = sh(run.target.commands["test"], cwd=run.repo.path)
        out["ctx"]["baseline_passed"] = count_tests(r.out)
        out["ctx"]["baseline_total"] = count_total(r.out)
        if logs := section(run.github.get_issue(run.issue_id).body, "Logs"):
            out["ctx"]["log_excerpt"] = logs
        return out
    return node


def spec_deltas(run: Run, ctx: dict) -> str:
    return "\n\n".join(p.read_text() for p in sorted((change_dir(run, ctx) / "specs").glob("*/spec.md")))


def gate_triage(run: Run, ctx: dict, text: str) -> str | None:
    ok, out = run.spec.validate(ctx["change_id"])
    if not ok:
        return f"openspec validate --strict failed:\n{out}"
    if "## MODIFIED Requirements" not in spec_deltas(run, ctx):
        return "triage must modify an existing requirement (## MODIFIED Requirements) and describe the correct behaviour in a scenario"
    return None


def after_triage(run: Run, ctx: dict, text: str) -> dict:
    d = change_dir(run, ctx)
    proposal, design = (d / "proposal.md").read_text(), (d / "design.md").read_text()
    return {"triage": "\n\n".join([proposal, design, spec_deltas(run, ctx)]), "design": design,
            "tasks": (d / "tasks.md").read_text(), "change_summary": proposal}


def gate_reproduce(run: Run, ctx: dict, text: str) -> str | None:
    r = sh(run.target.commands["test"], cwd=run.repo.path)
    if r.code == 0:
        return "No failing test, no fix: every test passes on the current code. Write a test that fails because of the bug."
    if count_total(r.out) <= ctx["baseline_total"]:
        return "No new test was added: the suite has no more tests than before the change."
    if count_failed(r.out) < 1:
        return f"The suite errored without a failing test (build or collection error?):\n{tail(r.out)}"
    if count_tests(r.out) < ctx["baseline_passed"]:
        return f"Existing tests broke: {count_tests(r.out)} pass, {ctx['baseline_passed']} passed before."
    return None


def after_reproduce(run: Run, ctx: dict, text: str) -> dict:
    tick_tasks(run, ctx, text)
    r = sh(run.target.commands["test"], cwd=run.repo.path)
    tests_glob = run.target.paths["tests"]
    written = [f"### {path}\n{body}" for path, body in run.repo.snapshot().items()
               if body is not None and glob_match(path, tests_glob)]
    return {"failing_test": "\n\n".join(written), "failure_output": tail(r.out, 40)}


def gate_rca(run: Run, ctx: dict, text: str) -> str | None:
    path = run.repo.path / f"docs/rca/{ctx['issue_number']}.md"
    if not path.exists():
        return f"docs/rca/{ctx['issue_number']}.md was not written"
    missing = [h for h in RCA_SECTIONS if not section(path.read_text(), h)]
    return f"the RCA is missing sections: {', '.join(missing)}" if missing else None


def after_rca(run: Run, ctx: dict, text: str) -> dict:
    return {"rca_path": f"docs/rca/{ctx['issue_number']}.md"}


TRIAGE = Stage("triage", "triager", produces="triage_summary", gate=gate_triage, after=after_triage)
REPRODUCE = Stage("reproduce", "reproducer", produces="reproduced", gate=gate_reproduce, after=after_reproduce)
FIX = Stage("fix", "fixer", produces="fixed", gate=gate_build, after=tick_tasks)
RCA = Stage("rca", "rca_writer", produces="rca_summary", gate=gate_rca, after=after_rca)


def validate_bugfix(run: Run):
    def node(state: State) -> dict:
        ctx = state["ctx"]
        failures, results = validate_feature_checks(run, ctx, min_tests=ctx["baseline_passed"] + 1)
        return finish_validation(run, ctx, failures, results)
    return node
