"""existing_change: a new feature in a repo that already has application code   (DESIGN.md section 5.2)

Same graph as new_implementation, with an impact-analysis step first: which files, modules, APIs and data flows
the change touches. Code checks that every file it names exists, so the rest of the run builds on facts.
"""
import re

from langgraph.graph import StateGraph

from foreman.core.runner import Run, Stage
from foreman.core.util import section
from foreman.workflows import new_implementation
from foreman.workflows import nodes as n

SECTIONS = ["Impacted files", "Modules and APIs", "Data flows"]


def listed_paths(text: str) -> list[str]:
    """The path at the start of each bullet under 'Impacted files' (backticks and trailing notes stripped)."""
    paths = []
    for bullet in n.parse_bullets(section(text, "Impacted files")):
        m = re.match(r"[`'\"]?([\w./\\-]+)", bullet)
        if m:
            paths.append(m.group(1))
    return paths


def gate_impact(run: Run, ctx: dict, text: str) -> str | None:
    missing = [h for h in SECTIONS if not section(text, h)]
    if missing:
        return f"the impact analysis is missing sections: {', '.join(missing)}"
    paths = listed_paths(text)
    if not paths:
        return "'Impacted files' must list at least one existing file as a bullet"
    absent = [p for p in paths if not (run.repo.path / p).exists()]
    return f"these 'Impacted files' do not exist in the repository: {', '.join(absent)}" if absent else None


def after_impact(run: Run, ctx: dict, text: str) -> dict:
    return {"impact": text, "impacted_files": section(text, "Impacted files")}


IMPACT = Stage("impact", "impact_analyst", produces="impact_analysis", gate=gate_impact, after=after_impact,
               commit=False)


def build(run: Run) -> StateGraph:
    return new_implementation.build(run, impact=IMPACT, with_baseline=True)
