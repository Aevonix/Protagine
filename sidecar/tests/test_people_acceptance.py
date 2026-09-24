"""M5 acceptance tests that need the whole path (build plan M5; evals 7.2 and 7.3).

The unit-level acceptance tests live beside their code: C1 in ``test_people_store``, C2 in
``test_people_store`` and ``test_people_router``, C3 and the backoff in ``test_mind_social``,
the permission rule in ``test_people_router`` and ``test_optout``, the canary in
``test_mind_compose``. Here: a group chat of ten unknown members through the real sender
resolution and one real tick (audit m9), and invariant episode 2 on the real stores (audit m10).
"""

from __future__ import annotations

from datetime import timedelta
import importlib.util
from types import SimpleNamespace

import pytest

from protagine.commitments.store import CommitmentStore
from protagine.feedback import TypeFeedbackStore
from protagine.initiatives.store import InitiativeStore
from protagine.mind import Mind
from protagine.qualification import native_memory_worker as worker
from protagine.turns import TurnIdempotencyLedger

# These drive the paired worker in-process, and the worker runs inside Hermes. The sidecar's own test run has
# no Hermes and skips them; CI runs them in a second step with stock Hermes installed.
needs_hermes = pytest.mark.skipif(importlib.util.find_spec("hermes_time") is None,
                                  reason="needs stock Hermes in the test interpreter")


@pytest.fixture
async def arm(tmp_path, monkeypatch):
    """The host routes over a real contact store and ledger, and a Mind on the same stores."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from protagine.api.routers import host
    from protagine.contacts.config import ContactsConfig
    from protagine.contacts.store import SQLiteContactStore
    from protagine.qualification import paired_body
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    store = SQLiteContactStore(ContactsConfig(sqlite_path=str(tmp_path / "protagine-contacts.db")))
    await store.connect()
    owner = (await store.create(display_name="Owner", trust_tier="inner_circle", may_contact="auto")).contact_id
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", owner)
    commitments = CommitmentStore(tmp_path / "protagine-commitments.db")
    for name, value in (("_contacts_store", store), ("_commitment_store", commitments), ("_comms_log", None),
                        ("_graph", None), ("_telemetry", None)):
        monkeypatch.setattr(host, name, value, raising=False)
    app = FastAPI()
    app.include_router(host.router)
    directory = tmp_path / "mind"
    directory.mkdir()
    initiatives = InitiativeStore(state_dir=directory)
    paired_body.install_clock(0)
    try:
        mind = Mind(config={"autonomy": "standard", "quiet_hours": "", "digest_hour": 24}, store=initiatives,
                    state_dir=directory, owner_id=owner, commitments=commitments,
                    feedback=TypeFeedbackStore(str(directory / "protagine-feedback.db")), contacts=store,
                    ledger=TurnIdempotencyLedger(directory / "ledger.db"), clock=worker.mind_clock, backups=False)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            async def sync(turn_id, text, *, sender=None, contact_id="unresolved-sender", session="group-1"):
                body = {"identity": {"host_id": "hermes"},
                        "context": {"session_id": session, "contact_id": contact_id, "turn_id": turn_id},
                        "user_message": {"role": "user", "content": text},
                        "assistant_message": {"role": "assistant", "content": "Thanks."}}
                if sender:
                    body["sender"] = sender
                response = await client.post("/v1/host/turns/sync", json=body)
                assert response.status_code == 200, response.text
                return response.json()
            yield SimpleNamespace(store=store, mind=mind, sync=sync, commitments=commitments, owner=owner,
                                  clock=paired_body, initiatives=initiatives)
    finally:
        paired_body.uninstall_clock()
        initiatives.close()
        await store.close()


@needs_hermes
async def test_a_group_chat_of_ten_unknown_members_raises_no_check_in_and_no_ask(arm):
    """Build plan M5: ten strangers writing in one group become ten contacts, none of whom the
    social drive may consider, so a real tick forms nothing: no check-in and no owner ask."""
    for n in range(10):
        await arm.sync(f"group-turn-{n}", f"Hello all, member {n} here; the plans look fine.",
                       sender={"platform": "whatsapp", "user_id": f"member-{n}@lid", "display_name": f"Member {n}",
                               "group_id": "family-group"})
    members = [c for c in await arm.store.list(limit=100) if c.contact_id != arm.owner]
    assert len(members) == 10 and len({c.contact_id for c in members}) == 10
    assert all(c.may_contact == "ask" and c.trust_tier not in {"regular", "trusted", "inner_circle"} for c in members)
    assert await arm.store.social_candidates() == []
    for advance in (0, 3 * 86400):
        arm.clock.advance_clock(advance)
        summary = await arm.mind.tick(force=True)
        assert summary["formed"] == [], summary["formed"]
    assert arm.initiatives.intentions(limit=100) == [] and arm.mind.asks() == []


@needs_hermes
async def test_invariant_episode_two_a_never_contact_under_every_reason_gets_nothing(arm):
    """Evals 7.3 episode 2 on the real stores: a ``never`` contact with an owner cadence, an owner
    grant to message them and their own open item gets no message over a day of ticks; the owner
    hears once that the grant cannot override ``never``."""
    blocked = await arm.store.create(display_name="p-07", trust_tier="trusted", may_contact="auto",
                                     cadence_minutes=10)
    await arm.store.add_handle(blocked.contact_id, "capture", "p-07", is_primary=True, verified=True)
    await arm.store.lower_may_contact(blocked.contact_id, reason="STOP", source_ref="turn:t-0")
    now = worker.mind_clock()
    arm.commitments.create(person_id=arm.owner, description="Tell p-07 the venue moved",
                           due_at=(now + timedelta(minutes=5)).isoformat(), source_type="cognition",
                           metadata={"kind": "notice", "recipient": "p-07", "content": "The venue moved.",
                                     "grant": "owner", "obligor": "assistant", "counterpart": "p-07"})
    arm.commitments.create(person_id=blocked.contact_id, description="p-07 sends the signed form",
                           due_at=(now + timedelta(minutes=5)).isoformat(), source_type="cognition",
                           metadata={"obligor": "p-07", "counterpart": "owner"})
    for _ in range(12):
        arm.clock.advance_clock(2 * 3600)
        await arm.mind.tick(force=True)
    to_blocked = [row for row in arm.initiatives.intentions(kind=["message"], limit=500)
                  if row.entity_id == blocked.contact_id]
    assert all(row.status == "dropped" for row in to_blocked)
    assert [p for p in await arm.mind.outbox_ready() if p["recipient"] == blocked.contact_id] == []
    refused = [row for row in arm.initiatives.intentions(kind=["message"], limit=500) if row.type == "grant_refused"]
    assert len(refused) == 1 and refused[0].entity_id == arm.owner
