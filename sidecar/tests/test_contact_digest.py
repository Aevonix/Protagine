"""The per-contact template digest (architecture 4.7 item 4; the LLM digest arrives in M8)."""

from __future__ import annotations

from datetime import timedelta

from protagine.contacts.digest import MAX_CHARS, render_digest
from test_mind_social import C, CONTACT, OTHER, OWNER, T0, contact, make  # noqa: F401  (pytest fixture)


def test_the_digest_says_who_how_known_permission_cadence_recency_open_items_and_claims():
    record = {**contact(CONTACT, name="Sam", tier="regular", may_contact="auto", cadence=30,
                        last=T0 - timedelta(hours=3), count=4), "import_source": "manual",
              "handles": [{"gateway": "email", "address": "sam@example.org"}]}
    text = render_digest(record, claims=["Sam prefers email in the mornings.", "Sam is handling the budget draft."],
                         counts={"inbound": 7, "outbound": 3, "open": 2}, cadence_minutes=30, may_contact="auto",
                         last_interaction_at=record["last_interaction_at"], now=T0)
    assert text.startswith("Sam (regular)") and "email:sam@example.org" in text
    assert "May be messaged: auto" in text and "every 30 min" in text and "3 h ago" in text
    assert "4 conversation" in text and "7 in / 3 out" in text and "2 open" in text
    assert "Sam prefers email in the mornings." in text and "budget draft" in text
    assert len(text) <= MAX_CHARS


def test_the_digest_is_bounded_and_survives_sparse_records():
    sparse = render_digest({"contact_id": "p-09"}, claims=[], counts={}, cadence_minutes=None, may_contact="ask",
                           last_interaction_at=None, now=T0)
    assert sparse.startswith("p-09") and "Never talked" in sparse and "no cadence" in sparse
    long = render_digest({"contact_id": "p-09", "display_name": "Pat"}, claims=[f"claim {n} " * 20 for n in range(30)],
                         counts={}, cadence_minutes=None, may_contact="ask", last_interaction_at=None, now=T0)
    assert len(long) <= MAX_CHARS and long.endswith("…")


async def test_the_daily_writer_digests_contacts_with_a_recent_interaction_once(make):
    seen = []

    async def claims_for(contact_id, limit=8):
        seen.append(contact_id)
        return [f"{contact_id} is sorting out the lease."]

    fx = make([contact(CONTACT, last=T0 - timedelta(hours=2), count=3),
               contact(OTHER, last=T0 - timedelta(days=3), count=1)], claims_for=claims_for)
    summary = await fx.tick()
    assert summary["digests"] == 1 and set(fx.contacts.digests) == {CONTACT} and seen == [CONTACT]
    text, sources = fx.contacts.digests[CONTACT]
    assert "sorting out the lease" in text and "2 h ago" in text and sources == []
    fx.shift(timedelta(hours=1))
    assert (await fx.tick())["digests"] is None                     # once a day
    fx.shift(timedelta(days=1))
    fx.contacts.talk(OTHER, fx.now - timedelta(minutes=5))
    assert (await fx.tick())["digests"] == 1 and set(fx.contacts.digests) == {CONTACT, OTHER}


async def test_people_off_writes_no_digests(make):
    fx = make([contact(CONTACT, last=T0 - timedelta(hours=2))], config={"faculties": {"people": False}})
    assert (await fx.tick())["digests"] is None and fx.contacts.digests == {}
