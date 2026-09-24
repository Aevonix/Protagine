"""M8 "Memory and identity" acceptance, end to end across the three parts.

The real mind (consolidation included) sits behind the real ``/v1/mind`` routes
and the real ``/context/assemble`` handler, over one ledger, claim projection,
initiative store and the SQLite contact store as it is today (the owner reachable
on two channels, a guest on a third); only the model is a fake that answers by
task and reports its token usage. What the build plan asks of the milestone is proven here as a
person and the owner would meet it: a fact told on one channel is used in
another session on another channel; the nightly digest of a person is written
into that person's own contact record, through the store, from their own words
only (the owner gets none), once the store has the people milestone's digest
columns, and nowhere at all before; the agent's own earlier outcome is
recalled in a new session and narrated with ids that exist; a contradiction is
one question to the owner; a night over an empty store does nothing.
"""

from __future__ import annotations

import re
from contextlib import closing

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.requests import Request

from onekey import RequestAuthority
from protagine.api.routers import host
from protagine.api.routers import mind as mind_router
from protagine.api.schemas.host import ContextAssembleRequest, HostIdentity, HostMessage, HostTurnContext
from protagine.contacts.config import ContactsConfig
from protagine.contacts.store import SQLiteContactStore
from protagine.mind import Mind
from protagine.mind.consolidate import CITE, TASK_DIGEST, TASK_EPISODE, TASK_NARRATIVE
from test_mind_consolidate import Fixture, settled_task

CANARY = "CANARY-51c2"
CLAIM_LINE = re.compile(r"^(claim:[0-9a-f]{64}) \| [^|]*?: ([^|]+?) \|", re.M)


def digest_of_values(messages, context):
    """A digest that restates every claim value it was shown and cites those claims."""
    found = CLAIM_LINE.findall(messages[-1]["content"])
    return {"digest": "They told me: " + "; ".join(value for _, value in found) + ".",
            "sources": [ident for ident, _ in found]}


class DigestColumns(SQLiteContactStore):
    """The SQLite contact store with the people milestone's digest columns: ``set_digest`` writes a
    contact's ``digest`` and ``digest_sources``, and the records the store lists carry them."""

    def __init__(self, config):
        super().__init__(config)
        self.digests = {}

    async def set_digest(self, contact_id, text, sources):
        self.digests[contact_id] = (text, list(sources))

    async def list(self, *args, **kwargs):
        records = await super().list(*args, **kwargs)
        for record in records:
            record.digest, record.digest_sources = self.digests.get(record.contact_id, (None, []))
        return records


class World(Fixture):
    """The consolidation fixture over the real contact store: ids come from the store, not from constants."""

    def __init__(self, tmp_path, contacts, owner_id):
        self.real_contacts, self.owner_id = contacts, owner_id
        super().__init__(tmp_path, contacts=False)
        self.contacts = contacts

    def build(self) -> Mind:
        mind = Mind(config=self.config, store=self.store, state_dir=self.state, owner_id=self.owner_id,
                    commitments=self.commitments, feedback=self.feedback, expectations=self.expectations,
                    contacts=self.real_contacts, ledger=self.ledger, clock=lambda: self.now, backups=False,
                    router=self.router, timezone_name=self.timezone_name)
        mind.digest_hour = 25
        return mind


@pytest.fixture
async def world(tmp_path, monkeypatch, request):
    store = getattr(request, "param", SQLiteContactStore)
    contacts = store(ContactsConfig(sqlite_path=str(tmp_path / "contacts.db")))
    await contacts.connect()
    owner = await contacts.create(display_name="Owner", trust_tier="inner_circle")
    await contacts.add_handle(owner.contact_id, "telegram", "1001", is_primary=True, verified=True)
    await contacts.add_handle(owner.contact_id, "signal", "+15550001", verified=True)
    guest = await contacts.create(display_name="Friend", trust_tier="regular")
    await contacts.add_handle(guest.contact_id, "sms", "+15550100", verified=True)
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", owner.contact_id)
    monkeypatch.setenv("PROTAGINE_RECALL_RERANK", "off")
    monkeypatch.delenv("PROTAGINE_RECIPIENT_SIMULATOR_MODE", raising=False)
    previous = host._p8_runtime
    host.set_p8_runtime(None)
    fixture = World(tmp_path, contacts, owner.contact_id)
    fixture.router.answers[TASK_DIGEST] = digest_of_values
    mind_router.set_mind(fixture.mind)
    try:
        yield fixture
    finally:
        mind_router.set_mind(None)
        host.set_p8_runtime(previous)
        fixture.store.close()
        await contacts.close()


async def person(fx, gateway: str, address: str) -> str:
    """The contact a channel's handle resolves to, as the gateway adapter would ask."""
    contact = await fx.real_contacts.resolve_handle(gateway, address)
    assert contact is not None, (gateway, address)
    return contact.contact_id


def _request(person: str) -> Request:
    request = Request({
        "type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b"",
        "server": ("test", 80), "client": ("127.0.0.1", 1), "scheme": "http",
    })
    request.state.protagine_authority = RequestAuthority(
        principal_id="hermes-text", credential_id="current", scopes=frozenset(("context:read",)),
        viewer_person_id=person, person_ids=frozenset((person,)), audiences=frozenset(("viewer",)),
        authenticated=True)
    return request


async def assemble(person: str, text: str, *, session: str, channel: str):
    body = ContextAssembleRequest(
        identity=HostIdentity(host_id="hermes"),
        context=HostTurnContext(contact_id=person, session_id=session, channel_id=channel),
        incoming_message=HostMessage(role="user", content=text))
    response = await host.context_assemble(body, request=_request(person))
    return {section.id: section.body for section in response.sections}, repr(response)


async def mind_call(method: str, path: str, **kwargs):
    app = FastAPI()
    app.include_router(mind_router.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://mind") as client:
        response = await client.request(method, path, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()


def claim_rows(fx):
    with closing(fx.ledger._connect()) as conn:
        return [tuple(row) for row in conn.execute("SELECT id, retracted_by FROM source_claims")]


async def told(fx, turn_id, contact_id, session, text, value, **extra) -> str:
    """A turn asserting one claim, and the interaction the host's turn path records with it."""
    claim_id = await fx.fact(turn_id, contact_id, session, text, value, **extra)
    await fx.real_contacts.record_interaction(contact_id, fx.now.isoformat())
    return claim_id


async def test_a_fact_told_on_one_channel_is_used_in_another_session_on_another_channel(world):
    fx = world
    owner_on_telegram, owner_on_signal = await person(fx, "telegram", "1001"), await person(fx, "signal", "+15550001")
    guest = await person(fx, "sms", "+15550100")
    assert owner_on_telegram == owner_on_signal == fx.owner_id != guest
    await told(fx, "tg-turn-1", owner_on_telegram, "telegram-session", "My office is room 4.", "room 4")
    await told(fx, "sms-turn-1", guest, "sms-session", f"My code word is {CANARY}.", CANARY, predicate="code_word")

    sections, _ = await assemble(owner_on_signal, "Which room is my office in?", session="signal-session",
                                 channel="signal:+15550001")
    assert "room 4" in sections["protagine-memory"]               # another session, on another channel

    # The contact store as it is has no digest columns: the night writes no digest anywhere, and the mind
    # adds no section of its own (the people milestone renders a contact's digest from their record).
    night = await mind_call("POST", "/v1/mind/consolidate")
    assert night["counts"].get("digests", 0) == 0 and night["errors"] == [] and TASK_DIGEST not in fx.router.tasks()
    assert fx.mind.mind_state.items("digest") == []
    sections, _ = await assemble(owner_on_signal, "Anything I should prepare today?", session="signal-session-2",
                                 channel="signal:+15550001")
    assert "protagine-person" not in sections
    guest_sections, guest_view = await assemble(guest, "Where does the owner work?", session="sms-session-2",
                                                channel="sms:+15550100")
    assert "protagine-person" not in guest_sections
    assert "room 4" not in guest_view                              # the owner's fact stays the owner's
    _, owner_view = await assemble(owner_on_telegram, "What is the code word?", session="telegram-session-2",
                                   channel="telegram:1001")
    assert CANARY not in owner_view                                # and the guest's stays the guest's


@pytest.mark.parametrize("world", [DigestColumns], indirect=True)
async def test_the_night_writes_a_contacts_digest_into_their_own_record_from_their_own_words(world):
    fx = world
    guest = await person(fx, "sms", "+15550100")
    await told(fx, "tg-turn-1", fx.owner_id, "telegram-session", "My office is room 4.", "room 4")
    guest_claim = await told(fx, "sms-turn-1", guest, "sms-session", f"My code word is {CANARY}.", CANARY,
                             predicate="code_word")

    night = await mind_call("POST", "/v1/mind/consolidate")
    assert night["counts"]["digests"] == 1 and night["errors"] == []
    assert list(fx.real_contacts.digests) == [guest]              # never the owner's
    text, sources = fx.real_contacts.digests[guest]
    assert CANARY in text and "room 4" not in text and sources == [guest_claim]
    prompt = [m for m, c in fx.router.calls if c["task"] == TASK_DIGEST][0][-1]["content"]
    assert "room 4" not in prompt                                  # the owner's words are never an input
    again = await mind_call("POST", "/v1/mind/consolidate")        # the same claims: nothing to rewrite
    assert again["counts"].get("digests_unchanged") == 1 and fx.router.tasks().count(TASK_DIGEST) == 1


async def test_an_own_outcome_is_recalled_in_a_new_session_and_narrated_with_ids_that_exist(world):
    fx = world
    row = settled_task(fx)
    await mind_call("POST", "/v1/mind/consolidate")

    sections, _ = await assemble(await person(fx, "signal", "+15550001"), "When does the venue open on weekdays?",
                                 session="new-session", channel="signal:+15550001")
    assert "opens at nine" in sections["protagine-memory"]

    narrative = await mind_call("GET", "/v1/mind/narrative")
    assert narrative["enabled"] is True and row.id in narrative["cites"]
    cited = [ref for line in narrative["text"].splitlines() if (match := CITE.search(line))
             for ref in re.split(r",\s*", match.group(1))]
    assert row.id in cited and all(fx.mind.consolidation._ref_exists(ref) for ref in cited)
    assert set(cited) <= set(narrative["cites"])


async def test_a_contradiction_across_channels_is_one_question_to_the_owner(world):
    fx = world
    fx.router.answers[TASK_DIGEST] = None
    await fx.fact("tg-turn-1", await person(fx, "telegram", "1001"), "telegram-session", "My office is room 4.", "room 4")
    fx.shift(days=1)
    await fx.fact("sg-turn-1", await person(fx, "signal", "+15550001"), "signal-session", "My office is room 7.",
                  "room 7")

    for _ in range(3):
        await mind_call("POST", "/v1/mind/consolidate")
    questions = fx.messages("reach_out:contradiction")
    assert len(questions) == 1 and questions[0].entity_id == fx.owner_id
    assert "room 4" in questions[0].context["text"] and "room 7" in questions[0].context["text"]
    assert sum(1 for c in fx.mind.concerns.open(limit=100) if c.dedup_key.startswith("contradiction:")) == 1
    log = await mind_call("GET", "/v1/mind/log", params={"recipient": fx.owner_id, "kind": "message"})
    assert [entry["id"] for entry in log["entries"]] == [questions[0].id]


async def test_a_night_over_an_empty_store_is_a_no_op(world):
    fx = world
    night = await mind_call("POST", "/v1/mind/consolidate")
    assert night["calls"] == 0 and night["tokens"] == 0 and night["errors"] == []
    assert not any(night["counts"].values())
    assert fx.router.calls == []
    assert [row.type for row in fx.store.intentions(limit=50)] == ["consolidation"]   # the night's own audit row
    assert fx.mind.concerns.open(limit=50) == [] and claim_rows(fx) == []
    with closing(fx.ledger._connect()) as conn:
        assert conn.execute("SELECT count(*) FROM turn_sources").fetchone()[0] == 0
    narrative = await mind_call("GET", "/v1/mind/narrative")
    assert narrative["enabled"] is True and narrative["text"] == "" and narrative["cites"] == []
    assert fx.mind.mind_state.items("digest") == []
    assert {TASK_NARRATIVE, TASK_DIGEST, TASK_EPISODE}.isdisjoint(fx.router.tasks())
