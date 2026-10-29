import pytest

from foreman.human import Decision, ScriptedHuman, TerminalHuman


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


def test_terminal_yes_approves():
    h = TerminalHuman(ask=lambda prompt: "y", show=lambda text: None)
    assert h.approve("proposal", "summary").approved


def test_terminal_other_text_rejects_with_that_note():
    h = TerminalHuman(ask=lambda prompt: "needs auth", show=lambda text: None)
    assert h.approve("proposal", "summary") == Decision(False, "needs auth", "terminal")


def test_terminal_blank_answer_is_an_assumption():
    h = TerminalHuman(ask=lambda prompt: "  ", show=lambda text: None)
    assert h.answer("which db?") is None


def test_terminal_shows_the_summary():
    shown = []
    TerminalHuman(ask=lambda p: "y", show=shown.append).approve("proposal", "THE SUMMARY")
    assert any("THE SUMMARY" in s for s in shown)


def no_terminal(prompt):
    raise EOFError


def test_terminal_without_input_says_how_to_answer_instead_of_crashing():
    h = TerminalHuman(ask=no_terminal, show=lambda text: None)
    for call in (lambda: h.answer("which db?"), lambda: h.approve("proposal", "summary")):
        with pytest.raises(RuntimeError, match="no terminal input.*command line"):
            call()
