import pytest

from foreman.core.human import Decision, StoreHuman
from foreman.core.runner import SafeStop
from foreman.state.store import Store
from tests.doubles.human import ScriptedHuman


def test_scripted_reject_then_approve_then_default_approve():
    h = ScriptedHuman({"proposal": ["reject: add rate limiting", "approve"]})
    assert h.approve("proposal", "s") == Decision(False, "add rate limiting", "scripted")
    assert h.approve("proposal", "s").approved
    assert h.approve("proposal", "s").approved


def test_scripted_kinds_are_independent():
    h = ScriptedHuman({"proposal": ["reject"], "high_impact": ["approve"]})
    assert h.approve("high_impact", "s").approved
    assert not h.approve("proposal", "s").approved


def test_scripted_answers_in_order_then_none():
    h = ScriptedHuman({"answers": ["one"]})
    assert h.answer("q1") == "one"
    assert h.answer("q2") is None


def human_that_gets_answered_after(store, polls, **answer):
    """A StoreHuman whose sleep() answers the open request once it has waited `polls` times."""
    waited = []

    def sleep(seconds):
        waited.append(seconds)
        if len(waited) == polls:
            store.respond(store.pending()[0]["id"], who="dev", **answer)

    return StoreHuman(store, "r1", poll_s=2, sleep=sleep, show=lambda message: None), waited


def test_approve_blocks_until_someone_responds_then_returns_the_decision():
    store = Store(":memory:")
    human, waited = human_that_gets_answered_after(store, 3, approved=False, note="needs auth")
    decision = human.approve("proposal", "Change c-1 ...")
    assert decision == Decision(False, "needs auth", "dev") and waited == [2, 2, 2]


def test_the_request_text_reaches_the_store():
    store = Store(":memory:")
    human, _ = human_that_gets_answered_after(store, 1, approved=True)
    human.approve("high_impact", "- dependency: requirements.txt")
    row = store.get_request(1)
    assert (row["run_id"], row["kind"], row["prompt"]) == ("r1", "high_impact", "- dependency: requirements.txt")


def test_an_answer_is_returned_and_a_blank_one_means_assume():
    store = Store(":memory:")
    human, _ = human_that_gets_answered_after(store, 1, answer="Postgres")
    assert human.answer("Which db?") == "Postgres"
    human, _ = human_that_gets_answered_after(store, 1, answer="")
    assert human.answer("Expiry?") is None


def test_no_answer_within_the_timeout_safe_stops():
    human = StoreHuman(Store(":memory:"), "r1", poll_s=10, timeout_s=25, sleep=lambda s: None, show=lambda m: None)
    with pytest.raises(SafeStop, match="no answer"):
        human.approve("proposal", "s")


def test_the_waiting_message_names_the_command_that_answers_it():
    shown = []
    store = Store(":memory:")
    human = StoreHuman(store, "r1", poll_s=1, show=shown.append,
                       sleep=lambda s: store.respond(1, approved=True))
    human.approve("proposal", "s")
    assert "foreman respond 1" in shown[0]
