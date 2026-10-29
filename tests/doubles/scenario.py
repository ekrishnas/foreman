"""A scripted run for tests: the issue text, what the developer typed, scripted human answers, injected faults
and what should be true afterwards."""
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from foreman.core.config import RunConfig


@dataclass(frozen=True)
class Scenario:
    name: str
    input: str  # markdown file whose first `# ` heading and body become the GitHub issue
    request: str = "work on this issue"  # what the developer typed
    start_ref: str = "main"  # main: empty project; v1: has the shortener; v1-regressed: has the stale-count bug
    script: dict = field(default_factory=dict)  # answers the scripted human gives
    budget_usd: float = 10.0
    faults: list = field(default_factory=list)
    expect: dict = field(default_factory=dict)

    def config(self) -> RunConfig:
        return RunConfig(issue=0, budget_usd=self.budget_usd, start_ref=self.start_ref)


def load_scenario(path: Path) -> Scenario:
    return Scenario(**yaml.safe_load(Path(path).read_text(encoding="utf-8")))


def check_expect(session, result, scenario: Scenario) -> list[str]:
    """Evaluate the scenario's `expect` block; returns human-readable failures."""
    run, expect, problems = session.run, scenario.expect, []
    events = {e.event for e in run.store.audit_events(run.run_id)}
    if "type" in expect and run.workflow != expect["type"]:
        problems.append(f"classified as {run.workflow!r}, expected {expect['type']!r}")
    if "status" in expect and result.status != expect["status"]:
        problems.append(f"status is {result.status!r}, expected {expect['status']!r} ({result.reason})")
    if "pr_state" in expect:
        state = run.github.pr_state(result.pr).state if result.pr else "none"
        if state != expect["pr_state"]:
            problems.append(f"PR state is {state!r}, expected {expect['pr_state']!r}")
    for event in expect.get("audit", []):
        if event not in events:
            problems.append(f"audit event {event!r} never happened")
    for rel in expect.get("files", []):
        if not (run.repo.path / rel).exists():
            problems.append(f"expected file {rel} is missing")
    if result.pr and expect.get("pr_body_contains"):
        body = run.github.pr_state(result.pr).body
        problems += [f"PR body lacks {needle!r}" for needle in expect["pr_body_contains"] if needle not in body]
    if "max_cost_usd" in expect and run.run_cost() > expect["max_cost_usd"]:
        problems.append(f"cost ${run.run_cost():.2f} exceeds ${expect['max_cost_usd']:.2f}")
    return problems
