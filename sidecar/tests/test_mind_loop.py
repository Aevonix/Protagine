"""The first closed loop, end to end in the sidecar, with a fake router model.

A commitment in a turn is captured by the projection worker's
``commitment_extract`` task, becomes overdue when the clock shifts, turns
into exactly one dispatchable intention within two ticks, is bound to a
Hermes reference, gets its outcome recorded with its ``verified`` value and
is recallable from the autobiography. The ask path, the 72 h expiry, the off
switch without a model endpoint, ``why`` and the dismissal multiplier are
covered against the same fixtures (build plan M2 acceptance tests; evals 7.2
tests 1, 2, 7, 10 and 14 on the sidecar side).
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from protagine.api.middleware import ApiKeyMiddleware
from protagine.api.routers import mind as mind_router
from protagine.commitments.extract import CommitmentExtractor
from protagine.commitments.store import CommitmentStore
from protagine.feedback import TypeFeedbackStore
from protagine.initiatives.store import InitiativeStore
from protagine.mind import Mind, audit
from protagine.mind.drives import schedule_key
from protagine.self_model.expectations import ExpectationEngine, ExpectationStore
from protagine.turns.idempotency import TurnIdempotencyLedger

OWNER = "p-01"
CONTACT = "p-02"
KEY = "test-api-key"
AUTH = {"Authorization": "Bearer " + KEY}
TARGET = f"telegram:{OWNER}-handle"      # where the body sends the owner's messages


class FakeRouter:
    """Answers ``commitment_extract`` with one commitment due one hour after the turn."""

    supports_function_routing = True

    def __init__(self, due_at: datetime, description: str = "Send the owner the report", metadata=None,
                 obligor=None) -> None:
        self.due_at, self.description, self.metadata, self.obligor, self.calls = due_at, description, metadata, obligor, []

    def function_deadline_seconds(self, *, context=None):
        return 20

    async def complete(self, messages, *, context=None, **_):
        self.calls.append(context)
        schema = (context or {}).get("response_schema")  # the real router's output contract (router.py)
        assert isinstance(schema, dict) and set(schema) == {"name", "schema"} and isinstance(schema["schema"], dict)
        return SimpleNamespace(content=json.dumps([{
            "description": self.description, "due_at": self.due_at.isoformat(), "priority": 70,
            "source_type": "cognition", "metadata": self.metadata, "obligor": self.obligor}]))


class FakeContacts:
    def __init__(self, records):
        self.records = records
        self.handles = {}      # contact_id -> [(gateway, address)]; anyone else has their default handle

    async def get(self, contact_id):
        record = self.records.get(contact_id)
        return SimpleNamespace(to_dict=lambda: record) if record else None

    async def get_handles(self, contact_id):
        pairs = self.handles.get(contact_id, [("telegram", f"{contact_id}-handle")])
        return [SimpleNamespace(gateway=gateway, address=address, is_primary=True, verified=True)
                for gateway, address in pairs]

    async def resolve_handle(self, gateway, address):
        for contact_id in self.records:
            if address == f"{contact_id}-handle":
                return SimpleNamespace(contact_id=contact_id)
        return None


class Fixture:
    def __init__(self, tmp_path, *, autonomy="standard", config=None, router=None, drain=False):
        self.now = datetime.now(timezone.utc).replace(microsecond=0)
        self.state = tmp_path
        self.store = InitiativeStore(state_dir=tmp_path)
        self.commitments = CommitmentStore(tmp_path / "protagine-commitments.db")
        self.feedback = TypeFeedbackStore(str(tmp_path / "protagine-feedback.db"))
        self.expectations = ExpectationEngine(ExpectationStore(str(tmp_path / "protagine-expectations.db")))
        self.ledger = TurnIdempotencyLedger(tmp_path / "turn-idempotency.db")
        self.contacts = FakeContacts({OWNER: {"contact_id": OWNER, "interaction_allowed": True},
                                      CONTACT: {"contact_id": CONTACT, "interaction_allowed": True},
                                      "p-03": {"contact_id": "p-03", "interaction_allowed": False}})
        self.config = {"autonomy": autonomy, **(config or {})}
        self.router, self.drain = router, drain      # the mind's router; drain = wire the extractor as production does
        self.persisted = []
        self.mind = self.build()

    def build(self) -> Mind:
        capture = CommitmentExtractor(self.ledger, lambda: self.commitments) if self.drain else None
        mind = Mind(config=self.config, store=self.store, state_dir=self.state, owner_id=OWNER,
                    commitments=self.commitments, feedback=self.feedback, expectations=self.expectations,
                    contacts=self.contacts, ledger=self.ledger, clock=lambda: self.now, backups=False,
                    persist=self.persisted.append, router=self.router, capture=capture)
        mind.digest_hour = 25          # the digest test lowers it; the wall clock decides otherwise
        return mind

    def restart(self) -> Mind:
        self.mind = self.build()
        return self.mind

    def shift(self, **delta) -> None:
        self.now += timedelta(**delta)

    def turn(self, turn_id, contact_id, user, assistant) -> None:
        self.ledger.record_source(turn_id, contact_id=contact_id, session_id=f"session-{contact_id}", messages=[
            {"role": "user", "content": user}, {"role": "assistant", "content": assistant}],
            occurred_at=self.now.isoformat())

    async def capture(self, router) -> bool:
        extractor = CommitmentExtractor(self.ledger, lambda: self.commitments)
        return await extractor.process_one(router)

    def app(self) -> FastAPI:
        app = FastAPI()
        app.add_middleware(ApiKeyMiddleware, api_key=KEY)
        app.include_router(mind_router.router)
        mind_router.set_mind(self.mind)
        return app


@pytest.fixture
def fx(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fixture = Fixture(tmp_path)
    yield fixture
    mind_router.set_mind(None)
    fixture.store.close()


# ---------------------------------------------------------------------------
# The closed loop
# ---------------------------------------------------------------------------

async def test_captured_owner_promise_becomes_one_reminder_message(fx):
    """What the owner promised becomes a word back to the owner, not a board task: the reminder is
    the effect, sent once, with the description and how overdue it is. The row is what today's
    extractor writes (no ``obligor``): an owner-owed row until the capture side says otherwise."""
    fx.turn("turn-1", OWNER, "I told Sam I would send him the report within the hour; if I go quiet past "
                             "that, remind me.", "Noted.")
    router = FakeRouter(fx.now + timedelta(hours=1), description="Send Sam the report")
    assert await fx.capture(router) is True
    assert router.calls[0]["task"] == "commitment_extract"
    open_items = fx.commitments.get_pending_for_person(OWNER)
    assert len(open_items) == 1 and open_items[0]["status"] == "pending"
    assert open_items[0]["source_type"] == "cognition"
    assert (open_items[0]["metadata"] or {}).get("obligor", "owner") == "owner"

    first = await fx.mind.tick(force=True)
    assert first["formed"] == [] and await fx.mind.outbox_ready() == []       # not due yet: nothing

    fx.shift(hours=2)
    second = await fx.mind.tick(force=True)
    assert second["overdue_flipped"] == 1
    assert fx.commitments.get(open_items[0]["id"])["status"] == "overdue"
    formed, = second["formed"]
    assert formed["type"] == "commitment_reminder" and formed["kind"] == "message" and formed["decision"] == "act"
    third = await fx.mind.tick(force=True)
    assert third["formed"] == []                                          # the obligation is reported once
    assert fx.mind.dispatch() == []                                       # a message is never a task

    ready, = await fx.mind.outbox_ready()
    assert ready["recipient"] == OWNER and ready["recipient_is_owner"] is True and ready["kind"] == "notice"
    assert ready["recipient_handles"][0]["address"] == f"{OWNER}-handle"
    due = datetime.fromisoformat(open_items[0]["due_at"])
    assert "Send Sam the report" in ready["text"] and due.strftime("%Y-%m-%d %H:%M UTC") in ready["text"]
    assert ready["text"].endswith("1 h ago.")
    row = fx.store.get(ready["id"])
    assert row.dedup_key == schedule_key(open_items[0]["id"], "overdue", due) and row.cls == "owner"
    fx.mind.outbox.sending(ready["id"], target=TARGET)
    assert fx.mind.outbox.sent(ready["id"], result="sent").status == "sent"
    fx.restart()
    assert await fx.mind.outbox_ready() == [] and (await fx.mind.tick(force=True))["formed"] == []
    why = audit.why(fx.store, ready["id"])
    for needle in ("duty drive", "commitment:", "decided act", "outcome done"):
        assert needle in why["sentence"], why["sentence"]


async def test_commitment_becomes_one_task_and_its_outcome_is_recalled(fx):
    """The assistant's own promise ("I'll send you the report by 3pm") is work the body performs,
    not a reminder to the owner: capture records who owes it (``metadata.obligor``), and the
    overdue row becomes exactly one dispatchable task whose outcome is recalled."""
    fx.turn("turn-1", OWNER, "Can you get me the report?", "Sure, I'll send you the report by 3pm.")
    router = FakeRouter(fx.now + timedelta(hours=1), metadata={"obligor": "assistant"})
    assert await fx.capture(router) is True
    assert router.calls[0]["task"] == "commitment_extract"
    open_items = fx.commitments.get_pending_for_person(OWNER)
    assert len(open_items) == 1 and open_items[0]["status"] == "pending"
    assert open_items[0]["source_type"] == "cognition" and open_items[0]["metadata"]["obligor"] == "assistant"

    first = await fx.mind.tick(force=True)
    assert first["formed"] == [] and fx.mind.dispatch() == []          # not due yet: nothing

    fx.shift(hours=2)
    second = await fx.mind.tick(force=True)
    assert second["overdue_flipped"] == 1
    assert fx.commitments.get(open_items[0]["id"])["status"] == "overdue"
    assert [item["type"] for item in second["formed"]] == ["commitment_overdue"]
    third = await fx.mind.tick(force=True)
    assert third["formed"] == []                                          # the obligation is reported once

    queue = fx.mind.dispatch()
    assert len(queue) == 1
    item = queue[0]
    assert item["assignee"] == "protagine-act" and item["idempotency_key"] == f"mind:{item['id']}"
    assert item["kind"] == "task" and "Report what you did" in item["body"]
    assert "send you the report" not in item["body"].lower() or "quoted context" in item["body"]

    # a lost bound ack and a restart create no second task
    fx.restart()
    assert [row["id"] for row in fx.mind.dispatch()] == [item["id"]]
    fx.mind.bound(item["id"], "kanban:task-42")
    assert fx.mind.dispatch() == []
    row = fx.store.get(item["id"])
    assert row.status == "dispatched" and row.hermes_ref == "kanban:task-42" and row.expectation_id

    fx.commitments.resolve(open_items[0]["id"], outcome="done", note="report sent", resolved_by="owner")
    done = fx.mind.outcomes.record(item["id"], status="done", hermes_ref="kanban:task-42", summary="sent it")
    assert done.outcome == "done" and done.verified == "check"
    assert done.result_metadata["check"]["passed"] is True
    assert fx.expectations.store.get(row.expectation_id).outcome == "hit"

    hits = fx.ledger.search_sources("report task ended", contact_id=OWNER, session_id="another-session")
    mind_hits = [hit for hit in hits if hit["session_id"] == "mind"]
    assert any("ended done" in hit["content"] and "kanban:task-42" in hit["content"] for hit in mind_hits)
    assert mind_hits and all(hit["role"] == "assistant" and hit["scope"] == "person" for hit in mind_hits)

    why = audit.why(fx.store, item["id"])
    for needle in ("duty drive", "commitment:", "decided act", "kanban:task-42", "outcome done", "verified: check"):
        assert needle in why["sentence"], why["sentence"]


async def test_body_report_closes_the_commitment_and_the_check_stays_honest(fx):
    """The worker cannot certify its own delivery: with the row still open when the body reports
    done, the check cannot run (verified none), and the mind closes the row in the body's name."""
    created = fx.commitments.create(person_id=OWNER, description="send the vendor the quote",
                                    due_at=(fx.now + timedelta(minutes=5)).isoformat())
    fx.shift(hours=1)
    await fx.mind.tick(force=True)
    item = fx.mind.dispatch()[0]
    fx.mind.bound(item["id"], "kanban:t9")
    done = fx.mind.outcomes.record(item["id"], status="done", hermes_ref="kanban:t9", summary="quote sent")
    assert done.outcome == "done" and done.verified == "none" and "check" not in done.result_metadata
    row = fx.commitments.get(created["id"])
    assert row["status"] == "fulfilled"
    resolution = row["metadata"]["resolution"]
    assert resolution["by"] == "body" and resolution["outcome"] == "done" and item["id"] in resolution["note"]
    assert fx.expectations.store.get(fx.store.get(item["id"]).expectation_id).outcome == "hit"
    # Reporting the same outcome again neither reopens nor re-resolves the row.
    again = fx.mind.outcomes.record(item["id"], status="done", hermes_ref="kanban:t9", summary="quote sent")
    assert again.outcome == "done" and fx.commitments.get(created["id"])["metadata"]["resolution"] == resolution


async def test_unverified_when_no_check_can_run(fx):
    fx.commitments.create(person_id=OWNER, description="call the vendor",
                          due_at=(fx.now + timedelta(minutes=5)).isoformat())
    fx.shift(hours=1)
    await fx.mind.tick(force=True)
    item = fx.mind.dispatch()[0]
    fx.mind.bound(item["id"], "kanban:t1")
    row = fx.store.get(item["id"])
    fx.store.update(row.id, success_check=None)
    done = fx.mind.outcomes.record(item["id"], status="done", summary="called")
    assert done.verified == "none"
    failed_row, _ = fx.store.create_intention(kind="task", type="x", title="x", drive="duty", cls="owner",
                                              decision="act", decision_reason="r", status="dispatched",
                                              dedup_key="x", recipient=OWNER, created_at=fx.now)
    failed = fx.mind.outcomes.record(failed_row.id, status="failed", summary="worker crashed")
    assert failed.outcome == "failed" and failed.verified == "hermes_failure"


# ---------------------------------------------------------------------------
# The ask path (7.7): a code, no Hermes object, expiry, unrelated work proceeds
# ---------------------------------------------------------------------------

async def test_ask_path_with_code_expiry_and_unrelated_work(fx):
    fx.commitments.create(person_id=CONTACT, description="send the contact the slides",
                          due_at=(fx.now + timedelta(minutes=1)).isoformat())
    fx.commitments.create(person_id=OWNER, description="file the owner's expense report",
                          due_at=(fx.now + timedelta(minutes=1)).isoformat())
    fx.shift(minutes=10)
    summary = await fx.mind.tick(force=True)
    decisions = {item["type"] + ":" + str(fx.store.get(item["id"]).entity_id): item["decision"]
                 for item in summary["formed"]}
    assert decisions == {f"commitment_overdue:{CONTACT}": "ask", f"commitment_overdue:{OWNER}": "act"}
    asks = fx.mind.asks()
    assert len(asks) == 1 and len(asks[0]["ask_code"]) == 3
    code = asks[0]["ask_code"]
    # nothing in Hermes for the ask; the owner's duty proceeds meanwhile
    queue = fx.mind.dispatch()
    assert [fx.store.get(item["id"]).entity_id for item in queue] == [OWNER]
    notice = [row for row in fx.store.intentions(kind=["message"], limit=10) if row.type == "ask_notice"]
    assert len(notice) == 1 and code in notice[0].context["text"]

    with pytest.raises(PermissionError):
        fx.mind.answer(code, yes=True, contact_id="p-03")                 # a guest cannot approve
    with pytest.raises(PermissionError):
        fx.mind.answer(code, yes=True, contact_id=OWNER, message="yes please")  # no code in the message
    assert fx.mind.answer("ZZZ", yes=True) is None

    fx.shift(hours=71)
    await fx.mind.tick(force=True)
    assert fx.store.get_by_ask_code(code).status == "asked"
    fx.shift(hours=2)
    await fx.mind.tick(force=True)
    expired = fx.store.get(asks[0]["id"])
    assert expired.status == "expired" and expired.outcome == "expired" and expired.verdict == "ignored"
    assert fx.feedback.multiplier("commitment_overdue:duty") < 1.0
    assert fx.mind.answer(code, yes=True) is None                          # the code is gone


async def test_ask_approval_creates_the_task_and_no_wins(fx):
    fx.commitments.create(person_id=CONTACT, description="send the contact the slides",
                          due_at=(fx.now + timedelta(minutes=1)).isoformat())
    fx.commitments.create(person_id="p-03", description="ping the silenced contact",
                          due_at=(fx.now + timedelta(minutes=1)).isoformat())
    fx.shift(minutes=10)
    summary = await fx.mind.tick(force=True)
    by_type = {fx.store.get(item["id"]).entity_id: item for item in summary["formed"]}
    assert by_type["p-03"]["decision"] == "drop"                         # never: dropped, never asked
    code = fx.store.get(by_type[CONTACT]["id"]).ask_code
    assert fx.mind.dispatch() == []
    approved = fx.mind.answer(code, yes=True, contact_id=OWNER, message=f"yes {code} go ahead")
    assert approved.status == "approved" and approved.verdict == "actioned"
    assert [item["id"] for item in fx.mind.dispatch()] == [approved.id]
    assert fx.feedback.multiplier("commitment_overdue:duty") > 1.0

    fx.commitments.create(person_id=CONTACT, description="another contact item",
                          due_at=(fx.now + timedelta(minutes=1)).isoformat())
    fx.shift(minutes=10)
    summary = await fx.mind.tick(force=True)
    code = fx.store.get(summary["formed"][0]["id"]).ask_code
    refused = fx.mind.answer(code, yes=False)
    assert refused.status == "cancelled" and refused.outcome == "denied" and refused.verdict == "dismissed"


# ---------------------------------------------------------------------------
# The off switch (7.9): works with no model endpoint at all
# ---------------------------------------------------------------------------

async def test_off_switch_stops_effects_without_a_model(fx):
    fx.commitments.create(person_id=OWNER, description="send the owner the notes",
                          due_at=(fx.now + timedelta(minutes=1)).isoformat())
    fx.shift(minutes=10)
    await fx.mind.tick(force=True)
    assert len(fx.mind.dispatch()) == 1
    fx.mind.outbox.notice(type="stale_task", title="a queued notice", text="hello", dedup_key="n1")
    assert len(await fx.mind.outbox_ready()) == 1

    result = fx.mind.off(reason="test")
    assert result["enabled"] is False and result["cancelled_messages"] == 1
    assert (fx.state / "mind.off").exists()
    assert fx.mind.dispatch() == [] and await fx.mind.outbox_ready() == []
    verdict = await fx.mind.guard(tool="write_file", args={"path": "x"}, run="mind")
    assert verdict["allow"] is False and "off" in verdict["reason"]
    assert (await fx.mind.tick(force=True))["skipped"] == "off"
    fx.commitments.create(person_id=OWNER, description="a second obligation",
                          due_at=(fx.now + timedelta(minutes=1)).isoformat())
    fx.shift(minutes=10)
    assert (await fx.mind.tick(force=True))["formed"] == []

    fx.restart()                                                            # the marker survives a restart
    assert fx.mind.enabled is False and fx.mind.dispatch() == []
    fx.mind.on()
    assert fx.mind.enabled and not (fx.state / "mind.off").exists()
    assert fx.persisted == [{"mind": {"enabled": True}}]
    assert len(fx.mind.dispatch()) == 1                                    # the queued task is still there
    assert fx.mind.stats()["off_switch_uses"] == 1


# ---------------------------------------------------------------------------
# Feedback: a dismissal drops a single candidate below the act threshold
# ---------------------------------------------------------------------------

async def test_dismissal_lowers_the_type_multiplier_below_the_threshold(fx):
    fx.commitments.create(person_id=OWNER, description="first overdue item",
                          due_at=(fx.now + timedelta(minutes=1)).isoformat())
    fx.shift(minutes=10)
    summary = await fx.mind.tick(force=True)
    first = summary["formed"][0]
    assert first["score"] >= fx.mind.act_threshold
    item = fx.mind.dispatch()[0]
    fx.mind.bound(item["id"], "kanban:t9")
    dismissed = fx.mind.outcomes.record(item["id"], status="archived", summary="the owner archived it")
    assert dismissed.outcome == "cancelled" and dismissed.verdict == "dismissed"
    assert fx.feedback.multiplier("commitment_overdue:duty") == pytest.approx(0.85)

    fx.commitments.create(person_id=OWNER, description="second overdue item",
                          due_at=(fx.now + timedelta(minutes=1)).isoformat())
    fx.shift(minutes=10)
    summary = await fx.mind.tick(force=True)
    assert summary["formed"] == [] and summary["below_threshold"] == 1
    assert fx.mind.dispatch() == []
    fx.mind.rate(item["id"], "useful")                                     # an explicit verdict lifts it back
    assert fx.feedback.multiplier("commitment_overdue:duty") > 0.9


async def test_an_intention_contributes_once_and_a_corrected_rating_replaces(fx):
    fx.commitments.create(person_id=OWNER, description="first overdue item",
                          due_at=(fx.now + timedelta(minutes=1)).isoformat())
    fx.shift(minutes=10)
    await fx.mind.tick(force=True)
    item = fx.mind.dispatch()[0]
    fx.mind.bound(item["id"], "kanban:t9")
    fx.mind.outcomes.record(item["id"], status="archived", summary="the owner archived it")
    assert fx.feedback.multiplier("commitment_overdue:duty") == pytest.approx(0.85)   # the implicit dismissal
    fx.mind.rate(item["id"], "useful")                                     # the owner's rating replaces it
    fx.mind.rate(item["id"], "useful")                                     # said again: nothing changes
    assert fx.feedback.multiplier("commitment_overdue:duty") == pytest.approx(1.12)
    fx.mind.rate(item["id"], "wrong")                                      # corrected: replaced, not added
    assert fx.feedback.multiplier("commitment_overdue:duty") == pytest.approx(0.85)
    row = {r["itype"]: r for r in fx.feedback.snapshot()}["commitment_overdue:duty"]
    assert (row["actioned"], row["dismissed"]) == (0, 1)
    assert fx.store.get(item["id"]).verdict == "wrong"


# ---------------------------------------------------------------------------
# Outbox: at most once, uncertain after a crash, the digest verbatim
# ---------------------------------------------------------------------------

async def test_outbox_at_most_once_and_uncertain_after_a_crash(fx):
    fx.commitments.create(person_id=OWNER, description="text the owner the code",
                          due_at=(fx.now + timedelta(minutes=1)).isoformat(),
                          metadata={"kind": "deliverable", "content": "The code is 4471.", "channel_hint": "sms"})
    fx.shift(minutes=10)
    summary = await fx.mind.tick(force=True)
    assert summary["formed"][0]["type"] == "commitment_deliverable"
    ready = await fx.mind.outbox_ready()
    assert len(ready) == 1 and ready[0]["text"] == "The code is 4471." and ready[0]["kind"] == "notice"
    assert ready[0]["recipient"] == OWNER and ready[0]["recipient_handles"][0]["address"] == f"{OWNER}-handle"
    assert fx.mind.dispatch() == []                                        # a message is never a task
    fx.mind.outbox.sending(ready[0]["id"], target=TARGET)
    fx.restart()                                                            # crash between sending and sent
    row = fx.store.get(ready[0]["id"])
    assert row.status == "uncertain" and row.outcome == "uncertain"
    assert await fx.mind.outbox_ready() == []                                    # never resent
    digest = fx.mind.outbox.build_digest(since=fx.now - timedelta(days=1), level="standard")
    assert "Delivery uncertain (1), not resent" in digest and "text the owner the code" in digest.lower()
    assert fx.mind.outbox.sent(ready[0]["id"], result="sent").status == "uncertain"   # a late ack changes nothing


async def test_digest_is_queued_once_a_day_verbatim(fx):
    fx.mind.digest_hour = 0
    fx.mind.quiet = None
    fx.commitments.create(person_id=OWNER, description="digest item",
                          due_at=(fx.now + timedelta(minutes=1)).isoformat())
    fx.shift(minutes=10)
    summary = await fx.mind.tick(force=True)
    assert summary["digest"] is not None
    digest = fx.store.get(summary["digest"])
    assert digest.type == "digest" and digest.entity_id == OWNER and digest.status == "approved"
    assert "Daily digest" in digest.context["text"] and "digest item" in digest.context["text"]
    again = await fx.mind.tick(force=True)
    assert again["digest"] is None
    payload = [item for item in await fx.mind.outbox_ready() if item["type"] == "digest"][0]
    assert payload["text"] == digest.context["text"] and payload["kind"] == "notice"


async def test_quiet_hours_hold_notices_but_not_the_digest(fx):
    fx.mind.quiet = (0, 1439)                                               # always quiet, for the test
    fx.mind.outbox.notice(type="stale_task", title="n", text="held", dedup_key="held")
    assert await fx.mind.outbox_ready() == []
    fx.mind.outbox.queue_digest(local_date="2026-09-23", text="Daily digest")
    assert [item["type"] for item in await fx.mind.outbox_ready()] == ["digest"]


# ---------------------------------------------------------------------------
# Observations and upkeep
# ---------------------------------------------------------------------------

async def test_stale_owner_task_observation_becomes_a_notice(fx):
    fx.mind.observe({"observations": [
        {"kind": "stale_task", "id": "t-1", "title": "Renew the domain", "assignee": "default", "age_hours": 80},
        {"kind": "stale_task", "id": "t-2", "title": "mind child", "assignee": "protagine-act", "age_hours": 80},
        {"kind": "stale_task", "id": "t-3", "title": "fresh", "assignee": "default", "age_hours": 5}]})
    summary = await fx.mind.tick(force=True)
    assert [item["type"] for item in summary["formed"]] == ["stale_task"]
    ready = await fx.mind.outbox_ready()
    assert len(ready) == 1 and "Renew the domain" in ready[0]["text"]
    assert (await fx.mind.tick(force=True))["formed"] == []


async def test_body_staleness_pauses_forming_but_not_expiry(fx):
    fx.commitments.create(person_id=OWNER, description="stale body item",
                          due_at=(fx.now + timedelta(minutes=1)).isoformat())
    fx.shift(minutes=10)
    summary = await fx.mind.tick()                                          # not forced: the body never pulled
    assert summary["skipped"] == "body stale" and summary["formed"] == []
    fx.mind.dispatch()                                                      # the body pulls
    summary = await fx.mind.tick()
    assert [item["type"] for item in summary["formed"]] == ["commitment_overdue"]


# ---------------------------------------------------------------------------
# The routes, behind the one key
# ---------------------------------------------------------------------------

async def test_routes_end_to_end(fx):
    app = fx.app()
    # Agent work (not spoken in conversation): the task path the dispatch and outcome routes carry.
    fx.commitments.create(person_id=OWNER, description="Send the owner the invoice summary",
                          due_at=(fx.now + timedelta(hours=1)).isoformat(), source_type="manual")
    fx.shift(hours=2)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/v1/mind/state")).status_code in {401, 403}
        state = await client.get("/v1/mind/state", headers=AUTH)
        assert state.status_code == 200 and state.json()["enabled"] is True
        assert (await client.get("/v1/mind/status", headers=AUTH)).json()["autonomy"] == "standard"

        tick = await client.post("/v1/mind/tick", headers=AUTH)
        assert tick.status_code == 200 and tick.json()["formed"][0]["type"] == "commitment_overdue"
        queue = (await client.get("/v1/mind/dispatch", headers=AUTH)).json()
        assert len(queue) == 1 and queue[0]["assignee"] == "protagine-act"
        # The body's ack carries what it knows about the task; the same ref may arrive twice.
        bound = await client.post(f"/v1/mind/dispatch/{queue[0]['id']}/bound", headers=AUTH, json={
            "hermes_ref": "kanban:t7", "hermes_kind": "kanban", "status": "ready", "bound_at": fx.now.isoformat()})
        assert bound.status_code == 200 and bound.json()["status"] == "dispatched"
        again = await client.post(f"/v1/mind/dispatch/{queue[0]['id']}/bound", headers=AUTH,
                                  json={"hermes_ref": "kanban:t7"})
        assert again.status_code == 200 and again.json()["hermes_ref"] == "kanban:t7"
        assert (await client.get("/v1/mind/dispatch", headers=AUTH)).json() == []

        guard = await client.post("/v1/mind/guard", headers=AUTH, json={
            "tool": "kanban_create", "args": {"assignee": "default"}, "session_id": "s", "run": "mind"})
        assert guard.json()["allow"] is False and guard.json()["action"] == "block"
        guard = await client.post("/v1/mind/guard", headers=AUTH, json={
            "tool": "send_message", "args": {"platform": "telegram", "target": "p-03-handle"}, "run": "mind",
            "session": "s", "task_id": "t7", "owner": None, "contact_id": None, "platform": "", "sender_id": ""})
        assert guard.json()["allow"] is False and "may not be contacted" in guard.json()["reason"]
        guard = await client.post("/v1/mind/guard", headers=AUTH, json={
            "tool": "send_message", "args": {"platform": "telegram", "target": f"{OWNER}-handle"}, "run": "mind"})
        assert guard.json()["allow"] is True

        # A requeued failed run is progress; the settled report is the body's full shape.
        progress = await client.post("/v1/mind/outcome", headers=AUTH, json={
            "id": queue[0]["id"], "hermes_ref": "kanban:t7", "hermes_kind": "kanban", "status": "ready",
            "outcome": "failed", "final": False, "summary": None, "error": "crashed once", "verified": "hermes_failure",
            "run": {"id": 1, "outcome": "crashed", "status": "ended"}, "block_kind": None, "consecutive_failures": 1,
            "completed_at": None, "observed_at": fx.now.isoformat()})
        assert progress.status_code == 200 and progress.json()["status"] == "dispatched"
        assert progress.json()["outcome"] is None
        outcome = await client.post("/v1/mind/outcome", headers=AUTH, json={
            "id": queue[0]["id"], "hermes_ref": "kanban:t7", "hermes_kind": "kanban", "status": "done",
            "outcome": "done", "final": True, "summary": "invoice sent", "error": None, "verified": None,
            "run": {"id": 2, "outcome": "completed", "status": "ended", "profile": "protagine-act",
                    "started_at": fx.now.isoformat(), "ended_at": fx.now.isoformat()},
            "block_kind": None, "consecutive_failures": 0, "completed_at": fx.now.isoformat(),
            "observed_at": fx.now.isoformat()})
        assert outcome.status_code == 200 and outcome.json()["outcome"] == "done"
        assert outcome.json()["verified"] in {"check", "none"}
        assert (await client.post("/v1/mind/outcome", headers=AUTH, json={
            "id": "nope", "status": "done", "outcome": "done", "final": True})).status_code == 404

        why = await client.get(f"/v1/mind/why/{queue[0]['id']}", headers=AUTH)
        assert why.status_code == 200
        body = why.json()
        assert body["drive"] == "duty" and body["decision"] == "act" and body["hermes_ref"] == "kanban:t7"
        assert body["outcome"] == "done" and body["verified"] and body["evidence"]
        assert (await client.get(f"/v1/mind/log/{queue[0]['id']}", headers=AUTH)).json()["id"] == queue[0]["id"]
        assert (await client.get("/v1/mind/why/nope", headers=AUTH)).status_code == 404

        log = await client.get("/v1/mind/log", headers=AUTH, params={"limit": 5})
        assert log.json()["entries"][0]["id"] == queue[0]["id"] and "commitment_overdue" in log.json()["text"]
        stats = await client.get("/v1/mind/stats", headers=AUTH)
        assert stats.json()["acted"] == 1

        observations = await client.post("/v1/mind/observations", headers=AUTH, json={"observations": [
            {"kind": "blocked_task", "id": "t-2", "title": "blocked", "assignee": "default"}]})
        assert observations.json() == {"accepted": 1, "kinds": ["blocked_task"]}
        # The body's own shape: lists per kind, idle seconds per task, its heartbeat and the counts.
        observations = await client.post("/v1/mind/observations", headers=AUTH, json={
            "observed_at": fx.now.isoformat(), "board": "default",
            "body": {"pid": 1, "profile": "default", "ticks": 3, "mind_ticks": 3, "stale": False},
            "counts": {"ready": 2, "blocked": 1},
            "stale_tasks": [{"id": "t-9", "title": "old", "status": "ready", "assignee": "default",
                             "idle_s": 80 * 3600, "stale_after_s": 72 * 3600}],
            "blocked_tasks": [{"id": "t-2", "title": "blocked", "status": "blocked", "block_kind": "needs_input"}],
            "goals": [], "mind_tasks": [{"id": "t7", "status": "done", "intention_id": queue[0]["id"], "run": None}]})
        assert observations.json() == {"accepted": 3, "kinds": ["blocked_task", "mind_task", "stale_task"]}
        assert fx.mind.observations["stale_task"][0]["age_hours"] == 80.0 and fx.mind.board_counts == {"ready": 2, "blocked": 1}
        assert (await client.get("/v1/mind/state", headers=AUTH)).json()["body"]["ticks"] == 3

        assert (await client.post("/v1/mind/rate", headers=AUTH,
                                  json={"id": queue[0]["id"], "verdict": "useful"})).json()["verdict"] == "useful"
        assert (await client.post("/v1/mind/rate", headers=AUTH,
                                  json={"id": queue[0]["id"], "verdict": "meh"})).status_code == 422
        level = await client.post("/v1/mind/level", headers=AUTH, json={"autonomy": "suggest"})
        assert level.json()["autonomy"] == "suggest" and fx.persisted[-1] == {"mind": {"autonomy": "suggest"}}
        assert (await client.post("/v1/mind/level", headers=AUTH, json={"autonomy": "yolo"})).status_code == 422
        reset = await client.post("/v1/mind/reset", headers=AUTH, json={"cls": "owner"})
        assert reset.json()["tripped"] is False

        off = await client.post("/v1/mind/off", headers=AUTH, json={"reason": "route test"})
        assert off.json()["enabled"] is False
        assert (await client.get("/v1/mind/state", headers=AUTH)).json()["enabled"] is False
        assert (await client.get("/v1/mind/outbox", headers=AUTH)).json() == []
        assert (await client.post("/v1/mind/on", headers=AUTH)).json()["enabled"] is True


async def test_ask_routes_check_the_owner_and_the_code(fx):
    app = fx.app()
    fx.commitments.create(person_id=CONTACT, description="send the contact the slides",
                          due_at=(fx.now + timedelta(minutes=1)).isoformat())
    fx.shift(minutes=10)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        await client.post("/v1/mind/tick", headers=AUTH)
        asks = (await client.get("/v1/mind/asks", headers=AUTH)).json()
        code = asks["asks"][0]["ask_code"]
        assert code in asks["text"]
        refused = await client.post(f"/v1/mind/asks/{code}/yes", headers=AUTH,
                                    json={"contact_id": "p-03", "message": f"yes {code}"})
        assert refused.status_code == 403
        refused = await client.post(f"/v1/mind/asks/{code}/yes", headers=AUTH,
                                    json={"contact_id": OWNER, "message": "yes"})
        assert refused.status_code == 403
        assert (await client.post("/v1/mind/asks/QQQ/yes", headers=AUTH)).status_code == 404
        state = (await client.get("/v1/mind/state", headers=AUTH)).json()
        assert state["open_asks"] == 1 and state["asks"][0]["code"] == code and state["asks"][0]["expires_at"]
        # The plugin's route: the same checks on {code, answer, contact_id, message}.
        refused = await client.post("/v1/mind/decide", headers=AUTH, json={
            "code": code, "answer": "yes", "session_id": "s-guest", "contact_id": "p-03", "message": f"yes {code}"})
        assert refused.status_code == 403
        assert (await client.post("/v1/mind/decide", headers=AUTH, json={
            "code": "QQQ", "answer": "yes", "contact_id": OWNER})).status_code == 404
        assert (await client.post("/v1/mind/decide", headers=AUTH, json={
            "code": code, "answer": "maybe", "contact_id": OWNER})).status_code == 422
        approved = await client.post("/v1/mind/decide", headers=AUTH, json={
            "code": code.lower(), "answer": "yes", "session_id": "s-owner", "contact_id": OWNER,
            "message": f"Yes {code}, do it"})
        assert approved.status_code == 200 and approved.json()["ok"] is True
        assert approved.json()["status"] == "approved" and approved.json()["id"]
        assert len((await client.get("/v1/mind/dispatch", headers=AUTH)).json()) == 1
        assert (await client.post("/v1/mind/decide", headers=AUTH, json={
            "code": code, "answer": "yes", "contact_id": OWNER})).status_code == 404  # answered: no open ask


async def test_outbox_routes(fx):
    app = fx.app()
    fx.mind.outbox.notice(type="stale_task", title="n", text="verbatim text", dedup_key="r1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        ready = (await client.get("/v1/mind/outbox", headers=AUTH)).json()
        assert ready[0]["text"] == "verbatim text" and ready[0]["dedup_key"] == f"mind:{ready[0]['id']}"
        assert ready[0]["recipient_is_owner"] is True and ready[0]["kind"] == "notice"
        sending = await client.post(f"/v1/mind/outbox/{ready[0]['id']}/sending", headers=AUTH,
                                    json={"target": "telegram:1001", "at": fx.now.isoformat()})
        assert sending.status_code == 200 and sending.json()["status"] == "sending"
        # Claimed once: a second claim (a body that lost its own ledger) is refused, never re-sent.
        assert (await client.post(f"/v1/mind/outbox/{ready[0]['id']}/sending", headers=AUTH)).status_code == 409
        assert (await client.post("/v1/mind/outbox/nope/sending", headers=AUTH)).status_code == 404
        assert (await client.get("/v1/mind/outbox", headers=AUTH)).json() == []
        sent = await client.post(f"/v1/mind/outbox/{ready[0]['id']}/sent", headers=AUTH, json={
            "result": "sent", "error": None, "hermes_ref": "telegram:msg-1", "at": fx.now.isoformat()})
        assert sent.json()["status"] == "sent" and sent.json()["outcome"] == "done"
        assert (await client.post("/v1/mind/outbox/nope/sent", headers=AUTH, json={"result": "sent"})).status_code == 404
        # A claim the body never settled becomes uncertain after ten minutes, not resent.
        fx.mind.outbox.notice(type="stale_task", title="n2", text="second", dedup_key="r2")
        second = (await client.get("/v1/mind/outbox", headers=AUTH)).json()[0]
        await client.post(f"/v1/mind/outbox/{second['id']}/sending", headers=AUTH, json={"target": "telegram:1001"})
        fx.shift(minutes=11)
        assert (await client.get("/v1/mind/outbox", headers=AUTH)).json() == []
        assert (await client.get(f"/v1/mind/why/{second['id']}", headers=AUTH)).json()["status"] == "uncertain"
        failed = fx.mind.outbox.notice(type="stale_task", title="n3", text="third", dedup_key="r3")
        await client.post(f"/v1/mind/outbox/{failed.id}/sending", headers=AUTH, json={"target": "telegram:1001"})
        report = await client.post(f"/v1/mind/outbox/{failed.id}/sent", headers=AUTH,
                                   json={"result": "failed", "error": "no platform configured"})
        assert report.json()["status"] == "failed" and report.json()["verified"] == "hermes_failure"
        assert "no platform" in (await client.get(f"/v1/mind/why/{failed.id}", headers=AUTH)).json()["summary"]


# ---------------------------------------------------------------------------
# Learning cannot write authority (evals 7.2 test 14, the sidecar side)
# ---------------------------------------------------------------------------

def test_no_learning_path_opens_the_config_or_constitution_for_writing():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1] / "protagine"
    offenders = []
    for path in [*(root / "mind").glob("*.py"), root / "feedback" / "store.py", root / "commitments" / "extract.py"]:
        if path.name == "cli.py":
            continue          # the owner's CLI writes the config on purpose
        text = path.read_text()
        for needle in ("protagine.yaml", "identity.yaml", "save_config(", "update_config(", "save_identity("):
            if needle in text:
                offenders.append(f"{path.name}: {needle}")
    assert offenders == [], offenders


def test_mind_persists_settings_only_through_the_injected_callback(fx):
    fx.mind.set_level("trusted")
    assert fx.persisted == [{"mind": {"autonomy": "trusted"}}]
    assert fx.mind.level == "trusted"
    with pytest.raises(ValueError):
        fx.mind.set_level("yolo")


# ---------------------------------------------------------------------------
# Level off, invalidation, the guard's recipients and suggest-level notices
# ---------------------------------------------------------------------------

async def test_level_off_stops_existing_effects_like_the_switch(fx):
    """``autonomy: off`` is the off switch by another name: what was approved waits, nothing goes out."""
    fx.commitments.create(person_id=OWNER, description="send the owner the notes",
                          due_at=(fx.now + timedelta(minutes=1)).isoformat())
    fx.shift(minutes=10)
    await fx.mind.tick(force=True)
    assert len(fx.mind.dispatch()) == 1
    fx.mind.outbox.notice(type="stale_task", title="a queued notice", text="hello", dedup_key="n1")
    assert len(await fx.mind.outbox_ready()) == 1

    result = fx.mind.set_level("off")
    assert result["cancelled_messages"] == 1 and fx.mind.enabled is False
    assert fx.mind.state()["enabled"] is False and fx.mind.state()["autonomy"] == "off"
    assert fx.mind.dispatch() == [] and await fx.mind.outbox_ready() == []
    verdict = await fx.mind.guard(tool="write_file", args={"path": "x"}, run="mind")
    assert verdict["allow"] is False and "autonomy level off" in verdict["reason"]
    assert (await fx.mind.tick(force=True))["skipped"] == "off"
    fx.commitments.create(person_id=OWNER, description="a second obligation",
                          due_at=(fx.now + timedelta(minutes=1)).isoformat())
    fx.shift(minutes=10)
    assert (await fx.mind.tick(force=True))["formed"] == []

    fx.config["autonomy"] = fx.persisted[-1]["mind"]["autonomy"]     # what the sidecar reads back at a restart
    fx.restart()
    assert fx.mind.enabled is False and fx.mind.dispatch() == []
    assert fx.mind.set_level("standard") == {"autonomy": "standard", "previous": "off"}
    assert fx.mind.enabled is True and len(fx.mind.dispatch()) == 1  # the approved task waited


async def test_resolved_obligations_invalidate_their_waiting_intentions(fx):
    """A fulfilled deliverable is not sent, a fulfilled commitment is not dispatched and its ask
    closes; the cancellation is the check's, never a dismissal of the type."""
    deliverable = fx.commitments.create(
        person_id=OWNER, description="text the owner the code", due_at=(fx.now + timedelta(minutes=1)).isoformat(),
        metadata={"kind": "deliverable", "content": "The code is 4471.", "channel_hint": "sms"})
    overdue = fx.commitments.create(person_id=OWNER, description="send the owner the notes",
                                    due_at=(fx.now + timedelta(minutes=1)).isoformat())
    asked = fx.commitments.create(person_id=CONTACT, description="send the contact the slides",
                                  due_at=(fx.now + timedelta(minutes=1)).isoformat())
    fx.shift(minutes=10)
    summary = await fx.mind.tick(force=True)
    assert len(summary["formed"]) == 3 and summary["invalidated"] == 0
    assert [m["type"] for m in await fx.mind.outbox_ready()] == ["commitment_deliverable", "ask_notice"]
    assert len(fx.mind.dispatch()) == 1 and len(fx.mind.asks()) == 1
    before = fx.feedback.multiplier("commitment_deliverable:duty")

    for row in (deliverable, overdue, asked):
        fx.commitments.update(row["id"], status="fulfilled")
    assert [m["type"] for m in await fx.mind.outbox_ready()] == ["ask_notice"]     # the pulls settle theirs
    assert fx.mind.dispatch() == []
    assert (await fx.mind.tick(force=True))["invalidated"] == 1               # the tick settles the ask
    assert fx.mind.asks() == []
    rows = fx.store.intentions(status=["cancelled"], limit=10)
    assert len(rows) == 3
    assert all(row.outcome == "cancelled" and row.verified == "check" and row.verdict is None for row in rows)
    assert all("invalidated: commitment" in (row.cancelled_reason or "") for row in rows)
    assert fx.feedback.multiplier("commitment_deliverable:duty") == before
    assert (await fx.mind.tick(force=True))["formed"] == []                    # dedup keys keep them settled


async def test_guard_reads_stock_targets_and_authorizes_cron_recipients(tmp_path, monkeypatch):
    """Stock ``send_message`` names its recipient as ``target="platform:chat_id[:thread_id]"``; a
    delivering cron job names its recipients through the plugin. Both get ``may_contact`` and budgets."""
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fx = Fixture(tmp_path, config={"budgets": {"contact_messages_per_day": 1}})
    try:
        guard = fx.mind.guard
        owner = await guard(tool="send_message", args={"target": f"telegram:{OWNER}-handle", "message": "hi"},
                            run="guest")
        assert owner["allow"] is True
        contact = await guard(tool="send_message", args={"target": f"telegram:{CONTACT}-handle"}, run="guest")
        assert contact["allow"] is False and contact["action"] == "ask"
        thread = await guard(tool="send_message", args={"target": f"telegram:{CONTACT}-handle:777"}, run="guest")
        assert thread["action"] == "ask"
        never = await guard(tool="send_message", args={"target": "telegram:p-03-handle"}, run="guest")
        assert never["action"] == "block" and "may not be contacted" in never["reason"]
        for target in ("telegram:nobody", "telegram", ""):
            unknown = await guard(tool="send_message", args={"target": target}, run="guest")
            assert unknown["action"] == "block" and "unknown recipient" in unknown["reason"], target

        cron = {"action": "create", "schedule": "in 1h", "prompt": "hi", "deliver": f"telegram:{CONTACT}-handle"}
        assert (await guard(tool="cronjob_manage", args=cron, run="guest", recipients=[OWNER]))["allow"] is True
        verdict = await guard(tool="cronjob_manage", args=cron, run="guest", recipients=[OWNER, CONTACT])
        assert verdict["action"] == "ask" and "approval" in verdict["reason"]
        verdict = await guard(tool="cronjob_manage", args=cron, run="guest", recipients=[CONTACT, "p-03"])
        assert verdict["action"] == "block" and "may not be contacted" in verdict["reason"]

        # One contact message queued today spends the contact budget for cron delivery too.
        assert await fx.mind.request_message({"message": "hello there", "entity_id": CONTACT}) is True
        code = fx.store.intentions(status=["asked"], limit=1)[0].ask_code
        assert fx.mind.answer(code, yes=True, contact_id=OWNER, message=f"yes {code}").status == "approved"
        verdict = await guard(tool="cronjob_manage", args=cron, run="guest", recipients=[CONTACT])
        assert verdict["action"] == "block" and "budget" in verdict["reason"]
        verdict = await guard(tool="send_message", args={"target": f"telegram:{CONTACT}-handle"}, run="guest")
        assert verdict["action"] == "block" and "budget" in verdict["reason"]
    finally:
        fx.store.close()


async def test_suggest_level_asks_wait_for_the_digest_but_floor_asks_are_noticed(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fx = Fixture(tmp_path, autonomy="suggest")
    try:
        fx.commitments.create(person_id=OWNER, description="send the owner the notes",
                              due_at=(fx.now + timedelta(minutes=1)).isoformat())
        fx.shift(minutes=10)
        summary = await fx.mind.tick(force=True)
        suggested = fx.store.get(summary["formed"][0]["id"])
        assert suggested.status == "asked" and suggested.context["notice"] is False
        assert summary["ask_notice"] is None and await fx.mind.outbox_ready() == []
        fx.shift(hours=5)
        assert (await fx.mind.tick(force=True))["ask_notice"] is None       # not four hours later either

        fx.commitments.create(person_id=OWNER, description="wire $500 to the vendor",
                              due_at=(fx.now + timedelta(minutes=1)).isoformat())
        fx.shift(minutes=10)
        summary = await fx.mind.tick(force=True)
        floor = fx.store.get(summary["formed"][0]["id"])
        assert floor.cls == "floor" and floor.context["notice"] is True and summary["ask_notice"]
        notice, = await fx.mind.outbox_ready()
        assert floor.ask_code in notice["text"] and suggested.ask_code not in notice["text"]
        digest = fx.mind.outbox.build_digest(since=fx.now - timedelta(days=1), level="suggest")
        assert suggested.ask_code in digest and "Suggested" in digest
    finally:
        fx.store.close()


# ---------------------------------------------------------------------------
# The capture drain: a promise made seconds before a tick is a row before the drives look
# ---------------------------------------------------------------------------

class SlowRouter(FakeRouter):
    """The extractor's router with the endpoint's latency; ``hang`` never answers."""

    def __init__(self, due_at, *, delay=0.3, hang=False, **fields):
        super().__init__(due_at, **fields)
        self.delay, self.hang = delay, hang

    async def complete(self, messages, *, context=None, **_):
        if self.hang:
            await asyncio.Event().wait()
        await asyncio.sleep(self.delay)
        return await super().complete(messages, context=context)


def _pending_jobs(fx):
    import sqlite3
    with sqlite3.connect(fx.state / "turn-idempotency.db") as conn:
        return conn.execute("SELECT status, attempts, next_attempt FROM commitment_runs").fetchall()


async def test_a_forced_tick_drains_pending_capture_before_it_decides(tmp_path, monkeypatch):
    """(a) The capture job of the last turn lands inside the tick, and the row it creates is flipped
    and formed in that same tick; the summary records the drain."""
    import time as _time
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    due = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=45)   # pending at creation
    fx = Fixture(tmp_path, router=SlowRouter(due, delay=0.3), drain=True)
    try:
        fx.turn("turn-d1", OWNER, "I'll get the deck to them in a minute.", "Noted.")
        assert _pending_jobs(fx) == [("pending", 0, 0)]
        fx.shift(minutes=5)
        started = _time.monotonic()
        summary = await fx.mind.tick(force=True)
        elapsed = _time.monotonic() - started
        drained = summary["capture_drained"]
        assert drained["recorded"] == 1 and drained["processed"] == 1 and drained["items"] == 1
        assert drained["pending_left"] == 0 and drained["budget_seconds"] == 30.0
        assert 0.3 <= drained["waited_seconds"] < 2.0 and elapsed < 3.0, (drained, elapsed)
        assert summary["overdue_flipped"] == 1
        assert [item["type"] for item in summary["formed"]] == ["commitment_reminder"]
        assert _pending_jobs(fx) == [("complete", 1, 0)]
        print(f"\ntick latency with one 0.3 s capture job: {elapsed:.3f} s "
              f"(drain waited {drained['waited_seconds']} s)")
    finally:
        fx.store.close()


async def test_a_tick_with_nothing_pending_does_not_wait(tmp_path, monkeypatch):
    """(b) No pending job: the drain costs one query, and the tick stays fast."""
    import time as _time
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fx = Fixture(tmp_path, router=SlowRouter(datetime.now(timezone.utc), delay=5.0), drain=True)
    try:
        await fx.mind.tick(force=True)                       # first tick: retention and the health probes warm up
        started = _time.monotonic()
        summary = await fx.mind.tick(force=True)
        elapsed = _time.monotonic() - started
        assert summary["capture_drained"]["processed"] == 0 and summary["capture_drained"]["pending_left"] == 0
        assert summary["capture_drained"]["waited_seconds"] < 0.05
        assert elapsed < 0.05, elapsed
        print(f"\ntick latency with nothing pending: {elapsed * 1000:.1f} ms")
        # The timer path (a body that has pulled) drains under the shorter budget.
        fx.mind.dispatch()
        timer = await fx.mind.tick()
        assert timer["skipped"] is None and timer["capture_drained"]["budget_seconds"] == 5.0
    finally:
        fx.store.close()


async def test_a_router_that_never_answers_returns_at_the_budget_with_the_job_still_pending(tmp_path, monkeypatch):
    """(c) The drain is bounded: the tick returns at its budget, forms nothing, and the job goes back
    to pending with its attempt uncharged (the worker or the next tick takes it)."""
    import time as _time
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fx = Fixture(tmp_path, router=SlowRouter(datetime.now(timezone.utc), hang=True), drain=True)
    try:
        fx.mind.drain_forced_s = 0.5
        fx.turn("turn-d3", OWNER, "I'll call them back before noon.", "OK.")
        fx.shift(hours=1)
        started = _time.monotonic()
        summary = await fx.mind.tick(force=True)
        elapsed = _time.monotonic() - started
        assert 0.5 <= elapsed < 1.5, elapsed
        drained = summary["capture_drained"]
        assert drained["processed"] == 0 and drained["recorded"] == 0 and drained["pending_left"] == 1
        assert drained["waited_seconds"] >= 0.5 and drained["budget_seconds"] == 0.5
        assert summary["formed"] == [] and fx.commitments.get_pending_for_person(OWNER) == []
        assert _pending_jobs(fx) == [("pending", 0, 0)]
        print(f"\ntick latency with a hung endpoint and a 0.5 s budget: {elapsed:.3f} s")
    finally:
        fx.store.close()


async def test_a_forced_drain_takes_a_job_that_is_backed_off(tmp_path, monkeypatch):
    """(d) A job re-queued 60 s ahead after a transport failure is claimed by the forced drain now."""
    import sqlite3
    import time as _time
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    due = datetime.now(timezone.utc).replace(microsecond=0) + timedelta(seconds=45)
    fx = Fixture(tmp_path, router=SlowRouter(due, delay=0.05), drain=True)
    try:
        fx.turn("turn-d4", OWNER, "Remind me to send the invoice in a minute.", "Will do.")
        with sqlite3.connect(fx.state / "turn-idempotency.db") as conn:
            conn.execute("UPDATE commitment_runs SET attempts=1, next_attempt=?, error='ConnectError'",
                         (_time.time() + 60,))
        fx.shift(minutes=5)
        summary = await fx.mind.tick(force=True)
        assert summary["capture_drained"]["recorded"] == 1 and summary["overdue_flipped"] == 1
        assert [item["type"] for item in summary["formed"]] == ["commitment_reminder"]
        assert _pending_jobs(fx)[0][:2] == ("complete", 1)          # the early attempt is uncharged
    finally:
        fx.store.close()


async def test_without_an_extractor_or_a_router_the_tick_records_no_drain(fx):
    summary = await fx.mind.tick(force=True)
    assert summary["capture_drained"] is None
    fx.drain, fx.router = True, None                          # an extractor with no usable router only looks
    fx.restart()
    fx.turn("turn-d5", OWNER, "I'll do it later.", "OK.")
    summary = await fx.mind.tick(force=True)
    assert summary["capture_drained"]["processed"] == 0 and summary["capture_drained"]["pending_left"] == 1
    assert summary["capture_drained"]["waited_seconds"] < 0.05


# ---------------------------------------------------------------------------
# Invalidation: a deadline pushed out, a hold or a removed row cancels the waiting intention
# ---------------------------------------------------------------------------

async def test_a_deadline_pushed_out_after_approval_invalidates_the_intention(fx):
    """The conversation moved the deadline later after the intention was approved: the body's pull
    offers nothing, and the next tick cancels it with the check's verification, never a dismissal."""
    row = fx.commitments.create(person_id=OWNER, description="send the owner the agenda",
                                due_at=(fx.now + timedelta(minutes=1)).isoformat(), source_type="manual")
    fx.shift(minutes=10)
    summary = await fx.mind.tick(force=True)
    intention = fx.store.get(summary["formed"][0]["id"])
    assert intention.status == "approved" and intention.type == "commitment_overdue"
    before = fx.feedback.multiplier("commitment_overdue:duty")
    # Capture's reschedule: the deadline moves into the future; the store reopens the row.
    moved = fx.commitments.update(row["id"], due_at=(fx.now + timedelta(hours=3)).isoformat())
    assert moved["status"] == "pending"
    summary = await fx.mind.tick(force=True)
    assert summary["invalidated"] == 1 and summary["formed"] == []
    cancelled = fx.store.get(intention.id)
    assert cancelled.status == "cancelled" and cancelled.verified == "check" and cancelled.verdict is None
    assert "no longer due" in cancelled.cancelled_reason
    assert fx.feedback.multiplier("commitment_overdue:duty") == before
    assert fx.mind.dispatch() == []
    # When the new deadline passes, the obligation is raised again as a fresh intention.
    fx.shift(hours=4)
    summary = await fx.mind.tick(force=True)
    assert [item["type"] for item in summary["formed"]] == ["commitment_overdue"]
    fresh = summary["formed"][0]["id"]
    assert fresh != intention.id and [item["id"] for item in fx.mind.dispatch()] == [fresh]
    # Pushed out again between the tick and the body's pull: the pull re-checks before any effect.
    fx.commitments.update(row["id"], due_at=(fx.now + timedelta(hours=3)).isoformat())
    assert fx.mind.dispatch() == [] and fx.store.get(fresh).status == "cancelled"
    assert (await fx.mind.tick(force=True))["invalidated"] == 0          # nothing left waiting


async def test_a_hold_or_a_removed_row_invalidates_too_and_the_outbox_re_checks(fx):
    held = fx.commitments.create(
        person_id=OWNER, description="text the owner the code", due_at=(fx.now + timedelta(minutes=1)).isoformat(),
        metadata={"kind": "deliverable", "content": "The code is 4471.", "channel_hint": "sms"})
    removed = fx.commitments.create(person_id=OWNER, description="send the owner the notes",
                                    due_at=(fx.now + timedelta(minutes=1)).isoformat(), source_type="manual")
    fx.shift(minutes=10)
    summary = await fx.mind.tick(force=True)
    assert {item["type"] for item in summary["formed"]} == {"commitment_deliverable", "commitment_overdue"}
    assert [m["type"] for m in await fx.mind.outbox_ready()] == ["commitment_deliverable"]
    fx.commitments.update(held["id"], clear_due_at=True)                # a hold: open, nothing due
    fx.commitments.resolve(removed["id"], outcome="obsolete", resolved_by="owner", note="gone")
    assert fx.commitments.delete(removed["id"]) is True                 # only a terminal row can be deleted
    # The body's pulls re-check before any effect and settle the rows themselves.
    assert await fx.mind.outbox_ready() == [] and fx.mind.dispatch() == []
    rows = fx.store.intentions(status=["cancelled"], limit=10)
    assert len(rows) == 2 and all(row.verified == "check" and row.verdict is None for row in rows)
    reasons = sorted(row.cancelled_reason for row in rows)
    assert any("no longer has a deadline" in reason for reason in reasons)
    assert any("was removed" in reason for reason in reasons)
    summary = await fx.mind.tick(force=True)
    assert summary["invalidated"] == 0 and summary["formed"] == []       # a held row raises nothing


# ---------------------------------------------------------------------------
# A heads-up before a deadline, and the grace it buys the overdue reminder
# ---------------------------------------------------------------------------

async def test_a_heads_up_goes_out_before_the_deadline_and_holds_the_reminder_for_the_grace(fx):
    due = fx.now + timedelta(minutes=20)
    row = fx.commitments.create(person_id=OWNER, description="file the return", due_at=due.isoformat(),
                                source_type="cognition",
                                metadata={"heads_up_at": (due - timedelta(minutes=10)).isoformat()})
    assert (await fx.mind.tick(force=True))["formed"] == []              # before the asked time
    fx.shift(minutes=12)
    summary = await fx.mind.tick(force=True)
    formed, = summary["formed"]
    assert formed["type"] == "commitment_due_soon" and formed["kind"] == "message"
    ready, = await fx.mind.outbox_ready()
    assert ready["recipient"] == OWNER and "file the return" in ready["text"] and "8 min from now" in ready["text"]
    assert fx.store.get(ready["id"]).dedup_key == schedule_key(row["id"], "heads_up", due)
    fx.mind.outbox.sending(ready["id"], target=TARGET)
    fx.mind.outbox.sent(ready["id"], result="sent")
    assert (await fx.mind.tick(force=True))["formed"] == []              # not twice
    fx.shift(minutes=10)                                                   # 2 min past due
    summary = await fx.mind.tick(force=True)
    assert summary["overdue_flipped"] == 1 and summary["formed"] == []   # held: the heads-up went out 10 min ago
    assert await fx.mind.outbox_ready() == []
    fx.shift(minutes=21)                                                   # 31 min after the heads-up
    summary = await fx.mind.tick(force=True)
    assert [item["type"] for item in summary["formed"]] == ["commitment_reminder"]
    ready, = await fx.mind.outbox_ready()
    assert "file the return" in ready["text"] and "23 min ago" in ready["text"]


async def test_the_grace_is_configured_in_minutes(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fx = Fixture(tmp_path, config={"heads_up_grace_minutes": 5})
    try:
        assert fx.mind.heads_up_grace == timedelta(minutes=5)
        due = fx.now + timedelta(minutes=2)
        fx.commitments.create(person_id=OWNER, description="call the bank", due_at=due.isoformat(),
                              source_type="cognition", metadata={"lead_minutes": 15})
        summary = await fx.mind.tick(force=True)
        assert [item["type"] for item in summary["formed"]] == ["commitment_due_soon"]
        ready, = await fx.mind.outbox_ready()
        fx.mind.outbox.sending(ready["id"], target=TARGET)
        fx.mind.outbox.sent(ready["id"], result="sent")                    # the grace runs from here
        fx.shift(minutes=3)
        assert (await fx.mind.tick(force=True))["formed"] == []          # 1 min overdue, 3 min into the 5 min grace
        fx.shift(minutes=3)
        assert [item["type"] for item in (await fx.mind.tick(force=True))["formed"]] == ["commitment_reminder"]
    finally:
        fx.store.close()


async def test_a_heads_up_not_yet_sent_when_the_deadline_passes_gives_way_to_the_reminder(fx):
    """The body was away: the heads-up is still approved when the row comes due. It is cancelled as
    already due and the reminder forms in the same tick, so exactly one message is ready."""
    due = fx.now + timedelta(minutes=10)
    fx.commitments.create(person_id=OWNER, description="send the slides", due_at=due.isoformat(),
                          source_type="cognition", metadata={"lead_minutes": 5})
    fx.shift(minutes=6)
    summary = await fx.mind.tick(force=True)
    heads_up = fx.store.get(summary["formed"][0]["id"])
    assert heads_up.type == "commitment_due_soon" and heads_up.status == "approved"
    fx.shift(minutes=5)
    summary = await fx.mind.tick(force=True)
    assert summary["invalidated"] == 1 and [item["type"] for item in summary["formed"]] == ["commitment_reminder"]
    assert "already due" in fx.store.get(heads_up.id).cancelled_reason
    ready, = await fx.mind.outbox_ready()                                      # the late heads-up is never sent
    assert ready["type"] == "commitment_reminder"


# ---------------------------------------------------------------------------
# A deadline moved after the word went out: one more reminder and one more heads-up, per schedule
# ---------------------------------------------------------------------------

async def test_a_reminder_already_sent_is_armed_again_when_the_deadline_moves(fx):
    """"Remind me again tomorrow" after the reminder went out is the same row with a new deadline.
    The sent reminder does not block the next schedule: at the new deadline one new reminder
    forms, and only one."""
    row = fx.commitments.create(person_id=OWNER, description="send the owner the agenda",
                                due_at=(fx.now + timedelta(minutes=1)).isoformat(), source_type="cognition")
    fx.shift(minutes=10)
    first, = (await fx.mind.tick(force=True))["formed"]
    assert first["type"] == "commitment_reminder"
    ready, = await fx.mind.outbox_ready()
    fx.mind.outbox.sending(ready["id"], target=TARGET)
    assert fx.mind.outbox.sent(ready["id"], result="sent").status == "sent"
    assert (await fx.mind.tick(force=True))["formed"] == []                # once for this schedule
    moved = fx.commitments.update(row["id"], due_at=(fx.now + timedelta(days=1)).isoformat())
    assert moved["status"] == "pending"
    assert (await fx.mind.tick(force=True))["formed"] == [] and await fx.mind.outbox_ready() == []
    fx.shift(days=1, minutes=5)
    second, = (await fx.mind.tick(force=True))["formed"]
    assert second["type"] == "commitment_reminder" and second["id"] != first["id"]
    assert (await fx.mind.tick(force=True))["formed"] == []                # and once for the new one
    ready, = await fx.mind.outbox_ready()
    assert ready["id"] == second["id"] and "5 min ago" in ready["text"]
    assert fx.store.get(first["id"]).status == "sent"                      # the earlier word stands


async def test_a_heads_up_already_sent_is_armed_again_when_the_deadline_moves(fx):
    """The heads-up went out; the deadline then moves out a day, its lead with it. One new heads-up
    at the new lead time, once; then the reminder for the new deadline after the grace, once."""
    due = fx.now + timedelta(minutes=20)
    row = fx.commitments.create(person_id=OWNER, description="file the return", due_at=due.isoformat(),
                                source_type="cognition", metadata={"lead_minutes": 10})
    fx.shift(minutes=12)                                                    # 8 min before: inside the lead
    first, = (await fx.mind.tick(force=True))["formed"]
    assert first["type"] == "commitment_due_soon"
    ready, = await fx.mind.outbox_ready()
    fx.mind.outbox.sending(ready["id"], target=TARGET)
    fx.mind.outbox.sent(ready["id"], result="sent")
    later = due + timedelta(days=1)
    fx.commitments.update(row["id"], due_at=later.isoformat())
    assert (await fx.mind.tick(force=True))["formed"] == []                # a day from the new lead
    fx.shift(days=1, minutes=-4)                                            # 12 min before the new deadline
    assert (await fx.mind.tick(force=True))["formed"] == []                # the lead is 10 min
    fx.shift(minutes=4)                                                     # 8 min before: inside the lead
    second, = (await fx.mind.tick(force=True))["formed"]
    assert second["type"] == "commitment_due_soon" and second["id"] != first["id"]
    assert (await fx.mind.tick(force=True))["formed"] == []
    ready, = await fx.mind.outbox_ready()
    assert ready["id"] == second["id"] and "8 min from now" in ready["text"]
    fx.mind.outbox.sending(ready["id"], target=TARGET)
    fx.mind.outbox.sent(ready["id"], result="sent")
    fx.shift(minutes=9)                                                     # 1 min past the new deadline
    summary = await fx.mind.tick(force=True)
    assert summary["overdue_flipped"] == 1 and summary["formed"] == []      # held by the grace
    fx.shift(minutes=30)
    assert [item["type"] for item in (await fx.mind.tick(force=True))["formed"]] == ["commitment_reminder"]
    assert (await fx.mind.tick(force=True))["formed"] == []


# ---------------------------------------------------------------------------
# No handle for the recipient: the message waits unclaimed, and the handles are read again
# ---------------------------------------------------------------------------

async def test_a_reminder_with_no_handle_waits_unclaimed_until_one_exists(fx):
    """No handle resolves for the owner. The body's claim names no target and is refused: the
    message stays ready, never claimed and never failed, and its handles are read again at every
    pull, so repairing the contact lets the reminder go out. A failed send is only for a delivery
    the body attempted."""
    fx.contacts.handles[OWNER] = []
    fx.commitments.create(person_id=OWNER, description="send the owner the agenda",
                          due_at=(fx.now + timedelta(minutes=1)).isoformat(), source_type="cognition")
    fx.shift(minutes=10)
    formed, = (await fx.mind.tick(force=True))["formed"]
    assert formed["type"] == "commitment_reminder"
    ready, = await fx.mind.outbox_ready()
    assert ready["recipient_handles"] == []
    assert fx.mind.outbox.sending(ready["id"]) is None                     # no target: not a claim
    fx.shift(minutes=1)                                                     # the body's pulls come later
    app = fx.app()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for _ in range(2):
            claim = await client.post(f"/v1/mind/outbox/{ready['id']}/sending", headers=AUTH,
                                      json={"target": None, "at": fx.now.isoformat()})
            assert claim.status_code == 409 and claim.json()["detail"]["code"] == "no_target"
        row = fx.store.get(ready["id"])
        assert row.status == "approved" and row.outcome is None and row.dedup_key   # still the live intention
        history = [item.action for item in fx.store.get_history(row.id, limit=20)]
        assert history.count("unroutable") == 1 and "sending" not in history       # noted once, not per pull
        # Nothing changed: a later pull offers it again rather than failing it.
        assert [m["id"] for m in (await client.get("/v1/mind/outbox", headers=AUTH)).json()] == [ready["id"]]
        # The owner's handle is repaired: the next pull carries it and the send goes out.
        fx.contacts.handles[OWNER] = [("telegram", f"{OWNER}-handle")]
        again, = (await client.get("/v1/mind/outbox", headers=AUTH)).json()
        assert again["recipient_handles"][0]["address"] == f"{OWNER}-handle"
        assert fx.store.get(ready["id"]).context["recipient_handles"][0]["address"] == f"{OWNER}-handle"
        claim = await client.post(f"/v1/mind/outbox/{ready['id']}/sending", headers=AUTH,
                                  json={"target": TARGET, "at": fx.now.isoformat()})
        assert claim.status_code == 200 and claim.json()["status"] == "sending"
        sent = await client.post(f"/v1/mind/outbox/{ready['id']}/sent", headers=AUTH, json={"result": "sent"})
        assert sent.json()["status"] == "sent"
    assert (await fx.mind.tick(force=True))["formed"] == []                # reported once


# ---------------------------------------------------------------------------
# The two halves together: the extractor's obligor field is what the duty drive reads
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("obligor, expected", [("assistant", "task"), ("owner", "message"), ("p-02", "message"),
                                               (None, "message")])
async def test_the_extractors_obligor_field_decides_between_a_task_and_a_reminder(fx, obligor, expected):
    """The item's top-level ``obligor`` lands in ``metadata.obligor`` (metadata null on the item), and the
    overdue row becomes a board task for the assistant's own promise and an owner message otherwise."""
    fx.turn("turn-1", OWNER, "Can you get me the report?", "Sure, I'll send you the report by 3pm.")
    assert await fx.capture(FakeRouter(fx.now + timedelta(hours=1), obligor=obligor)) is True
    row, = fx.commitments.get_pending_for_person(OWNER)
    assert (row["metadata"] or {}).get("obligor") == obligor
    fx.shift(hours=2)
    formed, = (await fx.mind.tick(force=True))["formed"]
    assert formed["kind"] == expected
    assert (len(fx.mind.dispatch()), len(await fx.mind.outbox_ready())) == ((1, 0) if expected == "task" else (0, 1))
    assert (await fx.mind.tick(force=True))["formed"] == []


# ---------------------------------------------------------------------------
# Schedule keys under pressure: two moves before the first new deadline; a move while re-armed
# ---------------------------------------------------------------------------

async def _sent_reminder(fx, row_due):
    row = fx.commitments.create(person_id=OWNER, description="send the owner the agenda",
                                due_at=row_due.isoformat(), source_type="cognition")
    fx.shift(minutes=10)
    first, = (await fx.mind.tick(force=True))["formed"]
    assert first["type"] == "commitment_reminder"
    ready, = await fx.mind.outbox_ready()
    fx.mind.outbox.sending(ready["id"], target=TARGET)
    assert fx.mind.outbox.sent(ready["id"], result="sent").status == "sent"
    return row, first


async def test_two_moves_before_the_first_new_deadline_earn_one_reminder_at_the_last(fx):
    """"Tomorrow", then "make that the day after" before tomorrow comes: nothing at the abandoned
    deadline, one reminder at the final one, once."""
    row, first = await _sent_reminder(fx, fx.now + timedelta(minutes=1))
    fx.commitments.update(row["id"], due_at=(fx.now + timedelta(days=1)).isoformat())
    fx.commitments.update(row["id"], due_at=(fx.now + timedelta(days=2)).isoformat())
    assert (await fx.mind.tick(force=True))["formed"] == []
    fx.shift(days=1, minutes=5)
    assert (await fx.mind.tick(force=True))["formed"] == [] and await fx.mind.outbox_ready() == []
    fx.shift(days=1)
    second, = (await fx.mind.tick(force=True))["formed"]
    assert second["type"] == "commitment_reminder" and second["id"] != first["id"]
    assert (await fx.mind.tick(force=True))["formed"] == []
    ready, = await fx.mind.outbox_ready()
    assert ready["id"] == second["id"] and "5 min ago" in ready["text"]
    assert len(fx.store.intentions(limit=50)) == 2


async def test_a_move_while_the_re_armed_reminder_waits_unsent_cancels_it_and_arms_the_next(fx):
    """The second reminder formed and sits in the outbox; the owner moves the deadline again. The
    waiting one is cancelled (its key freed) and exactly one forms at the third deadline."""
    row, first = await _sent_reminder(fx, fx.now + timedelta(minutes=1))
    fx.commitments.update(row["id"], due_at=(fx.now + timedelta(days=1)).isoformat())
    fx.shift(days=1, minutes=5)
    second, = (await fx.mind.tick(force=True))["formed"]
    assert second["type"] == "commitment_reminder"
    fx.commitments.update(row["id"], due_at=(fx.now + timedelta(days=1)).isoformat())
    assert await fx.mind.outbox_ready() == []                             # the pull re-checks and cancels
    cancelled = fx.store.get(second["id"])
    assert cancelled.status == "cancelled" and cancelled.dedup_key is None
    assert (await fx.mind.tick(force=True))["formed"] == []
    fx.shift(days=1, minutes=5)
    third, = (await fx.mind.tick(force=True))["formed"]
    assert third["type"] == "commitment_reminder" and third["id"] not in {first["id"], second["id"]}
    assert (await fx.mind.tick(force=True))["formed"] == []
    ready, = await fx.mind.outbox_ready()
    assert ready["id"] == third["id"]
    assert fx.store.get(first["id"]).status == "sent"


# ---------------------------------------------------------------------------
# A reminder nobody could route expires: the open obligation is raised again, not lost for good
# ---------------------------------------------------------------------------

async def test_an_unroutable_reminder_that_expires_is_raised_again_rather_than_lost(fx):
    """No handle for two days: the message expires unsent. The obligation is still open and was
    never reported, so its key is freed and the next tick forms the reminder again; once the handle
    exists it goes out."""
    fx.contacts.handles[OWNER] = []
    fx.commitments.create(person_id=OWNER, description="send the owner the agenda",
                          due_at=(fx.now + timedelta(minutes=1)).isoformat(), source_type="cognition")
    fx.shift(minutes=10)
    first, = (await fx.mind.tick(force=True))["formed"]
    ready, = await fx.mind.outbox_ready()
    assert fx.mind.outbox.sending(ready["id"]) is None                     # no target: not a claim
    fx.shift(hours=49)                                                      # past the message's window
    assert await fx.mind.outbox_ready() == []
    expired = fx.store.get(ready["id"])
    assert expired.status == "expired" and expired.outcome == "expired" and expired.verdict is None
    again, = (await fx.mind.tick(force=True))["formed"]
    assert again["type"] == "commitment_reminder" and again["id"] != first["id"]
    assert (await fx.mind.tick(force=True))["formed"] == []
    fx.contacts.handles[OWNER] = [("telegram", f"{OWNER}-handle")]
    offered, = await fx.mind.outbox_ready()
    assert offered["id"] == again["id"] and offered["recipient_handles"][0]["address"] == f"{OWNER}-handle"
    assert fx.mind.outbox.sending(offered["id"], target=TARGET).status == "sending"
    assert fx.mind.outbox.sent(offered["id"], result="sent").status == "sent"
    assert (await fx.mind.tick(force=True))["formed"] == []                # reported once
