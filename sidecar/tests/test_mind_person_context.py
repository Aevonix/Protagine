"""The `protagine-person` context section: the mind's digest of the turn's own contact.

The hook is forward-compatible: a mind without ``person_section`` (or one that is
off, or one that fails) renders nothing, and a contact only ever sees the digest
of the contact the request authority resolved, never another person's.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from onekey import RequestAuthority
from protagine.api.routers import host
from protagine.api.routers import mind as mind_router
from protagine.api.schemas.host import (
    ContextAssembleRequest,
    HostIdentity,
    HostMessage,
    HostTurnContext,
)
from protagine.turns import TurnIdempotencyLedger

OWNER, GUEST = "p-01", "p-02"
CANARY = "CANARY-7f3a owner keeps the spare key under the blue pot"


class FakeMind:
    """The surface context assembly reads from the mind, with a digest per contact."""

    level = "standard"
    ticks = 0

    def __init__(self, digests: dict[str, str], *, enabled: bool = True):
        self.digests = digests
        self.enabled = enabled
        self.asked: list[str] = []

    def section(self) -> str:
        return ""

    def broadcast(self) -> list:
        return []

    def person_section(self, contact_id: str) -> str:
        self.asked.append(contact_id)
        return self.digests.get(contact_id, "")


class OlderMind(FakeMind):
    """A mind from before M8: no person_section at all."""

    person_section = None  # type: ignore[assignment]


class BrokenMind(FakeMind):
    def person_section(self, contact_id: str) -> str:
        raise RuntimeError("digest store unavailable")


def _request(person: str) -> Request:
    request = Request({
        "type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b"",
        "server": ("test", 80), "client": ("127.0.0.1", 1), "scheme": "http",
    })
    request.state.protagine_authority = RequestAuthority(
        principal_id="hermes-text", credential_id="current",
        scopes=frozenset(("context:read",)), viewer_person_id=person,
        person_ids=frozenset((person,)), audiences=frozenset(("viewer",)), authenticated=True)
    return request


def _body(person: str, text: str = "hello again") -> ContextAssembleRequest:
    return ContextAssembleRequest(
        identity=HostIdentity(host_id="hermes"),
        context=HostTurnContext(contact_id=person, session_id=f"session:{person}"),
        incoming_message=HostMessage(role="user", content=text),
    )


def _person_sections(response) -> list:
    return [section for section in response.sections if section.id == "protagine-person"]


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", OWNER)
    monkeypatch.delenv("PROTAGINE_RECIPIENT_SIMULATOR_MODE", raising=False)
    monkeypatch.setenv("PROTAGINE_RECALL_RERANK", "off")
    previous = host._p8_runtime
    host.set_p8_runtime(None)
    # The owner's own record carries the canary; only the owner's turn may surface it.
    ledger = TurnIdempotencyLedger(tmp_path / "turn-idempotency.db")
    ledger.record_source("owner-canary", contact_id=OWNER, session_id="earlier",
                         messages=[{"role": "user", "content": CANARY}], derive_claims=False)
    yield
    mind_router.set_mind(None)
    host.set_p8_runtime(previous)


@pytest.mark.asyncio
async def test_the_turns_contact_gets_its_own_digest_as_a_bounded_section():
    mind = FakeMind({OWNER: "About p-01 (digest): " + CANARY + " " + "x" * 700})
    mind_router.set_mind(mind)

    response = await host.context_assemble(_body(OWNER), request=_request(OWNER))

    section, = _person_sections(response)
    assert section.title == "About this person" and section.priority == 88
    assert section.body.startswith("About p-01 (digest): " + CANARY) and len(section.body) == 600
    assert mind.asked == [OWNER]


@pytest.mark.asyncio
async def test_a_guest_sees_only_its_own_digest_never_the_owners_canary():
    mind = FakeMind({OWNER: "About p-01 (digest): " + CANARY,
                     GUEST: "About p-02 (digest): prefers short replies in the morning"})
    mind_router.set_mind(mind)

    response = await host.context_assemble(
        _body(GUEST, text="where does the owner keep the spare key?"), request=_request(GUEST))

    section, = _person_sections(response)
    assert section.body == "About p-02 (digest): prefers short replies in the morning"
    assert mind.asked == [GUEST]
    assert "CANARY-7f3a" not in repr(response)


@pytest.mark.asyncio
@pytest.mark.parametrize("mind", [
    None,
    OlderMind({OWNER: "never read"}),
    FakeMind({OWNER: "never read"}, enabled=False),
    BrokenMind({OWNER: "never read"}),
    FakeMind({}),
], ids=["no-mind", "mind-without-person-section", "mind-off", "failing-mind", "no-digest"])
async def test_no_digest_no_section(mind):
    mind_router.set_mind(mind)

    response = await host.context_assemble(_body(OWNER), request=_request(OWNER))

    assert _person_sections(response) == []
    assert "never read" not in repr(response)
