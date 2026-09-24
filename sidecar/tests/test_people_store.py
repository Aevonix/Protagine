"""The people store (M5): ``may_contact``, cadence, conversations, C1 identity, C2 merge, references."""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from protagine.contacts import models as models_mod
from protagine.contacts import store as store_mod
from protagine.contacts.config import ContactsConfig
from protagine.contacts.store import SQLiteContactStore, canonical_handle, is_e164
from protagine.identity.participants import ParticipantResolver
from protagine.migrations import run_migrations_sync

OWNER_ID = "cid-owner-000000000000"


@pytest.fixture
async def store(tmp_path):
    value = SQLiteContactStore(ContactsConfig(sqlite_path=str(tmp_path / "contacts.db")))
    await value.connect()
    yield value
    await value.close()


def _pre_006(path: Path, tmp_path: Path) -> None:
    """A store written before migration 006: the ``interaction_allowed`` column with both values."""
    older = tmp_path / "older-migrations"
    older.mkdir()
    for file in sorted(store_mod._MIGRATIONS_DIR.glob("*.sql")):
        if int(file.name[:3]) < 6:
            shutil.copy(file, older)
    conn = sqlite3.connect(path)
    run_migrations_sync(conn, older)
    conn.execute("INSERT INTO contacts (contact_id, display_name, trust_tier, interaction_allowed) VALUES "
                 "('cid-never-1', 'Blocked', 'peripheral', 0), ('cid-ask-1', 'Known', 'regular', 1)")
    conn.commit()
    conn.close()


# -- migration 006 and the model ------------------------------------------------------------

@pytest.mark.asyncio
async def test_migration_006_backfills_may_contact_and_drops_the_flag(tmp_path):
    path = tmp_path / "legacy.db"
    _pre_006(path, tmp_path)
    store = SQLiteContactStore(ContactsConfig(sqlite_path=str(path)))
    await store.connect()
    try:
        assert (await store.get("cid-never-1")).may_contact == "never"
        assert (await store.get("cid-ask-1")).may_contact == "ask"
        db = store._require_db()
        async with db.execute("PRAGMA table_info(contacts)") as cur:
            columns = {row[1] for row in await cur.fetchall()}
        assert "interaction_allowed" not in columns
        assert {"may_contact", "cadence_minutes", "digest", "digest_sources"} <= columns
        async with db.execute("SELECT version FROM schema_version") as cur:
            assert "006" in {row[0] for row in await cur.fetchall()}
    finally:
        await store.close()


def _columns(path: Path) -> set:
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(contacts)")}
    finally:
        conn.close()


def _versions(path: Path) -> set:
    conn = sqlite3.connect(path)
    try:
        return {row[0] for row in conn.execute("SELECT version FROM schema_version")}
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_a_migration_006_that_fails_part_way_leaves_the_store_at_005_and_a_rerun_completes(tmp_path):
    """Audit M13: ``executescript`` commits statement by statement, so a failure after the first
    ALTER left ``may_contact`` behind with no version row, and every later start failed on
    ``duplicate column name``. 006 is one transaction."""
    path = tmp_path / "legacy.db"
    _pre_006(path, tmp_path)
    broken = tmp_path / "broken-migrations"
    broken.mkdir()
    sql = (store_mod._MIGRATIONS_DIR / "006_may_contact.sql").read_text()
    marker = "ALTER TABLE contacts ADD COLUMN cadence_minutes"
    assert marker in sql
    (broken / "006_may_contact.sql").write_text(sql.replace(marker, "SELECT * FROM no_such_table;\n" + marker, 1))
    conn = sqlite3.connect(path)
    with pytest.raises(sqlite3.OperationalError):
        run_migrations_sync(conn, broken)
    conn.close()
    assert "may_contact" not in _columns(path) and "interaction_allowed" in _columns(path)
    assert "006" not in _versions(path)
    store = SQLiteContactStore(ContactsConfig(sqlite_path=str(path)))
    await store.connect()
    try:
        assert (await store.get("cid-never-1")).may_contact == "never"
    finally:
        await store.close()
    assert "006" in _versions(path) and "interaction_allowed" not in _columns(path)


@pytest.mark.asyncio
async def test_both_runners_refuse_a_drop_column_migration_on_an_old_sqlite_before_touching_anything(
        tmp_path, monkeypatch):
    """Audit M13: ``protagine upgrade`` migrates through the sync runner and never calls the store's
    ``connect``, so the SQLite >= 3.35 check lives in both runners, ahead of any DROP COLUMN file."""
    import aiosqlite
    from protagine import migrations
    for name in ("sync", "async"):
        (tmp_path / name).mkdir()
        _pre_006(tmp_path / f"{name}.db", tmp_path / name)
    monkeypatch.setattr(migrations.sqlite3, "sqlite_version_info", (3, 34, 1))
    conn = sqlite3.connect(tmp_path / "sync.db")
    with pytest.raises(RuntimeError, match="3.35"):
        migrations.run_migrations_sync(conn, store_mod._MIGRATIONS_DIR)
    conn.close()
    db = await aiosqlite.connect(tmp_path / "async.db")
    with pytest.raises(RuntimeError, match="3.35"):
        await migrations.run_migrations(db, store_mod._MIGRATIONS_DIR)
    await db.close()
    for name in ("sync.db", "async.db"):
        assert "may_contact" not in _columns(tmp_path / name) and "006" not in _versions(tmp_path / name)
    # A directory with no DROP COLUMN migrates on the same old SQLite.
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "001_t.sql").write_text("CREATE TABLE t (x INTEGER);")
    assert migrations.run_migrations_sync(sqlite3.connect(tmp_path / "plain.db"), plain) == ["001"]


@pytest.mark.asyncio
async def test_tier_implies_no_permission_and_the_default_is_ask(store):
    for tier in ("inner_circle", "trusted", "regular", "unknown", "peripheral", "silenced"):
        contact = await store.create(display_name=f"T {tier}", trust_tier=tier)
        assert contact.may_contact == "ask", tier
        assert contact.cadence_minutes is None and contact.digest is None and contact.digest_sources == []
    explicit = await store.create(display_name="Auto", may_contact="auto", cadence_minutes=90)
    assert explicit.may_contact == "auto" and explicit.cadence_minutes == 90
    assert "interaction_allowed" not in explicit.to_dict()
    with pytest.raises(ValueError):
        await store.create(display_name="Bad", may_contact="always")
    with pytest.raises(TypeError):
        await store.create(display_name="Old", interaction_allowed=False)
    assert not hasattr(models_mod, "TIER_DEFAULT_INTERACTION")
    assert models_mod.MAY_CONTACT == ("never", "ask", "auto")
    assert models_mod.regular_or_above("regular") and models_mod.regular_or_above("inner_circle")
    assert not models_mod.regular_or_above("group_guest") and not models_mod.regular_or_above("unknown")
    assert models_mod.tier_rank("trusted") > models_mod.tier_rank("regular") > models_mod.tier_rank("acquaintance")


# -- may_contact: raised only by the owner path, lowered by anyone -------------------------------

@pytest.mark.asyncio
async def test_set_may_contact_moves_any_direction_with_an_audit_row(store):
    contact = await store.create(display_name="Sam")
    raised = await store.set_may_contact(contact.contact_id, "auto", by=OWNER_ID, reason="owner said so")
    assert raised.may_contact == "auto"
    lowered = await store.set_may_contact(contact.contact_id, "never", by=OWNER_ID)
    assert lowered.may_contact == "never"
    back = await store.set_may_contact(contact.contact_id, "ask", by="cli")
    assert back.may_contact == "ask"
    rows = [row for row in await store.get_audit_log(contact.contact_id) if row["action"] == "may_contact_set"]
    assert len(rows) == 3
    details = {(row["performed_by"], *sorted(json.loads(row["detail"]).items())) for row in rows}
    assert details == {(OWNER_ID, ("from", "ask"), ("reason", "owner said so"), ("to", "auto")),
                       (OWNER_ID, ("from", "auto"), ("reason", ""), ("to", "never")),
                       ("cli", ("from", "never"), ("reason", ""), ("to", "ask"))}
    with pytest.raises(ValueError):
        await store.set_may_contact(contact.contact_id, "maybe", by=OWNER_ID)
    with pytest.raises(ValueError):
        await store.set_may_contact("cid-nope-1", "auto", by=OWNER_ID)


@pytest.mark.asyncio
async def test_lower_may_contact_only_goes_to_never(store):
    contact = await store.create(display_name="Sam", may_contact="auto")
    lowered = await store.lower_may_contact(contact.contact_id, reason="STOP", source_ref="turn:t-1")
    assert lowered.may_contact == "never"
    assert await store.lower_may_contact(contact.contact_id, reason="STOP", source_ref="turn:t-2") is None
    assert (await store.get(contact.contact_id)).may_contact == "never"
    rows = [row for row in await store.get_audit_log(contact.contact_id) if row["action"] == "opt_out"]
    assert len(rows) == 1
    assert json.loads(rows[0]["detail"]) == {"from": "auto", "to": "never", "reason": "STOP", "source_ref": "turn:t-1"}
    assert rows[0]["performed_by"] == "contact"
    assert await store.lower_may_contact("cid-nope-1", reason="x", source_ref="y") is None


@pytest.mark.asyncio
async def test_set_cadence_and_digest(store):
    contact = await store.create(display_name="Sam")
    assert (await store.set_cadence(contact.contact_id, 90, by=OWNER_ID)).cadence_minutes == 90
    assert (await store.set_cadence(contact.contact_id, None, by="cli")).cadence_minutes is None
    for bad in (0, -5, "soon"):
        with pytest.raises(ValueError):
            await store.set_cadence(contact.contact_id, bad, by=OWNER_ID)
    with pytest.raises(ValueError):
        await store.set_cadence("cid-nope-1", 10, by=OWNER_ID)
    rows = [row for row in await store.get_audit_log(contact.contact_id) if row["action"] == "cadence_set"]
    assert sorted((json.loads(row["detail"])["from"] or 0, json.loads(row["detail"])["to"] or 0) for row in rows) == [
        (0, 90), (90, 0)]
    await store.set_digest(contact.contact_id, "Sam: known a week.", ["turn:1", "turn:2"])
    again = await store.get(contact.contact_id)
    assert again.digest == "Sam: known a week." and again.digest_sources == ["turn:1", "turn:2"]
    assert again.to_dict()["digest_sources"] == ["turn:1", "turn:2"]


@pytest.mark.asyncio
async def test_list_filters_on_may_contact_and_update_accepts_the_new_columns(store):
    never = await store.create(display_name="N", may_contact="never")
    ask = await store.create(display_name="A")
    assert {c.contact_id for c in await store.list(may_contact="never")} == {never.contact_id}
    assert {c.contact_id for c in await store.list(may_contact="ask")} == {ask.contact_id}
    assert len(await store.list()) == 2
    with pytest.raises(TypeError):
        await store.list(interaction_allowed=True)
    updated = await store.update(ask.contact_id, cadence_minutes=45, digest="d", digest_sources=["s"], may_contact="auto")
    assert updated.cadence_minutes == 45 and updated.digest == "d" and updated.digest_sources == ["s"]
    assert updated.may_contact == "ask"  # permission never moves through update()
    assert not hasattr(store, "update_interaction_allowed")


# -- C3: conversations, not turns --------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_interaction_counts_conversations_with_a_thirty_minute_gap(store):
    contact = await store.create(display_name="Chatty")
    assert await store.record_interaction(contact.contact_id, "2026-09-01T10:00:00Z") is True
    await store.record_interaction(contact.contact_id, "2026-09-01T10:10:00Z")
    await store.record_interaction(contact.contact_id, "2026-09-01T10:29:00Z")
    row = await store.get(contact.contact_id)
    assert row.interaction_count == 1 and row.last_interaction_at == "2026-09-01T10:29:00Z"
    await store.record_interaction(contact.contact_id, "2026-09-01T11:00:00Z")  # 31 minutes later
    row = await store.get(contact.contact_id)
    assert row.interaction_count == 2 and row.last_interaction_at == "2026-09-01T11:00:00Z"
    await store.record_interaction(contact.contact_id, "2026-09-01T09:00:00Z")  # late arrival: monotone
    row = await store.get(contact.contact_id)
    assert row.interaction_count == 2 and row.last_interaction_at == "2026-09-01T11:00:00Z"
    assert await store.record_interaction("cid-nope-1") is False


@pytest.mark.asyncio
async def test_cadence_estimate_uses_conversations_and_prefers_the_owner_cadence(store):
    sparse = await store.create(display_name="Sparse", trust_tier="regular")
    chatty = await store.create(display_name="Chatty", trust_tier="regular")
    blocked = await store.create(display_name="Blocked", trust_tier="regular", may_contact="never")
    timed = await store.create(display_name="Timed", cadence_minutes=60)
    for day in (1, 4, 7):
        await store.record_interaction(sparse.contact_id, f"2026-09-0{day}T10:00:00Z")
        await store.record_interaction(blocked.contact_id, f"2026-09-0{day}T10:00:00Z")
        for minute in range(0, 25, 5):  # five turns inside one conversation
            await store.record_interaction(chatty.contact_id, f"2026-09-0{day}T10:{minute:02d}:00Z")
    await store.record_interaction(timed.contact_id, "2026-09-12T08:00:00Z")
    rows = await store.compute_cadence_overdue(now_iso="2026-09-12T10:00:00Z", overdue_only=False, limit=50)
    by_id = {row["contact_id"]: row for row in rows}
    assert blocked.contact_id not in by_id
    assert by_id[sparse.contact_id]["cadence_days"] == 3.0 and by_id[sparse.contact_id]["overdue"] is True
    assert by_id[chatty.contact_id]["cadence_days"] == pytest.approx(3.0, abs=0.05)  # turns inside a conversation do not count
    assert by_id[chatty.contact_id]["cadence_source"] == "estimate"
    timed_row = by_id[timed.contact_id]
    assert timed_row["cadence_minutes"] == 60 and timed_row["cadence_source"] == "owner"
    assert timed_row["overdue"] is True  # two hours of silence against a one-hour cadence
    assert timed_row["may_contact"] == "ask"
    overdue_only = await store.compute_cadence_overdue(now_iso="2026-09-12T08:30:00Z", limit=50)
    assert timed.contact_id not in {row["contact_id"] for row in overdue_only}


# -- social candidates and references ----------------------------------------------------------

@pytest.mark.asyncio
async def test_social_candidates_need_a_cadence_or_a_regular_tier_and_never_a_never(store):
    regular = await store.create(display_name="Regular", trust_tier="regular")
    timed = await store.create(display_name="Timed", trust_tier="unknown", cadence_minutes=30, may_contact="auto")
    shadow = await store.create(display_name="Shadow", trust_tier="unknown")
    guest = await store.create(display_name="Guest", trust_tier="acquaintance")  # a group-only member
    blocked = await store.create(display_name="Blocked", trust_tier="trusted", may_contact="never")
    gone = await store.create(display_name="Gone", trust_tier="regular")
    await store.soft_delete(gone.contact_id)
    await store.record_interaction(regular.contact_id, "2026-09-01T10:00:00Z")
    rows = await store.social_candidates()
    assert {row["contact_id"] for row in rows} == {regular.contact_id, timed.contact_id}
    assert shadow.contact_id not in {row["contact_id"] for row in rows}
    assert guest.contact_id not in {row["contact_id"] for row in rows}
    assert blocked.contact_id not in {row["contact_id"] for row in rows}
    row = next(row for row in rows if row["contact_id"] == timed.contact_id)
    assert set(row) == {"contact_id", "display_name", "trust_tier", "may_contact", "cadence_minutes", "first_seen_at",
                        "last_interaction_at", "interaction_count", "timezone"}
    assert row["cadence_minutes"] == 30 and row["may_contact"] == "auto"
    assert len(await store.social_candidates(limit=1)) == 1


@pytest.mark.asyncio
async def test_the_owner_is_never_a_social_candidate(store, monkeypatch):
    """Audit m1: the owner is not checked in on, so the owner never takes one of the listed slots."""
    owner = await store.create(display_name="Owner", trust_tier="inner_circle", may_contact="auto", cadence_minutes=5)
    friend = await store.create(display_name="Friend", trust_tier="regular")
    monkeypatch.setenv("PROTAGINE_OWNER_CONTACT_ID", owner.contact_id)
    assert [row["contact_id"] for row in await store.social_candidates()] == [friend.contact_id]
    assert [row["contact_id"] for row in await store.social_candidates(limit=1)] == [friend.contact_id]


@pytest.mark.asyncio
async def test_resolve_reference_by_id_handle_email_address_or_unique_name(store):
    sam = await store.create(display_name="Sam Rivera", given_name="Sam")
    await store.add_handle(sam.contact_id, "sms", "+15550001234")
    await store.add_handle(sam.contact_id, "email", "Sam@Example.test")
    p05 = await store.create(display_name="Casey Lee")
    await store.add_handle(p05.contact_id, "capture", "p-05")
    await store.create(display_name="Casey Park")
    await store.create(display_name="Alex")
    await store.create(display_name="alex")
    assert (await store.resolve_reference(sam.contact_id)).contact_id == sam.contact_id
    assert (await store.resolve_reference("+15550001234")).contact_id == sam.contact_id
    assert (await store.resolve_reference("sms:+1 (555) 000-1234")).contact_id == sam.contact_id
    assert (await store.resolve_reference("whatsapp:15550001234@s.whatsapp.net")).contact_id == sam.contact_id
    assert (await store.resolve_reference("sam@example.test")).contact_id == sam.contact_id
    assert (await store.resolve_reference("sam rivera")).contact_id == sam.contact_id
    assert (await store.resolve_reference("Sam")).contact_id == sam.contact_id
    assert (await store.resolve_reference("capture:p-05")).contact_id == p05.contact_id
    assert (await store.resolve_reference("p-05")).contact_id == p05.contact_id  # a handle address nobody else has
    assert (await store.resolve_reference("Casey Lee")).contact_id == p05.contact_id
    assert await store.resolve_reference("Casey") is None      # two Caseys: ambiguous, never a guess
    assert await store.resolve_reference("Alex") is None       # case-insensitive duplicates are ambiguous
    assert await store.resolve_reference("nobody here") is None
    assert await store.resolve_reference("") is None
    assert await store.resolve_reference("cid-nope-1") is None


@pytest.mark.asyncio
async def test_an_exact_reference_never_matches_by_name_and_a_handle_outranks_a_name(store):
    """Audit M4: an owner's grant may send automatically only to someone the owner identified
    exactly (an id, a handle, a number, an email); a name is a guess the owner confirms."""
    sam = await store.create(display_name="Sam Rivera", given_name="Sam")
    await store.add_handle(sam.contact_id, "sms", "+15550001234")
    await store.add_handle(sam.contact_id, "capture", "p-05")
    namesake = await store.create(display_name="p-05")          # a display name that looks like the handle
    for reference in (sam.contact_id, "p-05", "capture:p-05", "+15550001234", "sms:+1 (555) 000-1234"):
        assert (await store.resolve_reference(reference, exact=True)).contact_id == sam.contact_id, reference
    assert (await store.resolve_reference("p-05")).contact_id == sam.contact_id  # the handle, not the name
    for name in ("Sam", "Sam Rivera", "sam"):
        assert await store.resolve_reference(name, exact=True) is None, name
        assert (await store.resolve_reference(name)).contact_id == sam.contact_id, name
    assert namesake.contact_id != sam.contact_id


# -- C1: one phone identity on any gateway ------------------------------------------------------

def test_is_e164_and_canonical_handle():
    assert is_e164("+15550001234") and is_e164("+1 (555) 000-1234")
    assert is_e164("+44 20 7946 0958") and is_e164("15550001234@s.whatsapp.net") and is_e164("15550001234@c.us")
    assert not is_e164("p-05") and not is_e164("user@lid") and not is_e164("+0123456") and not is_e164("12345")
    # A bare digit string is a numeric user id as often as a number; an opaque ``@lid`` id never is one.
    assert not is_e164("5550001234") and not is_e164("15550001234") and not is_e164("15550001234@lid")
    assert not is_e164("1234567890123456") and not is_e164("") and not is_e164("sam@example.test")
    assert canonical_handle("email", "Sam@Example.test") == ("email", "sam@example.test")
    assert canonical_handle("sms", "+1 (555) 000-1234") == ("phone", "+15550001234")
    assert canonical_handle("whatsapp", "15550001234@s.whatsapp.net") == ("phone", "+15550001234")
    assert canonical_handle("custom-phone-app", "+1 555 000 1234") == ("phone", "+15550001234")
    assert canonical_handle("telegram", "5550001234") == ("telegram", "5550001234")
    assert canonical_handle("rcs", "+15550001234") == ("phone", "+15550001234")
    assert canonical_handle("Telegram", " 2003 ") == ("telegram", "2003")
    assert canonical_handle("capture", "p-05") == ("capture", "p-05")
    assert not hasattr(store_mod, "_get_phone_gateways") and not hasattr(store_mod, "_looks_like_phone")
    with pytest.raises(ImportError):
        import protagine.channels.phone_gateways  # noqa: F401


@pytest.mark.asyncio
async def test_three_phone_senders_on_three_gateways_are_three_contacts_and_no_orphans(store):
    senders = [("sms", "+15550000001"), ("whatsapp", "15550000002@s.whatsapp.net"),
               ("custom-phone-app", "+1 (555) 000-0003")]
    resolver = ParticipantResolver(store)
    first = [await resolver.resolve(platform=gateway, user_id=address) for gateway, address in senders]
    assert all(r.method == "shadow" and r.created for r in first)
    assert len({r.contact_id for r in first}) == 3
    second = [await resolver.resolve(platform=gateway, user_id=address) for gateway, address in senders]
    assert [r.contact_id for r in second] == [r.contact_id for r in first]
    assert all(r.method == "handle" and not r.created for r in second)
    assert len(await store.list(limit=100)) == 3
    shadow = await store.get(first[2].contact_id)
    assert shadow.may_contact == "ask" and shadow.trust_tier == "unknown" and shadow.import_source == "auto:sender"
    handles = await store.get_handles(first[2].contact_id)
    assert [(h.gateway, h.address) for h in handles] == [("custom-phone-app", "+15550000003")]


@pytest.mark.asyncio
async def test_a_numeric_id_on_another_gateway_is_not_the_phone_contact(store):
    """A bare digit string is a user id as often as a number: only an address written as a phone
    number (``+`` and digits, or a phone JID) reaches the contact holding that number elsewhere."""
    owner_of_number = await store.create(display_name="Number holder")
    await store.add_handle(owner_of_number.contact_id, "sms", "+15551234567", verified=True)
    assert await store.resolve_messaging_handle("telegram", "5551234567") is None
    assert await store.resolve_messaging_handle("telegram", "15551234567") is None
    assert (await store.resolve_messaging_handle("whatsapp", "+15551234567")).contact_id == owner_of_number.contact_id
    stranger = await ParticipantResolver(store).resolve(platform="telegram", user_id="5551234567")
    assert stranger.created and stranger.contact_id != owner_of_number.contact_id
    # A number stored bare on its own gateway (a legacy row) still answers there, NANP form included.
    legacy = await store.create(display_name="Legacy")
    await store.add_handle(legacy.contact_id, "voice", "15550007777", verified=True)
    assert (await store.resolve_messaging_handle("voice", "5550007777")).contact_id == legacy.contact_id
    assert await store.resolve_messaging_handle("telegram", "15550007777") is None


@pytest.mark.asyncio
async def test_same_number_on_two_gateways_is_one_contact_and_a_custom_id_stays_scoped(store, monkeypatch):
    monkeypatch.setenv("PROTAGINE_IDENTITY_SHADOW_CONTACTS", "false")  # the switch is gone: shadows are the design
    resolver = ParticipantResolver(store)
    by_sms = await resolver.resolve(platform="sms", user_id="+15550000009")
    by_other = await resolver.resolve(platform="some-voip-app", user_id="+1 (555) 000-0009")
    by_jid = await resolver.resolve(platform="whatsapp", user_id="15550000009@s.whatsapp.net")
    assert by_sms.created and by_sms.contact_id == by_other.contact_id == by_jid.contact_id
    assert by_other.method == "handle" and by_jid.method == "handle"
    one = await resolver.resolve(platform="custom", user_id="user-42")
    two = await resolver.resolve(platform="other", user_id="user-42")
    assert one.created and two.created and one.contact_id != two.contact_id
    assert len(await store.list(limit=100)) == 3
    telegram = await store.create(display_name="Numeric id")
    handle = await store.add_handle(telegram.contact_id, "telegram", "123456789")
    assert handle.address == "123456789"  # a bare numeric id keeps its transport form
    assert (await store.resolve_messaging_handle("telegram", "123456789")).contact_id == telegram.contact_id


async def _all_rows(store):
    db = store._require_db()
    async with db.execute("SELECT contact_id, deleted_at FROM contacts") as cur:
        return [tuple(row) for row in await cur.fetchall()]


@pytest.mark.asyncio
async def test_a_sender_whose_old_record_was_deleted_gets_one_new_shadow_not_one_per_turn(store):
    old = await store.create(display_name="Deleted")
    await store.add_handle(old.contact_id, "custom-phone-app", "+15550000051")
    await store.add_handle(old.contact_id, "custom", "user-51")
    await store.soft_delete(old.contact_id)
    resolver = ParticipantResolver(store)
    for gateway, address in (("custom-phone-app", "+15550000051"), ("custom", "user-51")):
        first = await resolver.resolve(platform=gateway, user_id=address)
        second = await resolver.resolve(platform=gateway, user_id=address)
        assert first.created and first.contact_id and second.contact_id == first.contact_id, gateway
        assert second.method == "handle"
    live = [cid for cid, deleted in await _all_rows(store) if deleted is None]
    assert len(live) == 2   # one shadow per identity, each holding the released handle


@pytest.mark.asyncio
async def test_an_ambiguous_number_gets_one_shadow_found_again_by_its_exact_handle(store):
    """The owner split one number between two people; a third gateway cannot tell which one it
    is, so the sender becomes one shadow on that gateway, found again by its exact handle on every
    later turn instead of a new shadow per turn."""
    one = await store.create(display_name="One")
    two = await store.create(display_name="Two")
    await store.add_handle(one.contact_id, "sms", "+15550000061", verified=True)
    await store.correct_handle_identity(operation_id="split-61", performed_by=OWNER_ID, gateway="signal",
                                        address="+15550000061", expected_contact_id=None, contact_id=two.contact_id,
                                        evidence_refs=["turn:owner-said"])
    resolver = ParticipantResolver(store)
    first = await resolver.resolve(platform="custom-phone-app", user_id="+1 555 000 0061")
    assert first.created and first.contact_id not in {one.contact_id, two.contact_id}
    for _ in range(3):
        again = await resolver.resolve(platform="custom-phone-app", user_id="+1 555 000 0061")
        assert again.contact_id == first.contact_id and again.method == "handle"
    assert len(await _all_rows(store)) == 3


@pytest.mark.asyncio
async def test_a_shadow_whose_handle_cannot_be_attached_is_discarded(store):
    class Refusing(SQLiteContactStore):
        async def add_handle(self, *args, **kwargs):
            raise ValueError("handle refused")

    refusing = Refusing(ContactsConfig(sqlite_path=":memory:"))
    await refusing.connect()
    try:
        result = await ParticipantResolver(refusing).resolve(platform="custom", user_id="user-71")
        assert result.contact_id is None and result.method == "none"
        assert await _all_rows(refusing) == []
    finally:
        await refusing.close()


@pytest.mark.asyncio
async def test_handles_resolve_through_the_canonical_form_on_every_path(store):
    contact = await store.create(display_name="Phone person")
    await store.add_handle(contact.contact_id, "imessage", "+1 (555) 010-1234", verified=True)
    await store.add_handle(contact.contact_id, "email", "Person@Example.test")
    for gateway, address in [("sms", "+15550101234"), ("rcs", "+1 555 010 1234"), ("signal", "+1 555-010-1234"),
                             ("whatsapp", "15550101234@s.whatsapp.net"), ("voice", "+15550101234")]:
        assert (await store.resolve_messaging_handle(gateway, address)).contact_id == contact.contact_id, gateway
        assert (await store.resolve_handle(gateway, address)).contact_id == contact.contact_id, gateway
    assert (await store.resolve_handle("email", "PERSON@example.test")).contact_id == contact.contact_id
    assert not hasattr(store, "find_by_handle")  # no caller: resolve_handle is the one lookup
    assert (await store.resolve_verified_handles("imessage", ["+15550101234"])).contact_id == contact.contact_id
    assert (await store.resolve_messaging_handle("", "+15550101234")).contact_id == contact.contact_id
    with pytest.raises(ValueError, match="already assigned"):  # an exact handle belongs to one live contact
        await store.add_handle((await store.create(display_name="Dup")).contact_id, "imessage", "+15550101234")
    # The same number on another gateway may be kept as a separate alias: the exact transport handle
    # still resolves to its holder, and phone inference from any other gateway becomes ambiguous.
    other = await store.create(display_name="Other")
    await store.add_handle(other.contact_id, "sms", "+1 (555) 010-1234")
    assert (await store.resolve_messaging_handle("sms", "+15550101234")).contact_id == other.contact_id
    assert (await store.resolve_messaging_handle("imessage", "+15550101234")).contact_id == contact.contact_id
    assert await store.resolve_messaging_handle("rcs", "+15550101234") is None


@pytest.mark.asyncio
async def test_verified_transport_handle_wins_over_the_phone_key(store):
    """An owner correction can split a shared number: the exact verified handle decides first."""
    one = await store.create(display_name="One")
    two = await store.create(display_name="Two")
    await store.add_handle(one.contact_id, "sms", "+15550000021", verified=True)
    await store.correct_handle_identity(operation_id="split-1", performed_by=OWNER_ID, gateway="signal",
                                        address="+15550000021", expected_contact_id=None, contact_id=two.contact_id,
                                        evidence_refs=["turn:owner-said-two-people-share-it"])
    assert (await store.resolve_messaging_handle("sms", "+15550000021")).contact_id == one.contact_id
    assert (await store.resolve_messaging_handle("signal", "+15550000021")).contact_id == two.contact_id
    assert await store.resolve_messaging_handle("whatsapp", "+15550000021") is None  # ambiguous, never a guess


# -- C2: merge --------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_merge_moves_handles_through_correct_folds_history_and_reattributes(store):
    keep = await store.create(display_name="Keep", trust_tier="regular")
    drop = await store.create(display_name="Drop", trust_tier="unknown", tags=["vendor"], notes="met at the shop")
    await store.add_handle(keep.contact_id, "email", "keep@example.test")
    await store.add_handle(drop.contact_id, "sms", "+15550000010", source="auto:sender")
    await store.add_handle(drop.contact_id, "telegram", "4242", source="auto:sender")
    await store.record_interaction(keep.contact_id, "2026-08-01T10:00:00Z")
    await store.record_interaction(drop.contact_id, "2026-09-01T10:00:00Z")
    await store.record_interaction(drop.contact_id, "2026-09-02T10:00:00Z")
    await store.set_cadence(drop.contact_id, 120, by=OWNER_ID)
    await store.set_digest(keep.contact_id, "Keep digest", ["turn:k"])
    await store.set_digest(drop.contact_id, "Drop digest", ["turn:d"])
    await store.lower_may_contact(drop.contact_id, reason="STOP", source_ref="turn:s")
    candidate = await store.propose_handle_link(drop.contact_id, "email", "guess@example.test")
    db = store._require_db()
    await db.execute("UPDATE contacts SET first_seen_at = ? WHERE contact_id = ?", ("2026-01-01T00:00:00Z", drop.contact_id))
    await db.commit()
    calls = []

    async def hook(old_id, new_id):
        calls.append((old_id, new_id))
        return 1

    async def sources_of(contact_id):
        return ["turn:d1", "turn:d2"] if contact_id == drop.contact_id else []

    merged = await store.merge(keep.contact_id, drop.contact_id, performed_by=OWNER_ID,
                               reattribute=[hook], sources_of=sources_of)
    assert merged.contact_id == keep.contact_id
    assert {(h.gateway, h.address) for h in await store.get_handles(keep.contact_id)} == {
        ("email", "keep@example.test"), ("sms", "+15550000010"), ("telegram", "4242")}
    assert merged.last_interaction_at == "2026-09-02T10:00:00Z"
    assert merged.first_seen_at == "2026-01-01T00:00:00Z"
    assert merged.interaction_count == 3
    assert merged.cadence_minutes == 120
    assert merged.may_contact == "never"           # an opt-out survives a merge
    assert merged.trust_tier == "regular"
    # One digest, the keeper's, never two joined past the 600-character bound; tomorrow's writer re-renders it.
    assert merged.digest == "Keep digest" and merged.digest_sources == ["turn:k"]
    assert merged.tags == ["vendor"] and "met at the shop" in (merged.notes or "")
    assert await store.get(drop.contact_id) is None
    assert calls == [(drop.contact_id, keep.contact_id)]
    proposals = await store.list_handle_proposals()
    assert [(p["contact_id"], p["address"]) for p in proposals] == [(keep.contact_id, "guess@example.test")]
    assert proposals[0]["candidate_id"] == candidate["candidate_id"]
    assert (await store.resolve_messaging_handle("whatsapp", "+15550000010")).contact_id == keep.contact_id
    # The sources ride on one receipt of their own; the handle receipts carry none, so the host's
    # reconciliation moves each source exactly once.
    pending = await store.pending_identity_reconciliations()
    assert [op["operation_id"] for op in pending] == [f"merge:{drop.contact_id}:sources:0"]
    assert pending[0]["affected_source_ids"] == ["turn:d1", "turn:d2"]
    assert pending[0]["old_contact_id"] == drop.contact_id and pending[0]["contact_id"] == keep.contact_id
    evidence = await store.identity_evidence(keep.contact_id)
    moved = {op["operation_id"] for op in evidence["operations"]}
    assert {f"merge:{drop.contact_id}:{h.handle_id}" for h in await store.get_handles(keep.contact_id)
            if h.gateway != "email"} < moved
    keep_audit = {row["action"] for row in await store.get_audit_log(keep.contact_id)}
    drop_audit = {row["action"] for row in await store.get_audit_log(drop.contact_id)}
    assert "merged_in" in keep_audit and "identity_corrected" in keep_audit
    assert {"merged_into", "identity_corrected"} <= drop_audit
    with pytest.raises(ValueError):
        await store.merge(keep.contact_id, keep.contact_id, performed_by=OWNER_ID)
    with pytest.raises(ValueError):
        await store.merge(keep.contact_id, "cid-nope-1", performed_by=OWNER_ID)
    assert not hasattr(store, "merge_contacts")


@pytest.mark.asyncio
async def test_merge_sources_are_batched_once_each_and_a_stopped_merge_can_run_again(store):
    """Each source rides on exactly one receipt (<= 100 per receipt, the ledger correction's bound),
    and a merge that failed half way (a re-attribution hook raised) completes on the next run
    without recording any source twice or folding twice."""
    keep = await store.create(display_name="Keep")
    drop = await store.create(display_name="Drop")
    await store.add_handle(drop.contact_id, "sms", "+15550000031")
    await store.add_handle(drop.contact_id, "custom", "drop-31")
    await store.record_interaction(drop.contact_id, "2026-09-01T10:00:00Z")
    sources = [f"turn:{n:03d}" for n in range(230)]

    async def sources_of(contact_id):
        return sources if contact_id == drop.contact_id else []

    def broken(old_id, new_id):
        raise RuntimeError("affect store unavailable")

    with pytest.raises(RuntimeError):
        await store.merge(keep.contact_id, drop.contact_id, performed_by=OWNER_ID, reattribute=[broken],
                          sources_of=sources_of)
    assert (await store.get(drop.contact_id)) is not None           # nothing folded, nothing deleted
    assert (await store.get(keep.contact_id)).interaction_count == 0
    first = await store.pending_identity_reconciliations(limit=500)
    assert [len(op["affected_source_ids"]) for op in first] == [100, 100, 30]
    calls = []
    merged = await store.merge(keep.contact_id, drop.contact_id, performed_by=OWNER_ID,
                               reattribute=[lambda old_id, new_id: calls.append((old_id, new_id))],
                               sources_of=sources_of)
    assert calls == [(drop.contact_id, keep.contact_id)]
    assert merged.interaction_count == 1 and await store.get(drop.contact_id) is None
    receipts = await store.pending_identity_reconciliations(limit=500)
    assert [op["operation_id"] for op in receipts] == [op["operation_id"] for op in first]
    claimed = [sid for op in receipts for sid in op["affected_source_ids"]]
    assert sorted(claimed) == sources                                # every source once, none twice
    assert {(h.gateway, h.address) for h in await store.get_handles(keep.contact_id)} == {
        ("sms", "+15550000031"), ("custom", "drop-31")}


@pytest.mark.asyncio
async def test_merge_uses_the_hooks_the_server_set_on_the_store(store):
    keep = await store.create(display_name="Keep")
    drop = await store.create(display_name="Drop")
    await store.add_handle(drop.contact_id, "sms", "+15550000041")
    calls = []

    async def sources_of(contact_id):
        return ["turn:x"] if contact_id == drop.contact_id else []

    store.reattribute = [lambda old_id, new_id: calls.append(("comms", old_id, new_id))]
    store.sources_of = sources_of
    await store.merge(keep.contact_id, drop.contact_id, performed_by="cli")
    assert calls == [("comms", drop.contact_id, keep.contact_id)]
    pending, = await store.pending_identity_reconciliations()
    assert pending["affected_source_ids"] == ["turn:x"]


@pytest.mark.asyncio
async def test_merge_moves_group_memberships_and_takes_the_dropped_digest_only_when_the_keeper_has_none(store):
    """Audit m6: the person's group memberships follow them; a membership both records held is one."""
    keep = await store.create(display_name="Keep")
    drop = await store.create(display_name="Drop")
    await store.set_digest(drop.contact_id, "Drop digest", ["template"])
    shared = await store.create_scope(scope_type="group", platform="whatsapp", external_id="both")
    only = await store.create_scope(scope_type="group", platform="whatsapp", external_id="drop-only")
    for scope in (shared, only):
        await store.add_scope_member(scope.scope_id, drop.contact_id)
    await store.add_scope_member(shared.scope_id, keep.contact_id)
    merged = await store.merge(keep.contact_id, drop.contact_id, performed_by=OWNER_ID)
    assert merged.digest == "Drop digest" and merged.digest_sources == ["template"]
    for scope in (shared, only):
        members = [m.contact_id for m in await store.scope_members(scope.scope_id)]
        assert members == [keep.contact_id]


@pytest.mark.asyncio
async def test_a_merge_that_raced_another_folds_once(store):
    """Audit m6: two merges of the same pair both pass the existence check; the fold and the
    soft delete are one guarded write, so the second folds nothing and counts nothing twice."""
    keep = await store.create(display_name="Keep")
    drop = await store.create(display_name="Drop")
    await store.record_interaction(drop.contact_id, "2026-09-01T10:00:00Z")
    await store.record_interaction(keep.contact_id, "2026-09-02T10:00:00Z")
    raced = []

    async def other_merge(old_id, new_id):
        if not raced:
            raced.append(await store.merge(new_id, old_id, performed_by="cli", reattribute=[]))

    merged = await store.merge(keep.contact_id, drop.contact_id, performed_by=OWNER_ID, reattribute=[other_merge])
    assert raced[0].interaction_count == 2 and merged.interaction_count == 2
    audit = [row["action"] for row in await store.get_audit_log(keep.contact_id)]
    assert audit.count("merged_in") == 1


@pytest.mark.asyncio
async def test_merge_keeps_the_keepers_permission_when_neither_opted_out(store):
    keep = await store.create(display_name="Keep", may_contact="auto", cadence_minutes=30)
    drop = await store.create(display_name="Drop", may_contact="ask", cadence_minutes=999)
    merged = await store.merge(keep.contact_id, drop.contact_id, performed_by="cli")
    assert merged.may_contact == "auto" and merged.cadence_minutes == 30


# -- link proposals: exact links are automatic, a name is an owner ask ----------------------------

async def _name_candidate(store):
    target = await store.create(display_name="Robin", trust_tier="trusted")
    scope = await store.create_scope(scope_type="group", platform="whatsapp", external_id="group")
    await store.add_scope_member(scope.scope_id, target.contact_id, "member")
    resolver = ParticipantResolver(store)
    seen = await resolver.resolve(platform="whatsapp", user_id="robin@lid", display_name="Robin", group_id="group")
    assert seen.method == "shadow" and seen.candidate_contact_id == target.contact_id and seen.proposal_id
    return target, seen, resolver


@pytest.mark.asyncio
async def test_confirm_link_moves_the_handle_and_folds_the_shadow(store):
    target, seen, resolver = await _name_candidate(store)
    await store.record_interaction(seen.contact_id, "2026-09-01T10:00:00Z")
    result = await store.confirm_link(seen.proposal_id, performed_by=OWNER_ID)
    assert result["contact_id"] == target.contact_id
    assert (await resolver.resolve(platform="whatsapp", user_id="robin@lid")).contact_id == target.contact_id
    assert await store.get(seen.contact_id) is None
    assert (await store.get(target.contact_id)).last_interaction_at == "2026-09-01T10:00:00Z"
    assert await store.list_handle_proposals() == []
    with pytest.raises(ValueError):
        await store.confirm_link(seen.proposal_id, performed_by=OWNER_ID)


@pytest.mark.asyncio
async def test_reject_link_keeps_the_shadow_and_its_handle(store):
    target, seen, resolver = await _name_candidate(store)
    result = await store.reject_link(seen.proposal_id, performed_by=OWNER_ID)
    assert result["status"] == "rejected"
    assert (await resolver.resolve(platform="whatsapp", user_id="robin@lid")).contact_id == seen.contact_id
    assert await store.get_handles(target.contact_id) == []
    assert await store.list_handle_proposals() == []
    again = await store.propose_handle_link(target.contact_id, "whatsapp", "robin@lid")
    assert again["status"] == "rejected"
    with pytest.raises(ValueError):
        await store.reject_link("identity-candidate:nope", performed_by=OWNER_ID)


@pytest.mark.asyncio
async def test_confirm_link_for_a_free_handle_creates_it_verified(store):
    contact = await store.create(display_name="Casey")
    candidate = await store.propose_handle_link(contact.contact_id, "email", "Casey@Example.test",
                                                evidence_refs=["turn:owner-said"], source="owner")
    result = await store.confirm_link(candidate["candidate_id"], performed_by="cli")
    assert result["contact_id"] == contact.contact_id and result["old_contact_id"] is None
    handle, = await store.get_handles(contact.contact_id)
    assert handle.address == "casey@example.test" and handle.verified
