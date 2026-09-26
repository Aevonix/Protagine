"""A contact's opt-out lowers ``may_contact`` and nothing else (architecture 4.7 item 9, 7.4)."""

from __future__ import annotations

import pytest

from protagine.contacts.config import ContactsConfig
from protagine.contacts.optout import OPT_OUT_PATTERNS, apply_opt_out, detects_opt_out
from protagine.contacts.store import SQLiteContactStore

FAMILY = [
    "Please stop the check-ins; I will get in touch when I need something.",
    "No more messages from you, thanks. I will reach out myself if anything comes up.",
    "STOP",
    "I would rather you did not message me again about the invoice; I have it covered.",
    "Do not text me; I will contact the owner directly from now on.",
]
CLOSE_VARIANTS = [
    "stop.", " Stop! ", "unsubscribe", "Please unsubscribe me from these.", "don't message me",
    "don't text me again", "do not contact me", "stop messaging me", "please don't dm me", "stop writing to me",
    "No more texts from you.", "no more check-ins from you please", "stop the reminders",
    "I'd prefer that you didn't contact me", "I would prefer you not message me", "leave me alone",
    "Remove me from your list.", "remove me", "Please leave me alone.", "Just leave me alone!",
    "Please remove me from this list", "Unsubscribe.", "unsubscribe me please", "Remove me, thanks.",
    "I need you to leave me alone.", "Stop the check-ins.",
]
NEAR_MISSES = [
    "stop by later if you want",
    "please stop worrying about it",
    "I could not stop laughing at that",
    "text me when you are free",
    "message me the address please",
    "no more coffee for me today",
    "the bus stop near the office is closed",
    "I unsubscribed from that newsletter years ago",
    "remove the old backups when you can",
    "I would rather you message me than call",
    "leave me a note on the door",
    "stop the timer when the pasta is done",
    # A request about how or when to write is not an opt-out (audit M14).
    "don't text me the file, email it",
    "do not message me before 9",
    "please don't dm me the password, call instead",
    # Review F5: these name the words of an opt-out inside an ordinary message.
    "Can you remove me from the Thursday thread and add my work email instead?",
    "The kids won't leave me alone today, can we move the call to 4?",
    "How do I unsubscribe from that supplier newsletter you mentioned?",
    "Please remove me as the second signer, the budget draft is attached.",
    "Can you stop the reminders about the dentist, it is booked now?",
    "Could you unsubscribe me from the supplier newsletter?",
]


def test_the_family_phrasings_and_close_variants_are_detected():
    for text in FAMILY + CLOSE_VARIANTS:
        assert detects_opt_out(text), text


def test_near_misses_are_not_opt_outs():
    for text in NEAR_MISSES:
        assert detects_opt_out(text) is None, text
    assert detects_opt_out("") is None and detects_opt_out(None) is None


def test_patterns_are_compiled_and_the_phrase_is_returned():
    assert OPT_OUT_PATTERNS and all(hasattr(p, "search") for p in OPT_OUT_PATTERNS)
    assert detects_opt_out("Well, STOP then.") is None  # a bare STOP is the whole message
    assert detects_opt_out("stop the check-ins please").lower() == "stop the check-ins"
    assert detects_opt_out("Do not text me; bye").lower() == "do not text me"


@pytest.fixture
async def store():
    value = SQLiteContactStore(ContactsConfig(sqlite_path=":memory:"))
    await value.connect()
    yield value
    await value.close()


@pytest.mark.asyncio
async def test_apply_opt_out_lowers_a_contact_and_never_the_owner(store):
    owner = await store.create(display_name="Owner", may_contact="auto")
    contact = await store.create(display_name="Contact", may_contact="auto")
    assert await apply_opt_out(store, owner.contact_id, "STOP", source_ref="turn:1", owner_id=owner.contact_id) is None
    assert (await store.get(owner.contact_id)).may_contact == "auto"
    assert await apply_opt_out(store, contact.contact_id, "thanks, see you", source_ref="turn:2",
                               owner_id=owner.contact_id) is None
    assert (await store.get(contact.contact_id)).may_contact == "auto"
    lowered = await apply_opt_out(store, contact.contact_id, FAMILY[3], source_ref="turn:3", owner_id=owner.contact_id)
    assert lowered is not None and lowered.may_contact == "never"
    rows = [row for row in await store.get_audit_log(contact.contact_id) if row["action"] == "opt_out"]
    assert len(rows) == 1 and "rather you did not message me" in rows[0]["detail"]
    assert await apply_opt_out(store, contact.contact_id, "STOP", source_ref="turn:4", owner_id=owner.contact_id) is None
    assert await apply_opt_out(store, "", "STOP", source_ref="turn:5", owner_id=None) is None
    assert await apply_opt_out(store, "system", "STOP", source_ref="turn:6", owner_id=None) is None


@pytest.mark.asyncio
async def test_opt_out_never_raises_permission(store):
    """Evals 7.2 test 4, the lowering half: a never contact saying anything stays never."""
    contact = await store.create(display_name="Contact", may_contact="never")
    for text in FAMILY + ["please do message me", "you may contact me any time"]:
        assert await apply_opt_out(store, contact.contact_id, text, source_ref="turn:x", owner_id=None) is None
        assert (await store.get(contact.contact_id)).may_contact == "never"


@pytest.mark.asyncio
async def test_opt_outs_since_a_time_are_listed_for_the_owners_digest(store):
    """Architecture 7.4: an opt-out is recorded and listed in the digest (audit M14)."""
    contact = await store.create(display_name="Casey", may_contact="auto")
    other = await store.create(display_name="Robin", may_contact="ask")
    await store.set_may_contact(other.contact_id, "never", by="owner")
    before = "2000-01-01T00:00:00Z"
    await apply_opt_out(store, contact.contact_id, "STOP", source_ref="turn:9", owner_id=None)
    rows = await store.audit_since(["opt_out"], before)
    assert [(row["contact_id"], row["display_name"], row["detail"]["reason"]) for row in rows] == [
        (contact.contact_id, "Casey", "STOP")]
    assert await store.audit_since(["opt_out"], "2999-01-01T00:00:00Z") == []


def test_a_bare_stop_is_still_the_whole_message_behind_a_timestamp_or_a_sender_header():
    """A gateway with message timestamps on, and the paired body, put bracketed prefixes before
    the contact's own words; a bare STOP behind them is still the whole message."""
    for text in ("[Wed 2026-09-23 09:19:34 UTC] STOP", "[Message from contact p-03 on sms]\nSTOP",
                 "[Wed 2026-09-23 09:19:34 UTC] [Message from contact p-03 on sms]\nStop."):
        assert detects_opt_out(text) is not None, text
    for text in ("[Wed 2026-09-23 09:19:34 UTC] stop by the office tomorrow", "[note] STOP the press, it is late",
                 "Report [draft]\nSTOP", "STOP [again]"):
        assert detects_opt_out(text) is None, text
