"""P2 — Feature A polish: scope lifecycle is auditable, and group_guest is a
first-class, restrictive trust tier (not a silent peripheral fallback).
"""

import pytest

from protagine.contacts.config import ContactsConfig
from protagine.contacts.models import TRUST_TIERS
from protagine.contacts.store import SQLiteContactStore


@pytest.fixture
async def store():
    s = SQLiteContactStore(config=ContactsConfig(sqlite_path=":memory:"))
    await s.connect()
    yield s
    await s.close()


# ── audit trail over the scope lifecycle ─────────────────────────────────────

@pytest.mark.asyncio
async def test_scope_lifecycle_is_audited(store):
    guest = await store.create(display_name="Guest", trust_tier="acquaintance")
    scope = await store.create_scope(platform="rcs", external_id="conv-7", label="G")

    await store.add_scope_member(scope.scope_id, guest.contact_id)
    actions = [a["action"] for a in await store.get_audit_log(guest.contact_id)]
    assert "scope_member_added" in actions

    # deactivating the scope records a revocation against each current member
    await store.deactivate_scope(scope.scope_id)
    actions = [a["action"] for a in await store.get_audit_log(guest.contact_id)]
    assert "scope_deactivated" in actions

    # explicit member removal is audited too
    await store.add_scope_member(scope.scope_id, guest.contact_id)
    await store.remove_scope_member(scope.scope_id, guest.contact_id)
    actions = [a["action"] for a in await store.get_audit_log(guest.contact_id)]
    assert "scope_member_removed" in actions


# ── group_guest is a real, restrictive tier ──────────────────────────────────

def test_group_guest_is_in_the_one_tier_vocabulary():
    # contacts.models is the only tier vocabulary; a stored group_guest must be valid.
    assert "group_guest" in TRUST_TIERS


# ── P2c: promotion (group member -> regular; a tier, never a permission) ────────

@pytest.mark.asyncio
async def test_promotion_candidates_and_promote(store):
    guest = await store.create(display_name="Frequent Guest", trust_tier="acquaintance")
    scope = await store.create_scope(platform="rcs", external_id="conv-pc", label="PC")
    await store.add_scope_member(scope.scope_id, guest.contact_id)
    for hour in range(5):  # five conversations (C3: turns within 30 minutes are one)
        await store.record_interaction(guest.contact_id, f"2026-09-01T{10 + hour:02d}:00:00Z")
        await store.record_interaction(guest.contact_id, f"2026-09-01T{10 + hour:02d}:05:00Z")

    cands = await store.group_promotion_candidates(min_interactions=5)
    assert any(c.contact_id == guest.contact_id for c in cands)
    # below threshold excluded
    assert not any(c.contact_id == guest.contact_id
                   for c in await store.group_promotion_candidates(min_interactions=6))

    # promote raises the tier to regular; may_contact is the owner's alone and stays 'ask'
    assert await store.promote_scope_member(guest.contact_id) is True
    g = await store.get(guest.contact_id)
    assert g.trust_tier == "regular" and g.may_contact == "ask"
    # no longer a candidate (regular already); idempotent re-promote is a no-op
    assert not any(c.contact_id == guest.contact_id
                   for c in await store.group_promotion_candidates(min_interactions=1))
    assert await store.promote_scope_member(guest.contact_id) is False
    assert "scope_promoted" in [a["action"] for a in await store.get_audit_log(guest.contact_id)]


@pytest.mark.asyncio
async def test_promote_never_lowers_standing(store):
    # an already-trusted contact in a group keeps their higher tier (promote only raises)
    vip = await store.create(display_name="VIP", trust_tier="trusted")
    scope = await store.create_scope(platform="rcs", external_id="conv-vip")
    await store.add_scope_member(scope.scope_id, vip.contact_id)
    await store.promote_scope_member(vip.contact_id)  # to_tier=regular < trusted
    assert (await store.get(vip.contact_id)).trust_tier == "trusted"


def test_config_flag_from_env(monkeypatch):
    """Group membership never grants 1:1 rights by itself: promotion is the owner's call through
    /scopes/promote, so no switch turns it automatic (audit m8; nothing consumed the old one)."""
    import dataclasses
    from protagine.contacts.config import ContactsConfig
    monkeypatch.setenv("PROTAGINE_GROUP_PROMOTE_MIN_INTERACTIONS", "3")
    cfg = ContactsConfig.from_env()
    assert cfg.group_promote_min_interactions == 3
    assert "auto_promote_group_to_1on1" not in {field.name for field in dataclasses.fields(ContactsConfig)}
