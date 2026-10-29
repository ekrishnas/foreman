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


def sh(
    cmd: str | list[str], cwd: Path, env: dict | None = None, timeout: int | None = None,
    input: str | None = None, keep_env: list[str] | None = None,
) -> ShResult:
    """Run a command (shell string or argv list); stdout and stderr merged.

    Timeout gives code 124, a missing executable 127. `keep_env` is an allowlist of
    inherited variables (PATH is always kept); None inherits everything.
    """
    base = os.environ if keep_env is None else {k: v for k, v in os.environ.items() if k in keep_env or k == "PATH"}
    e = {**base, **(env or {})}
    e["PATH"] = os.path.dirname(sys.executable) + os.pathsep + e.get("PATH", "")
    try:
        p = subprocess.Popen(
            cmd, shell=isinstance(cmd, str), cwd=cwd, env=e, encoding="utf-8", errors="replace",
            stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
        )
    except FileNotFoundError as ex:
        return ShResult(127, str(ex))
    try:
        out, _ = p.communicate(input=input, timeout=timeout)
    except subprocess.TimeoutExpired:
        if hasattr(os, "killpg"):
            os.killpg(p.pid, signal.SIGKILL)  # the shell and everything it started
        else:
            p.kill()  # Windows
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


def count_tests(output: str) -> int:
    """Passed-test count from a pytest or `dotnet test` summary (0 when none is found)."""
    m = re.search(r"(\d+) passed", output) or re.search(r"Passed:\s+(\d+)", output)
    return int(m.group(1)) if m else 0


def count_failed(output: str) -> int:
    m = re.search(r"(\d+) failed", output) or re.search(r"Failed:\s+(\d+)", output)
    return int(m.group(1)) if m else 0


def count_total(output: str) -> int:
    """All tests that ran (passed, failed, errored, skipped)."""
    if m := re.search(r"Total:\s+(\d+)", output):
        return int(m.group(1))
    return sum(int(n) for n in re.findall(r"(\d+) (?:passed|failed|errors?|skipped)", output))
