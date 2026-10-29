from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Decision:
    approved: bool
    note: str = ""
    who: str = ""


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


class TerminalHuman:
    def __init__(self, ask: Callable[[str], str] = input, show: Callable[[str], None] = print):
        self.ask = ask
        self.show = show

    def _read(self, prompt: str) -> str:
        try:
            return self.ask(prompt).strip()
        except EOFError:
            raise RuntimeError("Foreman needs an answer but there is no terminal input: run it in a terminal, "
                               "or give the answer on the command line (for branches: --base and --prefix)") from None

    def approve(self, kind: str, summary: str) -> Decision:
        self.show(f"\n=== Approval needed: {kind} ===\n{summary}\n")
        reply = self._read("Approve? [y = approve, anything else rejects and is kept as the note]: ")
        if reply.lower() in ("y", "yes"):
            return Decision(True, "", "terminal")
        return Decision(False, reply, "terminal")

    def answer(self, question: str) -> str | None:
        self.show(f"\nQuestion: {question}")
        return self._read("Answer (blank = proceed on an assumption): ") or None
