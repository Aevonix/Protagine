"""Health staleness tracks what the sidecar runs; inbound quiet is observed, never flagged.

The mind's tick beats into the telemetry store whether the mind is on or off,
and the capture queue is asked how long its oldest unfinished job has waited.
``turns/sync`` and ``context/assemble`` are driven by conversation, so their
silence is reported under ``silence_hours`` and never degrades the status: a
fresh install is ``ok`` before anyone has talked to it, and a quiet day stays
``ok``. The ``initiative`` timestamp the previous line touched from a route
nothing calls any more is gone, and an upgraded store that still carries it
is read without it.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from protagine.api.routers import host
from protagine.telemetry import TelemetryStore

TICK_THRESHOLD = {"tick": 0.25}


def _store(**ages_hours):
    """Build a store whose last_*_at are `ages_hours` in the past (None = unset)."""
    now = datetime.now(timezone.utc)
    s = TelemetryStore(started_at=now)
    for key, hrs in ages_hours.items():
        setattr(s, key, None if hrs is None else now - timedelta(hours=hrs))
    return s


def _mind(interval=60.0, backlog_seconds=None, capture=True):
    queue = SimpleNamespace(oldest_unfinished_seconds=lambda: backlog_seconds) if capture else None
    return SimpleNamespace(enabled=True, level="suggest", ticks=3, interval=interval, capture=queue)


@pytest.fixture
def quiet_host(monkeypatch):
    """Remove every other degradation source so only the temporal probes decide."""
    from protagine import vector
    monkeypatch.setattr(host, "_commitment_store", None)
    monkeypatch.setattr(host, "_embedder", None)
    monkeypatch.setattr(host, "_embed_failure", None)
    monkeypatch.setattr(vector, "_store", None)
    monkeypatch.setattr(host, "_mind", lambda: _mind())
    monkeypatch.delenv("PROTAGINE_STALE_TICK_HOURS", raising=False)
    monkeypatch.delenv("PROTAGINE_STALE_CAPTURE_HOURS", raising=False)
    return host


@pytest.mark.asyncio
async def test_quiet_conversation_is_observed_not_flagged(quiet_host, monkeypatch):
    """Hours without a sync or a prefetch are silence, not degradation."""
    telemetry = _store(last_sync_at=3.0, last_tick_at=0.02, last_prefetch_at=25.0)
    assert await telemetry.stale_flags(TICK_THRESHOLD) == []
    monkeypatch.setattr(host, "_telemetry", telemetry)
    result = await host.health()
    assert result.status == "ok" and result.problems == []
    assert result.temporal.stale_flags == []
    assert result.temporal.silence_hours["sync"] > 2.9 and result.temporal.silence_hours["prefetch"] > 24.9
    assert "last_initiative_at" not in result.temporal.model_dump()


@pytest.mark.asyncio
async def test_a_dead_tick_degrades_health_with_the_reason(quiet_host, monkeypatch):
    telemetry = _store(last_sync_at=0.02, last_tick_at=1.0, last_prefetch_at=0.1)
    monkeypatch.setattr(host, "_telemetry", telemetry)
    # The retired policy switch is not read: nothing turns a dead loop advisory.
    monkeypatch.setenv("PROTAGINE_TEMPORAL_HEALTH_POLICY", "advisory")
    result = await host.health()
    assert result.status == "degraded"
    assert result.temporal.stale_flags == ["tick"]
    assert result.problems == ["the mind's tick has not run for 1.0 h"]


@pytest.mark.asyncio
async def test_tick_threshold_follows_the_minds_interval(quiet_host, monkeypatch):
    telemetry = _store(last_tick_at=1.0)
    monkeypatch.setattr(host, "_telemetry", telemetry)
    monkeypatch.setattr(host, "_mind", lambda: _mind(interval=600.0))   # ten intervals = 1.67 h
    assert (await host.health()).status == "ok"
    monkeypatch.setattr(host, "_mind", lambda: _mind(interval=60.0))    # ten intervals, floored at 0.25 h
    assert (await host.health()).status == "degraded"
    monkeypatch.setenv("PROTAGINE_STALE_TICK_HOURS", "2")
    assert (await host.health()).status == "ok"
    monkeypatch.setattr(host, "_mind", lambda: None)                    # no mind: the floor applies
    monkeypatch.delenv("PROTAGINE_STALE_TICK_HOURS")
    assert (await host.health()).status == "degraded"


@pytest.mark.asyncio
async def test_a_fresh_install_is_ready_before_its_first_tick(quiet_host, monkeypatch, tmp_path):
    """No telemetry file, started just now, nothing has happened yet: ok."""
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    telemetry = TelemetryStore()
    telemetry.load()
    telemetry.started_at = datetime.now(timezone.utc)
    monkeypatch.setattr(host, "_telemetry", telemetry)
    result = await host.health()
    assert result.status == "ok" and result.problems == [] and result.temporal.stale_flags == []
    assert result.temporal.silence_hours == {"sync": None, "tick": None, "prefetch": None}


@pytest.mark.asyncio
async def test_a_mind_that_never_ticks_flags_after_the_threshold(quiet_host, monkeypatch):
    telemetry = _store()
    telemetry.started_at = datetime.now(timezone.utc) - timedelta(hours=1)
    monkeypatch.setattr(host, "_telemetry", telemetry)
    result = await host.health()
    assert result.status == "degraded"
    assert result.temporal.stale_flags == ["tick:never_ran"]
    assert result.problems == ["the mind's tick has not run since the sidecar started"]


@pytest.mark.asyncio
async def test_upgraded_store_with_a_stale_initiative_timestamp_is_ok(quiet_host, monkeypatch, tmp_path):
    """A 1.9 telemetry.json carries last_initiative_at from months ago; 1.10 neither reads nor keeps it."""
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    now = datetime.now(timezone.utc)
    (tmp_path / "telemetry.json").write_text(json.dumps({
        "started_at": (now - timedelta(days=70)).isoformat(),
        "last_sync_at": (now - timedelta(days=2)).isoformat(),
        "last_tick_at": (now - timedelta(days=2)).isoformat(),
        "last_initiative_at": (now - timedelta(days=71)).isoformat(),
        "last_prefetch_at": (now - timedelta(days=2)).isoformat(),
        "last_agent_outreach_at": None,
    }))
    telemetry = TelemetryStore()
    telemetry.load()
    telemetry.started_at = now
    assert not hasattr(telemetry, "last_initiative_at")
    monkeypatch.setattr(host, "_telemetry", telemetry)
    # Before the first tick of the new process, the old tick timestamp is stale: honest.
    assert (await host.health()).temporal.stale_flags == ["tick"]
    await telemetry.touch("last_tick_at")             # what the mind's first tick does
    result = await host.health()
    assert result.status == "ok" and result.problems == []
    persisted = json.loads((tmp_path / "telemetry.json").read_text())
    assert "last_initiative_at" not in persisted and persisted["last_sync_at"]


@pytest.mark.asyncio
async def test_stuck_capture_jobs_degrade_health(quiet_host, monkeypatch):
    telemetry = _store(last_tick_at=0.02)
    monkeypatch.setattr(host, "_telemetry", telemetry)
    monkeypatch.setattr(host, "_mind", lambda: _mind(backlog_seconds=1800))
    result = await host.health()
    assert result.status == "ok" and result.temporal.silence_hours["capture"] == 0.5
    monkeypatch.setattr(host, "_mind", lambda: _mind(backlog_seconds=2 * 3600))
    result = await host.health()
    assert result.status == "degraded" and result.temporal.stale_flags == ["capture"]
    assert result.problems == ["capture jobs are not landing: the oldest unfinished job has waited 2.0 h"]
    monkeypatch.setenv("PROTAGINE_STALE_CAPTURE_HOURS", "3")
    assert (await host.health()).status == "ok"
    monkeypatch.setattr(host, "_mind", lambda: _mind(capture=False))
    result = await host.health()
    assert result.status == "ok" and "capture" not in result.temporal.silence_hours


@pytest.mark.asyncio
async def test_failed_probe_paths_degrade_health(quiet_host, monkeypatch):
    """A crashing embedder probe or staleness computation must degrade
    /health — never be swallowed into an unconditional 'ok'."""

    class BrokenEmbedder:
        async def health_check(self):
            raise RuntimeError("embed backend gone")

    monkeypatch.setattr(host, "_telemetry", None)
    monkeypatch.setattr(host, "_embedder", BrokenEmbedder())
    result = await host.health()
    assert result.status == "degraded"
    assert "health probe failed" in (result.notes or {}).get("embed", "")

    class BrokenTelemetry:
        async def to_dict(self, thresholds):
            raise RuntimeError("telemetry store corrupt")

    monkeypatch.setattr(host, "_embedder", None)
    monkeypatch.setattr(host, "_telemetry", BrokenTelemetry())
    result = await host.health()
    assert result.status == "degraded"
    assert "staleness computation failed" in (result.notes or {}).get("temporal", "")
    assert result.problems == ["staleness computation failed: telemetry store corrupt"]


@pytest.mark.asyncio
async def test_corrupt_telemetry_reset_is_visible_and_flaggable(tmp_path, monkeypatch, caplog):
    """A corrupt telemetry file must not silently reset to a permanently
    unflaggable 'fresh' store: the reset is logged, state reads 'unknown',
    and a loop that never ran past its threshold flags."""
    monkeypatch.setenv("PROTAGINE_STATE_DIR", str(tmp_path))
    (tmp_path / "telemetry.json").write_text("{not json")

    s = TelemetryStore()
    with caplog.at_level(logging.WARNING, logger="protagine.telemetry"):
        s.load()
    assert s.state == "unknown"
    assert any("unreadable" in r.getMessage() for r in caplog.records)

    s.started_at = datetime.now(timezone.utc) - timedelta(hours=1)
    flags = await s.stale_flags(TICK_THRESHOLD)
    assert "tick:never_ran" in flags

    d = await s.to_dict(TICK_THRESHOLD)
    assert d["state"] == "unknown"


@pytest.mark.asyncio
async def test_model_degradation_is_not_cleared_by_a_live_tick(quiet_host, monkeypatch):
    from protagine import vector

    class Config:
        model_id = "configured-model"

    class Provider:
        _config = Config()

    class Embedder:
        _provider = Provider()
        index_identity = object()

        async def health_check(self):
            return {"status": "ok"}

    class Store:
        async def check_index_health(self, expected_identity):
            raise ValueError("Stored embedding identity differs from the pipeline")

    monkeypatch.setattr(host, "_telemetry", _store(last_tick_at=0.02))
    monkeypatch.setattr(host, "_embedder", Embedder())
    monkeypatch.setattr(vector, "_store", Store())
    result = await host.health()
    assert result.status == "degraded" and result.temporal.stale_flags == []
    assert result.problems == ["the semantic index check failed: ValueError: Stored embedding identity "
                               "differs from the pipeline"]
