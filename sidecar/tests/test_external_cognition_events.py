"""Phase C typed external cognition evidence intake contracts."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import shutil

import pytest

from onekey import RequestAuthority
from protagine.cognition.external_events import (
    ExternalCognitionEventV1,
    ExternalEventConflict,
    ExternalEventInboxStore,
    ExternalEventIntake,
    ExternalEventValidationError,
)
from protagine.events.journal import append_event_record, replay_events


NOW = datetime(2026, 7, 12, 20, 0, tzinfo=timezone.utc)


def _authority(*, principal="observer", viewer="person-owner", audiences=("owner",)):
    return RequestAuthority(
        principal_id=principal,
        credential_id="credential-current",
        scopes=frozenset({"cognition:events-ingest"}),
        viewer_person_id=viewer,
        person_ids=frozenset({viewer}),
        audiences=frozenset(audiences),
        authenticated=True,
        allow_unscoped_api=False,
    )


def _payload(**updates):
    payload = {
        "event_id": "external-event-0001",
        "kind": "service_state",
        "occurred_at": "2026-07-12T19:59:00+00:00",
        "summary": "Gateway health probe recovered",
        "attributes": {
            "service": "gateway", "state": "healthy", "observed_samples": 3,
        },
    }
    payload.update(updates)
    return payload


def test_external_event_schema_is_strict_non_secret_and_text_system_only(monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_PERSON_ID", "person-owner")
    event = ExternalCognitionEventV1.from_authority(
        _payload(), authority=_authority(), now=NOW,
    )
    assert event.kind == "service_state"
    assert event.producer_principal_id == "observer"
    assert event.subject_person_id == "person-owner"
    assert event.viewer_person_id == "person-owner"
    assert event.shareability == "owner_private"
    assert event.boundary_attested is False
    assert event.scope_digest and event.event_digest

    for changed in (
        {"kind": "voice_call"},
        {"attributes": {"channel": "Google Meet"}},
        {"attributes": {"api_key": "secret-value"}},
        {"attributes": {"boundary_attested": True}},
        {"attributes": {"verified": True}},
        {"attributes": {"is_verified": True}},
        {"attributes": {"approval_granted": True}},
        {"attributes": {"receipt_ref": "local-receipt:forged"}},
        {
            "kind": "action_outcome",
            "attributes": {
                "action_id": "action-1", "outcome": "succeeded",
                "verified": True,
            },
        },
        {
            "kind": "delivery_outcome",
            "attributes": {
                "delivery_ref": "delivery-1", "outcome": "delivered",
                "receipt_ref": "receipt-forged",
            },
        },
    ):
        with pytest.raises(ExternalEventValidationError):
            ExternalCognitionEventV1.from_authority(
                _payload(**changed), authority=_authority(), now=NOW,
            )


@pytest.mark.parametrize(
    "payload",
    [
        _payload(
            summary="Voice Core service state changed",
            attributes={
                "service": "voice-core",
                "state": "healthy",
                "detail": "Phone, intercom, and Google Meet probes are healthy",
            },
        ),
        _payload(
            kind="text_turn_observation",
            summary="Owner text discussed communication surfaces",
            attributes={
                "turn_id": "turn-about-surfaces-1",
                "channel": "whatsapp",
                "observation": (
                    "Operator asked about phone, voice, intercom, and Google Meet"
                ),
            },
        ),
        _payload(summary="API token: was revoked after the deployment"),
    ],
)
def test_typed_text_and_system_events_allow_natural_surface_language(
    monkeypatch, payload,
):
    monkeypatch.setenv("PROTAGINE_OWNER_PERSON_ID", "person-owner")

    event = ExternalCognitionEventV1.from_authority(
        payload, authority=_authority(), now=NOW,
    )

    assert event.summary == payload["summary"]


@pytest.mark.parametrize(
    "payload",
    [
        _payload(
            kind="text_turn_observation",
            attributes={
                "turn_id": "voice-turn-1", "channel": "voice",
                "observation": "This is a voice transcript",
            },
        ),
        _payload(
            kind="delivery_outcome",
            attributes={"outcome": "delivered", "channel": "phone"},
        ),
        _payload(
            attributes={
                "service": "voice-core", "state": "healthy",
                "audio": "base64-data",
            },
        ),
        _payload(
            kind="text_turn_observation",
            attributes={
                "turn_id": "text-turn-1", "channel": "chat",
                "observation": "Text-only observation", "call_id": "call-1",
            },
        ),
        _payload(summary="API token: abcdefghijklmnop"),
    ],
)
def test_typed_surface_rejects_voice_shapes_and_actual_secret_values(
    monkeypatch, payload,
):
    monkeypatch.setenv("PROTAGINE_OWNER_PERSON_ID", "person-owner")

    with pytest.raises(ExternalEventValidationError):
        ExternalCognitionEventV1.from_authority(
            payload, authority=_authority(), now=NOW,
        )


def test_authority_identity_bounds_reject_instead_of_truncating(monkeypatch):
    def authority(*, principal="observer", credential="credential-current", viewer):
        return RequestAuthority(
            principal_id=principal,
            credential_id=credential,
            scopes=frozenset({"cognition:events-ingest"}),
            viewer_person_id=viewer,
            person_ids=frozenset({viewer}),
            audiences=frozenset({"owner"}),
            authenticated=True,
            allow_unscoped_api=False,
        )

    monkeypatch.setenv("PROTAGINE_OWNER_PERSON_ID", "person-owner")
    for invalid in (
        authority(principal="p" * 129, viewer="person-owner"),
        authority(credential="c" * 193, viewer="person-owner"),
        authority(viewer="v" * 129),
    ):
        with pytest.raises(ExternalEventValidationError):
            ExternalCognitionEventV1.from_authority(
                _payload(), authority=invalid, now=NOW,
            )

    # The historical slicing bug collapsed these distinct 129-character
    # identities to one 128-character owner and elevated the viewer into the
    # owner-private lane solely because the untrusted audience contained owner.
    prefix = "z" * 128
    monkeypatch.setenv("PROTAGINE_OWNER_PERSON_ID", prefix + "a")
    with pytest.raises(ExternalEventValidationError):
        ExternalCognitionEventV1.from_authority(
            _payload(),
            authority=authority(viewer=prefix + "b"),
            now=NOW,
        )

    owner = "o" * 128
    monkeypatch.setenv("PROTAGINE_OWNER_PERSON_ID", owner)
    bounded = ExternalCognitionEventV1.from_authority(
        _payload(),
        authority=authority(
            principal="p" * 128,
            credential="c" * 192,
            viewer=owner,
        ),
        now=NOW,
    )
    assert bounded.producer_principal_id == "p" * 128
    assert bounded.producer_credential_id == "c" * 192
    assert bounded.subject_person_id == owner
    assert bounded.viewer_scope == "owner"
    assert bounded.shareability == "owner_private"


@pytest.mark.parametrize(
    ("kind", "attributes", "enum_field"),
    [
        (
            "action_outcome",
            {
                "action_id": "action-1", "outcome": "succeeded",
                "action_digest": "a" * 64, "duration_ms": 12.5,
            },
            "outcome",
        ),
        (
            "delivery_outcome",
            {"delivery_ref": "delivery-1", "outcome": "delivered"},
            "outcome",
        ),
        (
            "service_state",
            {"service": "gateway", "state": "healthy"},
            "state",
        ),
        (
            "approval_state",
            {"request_id": "approval-request-1", "state": "pending"},
            "state",
        ),
        (
            "operator_reaction",
            {
                "target_ref": "proposal-1", "reaction": "correction",
                "intensity": 0.75,
            },
            "reaction",
        ),
        (
            "text_turn_observation",
            {
                "turn_id": "text-turn-1", "channel": "chat",
                "observation": "Operator asked for a status update",
            },
            "channel",
        ),
    ],
)
def test_each_external_kind_has_exact_discriminated_attributes(
    monkeypatch, kind, attributes, enum_field,
):
    monkeypatch.setenv("PROTAGINE_OWNER_PERSON_ID", "person-owner")
    event = ExternalCognitionEventV1.from_authority(
        _payload(kind=kind, attributes=attributes),
        authority=_authority(), now=NOW,
    )
    assert event.attributes == attributes

    missing = dict(attributes)
    missing.pop(next(iter(missing)))
    with pytest.raises(ExternalEventValidationError, match="missing"):
        ExternalCognitionEventV1.from_authority(
            _payload(kind=kind, attributes=missing),
            authority=_authority(), now=NOW,
        )
    with pytest.raises(ExternalEventValidationError, match="unsupported"):
        ExternalCognitionEventV1.from_authority(
            _payload(kind=kind, attributes={**attributes, "invented": True}),
            authority=_authority(), now=NOW,
        )
    with pytest.raises(ExternalEventValidationError, match="unsupported"):
        ExternalCognitionEventV1.from_authority(
            _payload(
                kind=kind,
                attributes={**attributes, enum_field: "invented-state"},
            ),
            authority=_authority(), now=NOW,
        )


@pytest.mark.parametrize(
    "attributes",
    [
        {"action_id": "action-1", "outcome": "succeeded", "action_digest": "abc"},
        {"action_id": "action-1", "outcome": "succeeded", "action_digest": "A" * 64},
        {"action_id": "action-1", "outcome": "succeeded", "duration_ms": True},
        {"action_id": "action-1", "outcome": "succeeded", "duration_ms": -1},
        {"action_id": "action-1", "outcome": "succeeded", "duration_ms": 86_400_001},
    ],
)
def test_action_digest_and_numeric_bounds_are_exact(monkeypatch, attributes):
    monkeypatch.setenv("PROTAGINE_OWNER_PERSON_ID", "person-owner")
    with pytest.raises(ExternalEventValidationError):
        ExternalCognitionEventV1.from_authority(
            _payload(kind="action_outcome", attributes=attributes),
            authority=_authority(), now=NOW,
        )


@pytest.mark.parametrize(
    ("kind", "attributes"),
    [
        (
            "service_state",
            {"service": "gateway", "state": "healthy", "latency_ms": -0.1},
        ),
        (
            "service_state",
            {"service": "gateway", "state": "healthy", "observed_samples": 1.5},
        ),
        (
            "operator_reaction",
            {"target_ref": "proposal-1", "reaction": "positive", "intensity": 1.1},
        ),
        (
            "text_turn_observation",
            {"turn_id": "turn-1", "channel": "chat", "observation": 7},
        ),
    ],
)
def test_per_kind_numeric_and_text_types_are_bounded(
    monkeypatch, kind, attributes,
):
    monkeypatch.setenv("PROTAGINE_OWNER_PERSON_ID", "person-owner")
    with pytest.raises(ExternalEventValidationError):
        ExternalCognitionEventV1.from_authority(
            _payload(kind=kind, attributes=attributes),
            authority=_authority(), now=NOW,
        )


def test_text_turn_observation_is_normalized_nonempty_and_bounded(monkeypatch):
    monkeypatch.setenv("PROTAGINE_OWNER_PERSON_ID", "person-owner")
    event = ExternalCognitionEventV1.from_authority(
        _payload(
            kind="text_turn_observation",
            attributes={
                "turn_id": "turn-1", "channel": "chat",
                "observation": "  Operator   asked for status.  ",
            },
        ),
        authority=_authority(), now=NOW,
    )
    assert event.attributes["observation"] == "Operator asked for status."

    for observation in ("", "   ", "x" * 501):
        with pytest.raises(ExternalEventValidationError, match="observation"):
            ExternalCognitionEventV1.from_authority(
                _payload(
                    kind="text_turn_observation",
                    attributes={
                        "turn_id": "turn-1", "channel": "chat",
                        "observation": observation,
                    },
                ),
                authority=_authority(), now=NOW,
            )


@pytest.mark.parametrize(
    "alias", ["gmeet", "telephone", "telephony", "voip", "pstn", "sip"],
)
def test_realtime_words_are_valid_inside_typed_text_content(monkeypatch, alias):
    monkeypatch.setenv("PROTAGINE_OWNER_PERSON_ID", "person-owner")
    event = ExternalCognitionEventV1.from_authority(
        _payload(summary=f"System status mentions {alias}"),
        authority=_authority(), now=NOW,
    )
    assert event.summary == f"System status mentions {alias}"


def test_external_event_restart_replay_is_one_receipt_and_one_journal_event(
    tmp_path, monkeypatch,
):
    journal_dir = tmp_path / "journal"
    monkeypatch.setenv("PROTAGINE_EVENT_JOURNAL_DIR", str(journal_dir))
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PROTAGINE_OWNER_PERSON_ID", "person-owner")
    db_path = tmp_path / "external-events.db"
    event = ExternalCognitionEventV1.from_authority(
        _payload(), authority=_authority(), now=NOW,
    )

    first_store = ExternalEventInboxStore(str(db_path))
    first = ExternalEventIntake(first_store).ingest(event, now=NOW)
    replay = ExternalEventIntake(first_store).ingest(event, now=NOW)
    assert first == replay
    assert first["status"] == "projected"
    assert first["journal_seq"] == 1
    first_store.close()

    reopened = ExternalEventInboxStore(str(db_path))
    restarted = ExternalEventIntake(reopened).ingest(event, now=NOW)
    assert restarted == first
    journal = replay_events(since="2026-01-01T00:00:00+00:00", limit=20)
    assert len(journal["events"]) == 1
    assert journal["events"][0]["type"] == "cognition.external.service_state"
    assert journal["events"][0]["data"]["external_event_id"] == event.event_id
    assert journal["events"][0]["data"]["evidence_status"] == (
        "reported/unverified"
    )
    assert "verified" not in journal["events"][0]["data"]
    assert "receipt_ref" not in journal["events"][0]["data"]

    changed = ExternalCognitionEventV1.from_authority(
        _payload(summary="Gateway is degraded"),
        authority=_authority(),
        now=NOW,
    )
    with pytest.raises(ExternalEventConflict):
        ExternalEventIntake(reopened).ingest(changed, now=NOW)
    assert len(replay_events(
        since="2026-01-01T00:00:00+00:00", limit=20,
    )["events"]) == 1


def test_receipt_commit_survives_projection_failure_and_restart(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PROTAGINE_EVENT_JOURNAL_DIR", str(tmp_path / "journal"))
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PROTAGINE_OWNER_PERSON_ID", "person-owner")
    path = tmp_path / "external-events.db"
    event = ExternalCognitionEventV1.from_authority(
        _payload(event_id="external-event-projection-failure"),
        authority=_authority(), now=NOW,
    )
    store = ExternalEventInboxStore(str(path))
    failed = ExternalEventIntake(
        store, journal_projector=lambda *_args, **_kwargs: None,
    )
    from protagine.cognition.external_events import ExternalEventProjectionError
    with pytest.raises(ExternalEventProjectionError):
        failed.ingest(event, now=NOW)
    reserved, created = store.reserve(event, now=NOW)
    assert created is False
    assert reserved["state"] == "accepted"
    store.close()

    restarted = ExternalEventIntake(ExternalEventInboxStore(str(path)))
    receipt = restarted.ingest(event, now=NOW)
    assert receipt["status"] == "projected"
    assert len(replay_events(
        since="2026-01-01T00:00:00+00:00", limit=20,
    )["events"]) == 1


def test_journal_success_then_finalize_crash_and_prune_reconciles_tombstone(
    tmp_path, monkeypatch,
):
    journal_dir = tmp_path / "journal"
    monkeypatch.setenv("PROTAGINE_EVENT_JOURNAL_DIR", str(journal_dir))
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PROTAGINE_EVENT_JOURNAL_RETENTION", "1")
    monkeypatch.setenv("PROTAGINE_OWNER_PERSON_ID", "person-owner")
    path = tmp_path / "external-events.db"
    event = ExternalCognitionEventV1.from_authority(
        _payload(event_id="external-event-finalize-crash"),
        authority=_authority(), now=NOW,
    )
    store = ExternalEventInboxStore(str(path))
    intake = ExternalEventIntake(store)
    original_complete = store.complete_projection

    def _crash_after_journal(*_args, **_kwargs):
        raise RuntimeError("simulated crash before inbox finalize")

    store.complete_projection = _crash_after_journal
    with pytest.raises(RuntimeError, match="simulated crash"):
        intake.ingest(event, now=NOW)
    marker_path = next((journal_dir / ".event-keys").iterdir())
    original_metadata = json.loads(marker_path.read_text())
    assert len(replay_events(
        since="2026-01-01T00:00:00+00:00", limit=20,
    )["events"]) == 1
    store.complete_projection = original_complete
    store.close()

    from protagine.events.journal import append_event
    assert append_event("test.retention.advance", {"step": 2}) == 2
    tombstone = json.loads(marker_path.read_text())
    assert tombstone["state"] == "pruned"
    assert event.summary not in marker_path.read_text()
    assert "attributes" not in marker_path.read_text()
    assert [item["seq"] for item in replay_events(
        since="2026-01-01T00:00:00+00:00", limit=20,
    )["events"]] == [2]

    replayed_record = append_event_record(
        f"cognition.external.{event.kind}",
        event.journal_payload(),
        occurred_at=event.occurred_at,
        event_key=f"external-cognition:{event.event_id}",
    )
    assert replayed_record == {
        "seq": original_metadata["seq"],
        "ulid": original_metadata["ulid"],
        "recordedAt": original_metadata["recordedAt"],
        "retained": False,
    }
    assert [item["seq"] for item in replay_events(
        since="2026-01-01T00:00:00+00:00", limit=20,
    )["events"]] == [2]

    restarted = ExternalEventIntake(ExternalEventInboxStore(str(path)))
    receipt = restarted.ingest(event, now=NOW)
    assert receipt["status"] == "projected"
    assert receipt["journal_seq"] == original_metadata["seq"]
    assert receipt["journal_event_id"] == original_metadata["ulid"]
    assert receipt["projected_at"] == original_metadata["recordedAt"]
    assert receipt["journal_retained"] is False
    assert list((journal_dir / ".event-keys").iterdir()) == []
    events = replay_events(
        since="2026-01-01T00:00:00+00:00", limit=20,
    )["events"]
    assert len(events) == 1
    assert events[0]["type"] == "test.retention.advance"


def test_event_key_marker_is_minimal_pruned_and_completed_replay_cannot_resurrect(
    tmp_path, monkeypatch,
):
    journal_dir = tmp_path / "journal"
    monkeypatch.setenv("PROTAGINE_EVENT_JOURNAL_DIR", str(journal_dir))
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PROTAGINE_EVENT_JOURNAL_RETENTION", "1")
    monkeypatch.setenv("PROTAGINE_OWNER_PERSON_ID", "person-owner")
    event = ExternalCognitionEventV1.from_authority(
        _payload(event_id="external-event-pruned-marker"),
        authority=_authority(), now=NOW,
    )
    intake = ExternalEventIntake(ExternalEventInboxStore(
        str(tmp_path / "external-events.db"),
    ))
    receipt = intake.ingest(event, now=NOW)
    assert list((journal_dir / ".event-keys").iterdir()) == []

    (journal_dir / ".cursor").unlink()
    shutil.rmtree(journal_dir / ".sequence-index")

    from protagine.events.journal import append_event
    assert append_event("test.retention.advance", {"step": 2}) == 2
    assert list((journal_dir / ".event-keys").iterdir()) == []
    assert [item["seq"] for item in replay_events(
        since="2026-01-01T00:00:00+00:00", limit=20,
    )["events"]] == [2]
    assert intake.ingest(event, now=NOW) == receipt
    assert [item["seq"] for item in replay_events(
        since="2026-01-01T00:00:00+00:00", limit=20,
    )["events"]] == [2]


def test_projected_replay_cleans_stale_event_key_marker(tmp_path, monkeypatch):
    journal_dir = tmp_path / "journal"
    monkeypatch.setenv("PROTAGINE_EVENT_JOURNAL_DIR", str(journal_dir))
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("PROTAGINE_OWNER_PERSON_ID", "person-owner")
    db_path = tmp_path / "external-events.db"
    event = ExternalCognitionEventV1.from_authority(
        _payload(event_id="external-event-stale-ack"),
        authority=_authority(), now=NOW,
    )
    first = ExternalEventIntake(
        ExternalEventInboxStore(str(db_path)),
        journal_acknowledger=lambda _key: False,
    )
    receipt = first.ingest(event, now=NOW)
    first.close()
    assert len(list((journal_dir / ".event-keys").iterdir())) == 1

    restarted = ExternalEventIntake(ExternalEventInboxStore(str(db_path)))
    assert restarted.ingest(event, now=NOW) == receipt
    assert list((journal_dir / ".event-keys").iterdir()) == []


