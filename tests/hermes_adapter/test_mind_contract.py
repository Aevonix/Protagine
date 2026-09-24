"""Both halves of the loop together: the sidecar's own ``/v1/mind`` over a real Mind, the body on stock kanban.

The other body tests run against ``FakeMind``, the body's reading of the
contract. Here the sidecar's router (``protagine.api.routers.mind``) serves a
real ``protagine.mind.Mind`` over real stores in a temporary state directory,
and the plugin's body, tools and ``/mind`` command run against it in fresh
interpreters with stock ``hermes_cli.kanban_db``. What one half sends is what
the other half parses (docs/HERMES-ADAPTER.md, docs/MIND.md).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import socket
import threading
import time
from types import SimpleNamespace

import pytest
import yaml

from conftest import API_KEY, MIND_PRELUDE, OWNER, build_home, probe, refused
from test_tools_commands import TOOL_CODE

pytest.importorskip("protagine.mind", reason="the sidecar package is not installed here")


class Contacts:
    """The contact store the Mind asks about recipients and their handles."""

    def __init__(self, owner):
        self.records = {owner: {"contact_id": owner, "may_contact": "auto"},
                        "p-03": {"contact_id": "p-03", "may_contact": "ask"}}

    async def get(self, contact_id):
        record = self.records.get(contact_id)
        return SimpleNamespace(to_dict=lambda: record) if record else None

    async def get_handles(self, contact_id):
        if contact_id == "p-03":
            return [SimpleNamespace(gateway="telegram", address="2003", is_primary=True, verified=True)]
        return []

    async def resolve_handle(self, gateway, address):
        return SimpleNamespace(contact_id="p-03") if address == "2003" else None


@pytest.fixture
def real_sidecar(tmp_path, monkeypatch):
    """The real router and Mind on 127.0.0.1, with a clock the test can shift."""
    from fastapi import FastAPI
    import uvicorn
    from protagine.api.middleware import ApiKeyMiddleware
    from protagine.api.routers import mind as mind_router
    from protagine.commitments.store import CommitmentStore
    from protagine.feedback import TypeFeedbackStore
    from protagine.initiatives.store import InitiativeStore
    from protagine.mind import Mind
    from protagine.self_model.expectations import ExpectationEngine, ExpectationStore
    from protagine.turns.idempotency import TurnIdempotencyLedger

    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    state = tmp_path / "sidecar-state"
    state.mkdir()
    clock = {"now": datetime.now(timezone.utc).replace(microsecond=0)}
    store = InitiativeStore(state_dir=state)
    commitments = CommitmentStore(state / "protagine-commitments.db")
    ledger = TurnIdempotencyLedger(state / "turn-idempotency.db")
    mind = Mind(config={"autonomy": "standard", "quiet_hours": ""}, store=store, state_dir=state, owner_id=OWNER,
                commitments=commitments, feedback=TypeFeedbackStore(str(state / "protagine-feedback.db")),
                expectations=ExpectationEngine(ExpectationStore(str(state / "protagine-expectations.db"))),
                contacts=Contacts(OWNER), ledger=ledger, clock=lambda: clock["now"], backups=False,
                persist=lambda changes: None)
    mind.digest_hour = 25
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware, api_key=API_KEY)
    app.include_router(mind_router.router)

    @app.get("/v1/host/health")
    async def health():
        return {"status": "ok", "capabilities": ["memory", "mind"]}

    @app.get("/v1/host/contacts/resolve")
    async def resolve(gateway: str = "", address: str = ""):
        from fastapi import HTTPException
        if gateway == "telegram" and address == "1001":
            return {"contact_id": OWNER, "display_name": "Owner", "may_contact": "auto"}
        if gateway == "telegram" and address == "2003":
            return {"contact_id": "p-03", "display_name": "Friend", "may_contact": "ask"}
        raise HTTPException(status_code=404, detail="No contact for that handle")

    mind_router.set_mind(mind)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="off", access_log=False,
                                           log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    started = time.monotonic()
    while not server.started:
        assert thread.is_alive() and time.monotonic() - started < 10, "the sidecar did not start"
        time.sleep(0.02)

    def shift(**delta):
        clock["now"] += timedelta(**delta)

    try:
        yield SimpleNamespace(port=port, url=f"http://127.0.0.1:{port}", mind=mind, store=store,
                              commitments=commitments, ledger=ledger, clock=clock, shift=shift)
    finally:
        server.should_exit = True
        thread.join(10)
        mind_router.set_mind(None)
        store.close()


@pytest.fixture
def real_home(tmp_path, real_sidecar):
    return build_home(tmp_path, real_sidecar.port, real_sidecar.url)


def overdue(real, description, *, hours=1, person=OWNER):
    """A commitment due in a minute; the mind's clock then moves past it."""
    due = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
    row = real.commitments.create(person_id=person, description=description, due_at=due)
    real.shift(hours=hours)
    return row


# The parked body, the kanban helpers, the tools and the command; ``tick()`` here is the
# plugin's own: ``POST /v1/mind/tick`` then one body pass.
PRELUDE = MIND_PRELUDE + TOOL_CODE + "def tick():\n    return protagine_hermes.tick()\n"
TICK = "result = tick()\n"


def test_commitment_becomes_one_task_whose_outcome_comes_back(real_home, real_sidecar):
    """Build plan M2, end to end across the seam: one task, no duplicate after a restart, the outcome recorded."""
    real = real_sidecar
    overdue(real, "send the owner the report")
    first = probe(TICK + "emit(tick=result, tasks=tasks(), state=body.client.mind_state())",
                  real_home, prelude=PRELUDE)
    formed = first["tick"]["mind_tick"]["formed"]
    assert [item["type"] for item in formed] == ["commitment_overdue"] and formed[0]["decision"] == "act"
    assert first["tick"]["mind"] is True and first["tick"]["dispatched"] == 1
    intention_id = formed[0]["id"]
    task, = first["tasks"].values()
    assert list(first["tasks"]) == [f"mind:{intention_id}"]
    assert task["assignee"] == "protagine-act" and task["status"] == "ready" and task["created_by"] == "protagine"
    assert "Report what you did" in task["body"] and "duty drive" in task["body"]
    row = real.store.get(intention_id)
    assert row.status == "dispatched" and row.hermes_ref == task["id"] and row.hermes_kind == "kanban"
    assert first["state"]["enabled"] is True and first["state"]["dispatched"] == 1 and first["state"]["asks"] == []

    # A restart: the sidecar never re-offers a dispatched intention, the tick forms nothing new.
    second = probe(TICK + "emit(tick=result, tasks=tasks())", real_home, prelude=PRELUDE)
    assert second["tick"]["mind_tick"]["formed"] == [] and second["tick"]["dispatched"] == 0
    assert list(second["tasks"]) == [f"mind:{intention_id}"]

    # The worker finishes; reconciliation posts the body's report once and the sidecar settles the row.
    third = probe('''
conn = connect()
kb.claim_task(conn, tasks()["mind:%s"]["id"], claimer="worker-a")
kb.complete_task(conn, tasks()["mind:%s"]["id"], summary="Sent the report. Evidence: message id 7. It worked.")
conn.close()
after = tick()
again = tick()
emit(after=after, again=again, why=body.client.get("/v1/mind/why/%s").json())
''' % (intention_id, intention_id, intention_id), real_home, prelude=PRELUDE)
    assert third["after"]["reconciled"] == 1 and third["again"]["reconciled"] == 0
    row = real.store.get(intention_id)
    assert row.status == "done" and row.outcome == "done" and row.result.startswith("Sent the report")
    # Nothing outside the intention verified it (the worker cannot mark the row fulfilled itself), so
    # the outcome is unverified and the body's report is what closes the commitment.
    assert row.verified == "none" and "check" not in row.result_metadata
    assert row.result_metadata["run"]["outcome"] == "completed"
    commitment, = real.commitments.list(person_id=OWNER, status=["fulfilled"])["commitments"]
    assert commitment["metadata"]["resolution"]["by"] == "body" and intention_id in commitment["metadata"]["resolution"]["note"]
    why = third["why"]
    assert why["drive"] == "duty" and why["decision"] == "act" and why["hermes_ref"] == task["id"]
    assert why["outcome"] == "done" and why["verified"] == "none" and why["evidence"]
    assert any(item["action"] == "outcome_done" for item in why["history"])
    # The autobiography entry a later session recalls (architecture 4.1).
    references = real.ledger.source_references([f"mind:{intention_id}:outcome_done"], contact_id=OWNER,
                                               session_id="mind")
    assert references, "no autobiography entry in the ledger"


def test_ask_path_across_the_seam(real_home, real_sidecar):
    """A floor match becomes an ask: one notice with the code, no task; the owner's typed code approves it."""
    real = real_sidecar
    overdue(real, "wire $500 to the vendor")
    first = probe('''
_smt.send_message_tool = fake_send
result = tick()
emit(tick=result, tasks=tasks(), sends=SENDS, asks=command("asks"), status=command("status"))
''', real_home, prelude=PRELUDE)
    asked = [row for row in real.store.intentions(status=["asked"], limit=10)]
    assert len(asked) == 1 and asked[0].ask_code and first["tasks"] == {}
    code = asked[0].ask_code
    notice, = first["sends"]
    assert notice["target"] == "telegram:1001" and f"[{code}]" in notice["message"]
    assert "yes <code>" in notice["message"]
    assert code in first["asks"] and "asks:" in first["status"]
    assert first["tick"]["sent"] == 1

    # Approval in chat: a guest, an owner turn without the code and a worker are refused by the plugin;
    # the sidecar checks the owner and the code again on its side.
    second = probe(TOOL_CODE + '''
g = guest(message="yes %(code)s")
o_without = owner("owner-1", message="please go ahead")
o_with = owner("owner-2", message="yes %(code)s, do it")
emit(guest=call("protagine_self", {"operation": "yes", "code": "%(code)s"}, g),
     without=call("protagine_self", {"operation": "yes", "code": "%(code)s"}, o_without),
     approved=call("protagine_self", {"operation": "yes", "code": "%(code)s"}, o_with),
     repeat=call("protagine_self", {"operation": "yes", "code": "%(code)s"}, o_with))
''' % {"code": code}, real_home, prelude=PRELUDE)
    assert "owner" in refused(second["guest"]) and "own message" in refused(second["without"])
    assert second["approved"]["ok"] is True and second["approved"]["status"] == "approved"
    assert second["approved"]["id"] == asked[0].id
    assert "no ask is open" in refused(second["repeat"])
    third = probe(TICK + "emit(tick=result, tasks=tasks())", real_home, prelude=PRELUDE)
    assert list(third["tasks"]) == [f"mind:{asked[0].id}"] and third["tick"]["dispatched"] == 1
    assert real.store.get(asked[0].id).status == "dispatched"

    # Silence: a second ask expires after 72 h while unrelated duty work proceeds.
    overdue(real, "wire $900 to the landlord")
    overdue(real, "send the owner the slides")
    probe(TICK + "emit(tick=result)", real_home, prelude=PRELUDE)
    pending = [row for row in real.store.intentions(status=["asked"], limit=10)]
    assert len(pending) == 1 and "landlord" in pending[0].description
    assert any(row.status == "dispatched" and "slides" in row.description
               for row in real.store.intentions(kind=["task"], limit=20))
    real.shift(hours=73)
    fourth = probe(TICK + "emit(tick=result, tasks=tasks())", real_home, prelude=PRELUDE)
    assert fourth["tick"]["mind_tick"]["expired_asks"] == 1
    assert real.store.get(pending[0].id).status == "expired"
    assert f"mind:{pending[0].id}" not in fourth["tasks"]


def test_off_switch_across_the_seam(real_home, real_sidecar):
    """``/mind off`` in chat: the sidecar stops offering work and the body archives what was not started."""
    real = real_sidecar
    overdue(real, "send the owner the report")
    overdue(real, "send the owner the minutes")
    first = probe(TICK + "emit(tick=result, tasks=tasks())", real_home, prelude=PRELUDE)
    assert first["tick"]["dispatched"] == 2
    real.mind.outbox.notice(type="stale_task", title="n", text="late", dedup_key="late")
    second = probe('''
_smt.send_message_tool = fake_send
off = command("off")
after = tick()
emit(off=off, after=after, tasks=tasks(), sends=SENDS, state=body.client.mind_state())
''', real_home, prelude=PRELUDE)
    assert second["off"].startswith("Mind off.") and "sidecar: off" in second["off"]
    assert second["state"]["enabled"] is False and second["sends"] == []
    assert {task["status"] for task in second["tasks"].values()} == {"archived"}
    assert real.mind.enabled is False and real.store.intentions(status=["approved"], kind=["message"], limit=5) == []
    real.mind.on()
    overdue(real, "send the owner the agenda")
    third = probe(TICK + "emit(tick=result, tasks=tasks())", real_home, prelude=PRELUDE)
    assert third["tick"]["dispatched"] == 1
    assert sum(task["status"] == "ready" for task in third["tasks"].values()) == 1


def test_observations_and_the_owner_handle_shape_init_writes(real_home, real_sidecar):
    """Board observations reach the mind in the body's shape; ``identity.yaml`` handles as ``init`` writes them."""
    real = real_sidecar
    identity = yaml.safe_load((real_home.instance / "identity.yaml").read_text())
    identity["owner"]["handles"] = [{"platform": "telegram", "id": "1001"}]
    (real_home.instance / "identity.yaml").write_text(yaml.safe_dump(identity))
    first = probe('''
conn = connect()
old = kb.create_task(conn, title="owner task from long ago", assignee="default", idempotency_key="owner:old")
conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (int(time.time()) - 80 * 3600, old))
conn.commit()
conn.close()
body._last_observation = None
result = tick()
emit(tick=result, observed=body.client.get("/v1/mind/state").json()["body"])
''', real_home, prelude=PRELUDE)
    assert first["tick"]["observed"] is True and first["observed"]["ticks"] >= 1
    assert [item["id"] for item in real.mind.observations["stale_task"]] and \
        real.mind.observations["stale_task"][0]["age_hours"] >= 80
    second = probe('''
_smt.send_message_tool = fake_send
result = tick()
emit(tick=result, sends=SENDS)
''', real_home, prelude=PRELUDE)
    assert [item["type"] for item in second["tick"]["mind_tick"]["formed"]] == ["stale_task"]
    notice, = second["sends"]
    assert notice["target"] == "telegram:1001" and "owner task from long ago" in notice["message"]
