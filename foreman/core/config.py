from dataclasses import dataclass
from pathlib import Path

import yaml

WORKFLOWS = ("new_implementation", "existing_change", "bugfix", "test_improvement", "doc_improvement", "spike", "observe")  # what the router can choose


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
    dependency_manifests: list
    protected: list


PROFILE = ".foreman/target.yaml"  # where a product describes itself, inside the product repo


@dataclass(frozen=True)
class RunConfig:
    issue: int  # the GitHub issue to work on
    repo: str = "."  # the product repo Foreman works on (where Claude Code is open)
    budget_usd: float = 10.0
    start_ref: str = "main"  # the branch or tag the run branch starts from


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


def load_target(path: Path, repo: Path | None = None) -> Target:
    """A product profile. It lives in the product repo as `.foreman/target.yaml`, so the repo is two levels up."""
    path = Path(path).resolve()
    d = _read(path)
    repo = Path(repo) if repo else path.parent.parent
    return Target(
        name=d.get("name") or repo.name, repo=repo, paths=d["paths"], commands=d["commands"],
        dependency_manifests=d.get("dependency_manifests", []), protected=d.get("protected", []),
    )


# OpenSpec CLI commands an agent may run. Never `archive` or `apply`: only Foreman's code archives a change.
OPENSPEC_READ = [f"Bash(openspec {c}:*)" for c in ("list", "context", "status", "instructions", "show", "validate")]
OPENSPEC_AUTHOR = [*OPENSPEC_READ, "Bash(openspec new change:*)"]


def allowed_tools(agent: Agent, target: Target) -> list[str]:
    """Expand the symbolic tools (build_cmd, test_cmd, openspec_read, openspec_author) into scoped Bash permissions."""
    expand = {
        "build_cmd": [f"Bash({target.commands['build']}:*)"],
        "test_cmd": [f"Bash({target.commands['test']}:*)"],
        "openspec_read": OPENSPEC_READ,
        "openspec_author": OPENSPEC_AUTHOR,
    }
    return [tool for t in agent.tools for tool in expand.get(t, [t])]
