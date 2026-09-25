"""A saved fact is found by the words of its evidence, not only by its subject.

A claim reaches recall through the owner message that holds its evidence, found by the lexical
search over what was said. Two things kept that search from the evidence:

* an identifier in it ("p-72", "B-12", "4.2m") never took part: the search split it into one- and
  two-letter pieces and dropped them, so "the review with p-72" and "the review with p-51" matched a
  question about p-72 equally;
* a question's arrival stamp ("[Sat 2026-09-26 12:02:06 UTC]") spent three of the twelve search
  terms on words every stamped message has, pushing the question's own words out.
"""

from __future__ import annotations

from protagine.turns import TurnIdempotencyLedger

STAMP = "[Sat 2026-09-26 12:02:06 UTC] "


def _ledger(tmp_path, messages):
    ledger = TurnIdempotencyLedger(tmp_path / "turn-idempotency.db")
    for index, text in enumerate(messages):
        ledger.record_source(f"owner-1:t-{index}", contact_id="owner", session_id="s-1",
                             messages=[{"role": "user", "content": f"[Fri 2026-09-25 12:00:{index:02d} UTC] {text}"}],
                             derive_claims=False)
    return ledger


def _top(ledger, query, count=1):
    return [hit["content"] for hit in ledger.search_sources(query, contact_id="owner", session_id="s-2", limit=10)][:count]


def test_an_identifier_in_the_evidence_finds_its_fact(tmp_path):
    ledger = _ledger(tmp_path, [
        "The budget draft review with p-51 is on Wednesday at 10:45 in the main hall; the budget draft review "
        "matters.",
        "The budget draft review with p-72 is on Tuesday at 09:15 in the studio two.",
    ])
    top, = _top(ledger, STAMP + "From what I told you earlier about the budget draft review with p-72: where is it?")
    assert "p-72" in top


def test_a_code_with_a_dot_or_digits_finds_its_fact(tmp_path):
    ledger = _ledger(tmp_path, ["The spare key is under pot B-12 by the door, near the other pots and the key hook.",
                                "The spare key is under pot B-14 by the door."])
    top, = _top(ledger, "Which pot was the spare key under, B-12?")
    assert "B-12" in top


def test_the_arrival_stamp_leaves_the_question_its_own_words(tmp_path):
    noise = [f"Please check the room booking note number {index} again, exactly as before." for index in range(12)]
    ledger = _ledger(tmp_path, [*noise, "The quarterly offsite planning session is in the Cedar room."])
    question = (STAMP + "Please check your notes and tell me again exactly which room we booked for the quarterly "
                "offsite planning session.")
    assert any("Cedar" in text for text in _top(ledger, question, count=3))
