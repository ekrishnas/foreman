"""Commit messages: Conventional Commits, written by the committer agent and checked here."""
import re

TYPES = ("feat", "fix", "docs", "style", "refactor", "perf", "test", "chore", "ci")
SUBJECT = re.compile(rf"^({'|'.join(TYPES)})(\([a-z0-9][a-z0-9._/-]*\))?!?: (\S.*)$")
MAX_SUBJECT = 49  # "under 50 characters"
MAX_BODY_LINE = 72
# Imperative check is a heuristic on the first word: past tense (-ed), -ing and plural/third-person (-s).
IMPERATIVE_EXCEPTIONS = {"embed", "speed", "proceed", "exceed", "succeed", "feed", "seed", "need", "alias", "bias", "canvas"}

# stage -> (type, subject) used when there is no committer agent, nothing changed, or the agent keeps breaking the rules
STAGES = {
    "requirements": ("docs", "add the requirements analysis"),
    "proposal": ("docs", "propose the change"),
    "implement": ("feat", "implement the change"),
    "tests": ("test", "add tests for the change"),
    "docs": ("docs", "document the change"),
    "archive_spec": ("docs", "archive the change spec"),
    "triage": ("docs", "add the bug triage"),
    "reproduce": ("test", "add a failing reproduction"),
    "fix": ("fix", "resolve the bug"),
    "rca": ("docs", "add the root cause analysis"),
    "explore": ("chore", "add exploration notes"),
    "analysis": ("docs", "add the exploration analysis"),
    "prune_scratch": ("chore", "drop the exploration scratch"),
    "observe_summary": ("docs", "add the observe summary"),
}


def _imperative(word: str) -> bool:
    w = word.lower().strip("`'\"().,:")
    if w in IMPERATIVE_EXCEPTIONS:
        return True
    if w.endswith("ing") and len(w) > 5:
        return False
    if w.endswith("ed") and len(w) > 3:
        return False
    return not (w.endswith("s") and not w.endswith(("ss", "us", "is")))


def lint(message: str) -> list[str]:
    """Rule violations of a commit message (without trailers); empty when it is fine."""
    lines = message.strip().splitlines()
    if not lines:
        return ["the message is empty"]
    subject = lines[0]
    out = []
    m = SUBJECT.match(subject)
    if not m:
        out.append(f"the subject must look like 'type(scope): description' with a lowercase type from {', '.join(TYPES)}")
    if len(subject) > MAX_SUBJECT:
        out.append(f"the subject is {len(subject)} characters; keep it under 50")
    if subject.endswith("."):
        out.append("the subject must not end with a period")
    imperative = [m.group(3)] if m else []
    if len(lines) > 1 and lines[1].strip():
        out.append("leave a blank line between the subject and the body")
    for n, line in enumerate(lines[1:], start=2):
        if len(line) > MAX_BODY_LINE:
            out.append(f"line {n} is {len(line)} characters; wrap the body at 72")
        if line.startswith("- "):
            imperative.append(line[2:])
    for text in imperative:
        first = text.split(maxsplit=1)[0] if text.strip() else ""
        if first and not _imperative(first):
            out.append(f"use the imperative mood ('add', not 'added' or 'adds'): '{first}'")
    return out


def clean(text: str) -> str:
    """The message itself: surrounding whitespace and a wrapping code fence are not part of it."""
    text = text.strip()
    fenced = re.fullmatch(r"```[a-z]*\n(.*?)\n?```", text, re.DOTALL)
    return (fenced.group(1) if fenced else text).strip()


def fallback(stage: str) -> str:
    """Deterministic message for a stage ('explore/option-1' counts as 'explore')."""
    base = stage.split("/")[0]
    kind, subject = STAGES.get(base, ("chore", f"record the {base} stage"))
    return f"{kind}: {subject}"
