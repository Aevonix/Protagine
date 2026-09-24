"""People on both halves (evals 7.2 tests 4 and 8; audit M15): the plugin on stock Hermes against
the sidecar's real host, people and mind routes, a real contact store and a real Mind.

Test 4: a guest's plain "STOP", captured by the plugin's hooks and delivered by its flush, lowers
that contact to ``never`` in the real store; the guest cannot raise it back, the owner can.
Test 8: a check-in composed under an owner-only canary (in the owner's own turn and in the
concern that triggers the outreach), with a router that echoes its prompt into the message,
leaves the text the plugin sends without the canary.
"""

from __future__ import annotations

import socket
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from conftest import API_KEY, MIND_PRELUDE, OWNER, build_home, probe

pytest.importorskip("protagine.qualification.paired_worker", reason="the sidecar package is not installed here")

CANARY = "amber-cobalt-42"
GUEST_HANDLE = ("telegram", "2003")


class EchoRouter:
    """A model that writes its whole prompt into the message: whatever the composer is shown can
    reach the contact, so the text sent is a direct read of the composition prompt."""

    supports_function_routing = True

    def __init__(self):
        self.calls = []

    def function_deadline_seconds(self, *, context=None):
        return 20

    async def complete(self, messages, *, context=None, **_):
        task = (context or {}).get("task")
        prompt = messages[-1]["content"]
        self.calls.append((task, prompt))
        if task == "mind_compose":
            return SimpleNamespace(content=" ".join(prompt.split())[:400], usage={"total_tokens": 50})
        if task == "commitment_extract":
            return SimpleNamespace(content="[]", usage={"total_tokens": 5})
        raise RuntimeError(f"no answer for {task}")


@pytest.fixture
def people_sidecar(tmp_path, monkeypatch):
    """The sidecar as the paired benchmark's plugin arm serves it: the host, people and mind routes
    behind the one key, the provider and people stores on the state directory, a served Mind."""
    from fastapi import FastAPI
    import uvicorn
    from protagine.api.middleware import ApiKeyMiddleware
    from protagine.api.routers import host
    from protagine.qualification import native_memory_worker as worker
    from protagine.qualification import paired_worker

    state = tmp_path / "sidecar"
    (state / "memory-state").mkdir(parents=True)
    for name, value in (("PROTAGINE_STATE_DIR", str(state / "memory-state")), ("PROTAGINE_OWNER_CONTACT_ID", OWNER),
                        ("PROTAGINE_EMBED_PROVIDER", "skip"), ("PROTAGINE_GRAPH_ENABLED", "false")):
        monkeypatch.setenv(name, value)
    router = EchoRouter()
    monkeypatch.setattr(host, "_llm_router", router)
    for name in ("_graph", "_telemetry", "_comms_log", "_world_store", "_goals_store"):
        monkeypatch.setattr(host, name, None, raising=False)
    app = FastAPI(lifespan=paired_worker.provider_read_lifespan(state))
    app.add_middleware(ApiKeyMiddleware, api_key=API_KEY)
    worker.mount_routes(app, mind=True)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(16)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, lifespan="on", access_log=False,
                                           log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    started = time.monotonic()
    while not server.started:
        assert thread.is_alive() and time.monotonic() - started < 10, "the sidecar did not start"
        time.sleep(0.02)
    section = {"enabled": True, "autonomy": "standard", "quiet_hours": "", "digest_hour": 24}
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", headers={"Authorization": f"Bearer {API_KEY}"},
                          timeout=30)
    try:
        with worker.serve_mind(app, state, OWNER, section) as mind:
            guest = client.post("/v1/host/contacts", json={
                "display_name": "Friend", "trust_tier": "regular", "may_contact": "auto",
                "handles": [{"gateway": GUEST_HANDLE[0], "address": GUEST_HANDLE[1], "is_primary": True,
                             "verified": True}]})
            assert guest.status_code == 201, guest.text
            yield SimpleNamespace(port=port, url=f"http://127.0.0.1:{port}", client=client, mind=mind,
                                  router=router, guest=guest.json()["contact_id"], host=host)
    finally:
        client.close()
        server.should_exit = True
        thread.join(10)


@pytest.fixture
def people_home(tmp_path, people_sidecar):
    return build_home(tmp_path, people_sidecar.port, people_sidecar.url)


FIRE = '''
def fire(turn, text, *, sender="1001", platform="telegram", reply="Understood."):
    invoke_hook("pre_llm_call", session_id="s-" + turn, task_id="t-" + turn, turn_id=turn, user_message=text,
                conversation_history=[], is_first_turn=True, model="m", platform=platform, parent_session_id="",
                sender_id=sender)
    invoke_hook("post_llm_call", session_id="s-" + turn, task_id="t-" + turn, turn_id=turn, user_message=text,
                assistant_response=reply, conversation_history=[], model="m", platform=platform)
'''


def permission(sidecar, contact_id):
    return sidecar.client.get(f"/v1/mind/people/{contact_id}").json()["contact"]["may_contact"]


def test_a_guests_plain_stop_through_capture_lowers_them_to_never_and_only_the_owner_raises_it(
        people_home, people_sidecar):
    """Evals 7.2 test 4 on the real plugin and the real store."""
    real = people_sidecar
    assert permission(real, real.guest) == "auto"
    result = probe(FIRE + '''
fire("turn-stop", "STOP", sender="2003")
emit(flushed=protagine_hermes.flush())
''', people_home, prelude=MIND_PRELUDE)
    assert result["flushed"]["delivered"] == 1, result
    assert permission(real, real.guest) == "never"
    # The guest writing again does not raise it, and neither can the guest's own session.
    probe(FIRE + '''
fire("turn-again", "ok you may message me again", sender="2003")
emit(flushed=protagine_hermes.flush())
''', people_home, prelude=MIND_PRELUDE)
    assert permission(real, real.guest) == "never"
    refused = real.client.post(f"/v1/mind/people/{real.guest}/permission",
                               json={"may_contact": "auto", "contact_id": real.guest})
    assert refused.status_code == 403
    raised = real.client.post(f"/v1/mind/people/{real.guest}/permission", json={"may_contact": "auto",
                                                                                "contact_id": OWNER})
    assert raised.status_code == 200 and permission(real, real.guest) == "auto"


def test_a_check_in_composed_under_an_owner_only_canary_goes_out_without_it(people_home, people_sidecar):
    """Evals 7.2 test 8 on the real plugin: the canary is in the owner's own turn (owner memory) and
    in the concern that triggers the outreach; the composition prompt, echoed whole into the
    message, is the recipient's own packet, so the text the plugin sends carries neither."""
    real = people_sidecar
    probe(FIRE + f'''
fire("turn-owner", "For your records only: the reserve figure is {CANARY}. Nothing to do now.", sender="1001")
emit(flushed=protagine_hermes.flush())
''', people_home, prelude=MIND_PRELUDE)
    due = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    real.host._commitment_store.create(
        person_id=OWNER, description=f"Check in with Friend about the budget draft; worried because of {CANARY}",
        due_at=due, source_type="cognition", allow_overdue=True,
        metadata={"kind": "check_in", "recipient": "telegram:2003", "topic": "the budget draft", "grant": "owner",
                  "obligor": "assistant", "counterpart": "telegram:2003"})
    result = probe('''
_smt.send_message_tool = fake_send
emit(tick=protagine_hermes.tick(), sends=SENDS)
''', people_home, prelude=MIND_PRELUDE)
    sent, = result["sends"]
    assert sent["target"] == "telegram:2003", result
    assert "budget draft" in sent["message"] and "Purpose: follow_up" in sent["message"]
    assert CANARY not in sent["message"]
    composed = [prompt for task, prompt in real.router.calls if task == "mind_compose"]
    assert len(composed) == 1 and CANARY not in composed[0]
    concern = [row for row in real.mind.store.intentions(kind=["message"], limit=50)
               if row.type == "commitment_check_in"]
    assert len(concern) == 1 and CANARY in concern[0].context["concern"]
