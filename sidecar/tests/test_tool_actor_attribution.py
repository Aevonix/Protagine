"""Mutating tool handlers record who actually invoked them.

An in-process caller (the autonomy loop reasoning on its own) is the agent.
A call that arrives through ``ToolExecutor.execute_batch`` with an actor
policy is attributed to that actor; mutation authority is only ever granted
to the owner, so a mutation handler reached under a policy is the owner's.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from protagine.api.routers import host as host_mod
from protagine.reasoning.executor import ToolExecutor
from protagine.reasoning.tool_policy import ToolActorPolicy
from protagine.tools.handlers import handle_create_project, handle_merge_contacts


class _Contacts:
    def __init__(self):
        self.merges = []

    async def merge_contacts(self, keep_id, merge_id, performed_by="owner"):
        self.merges.append((keep_id, merge_id, performed_by))
        return SimpleNamespace(interaction_count=3)


class _Projects:
    def __init__(self):
        self.created = []

    def create_project(self, objective, *, title="", source="owner",
                       entity_ids=None):
        self.created.append((objective, title, source))
        return SimpleNamespace(id="proj-1", title=title or objective), "ok"


@pytest.fixture
def contacts(monkeypatch):
    store = _Contacts()
    monkeypatch.setattr(host_mod, "_contacts_store", store)
    return store


OWNER = ToolActorPolicy(
    principal_id="key-1", viewer_person_id="owner",
    allow_private_read=True, allow_mutation=True,
)


async def test_direct_handler_calls_are_attributed_to_the_agent(contacts):
    projects = _Projects()
    registry = SimpleNamespace(project_engine=projects)

    out = json.loads(await handle_merge_contacts(
        {"keep": "cid-a", "merge": "cid-b"}, registry))
    assert out["merged"] is True
    assert contacts.merges == [("cid-a", "cid-b", "agent")]

    out = json.loads(await handle_create_project(
        {"objective": "tidy the inbox"}, registry))
    assert out["created"] is True
    assert projects.created == [("tidy the inbox", "", "agent")]


async def test_executor_attributes_owner_policy_calls_to_the_owner(contacts):
    projects = _Projects()
    executor = ToolExecutor(registry=SimpleNamespace(project_engine=projects))

    results = await executor.execute_batch([
        {"id": "1", "name": "merge_contacts",
         "arguments": {"keep": "cid-a", "merge": "cid-b"}},
        {"id": "2", "name": "create_project",
         "arguments": {"objective": "tidy the inbox"}},
    ], actor_policy=OWNER)

    assert all(r["executed"] for r in results), results
    assert contacts.merges == [("cid-a", "cid-b", "owner")]
    assert projects.created == [("tidy the inbox", "", "owner")]


async def test_executor_in_process_calls_stay_attributed_to_the_agent(contacts):
    projects = _Projects()
    executor = ToolExecutor(registry=SimpleNamespace(project_engine=projects))

    results = await executor.execute_batch([
        {"id": "1", "name": "merge_contacts",
         "arguments": {"keep": "cid-a", "merge": "cid-b"}},
        {"id": "2", "name": "create_project",
         "arguments": {"objective": "tidy the inbox"}},
    ], actor_policy=None)

    assert all(r["executed"] for r in results), results
    assert contacts.merges == [("cid-a", "cid-b", "agent")]
    assert projects.created == [("tidy the inbox", "", "agent")]


async def test_actor_does_not_leak_past_the_batch(contacts):
    executor = ToolExecutor(registry=SimpleNamespace(project_engine=None))
    await executor.execute_batch([
        {"id": "1", "name": "merge_contacts",
         "arguments": {"keep": "cid-a", "merge": "cid-b"}},
    ], actor_policy=OWNER)
    await handle_merge_contacts({"keep": "cid-c", "merge": "cid-d"},
                                SimpleNamespace())
    assert contacts.merges[-1] == ("cid-c", "cid-d", "agent")


def _request(authority):
    from starlette.requests import Request

    request = Request({
        "type": "http", "method": "POST", "path": "/", "headers": [],
        "query_string": b"", "client": ("127.0.0.1", 1), "scheme": "http",
        "server": ("test", 80),
    })
    request.state.protagine_authority = authority
    return request


async def test_http_calls_without_p8_are_attributed_to_the_caller(contacts, monkeypatch):
    from protagine.api.authority import anonymous_authority, legacy_authority
    from protagine.api.schemas.host import HostIdentity, ToolInvokeRequest

    projects = _Projects()
    monkeypatch.setattr(host_mod, "_p8_runtime", None)
    monkeypatch.setattr(host_mod, "_tool_executor",
                        ToolExecutor(registry=SimpleNamespace(project_engine=projects)))
    identity = HostIdentity(host_id="hermes")

    out = await host_mod.tools_invoke(
        ToolInvokeRequest(identity=identity, name="merge_contacts",
                          arguments={"keep": "cid-a", "merge": "cid-b"}),
        _request(legacy_authority()))
    assert out.available is True
    assert contacts.merges == [("cid-a", "cid-b", "owner")]

    out = await host_mod.tools_invoke(
        ToolInvokeRequest(identity=identity, name="create_project",
                          arguments={"objective": "tidy the inbox"}),
        _request(anonymous_authority()))
    assert out.available is True
    assert projects.created == [("tidy the inbox", "", "guest")]


async def test_reasoning_turn_without_p8_names_the_caller_to_the_loop(monkeypatch):
    from protagine.api.authority import legacy_authority
    from protagine.api.schemas.host import (
        HostIdentity, HostMessage, HostTurnContext, ReasoningTurnRequest,
    )
    from protagine.reasoning.loop import ReasoningResult

    calls = []

    class _Loop:
        async def run_turn(self, **kwargs):
            calls.append(kwargs)
            return ReasoningResult(status="completed")

    monkeypatch.setattr(host_mod, "_p8_runtime", None)
    monkeypatch.setattr(host_mod, "_reasoning_loop", _Loop())
    body = ReasoningTurnRequest(
        identity=HostIdentity(host_id="hermes"),
        context=HostTurnContext(session_id="s1", contact_id="owner"),
        messages=[HostMessage(role="user", content="merge them")])
    await host_mod.reasoning_turn(body, _request(legacy_authority()))
    assert calls[0]["actor_policy"] is None
    assert calls[0]["actor"] == "owner"


def test_sealed_people_are_attributed_by_authority_not_policy_gating(monkeypatch):
    from dataclasses import replace

    from protagine.api.authority import RequestAuthority

    monkeypatch.delenv("PROTAGINE_OWNER_PERSON_ID", raising=False)
    monkeypatch.delenv("PROTAGINE_OWNER_CONTACT_ID", raising=False)
    owner = RequestAuthority(
        principal_id="key-owner", credential_id="c1", scopes=frozenset(),
        viewer_person_id="owner", person_ids=frozenset({"owner"}),
        audiences=frozenset(), authenticated=True)
    guest = replace(owner, principal_id="key-guest", viewer_person_id="cid-g",
                    person_ids=frozenset({"cid-g"}))
    # The owner without tools:mutate still acts as the owner, not as a guest.
    assert host_mod._request_tool_actor(_request(owner), None) == "owner"
    assert host_mod._request_tool_actor(_request(guest), None) == "cid-g"
