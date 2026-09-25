"""The backlog of the model-backed source projections, and the small budget it drains at.

Claim extraction, appraisal, capture (commitments) and media description each keep a durable
queue of jobs in the source ledger, one per source: a person's turn, an image. A release that
could not work a queue (its model role was missing, another lane held it back) leaves the jobs
pending, and a release that can would otherwise run them all back to back, oldest first: every
historical turn through the model at full rate, while the owner is talking to the same model.

The ledger records when the running release first opened it (``started_at``, moved whenever the
release changes). A job enqueued more than ``RECENT_S`` before that moment is backlog; the day's
grace keeps the turns of the hours before an upgrade out of it. Each queue takes its new jobs
first and at once. It takes a backlog job only when it has no new job due, and backlog jobs start
at most ``projections.backlog_per_hour`` times an hour across all the queues, evenly spaced
(``protagine.yaml``; default ``DEFAULT_PER_HOUR``; 0 leaves the backlog pending). A retry keeps
its job's class, and each backlog attempt spends one start.

Not governed here: the opinion pass finishes a turn job older than 48 hours as stale without a
model call (``self_model.judgments.STALE_JOB_S``); source-vector indexing calls the embedder, not
the model; a PDF's text is extracted locally.
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, Optional

RECENT_S = 24 * 3600.0
DEFAULT_PER_HOUR = 12
HOUR_S = 3600.0
_CONFIG_TTL_S = 60.0
_configured: Dict[str, float] = {"read_at": -math.inf, "value": float(DEFAULT_PER_HOUR)}

# Seconds since the epoch of a ledger ISO timestamp (``turn_sources.ingested_at``), for a column
# added to a queue that predates it.
EPOCH_SQL = "((julianday({}) - 2440587.5) * 86400.0)"


def _release() -> str:
    import protagine
    return str(getattr(protagine, "__version__", "") or "unknown")


def initialize(conn, *, release: Optional[str] = None, now: Optional[float] = None) -> None:
    """In the ledger's initialization: the table, and ``started_at`` for this release."""
    conn.execute('''CREATE TABLE IF NOT EXISTS projection_backlog (
        id INTEGER PRIMARY KEY CHECK (id = 1), release TEXT NOT NULL, started_at REAL NOT NULL,
        last_admitted_at REAL NOT NULL DEFAULT 0, admitted INTEGER NOT NULL DEFAULT 0)''')
    release = release or _release()
    now = time.time() if now is None else float(now)
    row = conn.execute("SELECT release FROM projection_backlog WHERE id=1").fetchone()
    if row is None:
        conn.execute("INSERT INTO projection_backlog(id,release,started_at) VALUES (1,?,?)", (release, now))
    elif row[0] != release:
        conn.execute("UPDATE projection_backlog SET release=?,started_at=? WHERE id=1", (release, now))


def watermark(conn) -> float:
    """Jobs enqueued before this moment are backlog; without a record, none is."""
    row = conn.execute("SELECT started_at FROM projection_backlog WHERE id=1").fetchone()
    return float(row[0]) - RECENT_S if row is not None else -math.inf


def configured_per_hour() -> float:
    """``projections.backlog_per_hour`` from ``protagine.yaml``, read at most once a minute."""
    clock = time.monotonic()
    if clock - _configured["read_at"] < _CONFIG_TTL_S:
        return _configured["value"]
    try:
        from protagine.config import load_config
        value = float(load_config().get("projections.backlog_per_hour", DEFAULT_PER_HOUR))
    except Exception:
        value = float(DEFAULT_PER_HOUR)
    _configured.update(read_at=clock, value=max(0.0, value) if math.isfinite(value) else float(DEFAULT_PER_HOUR))
    return _configured["value"]


def admit(conn, *, now: Optional[float] = None, per_hour: Optional[float] = None) -> bool:
    """Called inside the claiming transaction, with a backlog job in hand: True, and the start
    recorded, when the hourly budget has room for it now."""
    per_hour = configured_per_hour() if per_hour is None else float(per_hour)
    if not per_hour > 0:
        return False
    now = time.time() if now is None else float(now)
    row = conn.execute("SELECT last_admitted_at FROM projection_backlog WHERE id=1").fetchone()
    if row is None:
        return False
    # A clock that moved back behind the last start does not stop the backlog for good.
    if 0 <= now - float(row[0]) < HOUR_S / per_hour:
        return False
    conn.execute("UPDATE projection_backlog SET last_admitted_at=?,admitted=admitted+1 WHERE id=1", (now,))
    return True


def status(conn) -> Dict[str, Any]:
    """The record as it stands, and the backlog each queue still holds."""
    row = conn.execute("SELECT release,started_at,last_admitted_at,admitted FROM projection_backlog "
                       "WHERE id=1").fetchone()
    if row is None:
        return {}
    before = float(row[1]) - RECENT_S
    waiting = {}
    for name, sql in (
            ("claim", "SELECT count(*) FROM source_claim_jobs WHERE status IN ('pending','running') AND enqueued_at<?"),
            ("appraisal", "SELECT count(*) FROM appraisal_runs WHERE status IN ('pending','running') AND enqueued_at<?"),
            ("capture", "SELECT count(*) FROM commitment_runs WHERE status IN ('pending','running') AND enqueued_at<?"),
            ("media", "SELECT count(*) FROM source_media WHERE status IN ('pending','running','video_pending',"
                      "'video_running') AND enqueued_at<?")):
        try:
            waiting[name] = int(conn.execute(sql, (before,)).fetchone()[0])
        except Exception:
            waiting[name] = 0
    return {"release": row[0], "started_at": float(row[1]), "backlog_before": before,
            "last_admitted_at": float(row[2]) or None, "admitted": int(row[3]), "waiting": waiting}


__all__ = ["DEFAULT_PER_HOUR", "EPOCH_SQL", "RECENT_S", "admit", "configured_per_hour", "initialize", "status",
           "watermark"]
