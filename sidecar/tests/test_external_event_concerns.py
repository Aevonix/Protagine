"""External cognition journal -> scoped Concern -> governed work spine."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest

from onekey import RequestAuthority
from protagine.cognition.external_events import (
    ExternalCognitionEventV1,
)
from protagine.self_model.event_concerns import (
    EventConcernReducer,
    ExternalEventConcernReducer,
    external_event_concern_mode,
    project_external_event,
)
from protagine.self_model.workspace import ConcernStore, WorkspaceEngine


NOW = datetime(2026, 7, 12, 20, 0, tzinfo=timezone.utc)


def _digest(value):
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class FakeJournal:
    def __init__(self, events=()):
        self.events = list(events)

    def current(self):
        return max((int(item["seq"]) for item in self.events), default=0)

    def replay(self, *, after_seq=0, limit=500, **_kwargs):
        selected = [
            item for item in self.events if int(item["seq"]) > int(after_seq)
        ]
        first = min((int(item["seq"]) for item in self.events), default=0)
        return {
            "events": selected[:limit],
            "hasMore": len(selected) > limit,
            "firstAvailableSeq": first,
            "journalLastSeq": self.current(),
            "corruptCount": 0,
        }


def _authority(
    *, principal="observer-main", viewer="person-owner", audiences=("owner",),
):
    return RequestAuthority(
        principal_id=principal,
        credential_id=f"credential-{principal}",
        scopes=frozenset({"cognition:events-ingest"}),
        viewer_person_id=viewer,
        person_ids=frozenset({viewer}),
        audiences=frozenset(audiences),
        authenticated=True,
        allow_unscoped_api=False,
    )


def _external(
    seq: int,
    *,
    kind="service_state",
    attributes=None,
    summary="Gateway health is degraded",
    principal="observer-main",
    viewer="person-owner",
    audiences=("owner",),
    external_id=None,
    occurred_at=None,
):
    if attributes is None:
        attributes = {"service": "gateway", "state": "degraded"}
    external_id = external_id or f"external-event-{seq:04d}"
    occurred = occurred_at or (NOW + timedelta(seconds=seq))
    if isinstance(occurred, datetime):
        occurred = occurred.isoformat()
    item = ExternalCognitionEventV1.from_authority(
        {
            "event_id": external_id,
            "kind": kind,
            "occurred_at": occurred,
            "summary": summary,
            "attributes": attributes,
        },
        authority=_authority(
            principal=principal, viewer=viewer, audiences=audiences,
        ),
        now=NOW + timedelta(minutes=5),
    )
    return {
        "seq": seq,
        "ulid": f"journal-external-{seq:04d}",
        "type": f"cognition.external.{kind}",
        "occurredAt": item.occurred_at,
        "recordedAt": (NOW + timedelta(minutes=1, seconds=seq)).isoformat(),
        "data": item.journal_payload(),
    }


def _reducer(store, journal):
    return ExternalEventConcernReducer(
        store,
        replay_fn=journal.replay,
        current_sequence_fn=journal.current,
    )


@pytest.fixture(autouse=True)
def external_env(monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_PERSON_ID", "person-owner")
    monkeypatch.setenv("PROTAGINE_WORKSPACE", "live")
    monkeypatch.setenv("PROTAGINE_EVENT_CONCERNS", "live")
    monkeypatch.setenv("PROTAGINE_EVENT_CONCERNS_BOOTSTRAP", "replay")
    monkeypatch.setenv("PROTAGINE_COGNITION_SPINE", "live")
    monkeypatch.delenv("PROTAGINE_EXTERNAL_EVENT_CONCERNS", raising=False)
    monkeypatch.delenv("PROTAGINE_EXTERNAL_EVENT_CONCERNS_GAP_POLICY", raising=False)


def test_external_concern_flag_defaults_and_invalid_values_off(monkeypatch):
    assert external_event_concern_mode() == "off"
    monkeypatch.setenv("PROTAGINE_EXTERNAL_EVENT_CONCERNS", "unexpected")
    assert external_event_concern_mode() == "off"
    monkeypatch.setenv("PROTAGINE_EXTERNAL_EVENT_CONCERNS", "shadow")
    assert external_event_concern_mode() == "shadow"


def test_off_to_live_replays_retained_events_and_restart_is_exactly_once(
    tmp_path, monkeypatch,
):
    journal = FakeJournal([
        {
            "seq": 1,
            "ulid": "ordinary-event-1",
            "type": "conversation.turn",
            "occurredAt": NOW.isoformat(),
            "recordedAt": NOW.isoformat(),
            "data": {"content": "not an external concern"},
        },
        _external(2),
    ])
    store = ConcernStore(str(tmp_path / "workspace.db"))
    reducer = _reducer(store, journal)

    assert reducer.run_once() == {"enabled": False, "processed": 0}
    assert store.event_cursor(reducer.consumer_id) is None

    monkeypatch.setenv("PROTAGINE_EXTERNAL_EVENT_CONCERNS", "live")
    first = reducer.run_once()
    assert first["cursor"] == 2
    assert first["dispositions"] == {"skipped": 1, "created": 1}
    item = store.active()[0]

    restarted = _reducer(store, journal)
    assert restarted.run_once()["processed"] == 0
    assert store.active()[0].concern_id == item.concern_id
    assert restarted.status()["consumer_id"] != "workspace-concerns-v1"


def test_normal_and_external_reducers_have_independent_durable_cursors(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PROTAGINE_EXTERNAL_EVENT_CONCERNS", "live")
    journal = FakeJournal([_external(1)])
    store = ConcernStore(str(tmp_path / "workspace.db"))
    normal = EventConcernReducer(
        store, replay_fn=journal.replay, current_sequence_fn=journal.current,
    )
    external = _reducer(store, journal)

    assert normal.run_once()["dispositions"] == {"skipped": 1}
    assert store.active() == []
    assert external.run_once()["dispositions"] == {"created": 1}
    assert len(store.active()) == 1
    assert store.event_cursor(normal.consumer_id) == 1
    assert store.event_cursor(external.consumer_id) == 1
    assert normal.consumer_id != external.consumer_id


@pytest.mark.parametrize(
    "mutate",
    [
        lambda raw: raw.update(type="cognition.external.delivery_outcome"),
        lambda raw: raw.update(type="Cognition.External.Service_State"),
        lambda raw: raw["data"].update(kind="delivery_outcome"),
        lambda raw: raw["data"].update(schema="ExternalCognitionJournalProjectionV0"),
        lambda raw: raw["data"].update(version=True),
        lambda raw: raw["data"].update(boundary_attested=True),
        lambda raw: raw["data"].update(evidence_status="verified"),
        lambda raw: raw["data"].update(scope_digest="0" * 64),
        lambda raw: raw["data"].update(viewer_person_id="person-forged"),
        lambda raw: raw["data"].update(viewer_scope="public"),
        lambda raw: raw["data"].update(audience_scope=["global"]),
        lambda raw: raw["data"].update(external_event_digest="not-a-digest"),
        lambda raw: raw["data"].update(external_event_id="short"),
        lambda raw: raw["data"].update(producer_revision="forged-revision"),
        lambda raw: raw["data"].update(attributes={
            "service": "gateway", "state": "green",
        }),
        lambda raw: raw["data"].update(attributes={
            "service": "gateway", "state": "degraded", "unknown": True,
        }),
        lambda raw: raw.update(occurredAt="2026-07-12T20:00:01"),
    ],
)
def test_projection_rejects_forged_schema_scope_boundary_and_digests(mutate):
    raw = deepcopy(_external(1))
    mutate(raw)
    with pytest.raises(ValueError):
        project_external_event(raw)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kind", "SERVICE_STATE"),
        ("kind", " service_state "),
        ("external_event_id", 12345678),
        ("external_event_digest", int("1" * 64)),
        ("producer_principal_id", 12345678),
        ("producer_principal_id", "p" * 129),
    ],
)
def test_projection_rejects_noncanonical_or_coerced_provenance(field, value):
    raw = deepcopy(_external(1))
    raw["data"][field] = value

    with pytest.raises(ValueError):
        project_external_event(raw)


@pytest.mark.parametrize(
    ("length", "accepted"),
    [(128, True), (129, False), (192, False), (256, False)],
)
def test_external_concern_v2_envelope_has_exact_host_event_id_bound(
    length, accepted,
):
    raw = deepcopy(_external(1))
    raw["ulid"] = "j" * length

    if accepted:
        projection, reason, _digest_value = project_external_event(raw)
        assert projection is not None
        assert reason == ""
    else:
        with pytest.raises(ValueError, match="journal ID is not canonical"):
            project_external_event(raw)


def test_projection_rejects_integer_coercion_for_subject_and_viewer():
    raw = deepcopy(_external(
        1, principal="observer-a", viewer="person-a", audiences=(),
    ))
    raw["data"].update({
        "subject_person_id": 12345678,
        "viewer_person_id": 12345678,
        "viewer_scope": "person:12345678",
    })
    raw["data"]["scope_digest"] = _digest({
        "schema": "ExternalCognitionScopeV1",
        "version": 1,
        "subject_person_id": "12345678",
        "viewer_person_id": "12345678",
        "viewer_scope": "person:12345678",
        "shareability": "subject_private",
        "audience_scope": [],
    })

    with pytest.raises(ValueError):
        project_external_event(raw)


def test_projection_binds_host_event_time_to_server_projection():
    raw = _external(
        1,
        occurred_at=NOW + timedelta(seconds=10),
    )
    original = raw["occurredAt"]
    raw["occurredAt"] = (NOW + timedelta(seconds=40)).isoformat()

    with pytest.raises(ValueError):
        project_external_event(raw)

    # The exact server projection must carry the canonical time used above;
    # downstream ordering may never depend on an unbound host wrapper field.
    assert raw["data"]["external_occurred_at"] == original


def test_projection_rejects_noncanonical_configured_owner(monkeypatch):
    raw = _external(1)
    monkeypatch.setenv("PROTAGINE_OWNER_PERSON_ID", "person-owner ")

    with pytest.raises(ValueError, match="authority identifier boundary"):
        project_external_event(raw)


def test_unbound_late_terminal_cannot_close_a_newer_external_episode(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PROTAGINE_EXTERNAL_EVENT_CONCERNS", "live")
    delayed_terminal = _external(
        3,
        attributes={"service": "gateway", "state": "healthy"},
        summary="Older gateway recovery arrived late",
        external_id="older-terminal-with-mutated-wrapper",
        occurred_at=NOW + timedelta(seconds=20),
    )
    delayed_terminal["occurredAt"] = (NOW + timedelta(seconds=40)).isoformat()
    journal = FakeJournal([
        _external(
            1,
            attributes={"service": "gateway", "state": "degraded"},
            summary="Gateway first degraded",
            external_id="episode-negative-one",
            occurred_at=NOW + timedelta(seconds=10),
        ),
        _external(
            2,
            attributes={"service": "gateway", "state": "offline"},
            summary="Gateway newer offline report",
            external_id="episode-negative-two",
            occurred_at=NOW + timedelta(seconds=30),
        ),
        delayed_terminal,
    ])
    store = ConcernStore(str(tmp_path / "workspace.db"))
    reducer = _reducer(store, journal)

    result = reducer.run_once()

    assert result["dispositions"] == {"created": 1, "updated": 1, "skipped": 1}
    assert result["cursor"] == 3
    assert len(store.active()) == 1
    assert store.active()[0].summary == "Gateway newer offline report"
    with store._lock:
        watermark = store._conn.execute(
            "SELECT occurred_at,event_seq,operation FROM "
            "concern_external_event_watermarks",
        ).fetchone()
        receipt = store._conn.execute(
            "SELECT disposition,reason FROM concern_event_receipts "
            "WHERE consumer_id=? AND event_seq=3",
            (reducer.consumer_id,),
        ).fetchone()
    assert tuple(watermark) == (
        (NOW + timedelta(seconds=30)).isoformat(), 2, "upsert",
    )
    assert tuple(receipt) == ("skipped", "malformed_event:ValueError")


@pytest.mark.parametrize("sequence", ["1", 1.0, True])
def test_projection_requires_exact_nonboolean_integer_journal_sequence(sequence):
    raw = deepcopy(_external(1))
    raw["seq"] = sequence

    with pytest.raises(ValueError):
        project_external_event(raw)


def test_projection_requires_exact_audience_scope_string_elements():
    class StringSubclass(str):
        pass

    raw = deepcopy(_external(1))
    raw["data"]["audience_scope"] = [StringSubclass("owner")]

    with pytest.raises(ValueError):
        project_external_event(raw)


def test_forged_projection_becomes_fixed_skip_receipt(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_EXTERNAL_EVENT_CONCERNS", "live")
    forged = _external(1)
    forged["data"]["scope_digest"] = "0" * 64
    journal = FakeJournal([forged])
    store = ConcernStore(str(tmp_path / "workspace.db"))
    reducer = _reducer(store, journal)

    result = reducer.run_once()

    assert result["dispositions"] == {"skipped": 1}
    assert result["cursor"] == 1
    assert store.active() == []
    assert _reducer(store, journal).run_once()["processed"] == 0
    with store._lock:
        receipt = store._conn.execute(
            "SELECT disposition,reason FROM concern_event_receipts "
            "WHERE consumer_id=?", (reducer.consumer_id,),
        ).fetchone()
    assert tuple(receipt) == ("skipped", "malformed_event:ValueError")


def test_recomputed_guest_scope_cannot_forge_owner_private_lane():
    raw = _external(
        1, principal="observer-a", viewer="person-a", audiences=(),
    )
    raw["data"].update({
        "viewer_scope": "owner",
        "shareability": "owner_private",
        "audience_scope": ["owner"],
    })
    raw["data"]["scope_digest"] = _digest({
        "schema": "ExternalCognitionScopeV1",
        "version": 1,
        "subject_person_id": "person-a",
        "viewer_person_id": "person-a",
        "viewer_scope": "owner",
        "shareability": "owner_private",
        "audience_scope": ["owner"],
    })

    with pytest.raises(ValueError, match="owner lane subject"):
        project_external_event(raw)


def test_maximum_length_external_ids_remain_exact_bounded_source_refs(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PROTAGINE_EXTERNAL_EVENT_CONCERNS", "live")
    external_id = "e" * 192
    entity = "s" * 192
    principal = "p" * 128
    journal = FakeJournal([_external(
        1,
        attributes={"service": entity, "state": "degraded"},
        external_id=external_id,
        principal=principal,
    )])
    store = ConcernStore(str(tmp_path / "workspace.db"))

    _reducer(store, journal).run_once()

    sources = store.active()[0].sources
    assert f"xevent:{external_id}" in sources
    assert f"xentity:{entity}" in sources
    assert f"external_producer:{principal}" in sources
    assert max(map(len, sources)) <= 200


def test_recovery_resolves_only_exact_subject_producer_kind_and_entity(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PROTAGINE_EXTERNAL_EVENT_CONCERNS", "live")
    journal = FakeJournal([
        _external(
            1, principal="observer-a", viewer="person-a", audiences=(),
            external_id="external-a-degraded",
        ),
        _external(
            2, principal="observer-b", viewer="person-a", audiences=(),
            external_id="external-producer-b-degraded",
        ),
        _external(
            3, principal="observer-a", viewer="person-b", audiences=(),
            external_id="external-subject-b-degraded",
        ),
        _external(
            4,
            attributes={"service": "gateway-secondary", "state": "degraded"},
            principal="observer-a", viewer="person-a", audiences=(),
            external_id="external-entity-secondary",
        ),
        _external(
            5,
            kind="action_outcome",
            attributes={"action_id": "gateway", "outcome": "failed"},
            summary="Reported gateway action failed",
            principal="observer-a", viewer="person-a", audiences=(),
            external_id="external-kind-action",
        ),
        _external(
            6,
            attributes={"service": "gateway", "state": "healthy"},
            summary="Gateway health recovered",
            principal="observer-a",
            viewer="person-a",
            audiences=(),
            external_id="external-a-healthy",
        ),
    ])
    store = ConcernStore(str(tmp_path / "workspace.db"))

    result = _reducer(store, journal).run_once()

    assert result["dispositions"] == {"created": 5, "resolved": 1}
    active = store.active()
    assert len(active) == 4
    assert len({item.dedup_key for item in active}) == 4
    assert any(
        "external_producer:observer-b" in item.sources
        and item.subject_person_id == "person-a"
        and "external_kind:service_state" in item.sources
        and "xentity:gateway" in item.sources
        for item in active
    )
    assert any(
        item.subject_person_id == "person-b"
        and "external_producer:observer-a" in item.sources
        and "xentity:gateway" in item.sources
        for item in active
    )
    assert any(
        "xentity:gateway-secondary" in item.sources
        for item in active
    )
    assert any(
        "external_kind:action_outcome" in item.sources
        and "xentity:gateway" in item.sources
        for item in active
    )
    assert len(store.active_for_viewer(
        viewer_person_id="person-a", owner_person_id="person-owner", limit=10,
    )) == 3
    assert [item.subject_person_id for item in store.active_for_viewer(
        viewer_person_id="person-b", owner_person_id="person-owner", limit=10,
    )] == ["person-b"]


def test_cancelled_action_is_terminal_for_only_its_external_concern(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PROTAGINE_EXTERNAL_EVENT_CONCERNS", "live")
    journal = FakeJournal([
        _external(
            1,
            kind="action_outcome",
            attributes={"action_id": "action-cancel-1", "outcome": "blocked"},
            summary="Reported action is blocked",
        ),
        _external(
            2,
            kind="action_outcome",
            attributes={"action_id": "action-cancel-2", "outcome": "blocked"},
            summary="Other reported action is blocked",
        ),
        _external(
            3,
            kind="action_outcome",
            attributes={"action_id": "action-cancel-1", "outcome": "cancelled"},
            summary="Reported action was cancelled",
        ),
    ])
    store = ConcernStore(str(tmp_path / "workspace.db"))

    result = _reducer(store, journal).run_once()

    assert result["dispositions"] == {"created": 2, "resolved": 1}
    assert len(store.active()) == 1
    assert "xentity:action-cancel-2" in store.active()[0].sources


@pytest.mark.parametrize(
    ("kind", "entity", "open_attributes", "terminal_attributes"),
    [
        (
            "service_state",
            "service-terminal",
            {"service": "service-terminal", "state": "offline"},
            {"service": "service-terminal", "state": "healthy"},
        ),
        (
            "delivery_outcome",
            "delivery-terminal",
            {"delivery_ref": "delivery-terminal", "outcome": "failed"},
            {"delivery_ref": "delivery-terminal", "outcome": "delivered"},
        ),
        (
            "approval_state",
            "approval-terminal",
            {"request_id": "approval-terminal", "state": "pending"},
            {"request_id": "approval-terminal", "state": "approved"},
        ),
    ],
)
def test_positive_terminal_reports_only_resolve_matching_external_concern(
    tmp_path, monkeypatch, kind, entity, open_attributes, terminal_attributes,
):
    monkeypatch.setenv("PROTAGINE_EXTERNAL_EVENT_CONCERNS", "live")
    journal = FakeJournal([
        _external(
            1, kind=kind, attributes=open_attributes,
            summary=f"Reported open state for {entity}",
            external_id=f"external-open-{entity}",
        ),
        _external(
            2, kind=kind, attributes=terminal_attributes,
            summary=f"Reported terminal state for {entity}",
            external_id=f"external-terminal-{entity}",
        ),
    ])
    store = ConcernStore(str(tmp_path / "workspace.db"))

    result = _reducer(store, journal).run_once()

    assert result["dispositions"] == {"created": 1, "resolved": 1}
    assert store.active() == []


def test_newer_external_negative_reopens_immediately_after_terminal_state(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PROTAGINE_EXTERNAL_EVENT_CONCERNS", "live")
    journal = FakeJournal([_external(
        1,
        attributes={"service": "gateway", "state": "degraded"},
        external_id="episode-one-negative",
        occurred_at=NOW + timedelta(seconds=10),
    )])
    store = ConcernStore(str(tmp_path / "workspace.db"))
    reducer = _reducer(store, journal)

    assert reducer.run_once()["dispositions"] == {"created": 1}
    first_id = store.active()[0].concern_id
    journal.events.append(_external(
        2,
        attributes={"service": "gateway", "state": "healthy"},
        summary="Gateway recovered",
        external_id="episode-one-terminal",
        occurred_at=NOW + timedelta(seconds=20),
    ))
    assert reducer.run_once()["dispositions"] == {"resolved": 1}
    assert store.active() == []
    journal.events.append(_external(
        3,
        attributes={"service": "gateway", "state": "offline"},
        summary="Gateway is offline again",
        external_id="episode-two-negative",
        occurred_at=NOW + timedelta(seconds=30),
    ))

    reopened = reducer.run_once()

    assert reopened["dispositions"] == {"reopened": 1}
    assert len(store.active()) == 1
    assert store.active()[0].concern_id != first_id
    assert store.active()[0].summary == "Gateway is offline again"


@pytest.mark.parametrize(
    (
        "states", "event_seconds", "expected_dispositions", "active",
        "last_disposition", "last_reason", "watermark_seq", "watermark_operation",
    ),
    [
        (
            ("degraded", "healthy", "offline"), (10, 30, 20),
            {"created": 1, "resolved": 1, "external_stale_event": 1},
            False, "external_stale_event",
            "external_event_time_older_than_watermark", 2, "resolve",
        ),
        (
            ("degraded", "healthy"), (30, 20),
            {"created": 1, "external_stale_event": 1},
            True, "external_stale_event",
            "external_event_time_older_than_watermark", 1, "upsert",
        ),
        (
            ("healthy", "degraded"), (30, 20),
            {"resolve_noop": 1, "external_stale_event": 1},
            False, "external_stale_event",
            "external_event_time_older_than_watermark", 1, "resolve",
        ),
        (
            ("degraded", "healthy"), (30, 30),
            {"created": 1, "external_event_time_conflict": 1},
            True, "external_event_time_conflict",
            "external_event_time_equal_to_watermark_conflict", 1, "upsert",
        ),
    ],
    ids=(
        "delayed-negative-after-terminal",
        "delayed-terminal-after-negative",
        "terminal-first-then-older-negative",
        "equal-time-conflicting-state",
    ),
)
def test_external_event_time_watermark_prevents_stale_or_conflicting_mutation(
    tmp_path, monkeypatch, states, event_seconds, expected_dispositions, active,
    last_disposition, last_reason, watermark_seq, watermark_operation,
):
    monkeypatch.setenv("PROTAGINE_EXTERNAL_EVENT_CONCERNS", "live")
    journal = FakeJournal([
        _external(
            seq,
            attributes={"service": "gateway", "state": state},
            summary=f"Gateway reports {state}",
            external_id=f"ordered-state-{seq}-{state}",
            occurred_at=NOW + timedelta(seconds=seconds),
        )
        for seq, (state, seconds) in enumerate(
            zip(states, event_seconds), start=1,
        )
    ])
    store = ConcernStore(str(tmp_path / "workspace.db"))
    reducer = _reducer(store, journal)

    result = reducer.run_once()

    assert result["dispositions"] == expected_dispositions
    assert result["cursor"] == len(states)
    assert store.event_cursor(reducer.consumer_id) == len(states)
    assert bool(store.active()) is active
    with store._lock:
        receipts = store._conn.execute(
            "SELECT disposition,reason FROM concern_event_receipts "
            "WHERE consumer_id=? ORDER BY event_seq",
            (reducer.consumer_id,),
        ).fetchall()
        watermark = store._conn.execute(
            "SELECT occurred_at,operation,event_id,event_seq "
            "FROM concern_external_event_watermarks",
        ).fetchone()
    assert tuple(receipts[-1]) == (last_disposition, last_reason)
    assert watermark["occurred_at"] == (
        NOW + timedelta(seconds=event_seconds[watermark_seq - 1])
    ).isoformat()
    assert watermark["operation"] == watermark_operation
    assert watermark["event_id"] == f"journal-external-{watermark_seq:04d}"
    assert watermark["event_seq"] == watermark_seq
    if last_disposition == "external_event_time_conflict":
        assert store.active()[0].last_material_event_seq == watermark_seq
    status = reducer.status()
    assert status["dispositions"][last_disposition]["count"] == 1
    assert status["event_time_watermarks"]["count"] == 1


def test_external_event_time_watermark_and_receipt_survive_exact_replay(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PROTAGINE_EXTERNAL_EVENT_CONCERNS", "live")
    path = tmp_path / "workspace.db"
    journal = FakeJournal([_external(
        1,
        attributes={"service": "gateway", "state": "degraded"},
        external_id="durable-watermark-negative",
        occurred_at=NOW + timedelta(seconds=45),
    )])
    first_store = ConcernStore(str(path))
    first = _reducer(first_store, journal)
    assert first.run_once()["dispositions"] == {"created": 1}
    concern_id = first_store.active()[0].concern_id

    reopened_store = ConcernStore(str(path))
    replayed = _reducer(reopened_store, journal)

    assert replayed.run_once()["processed"] == 0
    assert reopened_store.active()[0].concern_id == concern_id
    with reopened_store._lock:
        assert reopened_store._conn.execute(
            "SELECT COUNT(*) FROM concern_event_receipts WHERE consumer_id=?",
            (replayed.consumer_id,),
        ).fetchone()[0] == 1
        assert reopened_store._conn.execute(
            "SELECT COUNT(*) FROM concern_external_event_watermarks",
        ).fetchone()[0] == 1
        primary_key = [
            row["name"]
            for row in sorted(
                reopened_store._conn.execute(
                    "PRAGMA table_info(concern_external_event_watermarks)",
                ).fetchall(),
                key=lambda row: int(row["pk"] or 99),
            )
            if int(row["pk"] or 0) > 0
        ]
    assert primary_key == ["consumer_id", "dedup_key"]


def test_generic_event_concern_resolved_ttl_behavior_is_unchanged(tmp_path):
    def ordinary(seq, event_type, reason):
        return {
            "seq": seq,
            "ulid": f"ordinary-service-{seq}",
            "type": event_type,
            "occurredAt": (NOW + timedelta(seconds=seq)).isoformat(),
            "recordedAt": (NOW + timedelta(minutes=1, seconds=seq)).isoformat(),
            "data": {
                "service_id": "ordinary-gateway",
                "reason": reason,
            },
        }

    journal = FakeJournal([
        ordinary(1, "service.degraded", "Gateway degraded"),
        ordinary(2, "service.recovered", "Gateway recovered"),
        ordinary(3, "service.degraded", "Gateway degraded again"),
    ])
    store = ConcernStore(str(tmp_path / "workspace.db"))
    reducer = EventConcernReducer(
        store,
        replay_fn=journal.replay,
        current_sequence_fn=journal.current,
    )

    result = reducer.run_once()

    assert result["dispositions"] == {
        "created": 1, "resolved": 1, "suppressed_resolved": 1,
    }
    assert store.active() == []


def test_workspace_status_exposes_both_independent_reducers(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_EXTERNAL_EVENT_CONCERNS", "shadow")
    store = ConcernStore(str(tmp_path / "workspace.db"))
    journal = FakeJournal()
    normal = EventConcernReducer(
        store, replay_fn=journal.replay, current_sequence_fn=journal.current,
    )
    external = _reducer(store, journal)
    workspace = WorkspaceEngine(
        store,
        event_reducer=normal,
        external_event_reducer=external,
    )

    status = workspace.snapshot()

    assert status["event_reducer"]["consumer_id"] == normal.consumer_id
    assert status["external_event_reducer"]["consumer_id"] == external.consumer_id
    assert status["external_event_reducer"]["mode"] == "shadow"


