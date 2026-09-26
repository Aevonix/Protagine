"""A turn sent without a session is its own session, derived from its caller and itself.

A voice gateway posts each dispatched task's result to ``/v1/host/turns/sync`` with
``context.session_id: ""`` and no turn ID; the sidecar refused the source (422), so
those results were never captured. The derived session is stable, so retries stay
replays and idempotency is unchanged; two principals never share one; a checkpoint
still needs its caller's session.
"""

from types import SimpleNamespace

from httpx import ASGITransport, AsyncClient
import pytest

from protagine.api.auth import anonymous_authority
from protagine.api.routers import host
from protagine.api.schemas.host import TurnSyncRequest
from protagine.turns import get_turn_idempotency_ledger
from test_turn_source_evidence import source_app  # noqa: F401 (fixture)


def task_observation(request="What is seventeen times twenty-three?", result="391", **context):
    """The voice gateway's ``_observe_task`` body, as it sends it."""
    return {"identity": {"host_id": "hermes"},
            "context": {"session_id": "", "contact_id": "contact-a", "channel_id": "voice", **context},
            "sender": {"platform": "voice", "user_id": "contact-a", "display_name": "Neutral Caller"},
            "user_message": {"role": "user", "content": request},
            "assistant_message": {"role": "assistant", "content": result}}


def sources(tmp_path):
    with get_turn_idempotency_ledger(tmp_path)._connect() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT turn_id, contact_id, session_id, scope FROM turn_sources ORDER BY turn_id")]


@pytest.mark.asyncio
async def test_a_task_observation_without_a_session_is_captured(source_app, tmp_path):
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        response = await client.post("/v1/host/turns/sync", json=task_observation())
    assert response.status_code == 200, response.text
    assert response.json()["accepted"] is True and response.json()["source_recorded"] is True
    source, = sources(tmp_path)
    assert source["contact_id"] == "contact-a" and source["scope"] == "person"
    assert source["session_id"].startswith(host.DERIVED_SESSION_PREFIX)
    # Person-scoped: another channel's session of the same contact recalls the result.
    hits = get_turn_idempotency_ledger(tmp_path).search_sources("seventeen twenty", contact_id="contact-a",
                                                                session_id="an-owner-chat")
    assert [hit["content"] for hit in hits] == ["What is seventeen times twenty-three?"]


@pytest.mark.asyncio
async def test_a_retried_observation_is_one_source_and_another_is_another(source_app, tmp_path):
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        for body in (task_observation(), task_observation(), task_observation(result="392")):
            assert (await client.post("/v1/host/turns/sync", json=body)).status_code == 200
    first, second = sources(tmp_path)
    assert first["session_id"] != second["session_id"]


@pytest.mark.asyncio
async def test_a_keyed_turn_without_a_session_keeps_replay_and_conflict(source_app, tmp_path):
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        put = lambda body: client.put("/v2/host/turns/task-observed-1", json=body)  # noqa: E731
        created = await put(task_observation(turn_id="task-observed-1"))
        replayed = await put(task_observation(turn_id="task-observed-1"))
        changed = await put(task_observation(result="392", turn_id="task-observed-1"))
    assert created.status_code == 201, created.text
    assert replayed.status_code == 200 and replayed.headers["Idempotency-Status"] == "replayed"
    assert changed.status_code == 409 and changed.json()["detail"]["code"] == "turn_id_content_conflict"
    source, = sources(tmp_path)
    assert source["turn_id"] == "task-observed-1" and source["session_id"].startswith(host.DERIVED_SESSION_PREFIX)


def test_the_derived_session_belongs_to_the_principal_and_the_turn():
    def derived(body, request=None):
        turn = TurnSyncRequest.model_validate(body)
        host._derive_missing_session(turn, request)
        return turn.context.session_id

    key = derived(task_observation())
    assert key == derived(task_observation()) and key.startswith(host.DERIVED_SESSION_PREFIX)
    assert derived(task_observation(), SimpleNamespace(state=SimpleNamespace(
        protagine_authority=anonymous_authority()))) != key
    assert derived(task_observation(result="392")) != key
    assert derived(task_observation(channel_id="sms")) != key
    assert derived(task_observation(session_id="call-7")) == "call-7"          # a caller's session is kept
    assert derived(task_observation(session_id="  ")).startswith(host.DERIVED_SESSION_PREFIX)


@pytest.mark.asyncio
async def test_a_checkpoint_still_needs_its_callers_session(source_app, tmp_path):
    body = {"identity": {"host_id": "hermes"},
            "context": {"session_id": "", "contact_id": "contact-a", "turn_id": "checkpoint-1"},
            "checkpoint_messages": [{"role": "user", "content": "A neutral transcript line."}]}
    async with AsyncClient(transport=ASGITransport(app=source_app), base_url="http://test") as client:
        response = await client.post("/v1/host/turns/sync", json=body)
    assert response.status_code >= 400 and not response.json().get("source_recorded")
    assert sources(tmp_path) == []
