"""Which engine responses count as a product the customer can be charged for (CONTRACTS 3.4)."""
from __future__ import annotations

from starling_server.engines.streams import is_usable


def test_a_draft_or_an_answer_is_usable():
    assert is_usable({"ok": True, "answer": "Here is a take."})
    assert is_usable({"ok": True, "decision": {"draft": "A reply."}})


def test_a_withheld_result_is_never_usable():
    withheld = {"ok": True, "withheld": True, "answer": "I couldn't produce a version that passes my gates."}
    assert not is_usable(withheld)
    assert is_usable({**withheld, "withheld": False})  # negative control: the flag is what decides


def test_the_customers_own_words_handed_back_are_not_a_product():
    """An Enhance that changes nothing fills `draft` with the text the customer typed."""
    unchanged = {"ok": True, "unchanged": True, "decision": {"draft": "my own line, exactly as I typed it"}}
    assert not is_usable(unchanged)
    assert is_usable({**unchanged, "unchanged": False})  # negative control: the flag is what decides


def test_failures_and_empty_results_are_not_usable():
    assert not is_usable({"ok": False, "answer": "text"})
    assert not is_usable({"ok": True, "answer": "   ", "decision": {"draft": ""}})
    assert not is_usable({"ok": "true", "answer": "text"})
    assert not is_usable(None)
