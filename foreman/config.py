from dataclasses import dataclass, field
from pathlib import Path

import yaml

WORKFLOWS = ("feature", "bugfix", "spike")


@dataclass(frozen=True)
class Agent:
    name: str
    tier: str
    tools: list
    writable: list
    context: list

    def context_keys(self) -> list[tuple[str, bool]]:
        """(key, required) pairs; a trailing '?' marks a key optional."""
        return [(k.rstrip("?"), not k.endswith("?")) for k in self.context]


@dataclass(frozen=True)
class Target:
    name: str
    repo: Path
    paths: dict
    commands: dict
    health_url: str
    slo: dict
    dependency_manifests: list
    protected: list


@dataclass(frozen=True)
class Scenario:
    name: str
    workflow: str
    input: str | int
    target: str
    start_ref: str = "main"
    approvals: str = "scripted"
    script: dict = field(default_factory=dict)
    budget_usd: float = 10.0
    faults: list = field(default_factory=list)
    expect: dict = field(default_factory=dict)
    tag: str = ""  # git tag to put on the merged release commit, e.g. v1


def _read(path: Path) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def load_agents(path: Path) -> tuple[dict[str, Agent], dict[str, str]]:
    data = _read(path)
    models = data["models"]
    agents = {}
    for name, a in data["agents"].items():
        if a["tier"] not in models:
            raise ValueError(f"agent {name}: unknown tier {a['tier']}")
        agents[name] = Agent(name, a["tier"], a["tools"], a["writable"], a["context"])
    return agents, models


def load_target(path: Path) -> Target:
    path = Path(path).resolve()
    d = _read(path)
    # `repo` is relative to the Foreman repo root (the parent of targets/).
    repo = (path.parent.parent / d["repo"]).resolve()
    return Target(
        name=d["name"], repo=repo, paths=d["paths"], commands=d["commands"],
        health_url=d["health_url"], slo=d["slo"],
        dependency_manifests=d["dependency_manifests"], protected=d["protected"],
    )


def load_scenario(path: Path) -> Scenario:
    d = _read(path)
    if d.get("workflow") not in WORKFLOWS:
        raise ValueError(f"scenario workflow must be one of {WORKFLOWS}, got {d.get('workflow')!r}")
    return Scenario(**d)


def allowed_tools(agent: Agent, target: Target) -> list[str]:
    """Expand the symbolic build_cmd/test_cmd tools into scoped Bash permissions."""
    expand = {
        "build_cmd": f"Bash({target.commands['build']}:*)",
        "test_cmd": f"Bash({target.commands['test']}:*)",
    }
    return [expand.get(t, t) for t in agent.tools]
