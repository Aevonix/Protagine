"""The ``protagine-stances`` context section: every viewer gets the views meant for them, and no others."""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient
import pytest

from protagine.api.middleware import ApiKeyMiddleware
from protagine.api.routers import mind as mind_router
from protagine.self_model.judgments import Proposal
from onekey import AUTH, KEY
from test_mind_loop import OWNER, Fixture
from test_turn_source_evidence import source_app  # noqa: F401  (pytest fixture)

GUEST = "p-02"


@pytest.fixture
def minded(source_app, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    fx = Fixture(tmp_path, config={"faculties": {"opinions": True}})
    store = fx.mind.opinions.store
    fx.ledger.record_source("view-1", contact_id=OWNER, session_id="s-1", derive_claims=False, messages=[
        {"role": "user", "content": "Which garden plan should we use?"},
        {"role": "assistant", "content": "Raised beds suit this garden; the soil drains poorly."}])
    private = store.form(Proposal(subject_kind="topic", subject="", topic="garden plan",
                                  stance="Raised beds suit this garden.", reason="The soil drains poorly.",
                                  certainty="moderate", revise_if="A soil test that drains well.",
                                  premises=store.statements("view-1"), source_ref="turn:view-1")).stance_id
    shared = store.form(Proposal(subject_kind="topic", subject="", topic="garden soil", stance="The soil is clay.",
                                 reason="The soil survey found clay.", certainty="strong", revise_if="",
                                 premises=[store.outcome_premise({"id": "i-09", "outcome": "done",
                                                                  "result": "clay under the plot", "verified": "check",
                                                                  "completed_at": "2026-09-01"})],
                                 source_ref="intention:i-09")).stance_id
    source_app.add_middleware(ApiKeyMiddleware, api_key=KEY)
    mind_router.set_mind(fx.mind)
    try:
        yield source_app, fx, private, shared
    finally:
        mind_router.set_mind(None)
        fx.store.close()


async def sections(app, contact, query):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/v1/host/context/assemble", headers=AUTH, json={
            "identity": {"host_id": "fixture"}, "context": {"contact_id": contact, "session_id": f"later-{contact}"},
            "incoming_message": {"role": "user", "content": query}})
    assert response.status_code == 200, response.text
    return {section["id"]: section for section in response.json()["sections"]}


async def test_the_owner_sees_every_relevant_view_and_a_guest_only_the_shared_one(minded):
    app, fx, private, shared = minded
    query = "Tell me about the garden plan and the garden soil."
    owner = await sections(app, OWNER, query)
    stance = owner["protagine-stances"]
    assert stance["title"] == "Your recorded views" and stance["priority"] == 87
    assert f"[opinion {private}]" in stance["body"] and f"[opinion {shared}]" in stance["body"]
    assert "Change a recorded view only on new evidence" in stance["body"]
    guest = await sections(app, GUEST, query)
    assert f"[opinion {shared}]" in guest["protagine-stances"]["body"]
    assert "Raised beds" not in "\n".join(section["body"] for section in guest.values())


async def test_no_section_when_nothing_is_relevant_off_or_the_mind_is_off(minded):
    app, fx, private, shared = minded
    assert "protagine-stances" not in await sections(app, GUEST, "What time is it in Lisbon?")
    cue = await sections(app, OWNER, "Should we repaint the shed?")
    assert cue["protagine-stances"]["body"].startswith("When you give a recommendation or judgment")
    fx.mind.off(reason="test")
    assert "protagine-stances" not in await sections(app, OWNER, "Tell me about the garden plan.")
    fx.mind.on()
    fx.mind.opinions.enabled = False
    assert "protagine-stances" not in await sections(app, OWNER, "Tell me about the garden plan.")


async def test_a_recipient_packet_carries_no_owner_view_or_feeling_about_them(minded):
    """Integration map X6: ``assemble_packet(cid)``, what the mind composes a message to a contact from, is
    that contact's own view: the everyone-audience stance, never an owner-audience opinion about them, and
    no affect text (a failed owner task's topic). Every section id in any view is unique."""
    from protagine.api.routers import host
    app, fx, private, shared = minded
    store = fx.mind.opinions.store
    fx.ledger.record_source("view-2", contact_id=OWNER, session_id="s-2", derive_claims=False, messages=[
        {"role": "user", "content": "p-02 keeps missing the garden deadlines, code amber-cobalt-42."},
        {"role": "assistant", "content": "Noted: the garden deadlines slip with p-02."}])
    about = store.form(Proposal(subject_kind="person", subject=GUEST, topic="garden deadlines",
                                stance="p-02 misses garden deadlines (amber-cobalt-42).",
                                reason="The owner reported missed garden deadlines.", certainty="moderate",
                                revise_if="Two garden deadlines met.", premises=store.statements("view-2"),
                                source_ref="turn:view-2")).stance_id
    assert about is not None
    failed, _ = fx.store.create_intention(kind="task", type="research", title="garden deadline violet-canary-9",
                                          drive="duty", cls="internal", decision="act", decision_reason="x",
                                          status="approved", dedup_key="canary-task",
                                          context={"topic": "garden deadline violet-canary-9"})
    fx.store.transition(failed.id, "failed", action="x", outcome="failed", failed_at=fx.now,
                        failed_reason="violet-canary-9 stalled")
    fx.mind.feelings.update(fx.now)
    query = "garden deadlines, the garden plan and the garden soil"
    owner = await sections(app, OWNER, query)
    assert f"[opinion {about}]" in owner["protagine-stances"]["body"]
    packet = await host.assemble_packet(GUEST, query=query, limit_chars=20000)
    assert f"[opinion {shared}]" in packet
    assert "amber-cobalt-42" not in packet and f"[opinion {about}]" not in packet
    assert "violet-canary-9" not in packet and "Raised beds" not in packet
    for viewer in (OWNER, GUEST):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            response = await client.post("/v1/host/context/assemble", headers=AUTH, json={
                "identity": {"host_id": "fixture"}, "context": {"contact_id": viewer, "session_id": f"u-{viewer}"},
                "incoming_message": {"role": "user", "content": query}})
        ids = [section["id"] for section in response.json()["sections"]]
        assert len(ids) == len(set(ids)), ids
