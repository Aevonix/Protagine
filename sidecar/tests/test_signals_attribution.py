"""/signals/ingest attribution.

A supplied sender resolves via ParticipantResolver (like turns/sync) and
overwrites contact_id; without one, an unknown or unresolvable contact is
diverted to the system sentinel so a signal can never poison a person's
baselines. There is no switch: this is the only behaviour.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from protagine.api.routers import host as host_mod
from protagine.api.schemas.host import (
    HostIdentity, HostMessage, HostSender, HostTurnContext,
    SignalIngestRequest)


class _FakeCollector:
    def __init__(self):
        self.sender_ids = []

    async def collect(self, msg):
        self.sender_ids.append(msg.sender_id)
        return []

    async def ingest_raw(self, sig):
        return None


class _FakeContactsStore:
    """Just enough of ContactsStore for ParticipantResolver + get()."""

    def __init__(self, known=(), handles=None):
        self._known = set(known)
        self._handles = handles or {}   # (platform, user_id) -> contact_id

    async def get(self, contact_id):
        if contact_id in self._known:
            return SimpleNamespace(contact_id=contact_id)
        return None

    async def resolve_messaging_handle(self, platform, user_id):
        cid = self._handles.get((platform, user_id))
        return SimpleNamespace(contact_id=cid) if cid else None


def _request(contact_id="c1", sender=None):
    return SignalIngestRequest(
        identity=HostIdentity(host_id="test-host"),
        context=HostTurnContext(session_id="s1", contact_id=contact_id),
        sender=sender,
        incoming_message=HostMessage(role="user", content="hello there"),
    )


@pytest.fixture
def wired(monkeypatch):
    collector = _FakeCollector()
    monkeypatch.setattr(host_mod, "_signal_collector", collector)
    return collector


@pytest.mark.asyncio
async def test_sender_resolution_overwrites_contact(wired, monkeypatch):
    store = _FakeContactsStore(known={"cid-real"},
                               handles={("sms", "+15550001"): "cid-real"})
    monkeypatch.setattr(host_mod, "_contacts_store", store)
    body = _request(contact_id="stale-cache",
                    sender=HostSender(platform="sms", user_id="+15550001"))
    await host_mod.signals_ingest(body)
    assert wired.sender_ids == ["cid-real"]     # server-side resolution wins


@pytest.mark.asyncio
async def test_unknown_contact_goes_to_system_whatever_the_retired_switch_says(wired, monkeypatch):
    monkeypatch.setenv("PROTAGINE_SIGNALS_ATTRIBUTION", "legacy")
    monkeypatch.setattr(host_mod, "_contacts_store", _FakeContactsStore())
    await host_mod.signals_ingest(_request(contact_id="ghost-99"))
    assert wired.sender_ids == ["system"]       # never poisons a person


@pytest.mark.asyncio
async def test_unresolvable_sender_goes_to_system(wired, monkeypatch):
    # The fake store cannot create a shadow contact, so the unknown handle stays unresolvable.
    monkeypatch.setattr(host_mod, "_contacts_store", _FakeContactsStore())
    body = _request(contact_id="ghost-99",
                    sender=HostSender(platform="sms", user_id="+15559999"))
    await host_mod.signals_ingest(body)
    assert wired.sender_ids == ["system"]


@pytest.mark.asyncio
async def test_known_contact_is_kept(wired, monkeypatch):
    monkeypatch.setattr(host_mod, "_contacts_store",
                        _FakeContactsStore(known={"cid-real"}))
    await host_mod.signals_ingest(_request(contact_id="cid-real"))
    assert wired.sender_ids == ["cid-real"]


@pytest.mark.asyncio
async def test_no_contacts_store_is_a_noop(wired, monkeypatch):
    """Attribution fails open when the store is absent (test/degraded envs)."""
    monkeypatch.setattr(host_mod, "_contacts_store", None)
    await host_mod.signals_ingest(_request(contact_id="whoever"))
    assert wired.sender_ids == ["whoever"]
