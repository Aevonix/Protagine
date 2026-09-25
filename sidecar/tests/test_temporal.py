"""Unit tests for protagine.util.temporal (v0.21.0)."""

import os
from datetime import datetime, timedelta, timezone

import pytest

from protagine.util import temporal as T


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    for k in ("PROTAGINE_AGENT_TIMEZONE", "PROTAGINE_DEFAULT_CONTACT_TIMEZONE", "TZ"):
        monkeypatch.delenv(k, raising=False)
    yield


def test_valid_timezone():
    assert T.is_valid_timezone("America/New_York")
    assert T.is_valid_timezone("UTC")
    assert not T.is_valid_timezone("Mars/Phobos")
    assert not T.is_valid_timezone("")
    assert not T.is_valid_timezone(None)


def test_agent_timezone_default_and_set():
    # With nothing configured + isolated state, falls back to system or UTC.
    assert T.is_valid_timezone(T.agent_timezone())
    T.set_agent_timezone("America/New_York")
    assert T.agent_timezone() == "America/New_York"
    # env overrides stored
    os.environ["PROTAGINE_AGENT_TIMEZONE"] = "Europe/London"
    try:
        assert T.agent_timezone() == "Europe/London"
    finally:
        del os.environ["PROTAGINE_AGENT_TIMEZONE"]


def test_set_agent_timezone_invalid():
    with pytest.raises(ValueError):
        T.set_agent_timezone("Not/AZone")


def test_default_contact_tz_roundtrip():
    assert T.default_contact_timezone() is None
    T.set_default_contact_timezone("Asia/Tokyo")
    assert T.default_contact_timezone() == "Asia/Tokyo"
    T.set_default_contact_timezone(None)
    assert T.default_contact_timezone() is None


def test_resolve_communication_timezone_precedence():
    T.set_agent_timezone("America/New_York")
    # override wins
    assert T.resolve_communication_timezone("Asia/Tokyo", "Europe/Paris") == "Europe/Paris"
    # then contact
    assert T.resolve_communication_timezone("Asia/Tokyo", None) == "Asia/Tokyo"
    # then default
    T.set_default_contact_timezone("Europe/Berlin")
    assert T.resolve_communication_timezone(None, None) == "Europe/Berlin"
    # then agent
    T.set_default_contact_timezone(None)
    assert T.resolve_communication_timezone(None, None) == "America/New_York"
    # invalid override ignored
    assert T.resolve_communication_timezone("Asia/Tokyo", "bogus") == "Asia/Tokyo"


def test_parse_iso():
    assert T.parse_iso(None) is None
    a = T.parse_iso("2026-06-11T04:16:25Z")
    assert a.tzinfo is not None and a.year == 2026
    b = T.parse_iso("2026-06-11T04:16:25")  # naive → UTC
    assert b.tzinfo == timezone.utc
    c = T.parse_iso(datetime(2026, 6, 11, tzinfo=timezone.utc))
    assert c.year == 2026


def test_humanize_delta():
    now = datetime.now(timezone.utc)
    assert T.humanize_delta(now) == "just now"
    assert T.humanize_delta(now - timedelta(minutes=30)) == "30m ago"
    assert T.humanize_delta(now - timedelta(hours=6)) == "6h ago"
    assert T.humanize_delta(now - timedelta(days=3)) == "3d ago"
    assert T.humanize_delta(now - timedelta(days=21)) == "3w ago"
    assert T.humanize_delta(now + timedelta(hours=5)).startswith("in 5h")
    assert T.humanize_delta("not-a-date") == "unknown"


def test_bucket():
    T.set_agent_timezone("UTC")
    now = datetime.now(timezone.utc)
    assert T.bucket(now) == "today"
    assert T.bucket(now - timedelta(days=1, hours=2)) in ("yesterday", "earlier this week")
    assert T.bucket(now - timedelta(days=10)) == "last week"


def test_hours_since():
    now = datetime.now(timezone.utc)
    h = T.hours_since(now - timedelta(hours=2))
    assert 1.9 < h < 2.1
    assert T.hours_since(None) is None


def test_now_and_format():
    T.set_agent_timezone("America/New_York")
    n = T.now_in("America/New_York")
    assert n.tzinfo is not None
    s = T.format_clock(n)
    assert "," in s and (":" in s)
    assert T.part_of_day(n) in (
        "the middle of the night", "early morning", "morning",
        "midday", "afternoon", "evening", "night",
    )


def test_parse_relative_since():
    now = datetime.now(timezone.utc)
    # relative
    a = T.parse_iso(T.parse_relative_since("24h"))
    assert 23.9 < (now - a).total_seconds() / 3600 < 24.1
    b = T.parse_iso(T.parse_relative_since("30m"))
    assert 29.0 < (now - b).total_seconds() / 60 < 31.0
    c = T.parse_iso(T.parse_relative_since("7d"))
    assert 6.9 < (now - c).total_seconds() / 86400 < 7.1
    # empty -> default 24h
    d = T.parse_iso(T.parse_relative_since(""))
    assert 23.0 < (now - d).total_seconds() / 3600 < 25.0
    # absolute ISO passes through
    iso = "2026-06-01T00:00:00+00:00"
    assert T.parse_iso(T.parse_relative_since(iso)).date().isoformat() == "2026-06-01"
    # today -> midnight agent-local, in the past
    T.set_agent_timezone("UTC")
    assert T.parse_iso(T.parse_relative_since("today")) <= now


def test_describe_now_labels_frames_across_date_boundary(monkeypatch):
    captured = datetime(2030, 7, 11, 23, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(T, "now_utc", lambda: captured)
    out = T.describe_now("America/New_York", "Asia/Tokyo", "Robin")
    assert "Now: 2030-07-11T23:30:00+00:00 UTC" in out
    assert "agent zone America/New_York: 2030-07-11T19:30:00-04:00" in out
    assert "Robin's recorded zone Asia/Tokyo: 2030-07-12T08:30:00+09:00" in out
    assert "not evidence of current location" in out
    assert "your local time" not in out
    assert out.count("\n") == 0  # one line: it rides in every turn and is replayed as history


def test_describe_now_same_zone_keeps_contact_provenance():
    out = T.describe_now("America/New_York", "America/New_York", "Robin")
    assert "agent zone America/New_York" in out
    assert "Robin's recorded zone America/New_York" in out


def test_describe_now_unknown_contact_stays_unknown_with_default_and_override():
    T.set_default_contact_timezone("Asia/Tokyo")
    out = T.describe_now("America/New_York", override_tz="Europe/Berlin")
    assert "the contact's zone and location unknown" in out
    assert "communication override Europe/Berlin" in out
    assert "recorded zone" not in out and "Asia/Tokyo" not in out


def test_describe_now_omits_a_utc_agent_frame_that_repeats_the_instant():
    out = T.describe_now("UTC")
    assert out.startswith("Now: ") and "agent zone" not in out


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded", [None, "Asia/Tokyo"])
async def test_context_builder_does_not_present_fallback_as_contact_record(monkeypatch, recorded):
    from types import SimpleNamespace
    from protagine.api.routers import host

    class Contacts:
        async def get(self, contact_id):
            assert contact_id == "robin"
            return SimpleNamespace(timezone=recorded, display_name="Robin", last_interaction_at=None)

    monkeypatch.setattr(host, "_contacts_store", Contacts())
    T.set_agent_timezone("America/New_York")
    T.set_default_contact_timezone("Europe/Berlin")
    section = await host._build_temporal_section("robin", include_global_heads_up=False)
    assert "agent zone America/New_York" in section.body
    assert ("Robin's recorded zone" in section.body) is bool(recorded)
    assert "Europe/Berlin" not in section.body
    # The reading rule (a retained clock is history) is the host's system-prompt text, not per turn.
    assert "historical" not in section.body
    assert "your local time" not in section.body and "this is NOW" not in section.body


def test_now_follows_the_one_wall_clock(monkeypatch):
    """``time.time`` is the one wall clock: Hermes' clock, the mind's and every "Now" line the sidecar renders
    read it, so a host that shifts it (the paired harness's body clock) moves them together. The pilots'
    prompts said "Now" 14 to 19 hours behind the shifted message stamps, and the model wrote those dates down."""
    import time

    from protagine.mind.tick import _wall_clock

    shifted = time.time() + 19 * 3600
    monkeypatch.setattr(time, "time", lambda: shifted)
    expected = datetime.fromtimestamp(shifted, timezone.utc)
    assert abs((T.now_utc() - expected).total_seconds()) < 1
    assert abs((_wall_clock()() - expected).total_seconds()) < 1
    assert f"Now: {expected.isoformat(timespec='seconds')[:16]}" in T.describe_now("UTC")
