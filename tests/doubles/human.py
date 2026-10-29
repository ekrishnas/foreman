"""A human who answers from a script, for tests."""
from foreman.core.human import Decision


class ScriptedHuman:
    """Answers from a script: {"proposal": ["reject: why", "approve"], "answers": ["..."]}.

    Approvals default to approve once a kind's list is used up; answers default to None.
    """

    def __init__(self, script: dict[str, list[str]] | None = None):
        self.script = {k: list(v) for k, v in (script or {}).items()}

    def approve(self, kind: str, summary: str) -> Decision:
        queue = self.script.get(kind, [])
        entry = queue.pop(0) if queue else "approve"
        if entry.startswith("reject"):
            return Decision(False, entry.partition(":")[2].strip(), "scripted")
        return Decision(True, "", "scripted")

    def answer(self, question: str) -> str | None:
        queue = self.script.get("answers", [])
        return queue.pop(0) if queue else None
