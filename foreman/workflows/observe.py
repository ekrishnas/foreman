"""observe: the work is deployed; read its logs and say whether it works   (DESIGN.md section 5.7)

gather -> locate_logs -> read_logs -> observe -> report

Started by a sentence like "issue #1 is deployed, check the logs". It finds where the logs are, in this order:
a path in your message, a location saved earlier on the issue, the "Observability notes" the implementer left
on the issue. If none of those exists it asks you, and saves your answer on the issue for next time.
The observer agent sees only the log excerpt (never the source); code checks that every line it quotes as
evidence really is in the log. Local files only: a log service such as Datadog is a later improvement.
"""
import re
from pathlib import Path

from langgraph.graph import END, START, StateGraph

from foreman.core.runner import Run, SafeStop, Stage, run_stage
from foreman.core.util import section
from foreman.workflows import nodes as n

LOCATION_MARK = "Foreman log location:"
VERDICTS = ("working", "failing", "inconclusive")
MAX_LINES, MAX_BYTES = 300, 200_000
MAX_ASKS = 2


def paths_in(text: str) -> list[str]:
    """Things in a sentence that look like a path: contain a slash, or end in .log."""
    return re.findall(r"[^\s'\"`]+[\\/][^\s'\"`]+|[^\s'\"`]+\.log\b", text)


def location_in_notes(notes: str) -> str:
    m = re.search(r"Log location[^:\n]*:\s*`?([^`\n]+)`?", notes, re.I)
    return m.group(1).strip() if m else ""


def resolve(run: Run, raw: str) -> Path | None:
    path = Path(raw.strip().strip("`'\""))
    path = path if path.is_absolute() else run.repo.path / path
    return path if path.exists() else None


def gather(run: Run):
    def node(state: n.State) -> dict:
        issue = run.github.get_issue(run.issue_id)
        notes = saved = ""
        for comment in issue.comments:  # oldest first, so the latest of each kind wins
            if comment.startswith(n.OBSERVABILITY_MARK):
                notes = comment[len(n.OBSERVABILITY_MARK):].strip()
            elif comment.startswith(LOCATION_MARK):
                saved = comment[len(LOCATION_MARK):].strip()
        n.passed(run, "gather", has_notes=bool(notes), saved_location=saved)
        return {"ctx": {
            "issue_number": run.issue_id,
            "request": run.request,
            "user_story": f"# {issue.title}\n\n{issue.body}".strip(),
            "observability_notes": notes or "(the implementer left no observability notes)",
            "saved_location": saved,
            "notes_location": location_in_notes(notes),
        }}
    return node


def locate_logs(run: Run):
    def node(state: n.State) -> dict:
        ctx = state["ctx"]
        for raw in [*paths_in(run.request), ctx["saved_location"], ctx["notes_location"]]:
            if raw and (found := resolve(run, raw)):
                if raw != ctx["saved_location"]:
                    run.github.comment(run.issue_id, f"{LOCATION_MARK} {raw}")
                return {"ctx": {"log_location": str(found)}}
        question = (f"Where are the logs for #{run.issue_id}? Give a file or folder path "
                    f"(a relative path is taken inside {run.repo.path}).")
        for _ in range(MAX_ASKS):
            raw = run.human.answer(question)
            if not raw:
                break
            if found := resolve(run, raw):
                run.github.comment(run.issue_id, f"{LOCATION_MARK} {raw}")
                return {"ctx": {"log_location": str(found)}}
            question = f"I could not find '{raw}'. Where are the logs? (a file or folder path)"
        raise SafeStop("no usable log location was given", stage="locate_logs")
    return node


def read_logs(run: Run):
    def node(state: n.State) -> dict:
        path = Path(state["ctx"]["log_location"])
        if path.is_dir():
            files = sorted((f for f in path.rglob("*") if f.is_file()), key=lambda f: f.stat().st_mtime, reverse=True)
            if not files:
                raise SafeStop(f"{path} contains no files", stage="read_logs")
            path = files[0]
        text = path.read_bytes()[-MAX_BYTES:].decode("utf-8", errors="replace")
        excerpt = "\n".join(text.splitlines()[-MAX_LINES:])
        if not excerpt.strip():
            raise SafeStop(f"{path} is empty", stage="read_logs")
        n.passed(run, "read_logs", log_file=str(path), lines=len(excerpt.splitlines()))
        return {"ctx": {"log_file": str(path), "log_excerpt": excerpt}}
    return node


def gate_observation(run: Run, ctx: dict, text: str) -> str | None:
    m = re.search(r"^Verdict:\s*(\w+)", text, re.I | re.M)
    if not m or m.group(1).lower() not in VERDICTS:
        return f"the first line must be 'Verdict: {'|'.join(VERDICTS)}'"
    quotes = re.findall(r"`([^`\n]+)`", section(text, "Evidence"))
    if m.group(1).lower() != "inconclusive" and not quotes:
        return "under '## Evidence', quote at least one log line in backticks"
    invented = [q for q in quotes if q.strip() not in ctx["log_excerpt"]]
    return f"these quoted lines are not in the log excerpt: {'; '.join(invented)}" if invented else None


def after_observation(run: Run, ctx: dict, text: str) -> dict:
    verdict = re.search(r"^Verdict:\s*(\w+)", text, re.I | re.M).group(1).lower()
    return {"verdict": verdict, "evidence": section(text, "Evidence"), "finding": section(text, "Summary"),
            "summary": f"{verdict}; logs checked: {ctx['log_file']}"}


OBSERVE = Stage("observe", "observer", produces="observation", gate=gate_observation, after=after_observation,
                commit=False, sandbox=True)


def observe_node(run: Run):
    def node(state: n.State) -> dict:
        return run_stage(run, OBSERVE, state["ctx"])
    return node


def report(run: Run):
    def node(state: n.State) -> dict:
        ctx = state["ctx"]
        run.github.comment(
            run.issue_id,
            f"Observe result for #{run.issue_id}: **{ctx['verdict']}**\n\n{ctx['finding']}\n\n"
            f"Evidence:\n{ctx['evidence']}\n\nLogs checked: `{ctx['log_file']}`")
        n.passed(run, "report", verdict=ctx["verdict"])
        return {}
    return node


def build(run: Run) -> StateGraph:
    g = StateGraph(n.State)
    g.add_node("gather", gather(run))
    g.add_node("locate_logs", locate_logs(run))
    g.add_node("read_logs", read_logs(run))
    g.add_node("observe", observe_node(run))
    g.add_node("report", report(run))

    g.add_edge(START, "gather")
    g.add_edge("gather", "locate_logs")
    g.add_edge("locate_logs", "read_logs")
    g.add_edge("read_logs", "observe")
    g.add_edge("observe", "report")
    g.add_edge("report", END)
    return g
