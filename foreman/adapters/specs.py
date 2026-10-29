"""The only module that knows how to call the OpenSpec CLI."""
from pathlib import Path

from foreman.core.util import sh


class SpecAdapter:
    def __init__(self, cmd: list[str], cwd: Path):
        self.cmd = cmd
        self.cwd = Path(cwd)

    def validate(self, change: str) -> tuple[bool, str]:
        r = sh([*self.cmd, "validate", change, "--strict"], cwd=self.cwd)
        return r.code == 0, r.out.strip()

    def archive(self, change: str) -> None:
        r = sh([*self.cmd, "archive", change, "--yes"], cwd=self.cwd)
        if r.code:
            raise RuntimeError(f"openspec archive {change} failed: {r.out.strip()}")
