import hashlib
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ShResult:
    code: int
    out: str


def sh(cmd: str, cwd: Path, env: dict | None = None, timeout: int | None = None) -> ShResult:
    """Run a shell command; stdout and stderr merged. Timeout gives code 124."""
    e = {**os.environ, **(env or {})}
    e["PATH"] = os.path.dirname(sys.executable) + os.pathsep + e.get("PATH", "")
    p = subprocess.Popen(
        cmd, shell=True, cwd=cwd, env=e, text=True, errors="replace",
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
    )
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(p.pid, signal.SIGKILL)
        out, _ = p.communicate()
        return ShResult(124, out or "")
    return ShResult(p.returncode, out or "")


def _glob_regex(pattern: str) -> str:
    out, i = "", 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        elif pattern[i] == "?":
            out += "[^/]"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return out


def glob_match(path: str, pattern: str) -> bool:
    return re.fullmatch(_glob_regex(pattern), path) is not None


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def merge_dicts(a: dict | None, b: dict | None) -> dict:
    return {**(a or {}), **(b or {})}


def _headings(lines: list[str]):
    """Yield (index, level, title) for markdown headings outside code fences."""
    in_fence = False
    for i, line in enumerate(lines):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
        elif not in_fence:
            m = re.match(r"^(#{1,6})\s+(.*?)\s*$", line)
            if m:
                yield i, len(m.group(1)), m.group(2)


def section(text: str, heading: str) -> str:
    """Body under the first markdown heading matching `heading` (case-insensitive)."""
    lines = text.splitlines()
    heads = list(_headings(lines))
    want = heading.strip().lower()
    for n, (i, level, title) in enumerate(heads):
        if title.strip().lower() == want:
            end = len(lines)
            for j, lvl, _ in heads[n + 1:]:
                if lvl <= level:
                    end = j
                    break
            return "\n".join(lines[i + 1:end]).strip()
    return ""
