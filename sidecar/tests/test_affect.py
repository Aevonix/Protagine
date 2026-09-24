"""Tests for AffectStore."""

import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from protagine.tom.affect import AffectStore


@pytest.fixture
def store():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        path = f.name
    s = AffectStore(path)
    yield s
    s.close()
    os.unlink(path)


class TestAffectCreateEvent:
    def test_create_basic(self, store):
        result = store.create_event(
            contact_id="owner",
            valence=0.5,
            arousal=0.7,
            source="explicit",
            trigger="good news",
        )
        assert result["contact_id"] == "owner"
        assert result["valence"] == 0.5
        assert result["arousal"] == 0.7
        assert result["source"] == "explicit"
        assert result["trigger"] == "good news"
        assert result["id"]
        assert result["timestamp"]

    def test_valence_clamped(self, store):
        result = store.create_event(contact_id="owner", valence=2.0, source="explicit")
        assert result["valence"] == 1.0

    def test_valence_negative_clamped(self, store):
        result = store.create_event(contact_id="owner", valence=-3.0, source="explicit")
        assert result["valence"] == -1.0

    def test_arousal_clamped(self, store):
        result = store.create_event(contact_id="owner", valence=0.0, arousal=1.5, source="explicit")
        assert result["arousal"] == 1.0

    def test_custom_timestamp(self, store):
        ts = "2026-01-01T00:00:00+00:00"
        result = store.create_event(contact_id="owner", valence=0.3, source="explicit", timestamp=ts)
        assert result["timestamp"] == ts


class TestAffectGetEvent:
    def test_get_existing(self, store):
        created = store.create_event(contact_id="owner", valence=0.5, source="explicit")
        result = store.get_event(created["id"])
        assert result is not None
        assert result["id"] == created["id"]

    def test_get_nonexistent(self, store):
        assert store.get_event("nope") is None


class TestAffectListEvents:
    def test_list_all(self, store):
        store.create_event(contact_id="owner", valence=0.5, source="explicit")
        store.create_event(contact_id="alice", valence=-0.3, source="inferred")
        events = store.list_events()
        assert len(events) == 2

    def test_list_by_contact(self, store):
        store.create_event(contact_id="owner", valence=0.5, source="explicit")
        store.create_event(contact_id="alice", valence=-0.3, source="inferred")
        events = store.list_events(contact_id="owner")
        assert len(events) == 1
        assert events[0]["contact_id"] == "owner"

    def test_list_by_source(self, store):
        store.create_event(contact_id="owner", valence=0.5, source="explicit")
        store.create_event(contact_id="owner", valence=0.3, source="inferred")
        events = store.list_events(source="inferred")
        assert len(events) == 1
        assert events[0]["source"] == "inferred"

    def test_list_pagination(self, store):
        for i in range(5):
            store.create_event(contact_id="owner", valence=0.1 * i, source="explicit")
        events = store.list_events(limit=2, offset=0)
        assert len(events) == 2


class TestAffectDeleteEvent:
    def test_delete_existing(self, store):
        created = store.create_event(contact_id="owner", valence=0.5, source="explicit")
        assert store.delete_event(created["id"]) is True
        assert store.get_event(created["id"]) is None

    def test_delete_nonexistent(self, store):
        assert store.delete_event("nope") is False

    def test_delete_recomputes_state(self, store):
        e1 = store.create_event(contact_id="owner", valence=0.8, source="explicit")
        store.create_event(contact_id="owner", valence=0.2, source="explicit")
        store.delete_event(e1["id"])
        state = store.get_state("owner")
        assert state["event_count"] == 1


class TestAffectState:
    def test_state_no_events(self, store):
        state = store.get_state("nobody")
        assert state["current_valence"] == 0.0
        assert state["trend"] == "stable"
        assert state["event_count"] == 0

    def test_state_single_event(self, store):
        store.create_event(contact_id="owner", valence=0.5, source="explicit")
        state = store.get_state("owner")
        assert state["current_valence"] == 0.5
        assert state["event_count"] == 1
        assert state["trend"] == "stable"

    def test_state_multiple_events(self, store):
        store.create_event(contact_id="owner", valence=-0.3, source="explicit")
        store.create_event(contact_id="owner", valence=0.5, source="explicit")
        store.create_event(contact_id="owner", valence=0.8, source="explicit")
        state = store.get_state("owner")
        assert state["current_valence"] > 0
        assert state["event_count"] == 3
        assert state["trend"] == "improving"

    def test_declining_trend(self, store):
        store.create_event(contact_id="owner", valence=0.8, source="explicit")
        store.create_event(contact_id="owner", valence=0.3, source="explicit")
        store.create_event(contact_id="owner", valence=-0.2, source="explicit")
        state = store.get_state("owner")
        assert state["trend"] == "declining"

    def test_stable_trend(self, store):
        store.create_event(contact_id="owner", valence=0.3, source="explicit")
        store.create_event(contact_id="owner", valence=0.35, source="explicit")
        state = store.get_state("owner")
        assert state["trend"] == "stable"


class TestAffectDetection:
    def test_negative_spike_detected(self, store):
        store.create_event(contact_id="owner", valence=-0.7, source="explicit")
        assert store.detect_negative_spike("owner") is True

    def test_no_negative_spike(self, store):
        store.create_event(contact_id="owner", valence=0.3, source="explicit")
        assert store.detect_negative_spike("owner") is False

    def test_negative_spike_no_events(self, store):
        assert store.detect_negative_spike("nobody") is False

    def test_sustained_decline(self, store):
        store.create_event(contact_id="owner", valence=-0.1, source="explicit")
        store.create_event(contact_id="owner", valence=-0.5, source="explicit")
        store.create_event(contact_id="owner", valence=-0.9, source="explicit")
        assert store.detect_sustained_decline("owner") is True

    def test_no_sustained_decline(self, store):
        store.create_event(contact_id="owner", valence=0.5, source="explicit")
        assert store.detect_sustained_decline("owner") is False

    def test_near_neutral_decline_is_below_magnitude_floor(self, store):
        store.create_event(contact_id="owner", valence=0.06, source="explicit")
        store.create_event(contact_id="owner", valence=-0.04, source="explicit")
        store.create_event(contact_id="owner", valence=-0.08, source="explicit")

        state = store.get_state("owner")
        assert state["trend"] == "declining"
        # Newest first: (-0.08 + 0.9 x -0.04 + 0.81 x 0.06) / 2.71 = -0.025, still near neutral.
        assert -0.03 < state["current_valence"] < 0
        assert store.detect_sustained_decline("owner") is False

    def test_time_decay_below_magnitude_floor_clears_detection(self, store):
        store.create_event(contact_id="owner", valence=-0.1, source="explicit")
        store.create_event(contact_id="owner", valence=-0.5, source="explicit")
        store.create_event(contact_id="owner", valence=-0.9, source="explicit")
        assert store.detect_sustained_decline("owner") is True

        stale = (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat()
        store._conn.execute(
            "UPDATE affect_state SET last_updated = ? WHERE contact_id = ?",
            (stale, "owner"),
        )
        store._conn.commit()

        assert store.detect_sustained_decline("owner") is False
        assert store.get_state("owner")["current_valence"] > -0.3

    def test_decay_only_events_do_not_form_declining_trend(self, store):
        store.create_event(contact_id="owner", valence=-0.1, source="decay")
        store.create_event(contact_id="owner", valence=-0.5, source="decay")
        store.create_event(contact_id="owner", valence=-0.9, source="decay")

        state = store.get_state("owner")
        assert state["current_valence"] <= -0.3
        assert state["trend"] == "stable"
        assert store.detect_sustained_decline("owner") is False


class TestRecencyAndReattribution:
    def test_the_newest_event_weighs_most(self, store):
        store.create_event(contact_id="p-02", valence=0.8, source="explicit")
        store.create_event(contact_id="p-02", valence=0.6, source="explicit")
        store.create_event(contact_id="p-02", valence=-0.9, source="explicit")
        state = store.get_state("p-02")
        # Weights 1.0, 0.9, 0.81 newest first: (-0.9 + 0.54 + 0.648) / 2.71 = 0.106; the old
        # oldest-first weighting gave 0.32 and left a fresh negative turn nearly invisible.
        assert state["current_valence"] == pytest.approx(0.1063, abs=1e-3)
        assert state["last_event_id"] == store.list_events(contact_id="p-02", limit=1)[0]["id"]
        store.create_event(contact_id="p-02", valence=-0.9, source="explicit")
        assert store.get_state("p-02")["current_valence"] < 0            # two fresh negatives flip it

    def test_reattribute_moves_events_and_recomputes_both_states(self, store):
        store.create_event(contact_id="dup", valence=-0.8, source="explicit")
        store.create_event(contact_id="dup", valence=-0.7, source="explicit")
        store.create_event(contact_id="keep", valence=0.5, source="explicit")
        assert store.reattribute("dup", "keep") == 2
        assert store.get_state("dup")["event_count"] == 0
        keep = store.get_state("keep")
        assert keep["event_count"] == 3 and keep["current_valence"] < 0.5
        assert store.count_events(contact_id="dup") == 0 and store.reattribute("dup", "keep") == 0

    def test_trend_is_the_one_read_the_social_drive_uses(self, store):
        assert store.trend("nobody") == {"valence": 0.0, "trend": "stable", "declining": False}
        for valence in (0.4, 0.0, -0.5, -0.7, -0.9):
            store.create_event(contact_id="p-03", valence=valence, source="appraisal")
        down = store.trend("p-03")
        assert down["declining"] is True and down["trend"] == "declining" and down["valence"] < -0.3
        store.create_event(contact_id="p-04", valence=-0.9, source="explicit")
        one = store.trend("p-04")
        assert one["declining"] is False and one["trend"] == "stable" and one["valence"] == -0.9


def test_affect_stamps_and_decays_on_the_clock_the_mind_reads(tmp_path, monkeypatch):
    """Audit B1 residual: the mind and the contact stamps read ``time.time`` (the body clock the
    benchmark shifts); an affect store stamping and decaying on ``datetime.now`` split the two."""
    import time as time_mod
    store = AffectStore(str(tmp_path / "affect.db"))
    shifted = time_mod.time() + 3 * 86400
    monkeypatch.setattr(time_mod, "time", lambda: shifted)
    event = store.create_event(contact_id="p-02", valence=-0.8)
    assert abs(datetime.fromisoformat(event["timestamp"]).timestamp() - shifted) < 5
    state = store._conn.execute("SELECT last_updated FROM affect_state WHERE contact_id='p-02'").fetchone()
    assert abs(datetime.fromisoformat(state["last_updated"]).timestamp() - shifted) < 5
    monkeypatch.setattr(time_mod, "time", lambda: shifted + 10 * 3600)
    store._apply_decay("p-02")
    decayed = store._conn.execute("SELECT current_valence FROM affect_state WHERE contact_id='p-02'").fetchone()
    assert decayed["current_valence"] == pytest.approx(-0.8 * 0.95 ** 10, abs=1e-3)
