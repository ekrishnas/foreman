"""The baseline: on a product that has code but no OpenSpec specs yet, record what it does today (DESIGN.md 7.2).

OpenSpec's `openspec/specs/` starts empty and fills as changes are archived, so the first change on an existing
product has nothing to build on, and a bugfix has nothing to modify. A baseliner agent explores the code and writes
a baseline change (ADDED requirements only); Foreman archives it straight away, then the real work carries on.
It runs once per product: afterwards `openspec/specs/` is not empty.
"""
from foreman.core.runner import Run, SafeStop, Stage
from foreman.workflows import nodes as n


def needs_baseline(run: Run) -> bool:
    return not any((run.repo.path / "openspec/specs").glob("*/spec.md"))


def baseline_id(ctx: dict) -> str:
    return f"baseline-{ctx['change_id']}"


def gate_baseline(run: Run, ctx: dict, text: str) -> str | None:
    ok, out = run.spec.validate(baseline_id(ctx))
    if not ok:
        return f"openspec validate --strict failed:\n{out}"
    specs = run.repo.path / "openspec/changes" / baseline_id(ctx) / "specs"
    deltas = "\n".join(p.read_text(encoding="utf-8") for p in sorted(specs.glob("*/spec.md")))
    if "## ADDED Requirements" not in deltas:
        return "a baseline must describe the current behaviour as requirements under '## ADDED Requirements'"
    if "## MODIFIED Requirements" in deltas or "## REMOVED Requirements" in deltas:
        return "a baseline may only contain '## ADDED Requirements': there is nothing to modify or remove yet"
    return None


def after_baseline(run: Run, ctx: dict, text: str) -> dict:
    return {"baseline_id": baseline_id(ctx)}


BASELINE = Stage("baseline", "baseliner", produces="baseline_summary", gate=gate_baseline, after=after_baseline)


def archive_baseline(run: Run):
    def node(state: dict) -> dict:
        try:
            run.spec.archive(state["ctx"]["baseline_id"])
        except RuntimeError as e:
            raise SafeStop(str(e), stage="archive_baseline") from e
        run.repo.commit("foreman: archive_baseline")
        n.passed(run, "archive_baseline")
        return {"ctx": {"planning_base": run.repo.head()}}  # a rejected proposal resets to here, not before the baseline
    return node


def wire(g, run: Run, source: str, target: str) -> None:
    """Route `source` -> `target`, with the baseline in between when the product has no specs yet."""
    g.add_node("baseline", n.agent_node(run, BASELINE))
    g.add_node("archive_baseline", archive_baseline(run))
    g.add_conditional_edges(source, lambda s: "baseline" if needs_baseline(run) else target, ["baseline", target])
    g.add_edge("baseline", "archive_baseline")
    g.add_edge("archive_baseline", target)
