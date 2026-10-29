import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Protocol

from foreman.config import Agent
from foreman.gitops import GitRepo
from foreman.util import sh

# Only what `claude` itself needs: no API keys, no GitHub token, nothing else.
ENV_KEEP = [
    "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "CLAUDE_CODE_OAUTH_TOKEN",
    # Windows needs these for the process to find its profile, temp dir and shell.
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "SYSTEMROOT", "TEMP", "TMP", "HOMEDRIVE", "HOMEPATH", "PATHEXT", "COMSPEC",
]
DENIED_TOOLS = "WebFetch,WebSearch"


@dataclass
class WorkerRequest:
    agent: Agent
    model: str
    prompt: str
    cwd: Path
    key: str  # unique per worker call; names the recording
    stage: str
    call: int  # how many times this stage instance has called a worker before
    tools: list[str] = field(default_factory=list)


@dataclass
class WorkerResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    duration_ms: int = 0
    error: str | None = None


class Worker(Protocol):
    def run(self, req: WorkerRequest) -> WorkerResult: ...


def _parse_json(out: str):
    for candidate in (out, out.strip().splitlines()[-1] if out.strip() else ""):
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return None


class ClaudeCodeWorker:
    def __init__(self, timeout: int = 1800, claude_bin: str = "claude"):
        self.timeout = timeout
        self.claude_bin = claude_bin

    def run(self, req: WorkerRequest) -> WorkerResult:
        argv = [
            self.claude_bin, "-p", "--output-format", "json", "--model", req.model,
            "--allowedTools", ",".join(req.tools), "--disallowedTools", DENIED_TOOLS,
        ]
        r = sh(argv, cwd=req.cwd, input=req.prompt, timeout=self.timeout, keep_env=ENV_KEEP)
        if r.code == 127:
            return WorkerResult("", error=f"claude not found ({self.claude_bin})")
        if r.code == 124:
            return WorkerResult("", error=f"claude timed out after {self.timeout}s")
        if r.code != 0:
            return WorkerResult("", error=f"claude exited with exit code {r.code}: {r.out[-500:]}")
        data = _parse_json(r.out)
        if not isinstance(data, dict):
            return WorkerResult("", error=f"claude printed non-JSON output: {r.out[:200]}")
        usage = data.get("usage") or {}
        result = WorkerResult(
            text=data.get("result") or "",
            input_tokens=sum(usage.get(k, 0) for k in (
                "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
            output_tokens=usage.get("output_tokens", 0),
            cost_usd=data.get("total_cost_usd") or 0.0,
            duration_ms=data.get("duration_ms") or 0,
        )
        if data.get("is_error"):
            result.error = result.text or "claude reported an error"
        return result


class Recorder:
    def __init__(self, directory: Path):
        self.dir = Path(directory)

    def save(self, key: str, result: WorkerResult, snapshot: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / f"{key}.json").write_text(json.dumps({"result": asdict(result), "files": snapshot}, indent=1), encoding="utf-8", newline="\n")


class ReplayWorker:
    def __init__(self, directory: Path):
        self.dir = Path(directory)

    def run(self, req: WorkerRequest) -> WorkerResult:
        path = self.dir / f"{req.key}.json"
        if not path.exists():
            return WorkerResult("", error=f"no recording for {req.key}")
        data = json.loads(path.read_text(encoding="utf-8"))
        GitRepo(req.cwd).restore(data["files"])
        return WorkerResult(**data["result"])
