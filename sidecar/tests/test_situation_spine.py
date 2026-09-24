"""P6 regressions: evidence-derived situation, scope, freshness, and policy."""

from dataclasses import FrozenInstanceError, replace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from protagine.self_model.situation import (
    AppropriatenessGate,
    JournalSituationAdapter,
    SituationFactV1,
    SituationObservationV1,
    SituationReducer,
    SituationSnapshotV1,
    SituationStore,
    compact_situation,
)


NOW = 1_800_000_000.0


def observation(
    *,
    oid="so-1",
    category="service",
    entity="sidecar",
    state="healthy",
    active=True,
    observed=NOW,
    ttl=60,
    subject="owner",
    viewer="owner",
    sharing="owner_private",
    source="service_probe",
    refs=("health:probe-1",),
    attributes=None,
):
    return SituationObservationV1.create(
        observation_id=oid,
        category=category,
        entity_id=entity,
        state=state,
        active=active,
        observed_at=observed,
        ttl_seconds=ttl,
        evidence_refs=refs,
        source_kind=source,
        subject_person_id=subject,
        viewer_scope=viewer,
        shareability=sharing,
        attributes=attributes or {},
    )


def store(tmp_path):
    return SituationStore(str(tmp_path / "situation.db"))


def test_observation_rejects_model_assertions_and_missing_evidence():
    with pytest.raises(ValueError, match="trusted observation"):
        observation(source="model_assertion")
    with pytest.raises(ValueError, match="evidence"):
        observation(refs=())


def test_observation_scope_is_exact_and_immutable():
    with pytest.raises(ValueError, match="exact subject"):
        observation(
            subject="person-a", viewer="owner", sharing="subject_private",
        )
    item = observation()
    with pytest.raises(FrozenInstanceError):
        item.state = "degraded"


def test_ingest_is_idempotent_and_conflicting_replay_fails(tmp_path):
    s = store(tmp_path)
    item = observation()
    assert s.ingest(item)["disposition"] == "applied"
    assert s.ingest(item)["disposition"] == "duplicate"
    conflict = observation(state="degraded")
    with pytest.raises(ValueError, match="conflicting content"):
        s.ingest(conflict)


def test_older_observation_is_history_not_current_state(tmp_path):
    s = store(tmp_path)
    s.ingest(observation(oid="so-new", state="healthy", observed=NOW))
    assert s.ingest(
        observation(oid="so-old", state="degraded", observed=NOW - 10),
    )["disposition"] == "historical"
    snap = s.snapshot(subject_person_id="owner", viewer_scope="owner", as_of=NOW)
    assert snap.active_facts("service")[0].state == "healthy"


def test_snapshot_has_explicit_fresh_stale_unknown_semantics(tmp_path):
    s = store(tmp_path)
    s.ingest(observation(ttl=30))
    fresh = s.snapshot(
        subject_person_id="owner", viewer_scope="owner", as_of=NOW + 20,
    )
    stale = s.snapshot(
        subject_person_id="owner", viewer_scope="owner", as_of=NOW + 40,
    )
    assert fresh.freshness("service") == "fresh"
    assert stale.freshness("service") == "stale"
    assert stale.freshness("approval") == "unknown"
    assert fresh.snapshot_id != stale.snapshot_id
    assert s.get_snapshot(fresh.snapshot_id, viewer_scope="owner") == fresh


def test_snapshot_projection_never_crosses_viewer_or_subject(tmp_path):
    s = store(tmp_path)
    s.ingest(observation())
    s.ingest(observation(
        oid="so-person", category="person", entity="person-a",
        subject="person-a", viewer="person:person-a",
        sharing="subject_private", source="presence_observation",
        refs=("presence:sighting-a",),
    ))
    owner = s.snapshot(subject_person_id="owner", viewer_scope="owner", as_of=NOW)
    wrong_viewer = s.snapshot(
        subject_person_id="owner", viewer_scope="person:person-a", as_of=NOW,
    )
    subject = s.snapshot(
        subject_person_id="person-a", viewer_scope="person:person-a", as_of=NOW,
    )
    assert owner.freshness("service") == "fresh"
    assert wrong_viewer.freshness("service") == "unknown"
    assert subject.active_facts("person")[0].entity_id == "person-a"
    assert s.get_snapshot(owner.snapshot_id, viewer_scope="person:person-a") is None


def test_snapshot_bounds_each_category_and_reports_omissions(tmp_path):
    s = store(tmp_path)
    for index in range(5):
        s.ingest(observation(
            oid=f"so-{index}", entity=f"service-{index}",
            observed=NOW + index,
            refs=(f"health:probe-{index}",),
        ))
    snap = s.snapshot(
        subject_person_id="owner", viewer_scope="owner", as_of=NOW + 5,
        per_category_limit=2,
    )
    assert len(snap.active_facts("service")) == 2
    assert dict(snap.omitted)["service"] == 3


def journal_event(event_type="conversation.turn", data=None, *, seq=7):
    return {
        "seq": seq,
        "ulid": f"event-{seq}",
        "type": event_type,
        "occurredAt": "2027-01-15T08:00:00+00:00",
        "recordedAt": "2027-01-15T08:00:01+00:00",
        "data": data or {
            "contact_id": "person-a",
            "session_id": "session-a",
            "channel_id": "rcs",
            "summary": "untrusted prose must not become state",
        },
    }


def test_journal_adapter_projects_structured_turn_not_prose():
    items, disposition, _ = JournalSituationAdapter.adapt(journal_event())
    assert disposition == "projected"
    assert {item.category for item in items} == {"conversation", "person", "channel"}
    payload = str([item.payload() for item in items])
    assert "untrusted prose" not in payload
    assert all(item.evidence_refs == ("journal:7:event-7",) for item in items)


def test_unattested_shared_event_is_downgraded_to_owner_private():
    event = journal_event("service.degraded", {
        "service_id": "gateway", "status": "degraded",
        "shareability": "public",
    })
    items, _, _ = JournalSituationAdapter.adapt(event)
    assert items[0].viewer_scope == "owner"
    assert items[0].shareability == "owner_private"


def test_negative_service_state_remains_visible_to_policy():
    event = journal_event("service.offline", {
        "service_id": "gateway", "status": "offline",
    })
    items, _, _ = JournalSituationAdapter.adapt(event)
    assert items[0].active is True




def test_current_outreach_channel_field_is_supported():
    event = journal_event("outreach.sent", {
        "contact_id": "owner", "channel": "rcs", "reason": "ignored prose",
    })
    items, disposition, _ = JournalSituationAdapter.adapt(event)
    assert disposition == "projected"
    assert items[0].category == "channel" and items[0].entity_id == "rcs"
    assert "ignored prose" not in str(items[0].payload())


def test_reducer_replays_journal_atomically_and_advances_cursor(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_SITUATION_SPINE", "shadow")
    monkeypatch.setenv("PROTAGINE_SITUATION_BOOTSTRAP", "replay")
    event = journal_event(seq=1)

    def replay(*, after_seq, limit):
        return {
            "events": [event] if after_seq < 1 else [],
            "firstAvailableSeq": 1,
            "journalLastSeq": 1,
            "hasMore": False,
        }

    s = store(tmp_path)
    reducer = SituationReducer(
        s, replay_fn=replay, current_sequence_fn=lambda: 1,
    )
    result = reducer.run_once()
    assert result["processed"] == 1 and result["cursor"] == 1
    assert reducer.run_once()["processed"] == 0
    assert reducer.status()["healthy"] is True
    snap = s.snapshot(
        subject_person_id="person-a", viewer_scope="owner",
        as_of=1_800_000_000,
    )
    assert snap.active_facts("conversation")


def test_situation_reducer_stops_on_corrupt_journal(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_SITUATION_SPINE", "shadow")
    monkeypatch.setenv("PROTAGINE_SITUATION_BOOTSTRAP", "replay")
    event = journal_event(seq=1)

    def replay(*, after_seq, limit):
        return {
            "events": [event] if after_seq < 1 else [],
            "firstAvailableSeq": 1,
            "journalLastSeq": 1,
            "hasMore": False,
            "corruptCount": 1,
        }

    s = store(tmp_path)
    reducer = SituationReducer(
        s, replay_fn=replay, current_sequence_fn=lambda: 1,
    )

    stopped = reducer.run_once()

    assert stopped["error"] == "journal_corruption_detected:1"
    assert s.cursor(reducer.consumer_id) == 0
    assert reducer.status()["healthy"] is False


def test_situation_reducer_stops_on_journal_rewind(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_SITUATION_SPINE", "shadow")
    monkeypatch.setenv("PROTAGINE_SITUATION_BOOTSTRAP", "replay")
    s = store(tmp_path)
    s.initialize_cursor(
        "situation-spine-v1", 5, bootstrap_mode="replay",
    )

    def replay(*, after_seq, **_kwargs):
        assert after_seq == 5
        return {
            "events": [], "firstAvailableSeq": 1,
            "journalLastSeq": 3, "corruptCount": 0, "hasMore": False,
        }

    reducer = SituationReducer(
        s, replay_fn=replay, current_sequence_fn=lambda: 3,
    )

    stopped = reducer.run_once()

    assert stopped["error"] == "event_journal_rewind:5:3"
    assert stopped["processed"] == 0
    assert stopped["cursor"] == 5
    assert s.cursor(reducer.consumer_id) == 5
    assert reducer.status()["healthy"] is False


def test_situation_internal_gap_requires_explicit_acknowledgement(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("PROTAGINE_SITUATION_SPINE", "shadow")
    monkeypatch.setenv("PROTAGINE_SITUATION_BOOTSTRAP", "replay")
    events = [journal_event(seq=1), journal_event(seq=3)]
    events[1]["occurredAt"] = "2027-01-15T08:00:02+00:00"
    events[1]["recordedAt"] = "2027-01-15T08:00:03+00:00"

    def replay(*, after_seq, limit):
        selected = [item for item in events if item["seq"] > after_seq]
        return {
            "events": selected[:limit],
            "firstAvailableSeq": 1,
            "journalLastSeq": 3,
            "hasMore": len(selected) > limit,
            "corruptCount": 0,
        }

    s = store(tmp_path)
    reducer = SituationReducer(
        s, replay_fn=replay, current_sequence_fn=lambda: 3,
    )

    stopped = reducer.run_once()
    assert stopped["error"] == "journal_sequence_gap:1:3"
    assert stopped["processed"] == 1
    assert s.cursor(reducer.consumer_id) == 1

    monkeypatch.setenv("PROTAGINE_SITUATION_GAP_POLICY", "acknowledge")
    resumed = reducer.run_once()
    assert "error" not in resumed, resumed
    assert resumed["processed"] == 1
    assert resumed["cursor"] == 3
    assert reducer.status()["gaps"]["count"] == 1


def test_event_reduction_is_idempotent_across_store_connections(tmp_path):
    path = tmp_path / "situation.db"
    first = SituationStore(str(path))
    second = SituationStore(str(path))
    first.initialize_cursor("situation-spine-v1", 0, bootstrap_mode="replay")
    event = journal_event(seq=1)
    items, disposition, digest = JournalSituationAdapter.adapt(event)

    def apply(target):
        return target.reduce_event(
            consumer_id="situation-spine-v1", event_seq=1,
            event_id="event-1", event_digest=digest,
            observations=items, disposition=disposition,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, (first, second)))
    assert {item["disposition"] for item in results} == {
        "projected", "duplicate_event",
    }


def test_reducer_is_dark_when_default_off(tmp_path):
    called = []
    reducer = SituationReducer(
        store(tmp_path), replay_fn=lambda **kw: called.append(kw),
    )
    assert reducer.run_once() == {"enabled": False, "processed": 0, "mode": "off"}
    assert called == []


def snap_with(tmp_path, *items, as_of=NOW):
    tmp_path.mkdir(parents=True, exist_ok=True)
    s = store(tmp_path)
    for item in items:
        s.ingest(item)
    return s.snapshot(
        subject_person_id="owner", viewer_scope="owner", as_of=as_of,
    )


def live_gate():
    return AppropriatenessGate(mode_fn=lambda: "live")


def test_policy_unknown_asks_and_stale_holds(tmp_path):
    snap = snap_with(tmp_path, observation())
    unknown = live_gate().evaluate(
        snap, operation="project_start", required_categories=("resource",),
    )
    assert unknown.action == "ask" and not unknown.allowed
    stale = live_gate().evaluate(
        snap_with(tmp_path / "stale", observation(ttl=1), as_of=NOW + 2),
        operation="project_start", required_categories=("service",),
    )
    assert stale.action == "hold" and "stale" in stale.reason


def test_policy_degraded_service_holds_even_with_fresh_evidence(tmp_path):
    snap = snap_with(tmp_path, observation(state="degraded"))
    verdict = live_gate().evaluate(
        snap, operation="project_start", required_categories=("service",),
    )
    assert verdict.action == "hold"
    assert "health:probe-1" in verdict.evidence_refs


def test_stale_degraded_peer_does_not_override_fresh_service_evidence(tmp_path):
    stale = observation(
        oid="so-stale-service", entity="historic-service", state="degraded",
        observed=NOW - 120, ttl=30, refs=("health:historic-service",),
    )
    current = observation(
        oid="so-current-service", entity="required-service", state="healthy",
        refs=("health:required-service",),
    )
    snap = snap_with(tmp_path, stale, current, as_of=NOW)
    assert snap.freshness("service") == "fresh"
    assert snap.active_facts("service")[0].freshness in {"fresh", "stale"}
    verdict = live_gate().evaluate(
        snap, operation="project_start", required_categories=("service",),
    )
    assert verdict.allowed


def test_stale_social_facts_do_not_block_fresh_outreach_context(tmp_path):
    available = observation(
        oid="so-owner-available", category="owner_engagement", entity="owner",
        state="available", source="presence_observation",
        refs=("presence:owner-current",),
    )
    stale_conversation = observation(
        oid="so-old-conversation", category="conversation", entity="old-call",
        state="active", observed=NOW - 120, ttl=30,
        source="presence_observation", refs=("presence:old-call",),
    )
    stale_person = observation(
        oid="so-old-person", category="person", entity="bystander",
        state="present", observed=NOW - 120, ttl=30,
        source="presence_observation", refs=("presence:old-bystander",),
    )
    snap = snap_with(
        tmp_path, available, stale_conversation, stale_person, as_of=NOW,
    )
    verdict = live_gate().evaluate(
        snap,
        operation="outreach",
        required_categories=("owner_engagement",),
        recipient_person_id="owner",
    )
    assert verdict.allowed


def test_policy_unavailable_capability_holds_and_pending_approval_asks(tmp_path):
    capability = observation(
        oid="so-cap", category="capability", entity="web-read",
        state="unavailable", source="service_probe",
        refs=("capability:web-read",),
    )
    cap_verdict = live_gate().evaluate(
        snap_with(tmp_path / "cap", capability),
        operation="project_start", required_categories=("capability",),
    )
    assert cap_verdict.reason == "required_capability_unavailable"

    approval = observation(
        oid="so-approval", category="approval", entity="approval-1",
        state="pending", source="approval_receipt",
        refs=("approval:approval-1",),
    )
    approval_verdict = live_gate().evaluate(
        snap_with(tmp_path / "approval", approval),
        operation="project_start", required_categories=("approval",),
    )
    assert approval_verdict.action == "ask"
    assert approval_verdict.reason == "pending_approval_requires_decision"


def test_outreach_respects_busy_owner_and_active_conversation(tmp_path):
    owner = observation(
        oid="so-owner", category="owner_engagement", entity="owner",
        state="busy", source="presence_observation",
        refs=("presence:owner-1",),
    )
    snap = snap_with(tmp_path, owner)
    verdict = live_gate().evaluate(
        snap, operation="outreach", required_categories=("owner_engagement",),
    )
    assert verdict.reason == "owner_interruption_cost_high"


def test_fresh_policy_allow_is_not_action_authority(tmp_path):
    snap = snap_with(tmp_path, observation())
    verdict = live_gate().evaluate(
        snap, operation="project_start", required_categories=("service",),
    )
    assert verdict.allowed and verdict.action == "allow"
    result = verdict.as_policy_result()
    assert result["does_not_grant_authority"] is True
    assert result["situation_verdict_ref"].startswith("situation-verdict:")


def test_shadow_policy_can_never_allow(tmp_path):
    snap = snap_with(tmp_path, observation())
    verdict = AppropriatenessGate(mode_fn=lambda: "shadow").evaluate(
        snap, operation="project_start", required_categories=("service",),
    )
    assert verdict.action == "hold" and not verdict.allowed


def test_p3_adapter_rejects_scope_mismatch(tmp_path):
    snap = snap_with(tmp_path, observation())
    proposal = SimpleNamespace(subject_person_id="someone-else", viewer_scope="owner")
    concern = SimpleNamespace(subject_person_id="owner")
    result = live_gate().for_goal_proposal(proposal, concern, snap)
    assert result["allowed"] is False
    assert result["reason"] == "situation_scope_mismatch"


def test_compact_situation_never_presents_stale_hardware_as_current():
    fact = SituationFactV1('probe', 'service', 'service:inference', 'healthy', True, 1000, 1120, 'fresh',
                          ('receipt:probe',), 'cid-owner', 'owner', 'owner_private')
    snapshot = SituationSnapshotV1('snapshot', 'digest', 'cid-owner', 'owner', 'owner_private', 1100,
                                  (fact,), (), (), ('receipt:probe',))
    assert compact_situation(snapshot)['facts'][0]['state'] == 'healthy'
    stale = compact_situation(replace(snapshot, facts=(replace(fact, freshness='stale'),), as_of=1200))
    assert stale['facts'] == [] and stale['stale'][0]['state'] == 'unknown'
    assert stale['stale'][0]['last_observed_state'] == 'healthy'
    assert compact_situation(None)['state'] == 'unknown'
