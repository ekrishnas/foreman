"""Humans are asked through the store: a request row is written, `foreman respond` answers it."""
import time
from dataclasses import dataclass
from typing import Callable

from foreman.core.runner import SafeStop


@dataclass(frozen=True)
class Decision:
    approved: bool
    note: str = ""
    who: str = ""


class StoreHuman:
    """Blocks the run until a person answers the request (through Claude Code or `foreman respond`)."""

    def __init__(self, store, run_id: str, poll_s: float = 2.0, timeout_s: float = 86400,
                 sleep: Callable[[float], None] = time.sleep, show: Callable[[str], None] = print):
        self.store, self.run_id = store, run_id
        self.poll_s, self.timeout_s, self.sleep, self.show = poll_s, timeout_s, sleep, show

    def approve(self, kind: str, summary: str) -> Decision:
        row = self._ask(kind, summary)
        return Decision(bool(row["approved"]), row["note"], row["who"])

    def answer(self, question: str) -> str | None:
        return self._ask("question", question)["answer"] or None

    def _ask(self, kind: str, text: str) -> dict:
        request = self.store.ask(self.run_id, kind, text)
        self.show(f"waiting for a human: {kind} (request {request}); answer with `foreman respond {request}`")
        waited = 0.0
        while (row := self.store.get_request(request))["status"] == "pending":
            if waited >= self.timeout_s:
                raise SafeStop(f"no answer to the {kind} request after {self.timeout_s:g}s", stage=kind)
            self.sleep(self.poll_s)
            waited += self.poll_s
        return row
