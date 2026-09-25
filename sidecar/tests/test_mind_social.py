"""The social drive: check-ins when warranted and permitted, timed by replies and silence.

Architecture 4.5 (the social row of the drive table) and 4.7 items 5 and 6;
build plan M5 acceptance: single-contact backoff (after two ignored check-ins
the next waits at least twice as long), a group of unknown members produces no
check-in asks, and the ``never`` contact under a strong reason gets nothing.
The tick timings are the family's (``mind-people-1``: ``+C+300``, ``+2C+300``,
``+C+300``). A fake contact store plays Part A's ``social_candidates``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from protagine.commitments.store import CommitmentStore
from protagine.contacts.comms import CommsLog, evaluate_outreach
from protagine.feedback import TypeFeedbackStore
from protagine.initiatives.store import InitiativeStore
from protagine.mind import Mind
from protagine.mind.authority import Authority, Policy
from protagine.mind.drives import DriveInputs, social
from protagine.turns.idempotency import TurnIdempotencyLedger

OWNER, CONTACT, OTHER = "p-01", "p-02", "p-03"
# Noon two days ahead: the commitment store refuses a deadline in the real past, and every test
# stays inside one UTC day (the daily digest writer keys on the date).
T0 = (datetime.now(timezone.utc) + timedelta(days=2)).replace(hour=12, minute=0, second=0, microsecond=0)
CADENCE = 10                         # minutes, the family's range is 5..20
C = timedelta(minutes=CADENCE)
PAST = timedelta(seconds=300)        # the family's PAST_HORIZON_SECONDS
REGULAR = {"regular", "trusted", "inner_circle"}


def confirmed(metadata, asked=None):
    """A stored owner's message to a third party as capture leaves it once the claim-review pass kept the
    owner's words asking for it (``commitments.extract.request_confirmed``): only such a row is sent."""
    from protagine.commitments.extract import REQUEST_REVIEW_VERSION
    asked = asked or f"If it has not happened by then, contact {metadata['recipient']} yourself"
    return {**metadata, "asked": asked, "request_review": {
        "version": REQUEST_REVIEW_VERSION, "keep": True, "reason": "the owner asks for it",
        "recipient": metadata["recipient"], "quote": asked, "model_id": "test"}}


def contact(cid, *, may_contact="ask", cadence=None, tier="regular", first_seen=T0, last=None, count=0, name=None):
    return {"contact_id": cid, "display_name": name or cid, "trust_tier": tier, "may_contact": may_contact,
            "cadence_minutes": cadence, "first_seen_at": first_seen.isoformat(),
            "last_interaction_at": last.isoformat() if last else None, "interaction_count": count, "timezone": None}


class FakeContacts:
    """Part A's store surface as the mind reads it (plan section 2.3)."""

    def __init__(self, records):
        self.records = {record["contact_id"]: dict(record) for record in records}
        self.digests, self.links, self.proposals, self.interactions, self.cadences = {}, [], [], [], []
        self.audit = []

    def _obj(self, record):
        return SimpleNamespace(**record, to_dict=lambda record=record: dict(record))

    async def get(self, contact_id):
        record = self.records.get(contact_id)
        return self._obj(record) if record else None

    async def get_handles(self, contact_id):
        return [SimpleNamespace(gateway="capture", address=contact_id, is_primary=True, verified=True)]

    async def resolve_handle(self, gateway, address):
        return SimpleNamespace(contact_id=address) if address in self.records else None

    async def social_candidates(self, *, limit=200):
        return [dict(r) for r in self.records.values()
                if r["may_contact"] != "never" and (r["cadence_minutes"] is not None or r["trust_tier"] in REGULAR)][:limit]

    async def resolve_reference(self, reference, *, exact=False):
        """An id (or its capture handle) is exact; a display name only when ``exact`` is off."""
        wanted = str(reference or "").strip().lower()
        for record in self.records.values():
            names = {record["contact_id"].lower()} | (set() if exact else {str(record["display_name"] or "").lower()})
            if wanted in names:
                return self._obj(record)
        return None

    async def record_interaction(self, contact_id, at_iso=None):
        self.interactions.append((contact_id, at_iso))
        return contact_id in self.records

    async def list_handle_proposals(self, limit=50):
        return list(self.proposals)

    async def confirm_link(self, candidate_id, *, performed_by):
        self.links.append(("confirm", candidate_id, performed_by))

    async def reject_link(self, candidate_id, *, performed_by):
        self.links.append(("reject", candidate_id, performed_by))

    async def set_digest(self, contact_id, text, sources):
        self.digests[contact_id] = (text, list(sources))

    async def set_cadence(self, contact_id, minutes, *, by):
        self.records[contact_id]["cadence_minutes"] = minutes
        self.cadences.append((contact_id, minutes, by))
        return self._obj(self.records[contact_id])

    async def list(self, **_):
        return [self._obj(record) for record in self.records.values()]

    async def audit_since(self, actions, since):
        return [row for row in self.audit if row["action"] in actions and row["created_at"] >= since]

    def talk(self, contact_id, at):
        """A conversation with the contact happened at ``at`` (what turns/sync records)."""
        record = self.records[contact_id]
        record["last_interaction_at"] = at.isoformat()
        record["interaction_count"] = int(record.get("interaction_count") or 0) + 1


class FakeAffect:
    def __init__(self):
        self.declining = set()

    def trend(self, contact_id):
        down = contact_id in self.declining
        return {"valence": -0.6 if down else 0.1, "trend": "declining" if down else "stable", "declining": down}


class Fx:
    def __init__(self, tmp_path, records, *, config=None, router=None, packet_for=None, claims_for=None):
        self.now = T0
        self.store = InitiativeStore(state_dir=tmp_path)
        self.commitments = CommitmentStore(tmp_path / "protagine-commitments.db")
        self.feedback = TypeFeedbackStore(str(tmp_path / "protagine-feedback.db"))
        self.ledger = TurnIdempotencyLedger(tmp_path / "turn-idempotency.db")
        self.contacts = FakeContacts(records)
        self.affect = FakeAffect()
        self.comms = CommsLog(str(tmp_path / "protagine-comms.db"), source_ledger=self.ledger)
        # The nightly consolidation has its own suite; here it stays off unless a test turns it on, so a
        # clock shifted past a night never starts a background run against these fakes (integration map X17).
        config = dict(config or {})
        config["faculties"] = {"consolidation": False, **(config.get("faculties") or {})}
        self.mind = Mind(config={"autonomy": "standard", **config}, store=self.store, state_dir=tmp_path,
                         owner_id=OWNER, commitments=self.commitments, feedback=self.feedback,
                         contacts=self.contacts, ledger=self.ledger, clock=lambda: self.now, backups=False,
                         router=router, comms=self.comms, contact_affect=self.affect, packet_for=packet_for,
                         claims_for=claims_for)
        self.mind.digest_hour = 25

    def shift(self, delta):
        self.now += delta

    async def tick(self):
        return await self.mind.tick(force=True)

    async def send_all(self):
        """The body: pull the outbox, claim and send each message, report ``sent``."""
        sent = []
        for payload in await self.mind.outbox_ready():
            self.mind.outbox.sending(payload["id"], target=f"capture:{payload['recipient']}")
            self.mind.outbox.sent(payload["id"])
            sent.append(payload)
        return sent

    def messages_to(self, contact_id):
        return [row for row in self.store.intentions(kind=["message"], limit=500) if row.entity_id == contact_id]

    def close(self):
        self.store.close()
        self.comms._conn.close()


@pytest.fixture
def make(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    made = []

    def build(records, **kwargs):
        directory = tmp_path / f"fx-{len(made)}"
        directory.mkdir()
        fx = Fx(directory, records, **kwargs)
        made.append(fx)
        return fx
    yield build
    for fx in made:
        fx.close()


# ---------------------------------------------------------------------------
# evaluate_outreach: the policy (architecture 4.7 items 5 and 6)
# ---------------------------------------------------------------------------

def outreach(now, **fields):
    base = dict(is_owner=False, cadence_minutes=CADENCE, last_interaction_ts=None, first_seen_ts=T0.isoformat(),
                last_outbound_ts=None, ignored_streak=0, open_followups=None, affect_declining=False)
    base.update(fields)
    return evaluate_outreach({"contact_id": CONTACT}, now=now, **base)


def test_outreach_is_due_one_cadence_after_the_reference_and_not_before():
    assert outreach(T0 + C / 3)["should_contact"] is False
    due = outreach(T0 + C + PAST)
    assert due["should_contact"] is True and due["cooldown_active"] is False
    assert due["next_eligible_at"] == T0 + C and due["cooldown_hours"] == pytest.approx(CADENCE / 60)
    # A conversation moves the reference: the contact wrote 0.6 cadences ago.
    talked = outreach(T0 + 1.3 * C, last_interaction_ts=(T0 + 0.6 * C).isoformat())
    assert talked["should_contact"] is False and "not due" in talked["reason"]
    # So does a check-in that ended unsent (an expired or refused ask): the next is a cadence later.
    unsent = outreach(T0 + C + PAST, last_attempt_ts=(T0 + C).isoformat())
    assert unsent["should_contact"] is False and unsent["next_eligible_at"] == T0 + 2 * C


def test_outreach_cooldown_doubles_per_ignored_check_in_and_caps_at_four_cadences():
    sent = T0 + C
    one = outreach(sent + C + PAST, last_outbound_ts=sent.isoformat(), ignored_streak=1)
    assert one["should_contact"] is False and one["cooldown_active"] is True
    assert one["cooldown_hours"] == pytest.approx(2 * CADENCE / 60) and one["next_eligible_at"] == sent + 2 * C
    assert outreach(sent + 2 * C + PAST, last_outbound_ts=sent.isoformat(), ignored_streak=1)["should_contact"] is True
    two = outreach(sent + C + PAST, last_outbound_ts=sent.isoformat(), ignored_streak=2)
    assert two["cooldown_active"] is True and two["cooldown_hours"] == pytest.approx(4 * CADENCE / 60)
    capped = outreach(sent + C, last_outbound_ts=sent.isoformat(), ignored_streak=5)
    assert capped["cooldown_hours"] == pytest.approx(4 * CADENCE / 60) and capped["next_eligible_at"] == sent + 4 * C


def test_outreach_holds_for_declining_affect_the_owner_and_no_cadence():
    declining = outreach(T0 + 2 * C, affect_declining=True)
    assert declining["should_contact"] is False and "affect declining" in declining["reason"]
    assert outreach(T0 + 2 * C, is_owner=True)["should_contact"] is False
    assert outreach(T0 + 2 * C, cadence_minutes=None)["should_contact"] is False
    with_threads = outreach(T0 + 2 * C, open_followups=["the invoice", "", "the draft"])
    assert with_threads["talking_points"] == ["the invoice", "the draft"] and with_threads["should_contact"] is True


# ---------------------------------------------------------------------------
# social(): the drive over the tick's snapshot
# ---------------------------------------------------------------------------

def snapshot(rows, *, now=T0 + C + PAST, people_on=True):
    return DriveInputs(now=now, owner_id=OWNER, contacts=rows, people_on=people_on)


def enriched(record, **extra):
    row = {**record, "last_check_in_at": None, "ignored_streak": 0, "open_followups": [], "affect_declining": False}
    row.update(extra)
    return row


def test_unknown_and_group_only_contacts_weigh_zero_and_ten_unknown_members_raise_nothing():
    members = [enriched(contact(f"p-{n:02d}", tier="unknown")) for n in range(10, 20)]
    group = [enriched(contact("p-30", tier="group_guest"))]
    assert social(snapshot(members + group)) == (0.0, [])


def test_an_owner_cadence_forms_one_check_in_with_the_family_key_and_no_dedup_base():
    level, candidates = social(snapshot([enriched(contact(CONTACT, may_contact="auto", cadence=CADENCE))]))
    candidate, = candidates
    assert candidate.type == "check_in" and candidate.drive == "social" and candidate.kind == "message"
    assert candidate.recipient == CONTACT and candidate.text == "" and candidate.purpose == "check_in"
    assert candidate.dedup_key == f"check_in:{CONTACT}:{(T0 + C):%Y%m%dT%H%M}" and candidate.dedup_base is None
    assert candidate.salience == pytest.approx(0.9) and candidate.cost == pytest.approx(0.05)
    assert candidate.cooldown_hours == pytest.approx(CADENCE / 60) and candidate.concern_kind == "social"
    assert candidate.invalidates_if == f"contact:{CONTACT}:replied" and candidate.grant is None
    assert 0 < level <= 1.0


def test_a_tier_only_contact_uses_the_estimated_cadence_at_lower_salience():
    row = enriched(contact(CONTACT, tier="regular"), estimated_cadence_minutes=1440)
    assert social(snapshot([row], now=T0 + timedelta(hours=12)))[1] == []
    _, candidates = social(snapshot([row], now=T0 + timedelta(days=1, minutes=1)))
    assert len(candidates) == 1 and candidates[0].salience == pytest.approx(0.7)


def test_the_drive_skips_the_owner_never_rows_declining_affect_in_flight_and_people_off():
    rows = [enriched(contact(OWNER, may_contact="auto", cadence=CADENCE)),
            enriched(contact("p-04", may_contact="never", cadence=CADENCE)),
            enriched(contact("p-05", may_contact="auto", cadence=CADENCE), affect_declining=True),
            enriched(contact("p-06", may_contact="auto", cadence=CADENCE), in_flight=True),
            enriched(contact("p-07", may_contact="auto", cadence=CADENCE))]
    _, candidates = social(snapshot(rows))
    assert [c.recipient for c in candidates] == ["p-07"]
    assert social(snapshot(rows, people_on=False)) == (0.0, [])


def test_the_topic_is_the_contacts_open_thread():
    row = enriched(contact(CONTACT, may_contact="auto", cadence=CADENCE), topic="the budget draft")
    candidate, = social(snapshot([row]))[1]
    assert candidate.topic == "the budget draft"


# ---------------------------------------------------------------------------
# The cooldown override reaches the budget check
# ---------------------------------------------------------------------------

def test_a_candidates_own_cooldown_replaces_the_flat_contact_cooldown(tmp_path):
    store = InitiativeStore(state_dir=tmp_path)
    try:
        clock = [T0]
        authority = Authority(Policy.from_config({"autonomy": "standard"}), store, owner_id=OWNER, clock=lambda: clock[0])
        row, _ = store.create_intention(kind="message", type="check_in", title="check in", drive="social", cls="contact",
                                        decision="act", decision_reason="t", status="approved", dedup_key="k1",
                                        recipient=CONTACT, hermes_kind="none", created_at=T0)
        store.transition(row.id, "approved", action="queued", at=T0)
        clock[0] = T0 + 2 * C
        assert "cooldown" in authority.budget_check(kind="message", recipient=CONTACT)
        assert authority.budget_check(kind="message", recipient=CONTACT, cooldown_hours=CADENCE / 60) is None
        assert "0.5 h cooldown" in authority.budget_check(kind="message", recipient=CONTACT, cooldown_hours=0.5)
        assert authority.decide(kind="message", recipient=CONTACT, text="hi", may_contact="auto",
                                cooldown_hours=CADENCE / 60).decision == "act"
        assert authority.decide(kind="message", recipient=CONTACT, text="hi", may_contact="auto").decision == "defer"
    finally:
        store.close()


# ---------------------------------------------------------------------------
# The tick, at the family's timings
# ---------------------------------------------------------------------------

async def test_cadence_due_sends_one_check_in_in_the_first_tick_and_none_in_the_next_two(make):
    fx = make([contact(CONTACT, may_contact="auto", cadence=CADENCE), contact(OTHER)])
    fx.shift(C + PAST)
    first = await fx.tick()
    formed, = first["formed"]
    assert formed["type"] == "check_in" and formed["decision"] == "act" and formed["status"] == "approved"
    sent = await fx.send_all()
    assert len(sent) == 1 and sent[0]["recipient"] == CONTACT and sent[0]["recipient_is_owner"] is False
    assert sent[0]["recipient_handles"] == [{"gateway": "capture", "address": CONTACT, "is_primary": True, "verified": True}]
    assert sent[0]["text"].startswith(f"Hi {CONTACT}, checking in")
    assert (await fx.tick())["formed"] == [] and (await fx.tick())["formed"] == []
    assert fx.messages_to(OTHER) == [] and await fx.mind.outbox_ready() == []


async def test_cadence_not_due_forms_nothing(make):
    fx = make([contact(CONTACT, may_contact="auto", cadence=CADENCE)])
    fx.shift(C / 3)
    for _ in range(3):
        assert (await fx.tick())["formed"] == []
    assert fx.messages_to(CONTACT) == []


async def test_a_conversation_satisfies_the_cadence(make):
    fx = make([contact(CONTACT, may_contact="auto", cadence=CADENCE)])
    fx.shift(0.7 * C)
    fx.contacts.talk(CONTACT, fx.now)
    fx.shift(0.6 * C)
    for _ in range(3):
        assert (await fx.tick())["formed"] == []
    fx.shift(0.5 * C)                      # 1.1 cadences after the conversation: due again
    assert [item["type"] for item in (await fx.tick())["formed"]] == ["check_in"]


async def test_ignored_check_ins_back_off_at_the_familys_ticks(make):
    fx = make([contact(CONTACT, may_contact="auto", cadence=CADENCE)])
    fx.shift(C + PAST)
    assert [item["type"] for item in (await fx.tick())["formed"]] == ["check_in"]
    first, = await fx.send_all()
    fx.shift(2 * C + PAST)
    second_tick = await fx.tick()
    assert [item["type"] for item in second_tick["formed"]] == ["check_in"]
    second, = await fx.send_all()
    assert second["id"] != first["id"]
    # The first check-in was scored ignored when its window passed: the contact's key moved, the
    # type key (every contact's check-ins) did not.
    assert fx.store.get(first["id"]).verdict == "ignored"
    assert fx.feedback.multiplier("check_in:social") == 1.0
    assert fx.feedback.multiplier(f"reach_out:{CONTACT}") == pytest.approx(0.9)
    fx.shift(C + PAST)
    third_tick = await fx.tick()
    assert third_tick["formed"] == [] and await fx.send_all() == []
    rows = await fx.mind._social_rows(fx.now)
    assert rows[0]["ignored_streak"] == 2 and fx.store.get(second["id"]).verdict == "ignored"
    assert len(fx.messages_to(CONTACT)) == 2
    # Past the doubled cooldown (four cadences from the second send) the next one goes: the
    # backoff is the brake on silence, and feedback only orders (0.9^2 on the contact's key).
    fx.shift(3 * C)
    assert [item["type"] for item in (await fx.tick())["formed"]] == ["check_in"]
    third, = await fx.send_all()
    assert fx.feedback.multiplier(f"reach_out:{CONTACT}") == pytest.approx(0.81)
    # They come back: the silence they broke is not held against them, and the next check-in is
    # due one cadence after that conversation.
    fx.shift(C / 2)
    fx.contacts.talk(CONTACT, fx.now)
    fx.shift(C / 2 + timedelta(minutes=1))   # the third's window has passed; the next is not due yet
    back = await fx.tick()
    assert back["check_ins_scored"] == {"actioned": 1, "ignored": 0} and back["formed"] == []
    assert fx.store.get(third["id"]).verdict == "actioned"
    assert (await fx.mind._social_rows(fx.now))[0]["ignored_streak"] == 0
    fx.shift(C / 2 + PAST)
    assert [item["type"] for item in (await fx.tick())["formed"]] == ["check_in"]


async def test_a_reply_inside_the_window_scores_actioned_resets_the_streak_and_satiates_social(make):
    fx = make([contact(CONTACT, may_contact="auto", cadence=CADENCE)])
    fx.shift(C + PAST)
    await fx.tick()
    sent, = await fx.send_all()
    fx.shift(C / 2)
    fx.contacts.talk(CONTACT, fx.now)       # they replied
    fx.shift(C)
    summary = await fx.tick()
    assert summary["check_ins_scored"] == {"actioned": 1, "ignored": 0}
    assert fx.store.get(sent["id"]).verdict == "actioned"
    assert fx.feedback.multiplier("check_in:social") == 1.0 and fx.feedback.multiplier(f"reach_out:{CONTACT}") > 1.0
    assert float(fx.mind.mind_state.get("satiety.social")["level"]) > 0
    rows = await fx.mind._social_rows(fx.now)
    assert rows[0]["ignored_streak"] == 0


async def test_satiety_never_holds_a_due_check_in(make):
    fx = make([contact(CONTACT, may_contact="auto", cadence=CADENCE)])
    fx.mind.mind_state.bump("satiety.social", 1.0, half_life_s=4 * 3600, now=fx.now)
    fx.shift(C + PAST)
    summary = await fx.tick()
    assert [item["type"] for item in summary["formed"]] == ["check_in"]
    assert summary["drives"]["weights"]["social"] < fx.mind.drive_weights["social"]


async def test_permission_ask_holds_with_an_owner_ask_and_never_sends(make):
    fx = make([contact(CONTACT, may_contact="ask", cadence=CADENCE)])
    fx.shift(C + PAST)
    for index in range(3):
        summary = await fx.tick()
        if index == 0:
            formed, = summary["formed"]
            assert formed["type"] == "check_in" and formed["decision"] == "ask" and formed["status"] == "asked"
        else:
            assert summary["formed"] == []
        assert [p["recipient"] for p in await fx.mind.outbox_ready()] == [OWNER]     # the ask notice only
    row, = [r for r in fx.messages_to(CONTACT)]
    assert row.status == "asked" and row.ask_code
    notice, = [r for r in fx.messages_to(OWNER) if r.type == "ask_notice"]
    assert row.ask_code in notice.context["text"]


async def test_a_never_contact_under_a_strong_reason_gets_nothing(make):
    fx = make([contact(CONTACT, may_contact="never", cadence=CADENCE)])
    fx.commitments.create(person_id=OWNER, description=f"Check on {CONTACT}", due_at=(fx.now + C).isoformat(),
                          metadata=confirmed({"kind": "check_in", "recipient": CONTACT, "topic": "their health",
                                              "grant": "owner"}))
    fx.shift(C + PAST)
    for _ in range(3):
        await fx.tick()
        assert [p["recipient"] for p in await fx.mind.outbox_ready()] == [OWNER] or await fx.mind.outbox_ready() == []
    assert all(row.status == "dropped" for row in fx.messages_to(CONTACT))
    assert [row.type for row in fx.messages_to(OWNER)] == ["grant_refused"]


async def test_declining_affect_suppresses_the_check_in(make):
    fx = make([contact(CONTACT, may_contact="auto", cadence=CADENCE)])
    fx.affect.declining.add(CONTACT)
    fx.shift(C + PAST)
    assert (await fx.tick())["formed"] == []
    fx.affect.declining.clear()
    assert [item["type"] for item in (await fx.tick())["formed"]] == ["check_in"]


async def test_a_reply_cancels_an_unsent_check_in(make):
    fx = make([contact(CONTACT, may_contact="auto", cadence=CADENCE)])
    fx.shift(C + PAST)
    formed, = (await fx.tick())["formed"]
    fx.shift(timedelta(minutes=1))
    fx.contacts.talk(CONTACT, fx.now)
    assert await fx.mind.outbox_ready() == []
    row = fx.store.get(formed["id"])
    assert row.status == "cancelled" and "replied" in row.cancelled_reason and row.verdict is None


async def test_people_off_gives_social_weight_zero_and_no_contact_messages(make):
    fx = make([contact(CONTACT, may_contact="auto", cadence=CADENCE)], config={"faculties": {"people": False}})
    assert fx.mind.drive_weights["social"] == 0.0 and fx.mind.composer.enabled is False
    fx.shift(C + PAST)
    summary = await fx.tick()
    assert summary["formed"] == [] and summary["check_ins_scored"] is None and fx.messages_to(CONTACT) == []


async def test_a_pending_link_proposal_becomes_one_owner_ask_answered_through_the_store(make):
    fx = make([contact(CONTACT, name="Sam")])
    fx.contacts.proposals = [{"candidate_id": "cand-1", "contact_id": CONTACT, "gateway": "email",
                              "address": "sam@example.org", "display_name": "Sam", "status": "pending"}]
    summary = await fx.tick()
    formed, = summary["formed"]
    assert formed["type"] == "link_proposal" and formed["decision"] == "ask"
    row = fx.store.get(formed["id"])
    assert row.dedup_key == "link:cand-1" and row.entity_id == OWNER and row.ask_code
    assert "sam@example.org" in row.description and "Sam" in row.description
    assert (await fx.tick())["formed"] == []                      # one ask per candidate
    answered = await fx.mind.answer(row.ask_code, yes=True, contact_id=OWNER, message=f"yes {row.ask_code}")
    assert answered.status == "done" and fx.contacts.links == [("confirm", "cand-1", "owner")]
    fx.contacts.proposals = [{"candidate_id": "cand-2", "contact_id": CONTACT, "gateway": "sms",
                              "address": "+15550002", "display_name": "Sam"}]
    formed, = (await fx.tick())["formed"]
    code = fx.store.get(formed["id"]).ask_code
    refused = await fx.mind.answer(code, yes=False)
    assert refused.status == "cancelled" and fx.contacts.links[-1] == ("reject", "cand-2", "owner")


async def test_a_granted_check_in_due_now_is_the_one_word_and_its_topic_carries_into_the_next(make):
    """The owner's cadence turn captured as a granted check-in falls due with the cadence: the
    contact gets that one message, not a second from the social drive, and the next check-in
    keeps the thread it raised."""
    fx = make([contact(CONTACT, may_contact="auto", cadence=CADENCE)])
    fx.commitments.create(person_id=OWNER, description=f"Check in with {CONTACT} on the budget draft",
                          due_at=(fx.now + C).isoformat(), source_type="cognition",
                          metadata=confirmed({"kind": "check_in", "recipient": CONTACT, "topic": "the budget draft",
                                              "grant": "owner", "counterpart": CONTACT, "obligor": "assistant"}))
    fx.shift(C + PAST)
    assert [item["type"] for item in (await fx.tick())["formed"]] == ["commitment_check_in"]
    first, = await fx.send_all()
    assert "the budget draft" in first["text"]
    fx.shift(2 * C + PAST)
    formed, = (await fx.tick())["formed"]
    assert formed["type"] == "check_in" and fx.store.get(formed["id"]).context["topic"] == "the budget draft"
    second, = await fx.send_all()
    assert second["text"] == f"Hi {CONTACT}, checking in about the budget draft: how is it going?"
    fx.shift(C + PAST)
    assert (await fx.tick())["formed"] == [] and len(fx.messages_to(CONTACT)) == 2


async def test_a_check_in_that_never_went_out_starts_the_next_period(make):
    """An ask the owner let expire, or refused, is neither lost for good nor asked again at once:
    the next check-in is due one cadence after it ended. The owner's silence teaches nothing (it
    is not the contact's); a refusal lowers only that contact's key, which orders check-ins and
    never switches the cadence off: the owner does that with the cadence or the permission."""
    fx = make([contact(CONTACT, may_contact="ask", cadence=CADENCE)])
    fx.shift(C + PAST)
    first, = (await fx.tick())["formed"]
    fx.shift(timedelta(hours=73))
    expired = await fx.tick()
    assert fx.store.get(first["id"]).status == "expired" and expired["formed"] == []
    fx.shift(C + PAST)
    second, = (await fx.tick())["formed"]
    assert second["type"] == "check_in" and second["status"] == "asked" and second["id"] != first["id"]
    assert fx.feedback.multiplier(f"reach_out:{CONTACT}") == 1.0 and fx.feedback.multiplier("check_in:social") == 1.0
    refused = await fx.mind.answer(fx.store.get(second["id"]).ask_code, yes=False)
    assert refused.status == "cancelled"
    assert (await fx.tick())["formed"] == []
    assert fx.feedback.multiplier(f"reach_out:{CONTACT}") < 1.0 and fx.feedback.multiplier("check_in:social") == 1.0
    fx.shift(C + PAST)
    third, = (await fx.tick())["formed"]
    assert third["type"] == "check_in" and third["status"] == "asked"


# ---------------------------------------------------------------------------
# Feedback orders check-ins, it never switches a contact off (audit M2)
# ---------------------------------------------------------------------------

async def test_ignored_check_ins_back_off_to_four_cadences_and_never_switch_the_contact_off(make):
    """Two ignored check-ins put 0.9^2 on the feedback keys, which used to hold that contact below
    the act threshold for good; the backoff (cadence x 2^streak, capped at 4) is the only brake."""
    fx = make([contact(CONTACT, may_contact="auto", cadence=CADENCE)])
    sent = []
    step = C / 2
    for _ in range(48):                       # 24 cadences, nobody ever answers
        fx.shift(step)
        await fx.tick()
        sent += [(fx.now, payload) for payload in await fx.send_all() if payload["recipient"] == CONTACT]
    assert len(sent) >= 5, [at for at, _ in sent]
    gaps = [later - earlier for (earlier, _), (later, _) in zip(sent, sent[1:])]
    assert all(gap <= 4 * C + step for gap in gaps), gaps


async def test_expired_check_in_asks_do_not_hold_another_contacts_check_in(make):
    """Five check-ins the owner never answered expire; the owner's silence is not the contacts'
    and says nothing about a sixth contact, whose due check-in still goes out."""
    askers = [contact(f"p-1{n}", may_contact="ask", cadence=CADENCE) for n in range(5)]
    late = T0 + timedelta(hours=80)
    fx = make([*askers, contact(CONTACT, may_contact="auto", cadence=CADENCE, first_seen=late)])
    fx.shift(C + PAST)
    for _ in range(5):                        # a tick forms a bounded number of asks
        await fx.tick()
        fx.shift(timedelta(minutes=1))
    asked = [row for row in fx.store.intentions(kind=["message"], limit=100) if row.status == "asked"]
    assert sorted(row.entity_id for row in asked) == sorted(item["contact_id"] for item in askers)
    fx.now = late + C + PAST                  # past the 72 h ask expiry
    summary = await fx.tick()
    expired = [row for row in fx.store.intentions(kind=["message"], limit=100)
               if row.status == "expired" and row.type == "check_in"]
    assert len(expired) == 5
    assert [(item["type"], item["decision"]) for item in summary["formed"] if item.get("recipient", CONTACT) == CONTACT
            and fx.store.get(item["id"]).entity_id == CONTACT] == [("check_in", "act")]
    assert fx.feedback.multiplier(f"reach_out:{askers[0]['contact_id']}") == 1.0


async def test_the_owners_daily_digest_lists_the_opt_outs_since_the_last_one(make):
    """Architecture 7.4: an opt-out lowers the contact to never and the owner hears of it in the
    next digest, with the contact's own words (audit M14)."""
    fx = make([contact(CONTACT, may_contact="auto", name="Sam")])
    fx.mind.digest_hour = 0
    fx.contacts.audit.append({"contact_id": CONTACT, "display_name": "Sam", "action": "opt_out",
                              "detail": {"reason": "stop the check-ins", "from": "auto", "to": "never"},
                              "created_at": (fx.now - timedelta(hours=1)).isoformat()})
    summary = await fx.tick()
    digest = fx.store.get(summary["digest"])
    assert "Opted out (1)" in digest.context["text"] and "Sam: stop the check-ins" in digest.context["text"]


# ---------------------------------------------------------------------------
# review fixes: permission when a message leaves (F1), a history that outlives retention (F8, F6)
# ---------------------------------------------------------------------------

async def test_an_owner_revocation_after_approval_stops_a_queued_check_in(make):
    """Review F1: a check-in approved at 22:15 waits out quiet hours; the owner revokes to never at
    22:30 (the family's mid-episode revocation). At 07:15 it must not go."""
    fx = make([contact(CONTACT, may_contact="auto", cadence=10)], config={"quiet_hours": "22:00-07:00"})
    fx.now = T0.replace(hour=22, minute=0)
    fx.contacts.records[CONTACT]["first_seen_at"] = fx.now.isoformat()
    fx.shift(C + PAST)
    formed, = (await fx.tick())["formed"]
    assert formed["type"] == "check_in" and formed["status"] == "approved"
    assert await fx.mind.outbox_ready() == []
    fx.contacts.records[CONTACT]["may_contact"] = "never"
    fx.shift(timedelta(hours=9))
    assert [p for p in await fx.mind.outbox_ready() if p["recipient"] == CONTACT] == []
    assert fx.store.get(formed["id"]).status == "cancelled"


async def test_a_permission_lowered_to_ask_turns_an_approved_check_in_into_the_owners_question(make):
    fx = make([contact(CONTACT, may_contact="auto", cadence=10)], config={"quiet_hours": "22:00-07:00"})
    fx.now = T0.replace(hour=22, minute=0)
    fx.contacts.records[CONTACT]["first_seen_at"] = fx.now.isoformat()
    fx.shift(C + PAST)
    formed, = (await fx.tick())["formed"]
    assert formed["status"] == "approved"
    fx.contacts.records[CONTACT]["may_contact"] = "ask"
    fx.shift(timedelta(hours=9))
    assert [p for p in await fx.mind.outbox_ready() if p["recipient"] == CONTACT] == []
    row = fx.store.get(formed["id"])
    assert row.status == "asked" and row.ask_code
    await fx.mind.answer(row.ask_code, yes=True, contact_id=OWNER)
    payload, = [p for p in await fx.mind.outbox_ready() if p["recipient"] == CONTACT]
    assert payload["id"] == formed["id"]


async def test_a_sixty_day_cadence_keeps_checking_in_after_its_history_leaves_retention(make):
    """Review F8: the streak and the last send were read from intention rows, which retention prunes
    after 90 days. With a 60-day cadence and one ignored check-in (next after 2C = 120 d), the last
    send was gone before the next was due, the drive fell back to the first check-in's key, and it
    went silent for good. The sends live in the comms ledger (architecture 4.7 item 6)."""
    day = timedelta(days=1)
    fx = make([contact(CONTACT, may_contact="auto", cadence=60 * 24 * 60)])
    fx.shift(60 * day + PAST)
    first, = (await fx.tick())["formed"]
    await fx.send_all()
    sent_at = fx.now
    fx.shift(61 * day)
    await fx.tick()                                    # scored ignored: streak 1, the next after 2C
    formed = []
    for _ in range(12):
        fx.shift(20 * day)
        formed += [(fx.now, item) for item in (await fx.tick())["formed"]]
        await fx.send_all()
    check_ins = [at for at, item in formed if item["type"] == "check_in"]
    assert check_ins and check_ins[0] >= sent_at + 120 * day
    assert check_ins[0] <= sent_at + 120 * day + 20 * day + PAST


async def test_a_monthly_cadence_backs_off_to_four_cadences_for_a_contact_who_never_replies(make):
    """Review F8b: architecture 4.7 item 6 doubles the wait per ignored check-in up to four cadences;
    with the history pruned at 90 days the streak never passed 1 and the wait stayed at 2C."""
    day = timedelta(days=1)
    fx = make([contact(CONTACT, may_contact="auto", cadence=30 * 24 * 60)])
    days = []
    for _ in range(45):
        fx.shift(10 * day)
        await fx.tick()
        if [p for p in await fx.send_all() if p["recipient"] == CONTACT]:
            days.append((fx.now - T0).days)
    gaps = [b - a for a, b in zip(days, days[1:])]
    assert gaps[:3] == [60, 120, 120], days


async def test_a_merge_carries_the_check_in_history_so_the_backoff_holds(make):
    """Review F6: a merge moves the comms ledger (``reattribute``), and the social drive reads the
    kept contact's sends from it: two ignored check-ins to the dropped record still hold the kept
    record in its backoff."""
    keep, drop = CONTACT, OTHER
    fx = make([contact(keep, may_contact="auto", cadence=10), contact(drop, may_contact="auto", cadence=10)])
    fx.contacts.records[keep]["first_seen_at"] = (T0 + timedelta(days=30)).isoformat()
    fx.shift(C + PAST)
    await fx.tick()
    await fx.send_all()
    fx.shift(C * 4 + PAST)
    await fx.tick()
    fx.shift(C + PAST)
    await fx.tick()
    await fx.send_all()
    fx.shift(C * 2)
    assert (await fx.tick())["formed"] == []
    record = fx.contacts.records.pop(drop)
    fx.contacts.records[keep].update(first_seen_at=record["first_seen_at"], cadence_minutes=10)
    fx.mind.comms.reattribute(drop, keep)              # the merge's comms hook
    fx.shift(timedelta(minutes=1))
    assert (await fx.tick())["formed"] == []
    fx.shift(C * 2)
    formed = (await fx.tick())["formed"]
    assert [f["type"] for f in formed] == ["check_in"] and fx.store.get(formed[0]["id"]).entity_id == keep


async def test_an_overload_postpones_a_due_check_in_without_a_send_or_a_streak(make):
    """Integration map X5: the agent's overload postpones social work, a due check-in included. It is
    not a send, so the contact's ignored streak and backoff are untouched, and the check-in keeps its
    period key: it forms, once, at the first tick after the overload ends."""
    from protagine.mind.affect import CONSUMERS, AffectView
    fx = make([contact(CONTACT, may_contact="auto", cadence=CADENCE)])
    real_view = fx.mind.feelings.view
    overloaded = AffectView(route={name: "state" for name in CONSUMERS}, owner_id=OWNER, overloaded=True, load=0.8)
    fx.mind.feelings.view = lambda: overloaded
    fx.shift(C + PAST)
    for _ in range(3):
        summary = await fx.tick()
        assert summary["formed"] == [] and await fx.send_all() == []
    assert fx.messages_to(CONTACT) == []
    rows = await fx.mind._social_rows(fx.now)
    assert rows[0]["ignored_streak"] == 0 and rows[0].get("last_outbound_ts") in (None, "")
    fx.mind.feelings.view = real_view
    fx.shift(timedelta(minutes=1))
    formed, = (await fx.tick())["formed"]
    assert formed["type"] == "check_in" and formed["status"] == "approved"
    sent, = await fx.send_all()
    assert sent["recipient"] == CONTACT and (await fx.tick())["formed"] == []
