"""The per-contact template digest (architecture 4.7 item 4; the LLM digest arrives in M8)."""

from __future__ import annotations

from datetime import timedelta

from protagine.contacts.digest import MAX_CHARS, render_digest
from test_mind_social import C, CONTACT, OTHER, OWNER, T0, contact, make  # noqa: F401  (pytest fixture)


def test_the_digest_says_who_how_known_recency_open_items_and_claims_and_nothing_the_owner_set():
    """The digest is read in the contact's own context and composes messages to them, so it
    carries what they are to the agent and what they said, never the owner's settings for them:
    permission, cadence and who introduced them stay in the owner's ``inspect`` (audit M6)."""
    record = {**contact(CONTACT, name="Sam", tier="regular", may_contact="never", cadence=30,
                        last=T0 - timedelta(hours=3), count=4), "import_source": "introduced by p-07",
              "handles": [{"gateway": "email", "address": "sam@example.org"}]}
    text = render_digest(record, claims=["Sam prefers email in the mornings.", "Sam is handling the budget draft."],
                         counts={"inbound": 7, "outbound": 3, "open": 2}, last_interaction_at=record["last_interaction_at"],
                         now=T0)
    assert text.startswith("Sam.") and "email:sam@example.org" in text and "3 h ago" in text
    assert "4 conversation" in text and "7 in / 3 out" in text and "2 open" in text
    assert "Sam prefers email in the mornings." in text and "budget draft" in text
    # Review F9: the tier is the owner's classification of the person, withheld from the guest
    # context everywhere else (``protagine-relationship`` renders only for the owner).
    for owner_only in ("never", "May be messaged", "cadence", "30 min", "p-07", "introduced", "regular", "Sam (",
                       "peripheral"):
        assert owner_only not in text, owner_only
    assert len(text) <= MAX_CHARS


def test_the_digest_is_bounded_and_survives_sparse_records():
    sparse = render_digest({"contact_id": "p-09"}, claims=[], counts={}, last_interaction_at=None, now=T0)
    assert sparse.startswith("p-09") and "Never talked" in sparse
    long = render_digest({"contact_id": "p-09", "display_name": "Pat"}, claims=[f"claim {n} " * 20 for n in range(30)],
                         counts={}, last_interaction_at=None, now=T0)
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
    # Marked as the template, so the memory milestone's digest knows what it may replace.
    assert "sorting out the lease" in text and "2 h ago" in text and sources == ["template"]
    fx.shift(timedelta(hours=1))
    assert (await fx.tick())["digests"] is None                     # once a day
    fx.shift(timedelta(days=1))
    fx.contacts.talk(OTHER, fx.now - timedelta(minutes=5))
    assert (await fx.tick())["digests"] == 1 and set(fx.contacts.digests) == {CONTACT, OTHER}


class NoCalls:
    """A router the digest writer must never call: the template digest is rendered, not generated."""
    supports_function_routing = True

    async def complete(self, *args, **kwargs):
        raise AssertionError("the template digest called the router")


async def test_the_template_writer_never_replaces_a_digest_the_memory_faculty_wrote(make):
    """Audit M10 / integration map X1: one digest column, two writers. While the memory faculty
    can write (consolidation on, a router), the template fills only an empty digest or its own
    earlier template; with consolidation off it is the only writer and always writes."""
    recent = dict(tier="unknown", last=T0 - timedelta(hours=2), count=3)
    written = {**contact(CONTACT, **recent), "digest": "Told me about the lease.", "digest_sources": ["claim:c-1"]}
    template = {**contact(OTHER, **recent), "digest": "An older template.", "digest_sources": ["template"]}
    empty = {**contact("p-04", **recent), "digest": None, "digest_sources": []}
    fx = make([written, template, empty], router=NoCalls(), config={"faculties": {"consolidation": True}})
    assert (await fx.tick())["digests"] == 2 and set(fx.contacts.digests) == {OTHER, "p-04"}
    alone = make([written], router=NoCalls(), config={"faculties": {"consolidation": False}})
    assert (await alone.tick())["digests"] == 1 and alone.contacts.digests[CONTACT][1] == ["template"]


async def test_people_off_writes_no_digests(make):
    fx = make([contact(CONTACT, last=T0 - timedelta(hours=2))], config={"faculties": {"people": False}})
    assert (await fx.tick())["digests"] is None and fx.contacts.digests == {}


async def test_an_owner_canary_about_a_contact_never_reaches_their_digest_claims(tmp_path, monkeypatch):
    """``claims_for`` reads only the contact's own sources: what the owner said about them (the
    family's canary turn) stays out of the digest and the packet built from it."""
    from protagine.api.routers import host
    from protagine.beliefs.source_projection import SourceClaimProjection
    from protagine.turns import get_turn_idempotency_ledger
    from test_source_claim_projection import Model, claim
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    ledger = get_turn_idempotency_ledger(tmp_path)
    worry, own = f"I am worried about {CONTACT} because of amber-cobalt-42.", "My office is in River."
    ledger.record_source("t-owner", contact_id=OWNER, session_id="owner-1", messages=[{"role": "user", "content": worry}])
    ledger.record_source("t-own", contact_id=CONTACT, session_id="contact-1", messages=[{"role": "user", "content": own}])
    projection = SourceClaimProjection(ledger)
    model = Model({worry: claim(worry, "amber-cobalt-42", subject=CONTACT, predicate="worry_reason"), own: claim(own, "River")})
    while await projection.process_one(model):
        pass
    lines = await host.claims_for(CONTACT)
    assert len(lines) == 1 and lines[0].endswith(": River") and "amber" not in " ".join(lines)
    assert any("amber-cobalt-42" in line for line in await host.claims_for(OWNER))
