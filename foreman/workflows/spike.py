"""spike: the requirement is ambiguous, so explore before committing to a build   (DESIGN.md section 5.6)

intake -> clarify -> frame -> explore(option 1 || 2 || 3) -> analysis -> prune_scratch -> open_pr
  -> AWAIT_MERGE -> handoff

Only the analysis document is merged. The handoff files a new GitHub issue from its draft user story, and
that issue can then go through /foreman like any other.
"""
import re
import shutil

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from foreman.core.runner import Run, Stage, run_stage
from foreman.core.util import section
from foreman.workflows import nodes as n

SPIKE_SECTIONS = ["Context", "Assumptions", "Options", "Trade-off matrix", "Recommendation",
                  "Open questions", "Draft user story"]
CRITERIA = ["complexity", "cost", "performance", "risk", "operability"]
MAX_OPTIONS = 3


def parse_options(text: str) -> list[dict]:
    """Options are the `###` headings under `## Options`; ids are assigned by position."""
    chunks = re.split(r"^###\s+", section(text, "Options"), flags=re.M)[1:]
    options = []
    for i, chunk in enumerate(chunks, 1):
        title, _, body = chunk.partition("\n")
        title = re.sub(r"^(option[\s-]*\d+\s*[:.\-]\s*|\d+\.\s*)", "", title.strip(), flags=re.I)
        options.append({"id": f"option-{i}", "title": title, "body": body.strip()})
    return options


def split_story(text: str) -> tuple[str, str]:
    """A draft user story's title is its leading heading, any level (else its first line); the body is the rest."""
    text = text.strip()
    first, _, rest = text.partition("\n")
    if m := re.match(r"^#{1,6}\s+(.*\S)\s*$", first):
        return m.group(1), rest.strip()
    return first.strip(), text


def intake(run: Run):
    """Normal intake; the issue's `## Questions` section becomes the blocking questions to clarify."""
    base = n.intake(run)

    def node(state: dict) -> dict:
        out = base(state)
        body = run.github.get_issue(run.issue_id).body
        out["ctx"]["blocking_questions"] = n.parse_bullets(section(body, "Questions"))
        return out
    return node


def gate_frame(run: Run, ctx: dict, text: str) -> str | None:
    found = len(parse_options(text))
    if not 1 <= found <= MAX_OPTIONS:
        return f"frame must list between 1 and {MAX_OPTIONS} options under '## Options' as '### <title>' headings (found {found})"
    return None


def after_frame(run: Run, ctx: dict, text: str) -> dict:
    return {"options": parse_options(text), "constraints": section(text, "Constraints") or "(none stated)"}


def notes_path(run: Run, ctx: dict) -> str:
    return f"spikes/{run.run_id}/{ctx['option_id']}/notes.md"


def gate_explore(run: Run, ctx: dict, text: str) -> str | None:
    path = run.repo.path / notes_path(run, ctx)
    return None if path.exists() and path.read_text(encoding="utf-8").strip() else f"{notes_path(run, ctx)} was not written"


def after_explore(run: Run, ctx: dict, text: str) -> dict:
    return {f"notes::{ctx['option_id']}": (run.repo.path / notes_path(run, ctx)).read_text(encoding="utf-8")}


def analysis_path(run: Run) -> str:
    return f"docs/spikes/{run.run_id}/analysis.md"


def gate_analysis(run: Run, ctx: dict, text: str) -> str | None:
    path = run.repo.path / analysis_path(run)
    if not path.exists():
        return f"{analysis_path(run)} was not written"
    body = path.read_text(encoding="utf-8")
    missing = [h for h in SPIKE_SECTIONS if not section(body, h)]
    if missing:
        return f"the analysis is missing sections: {', '.join(missing)}"
    matrix = section(body, "Trade-off matrix").lower()
    lacking = [c for c in CRITERIA if c not in matrix]
    return f"the trade-off matrix must cover: {', '.join(lacking)}" if lacking else None


def after_analysis(run: Run, ctx: dict, text: str) -> dict:
    body = (run.repo.path / analysis_path(run)).read_text(encoding="utf-8")
    return {"analysis_path": analysis_path(run), "draft_story": section(body, "Draft user story"),
            "pr_narrative": section(body, "Recommendation")}


FRAME = Stage("frame", "framer", produces="framing", gate=gate_frame, after=after_frame, commit=False)
EXPLORE = Stage("explore", "explorer", gate=gate_explore, after=after_explore)
ANALYSIS = Stage("analysis", "analyst", produces="analysis_summary", gate=gate_analysis, after=after_analysis)


def fan_out_options(state: dict) -> list[Send]:
    """One explorer per framed option, each with only its own option text."""
    ctx = state["ctx"]
    return [Send("explore", {"ctx": {**ctx, "option": f"{o['title']}\n\n{o['body']}", "option_id": o["id"]}})
            for o in ctx["options"]]


def explore_node(run: Run):
    def node(payload: dict) -> dict:
        return run_stage(run, EXPLORE, payload["ctx"], instance=payload["ctx"]["option_id"])
    return node


def analysis_node(run: Run):
    def node(state: dict) -> dict:
        ctx = state["ctx"]
        notes = {k.split("::", 1)[1]: v for k, v in ctx.items() if k.startswith("notes::")}
        gathered = "\n\n".join(f"### {option}\n{text}" for option, text in sorted(notes.items()))
        return run_stage(run, ANALYSIS, {**ctx, "explorer_notes": gathered})
    return node


def prune_scratch(run: Run):
    """Only the analysis document is merged: keep a record of the exploration, then drop it from the branch."""
    def node(state: dict) -> dict:
        scratch = run.repo.path / "spikes" / run.run_id
        if scratch.exists():
            shutil.copytree(scratch, run.dir / "spikes", dirs_exist_ok=True)
            run.repo.git("rm", "-r", "-q", "-f", f"spikes/{run.run_id}")
            run.repo.commit("foreman: drop exploration scratch")
        n.passed(run, "prune_scratch")
        return {}
    return node


def handoff(run: Run):
    def node(state: dict) -> dict:
        ctx = state["ctx"]
        title, body = split_story(ctx["draft_story"])
        footer = f"\n\n---\nSpawned from spike #{run.issue_id} (PR #{ctx['pr_number']}). Analysis: `{ctx['analysis_path']}`."
        issue = run.github.create_issue(title, body + footer, ["foreman:from-spike"])
        run.github.comment(run.issue_id, f"The recommended option's draft user story was filed as issue #{issue}.")
        n.passed(run, "handoff", issue=issue)
        return {"ctx": {"handoff_issue": issue, "summary": f"follow-up issue #{issue} filed"}}
    return node


def build(run: Run) -> StateGraph:
    g = StateGraph(n.State)
    g.add_node("intake", intake(run))
    g.add_node("clarify", n.clarify(run))
    g.add_node("frame", n.agent_node(run, FRAME))
    g.add_node("explore", explore_node(run))
    g.add_node("analysis", analysis_node(run))
    g.add_node("prune_scratch", prune_scratch(run))
    g.add_node("open_pr", n.open_pr(run))
    g.add_node("await_merge", n.await_merge(run))
    g.add_node("handoff", handoff(run))

    g.add_edge(START, "intake")
    g.add_edge("intake", "clarify")
    g.add_edge("clarify", "frame")
    g.add_conditional_edges("frame", fan_out_options, ["explore"])  # dynamic fan-out, one explorer per option
    g.add_edge("explore", "analysis")
    g.add_edge("analysis", "prune_scratch")
    g.add_edge("prune_scratch", "open_pr")
    g.add_edge("open_pr", "await_merge")
    g.add_edge("await_merge", "handoff")
    g.add_edge("handoff", END)
    return g
