"""Concerns and mind state: the two tables of ``mind.db`` (architecture 4.5, 5.3).

A concern is what a drive wants attended to. Repeats ``bump`` the same row
by ``dedup_key`` instead of piling up; salience decays with a 12 h
half-life; the workspace holds at most 24 open concerns; each concern has
a thought budget, and anti-rumination scales salience by 0.9 after progress
and 0.6 without. The top 3 open concerns are the broadcast set: the only
deliberation candidates, rendered in turn context and added to recall.

``mind_state`` keeps decaying levels with cited causes. Its key prefixes, one
owner each: ``drive.*`` and ``satiety.*`` (the drives), ``interest:*`` and
``question:*`` (curiosity), ``self.*`` (the self-narrative sections),
``consolidation.last`` (the nightly run) and ``people.digests.last`` (the
template digests' day). A text-only key carries no half-life, so
``MindState.decay`` never rewrites its ``updated_at``: ``consolidation.last``
and ``people.digests.last`` read the last run's moment from it. Affect arrives
with its own milestone and shares the table.
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

MIND_DB = "mind.db"
HALF_LIFE = timedelta(hours=12)
CAPACITY = 24
BROADCAST = 3
FLOOR = 0.05
MAX_THOUGHTS = 4
PROGRESS_FACTOR = 0.9
NO_PROGRESS_FACTOR = 0.6
SETTLED_FOR = timedelta(days=7)        # a resolved concern is not raised again for this long
RESOLVED_RETENTION = timedelta(days=30)
CONCERN_STATUSES = ("open", "intended", "resolved", "dropped")
CONCERN_KINDS = ("obligation", "interest", "question", "failure", "upkeep", "goal", "goal_step", "social")


def _ts(value: datetime) -> float:
    return value.timestamp()


def _dt(value: Any) -> datetime:
    return datetime.fromtimestamp(float(value or 0), timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _loads(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


@dataclass
class Concern:
    id: str
    drive: str
    kind: str
    summary: str
    dedup_key: str
    salience: float
    sources: List[str] = field(default_factory=list)
    thoughts_spent: int = 0
    max_thoughts: int = MAX_THOUGHTS
    status: str = "open"
    last_touched: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    intention_id: Optional[str] = None
    detail: Dict[str, Any] = field(default_factory=dict)

    @property
    def exhausted(self) -> bool:
        return self.thoughts_spent >= self.max_thoughts

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "drive": self.drive, "kind": self.kind, "summary": self.summary,
            "dedup_key": self.dedup_key, "salience": round(self.salience, 3), "sources": list(self.sources),
            "thoughts_spent": self.thoughts_spent, "max_thoughts": self.max_thoughts, "status": self.status,
            "last_touched": self.last_touched.isoformat(), "created_at": self.created_at.isoformat(),
            "intention_id": self.intention_id,
        }

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Concern":
        return cls(
            id=row["id"], drive=row["drive"], kind=row["kind"], summary=row["summary"], dedup_key=row["dedup_key"],
            salience=float(row["salience"] or 0.0), sources=_loads(row["sources_json"], []),
            thoughts_spent=int(row["thoughts_spent"] or 0), max_thoughts=int(row["max_thoughts"] or MAX_THOUGHTS),
            status=row["status"], last_touched=_dt(row["last_touched"]), created_at=_dt(row["created_at"]),
            intention_id=row["intention_id"], detail=_loads(row["detail_json"], {}),
        )


SCHEMA = """
CREATE TABLE IF NOT EXISTS mind_state (
  key          TEXT PRIMARY KEY,
  level        REAL, baseline REAL, half_life_s REAL,
  text         TEXT,
  causes_json  TEXT,
  updated_at   REAL
);
CREATE TABLE IF NOT EXISTS concerns (
  id TEXT PRIMARY KEY, drive TEXT, kind TEXT, summary TEXT,
  dedup_key TEXT UNIQUE, salience REAL, sources_json TEXT,
  thoughts_spent INT, max_thoughts INT,
  status TEXT,
  last_touched REAL,
  created_at REAL,
  intention_id TEXT,
  detail_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_concerns_status ON concerns(status, salience DESC);
CREATE INDEX IF NOT EXISTS idx_concerns_intention ON concerns(intention_id);
"""


def open_mind_db(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


class MindState:
    """Decaying levels and short texts keyed by name, each with up to 5 cited causes."""

    MAX_CAUSES = 5

    def __init__(self, conn: sqlite3.Connection, *, clock: Callable[[], datetime] | None = None) -> None:
        self.conn = conn
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def get(self, key: str) -> Optional[Dict[str, Any]]:
        row = self.conn.execute("SELECT * FROM mind_state WHERE key = ?", (key,)).fetchone()
        return self._entry(row) if row else None

    def items(self, prefix: str = "") -> List[Dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM mind_state WHERE key LIKE ? ORDER BY key", (prefix + "%",)).fetchall()
        return [self._entry(row) for row in rows]

    def set(self, key: str, *, level: float | None = None, baseline: float | None = None,
            half_life_s: float | None = None, text: str | None = None, causes: Iterable[str] | None = None,
            now: datetime | None = None) -> Dict[str, Any]:
        now = now or self.clock()
        current = self.get(key) or {}
        merged_causes = list(current.get("causes") or [])
        for cause in causes or []:
            if cause and cause not in merged_causes:
                merged_causes.append(str(cause))
        merged_causes = merged_causes[-self.MAX_CAUSES:]
        self.conn.execute(
            "INSERT INTO mind_state (key, level, baseline, half_life_s, text, causes_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET level=excluded.level, "
            "baseline=excluded.baseline, half_life_s=excluded.half_life_s, text=excluded.text, "
            "causes_json=excluded.causes_json, updated_at=excluded.updated_at",
            (key, current.get("level") if level is None else float(level),
             current.get("baseline", 0.0) if baseline is None else float(baseline),
             current.get("half_life_s") if half_life_s is None else float(half_life_s),
             current.get("text") if text is None else str(text), _json(merged_causes), _ts(now)))
        self.conn.commit()
        return self.get(key) or {}

    def bump(self, key: str, delta: float, *, cap: float = 1.0, half_life_s: float | None = None,
             causes: Iterable[str] | None = None, now: datetime | None = None) -> float:
        current = self.get(key) or {}
        level = max(0.0, min(cap, float(current.get("level") or 0.0) + float(delta)))
        return float(self.set(key, level=level, half_life_s=half_life_s, causes=causes, now=now)["level"] or 0.0)

    def delete(self, key: str) -> bool:
        cursor = self.conn.execute("DELETE FROM mind_state WHERE key = ?", (key,))
        self.conn.commit()
        return cursor.rowcount > 0

    def decay(self, now: datetime | None = None) -> int:
        """Every level with a half-life relaxes toward its baseline."""
        now = now or self.clock()
        count = 0
        for row in self.conn.execute("SELECT * FROM mind_state WHERE half_life_s IS NOT NULL AND half_life_s > 0").fetchall():
            level, baseline = float(row["level"] or 0.0), float(row["baseline"] or 0.0)
            elapsed = max(0.0, _ts(now) - float(row["updated_at"] or _ts(now)))
            if elapsed <= 0 or abs(level - baseline) < 1e-6:
                continue
            factor = 0.5 ** (elapsed / float(row["half_life_s"]))
            relaxed = baseline + (level - baseline) * factor
            if abs(relaxed - baseline) < 0.005:
                relaxed = baseline
            self.conn.execute("UPDATE mind_state SET level = ?, updated_at = ? WHERE key = ?",
                              (relaxed, _ts(now), row["key"]))
            count += 1
        self.conn.commit()
        return count

    @staticmethod
    def _entry(row: sqlite3.Row) -> Dict[str, Any]:
        return {"key": row["key"], "level": row["level"], "baseline": row["baseline"],
                "half_life_s": row["half_life_s"], "text": row["text"], "causes": _loads(row["causes_json"], []),
                "updated_at": _dt(row["updated_at"]).isoformat() if row["updated_at"] else None}


class Concerns:
    """The workspace: bump, decay, capacity, the broadcast set and anti-rumination."""

    def __init__(self, path: str | Path | sqlite3.Connection, *, clock: Callable[[], datetime] | None = None,
                 capacity: int = CAPACITY, half_life: timedelta = HALF_LIFE) -> None:
        self.conn = path if isinstance(path, sqlite3.Connection) else open_mind_db(path)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.capacity = int(capacity)
        self.half_life = half_life
        self.state = MindState(self.conn, clock=self.clock)
        self._last_decay: Optional[datetime] = None

    def close(self) -> None:
        with closing(self.conn):
            pass

    # -- reads ------------------------------------------------------------------------

    def get(self, concern_id: str) -> Optional[Concern]:
        row = self.conn.execute("SELECT * FROM concerns WHERE id = ?", (concern_id,)).fetchone()
        return Concern.from_row(row) if row else None

    def by_key(self, dedup_key: str) -> Optional[Concern]:
        row = self.conn.execute("SELECT * FROM concerns WHERE dedup_key = ?", (dedup_key,)).fetchone()
        return Concern.from_row(row) if row else None

    def by_intention(self, intention_id: str) -> Optional[Concern]:
        row = self.conn.execute("SELECT * FROM concerns WHERE intention_id = ? ORDER BY last_touched DESC LIMIT 1",
                                (intention_id,)).fetchone()
        return Concern.from_row(row) if row else None

    def open(self, *, limit: int = 100, status: Sequence[str] = ("open",)) -> List[Concern]:
        marks = ",".join("?" * len(status))
        rows = self.conn.execute(
            f"SELECT * FROM concerns WHERE status IN ({marks}) ORDER BY salience DESC, last_touched DESC LIMIT ?",
            (*status, int(limit))).fetchall()
        return [Concern.from_row(row) for row in rows]

    def count(self, status: str = "open") -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM concerns WHERE status = ?", (status,)).fetchone()[0])

    def top(self, k: int = BROADCAST) -> List[Concern]:
        """The broadcast set: the most salient open concerns with thought budget left."""
        return [c for c in self.open(limit=max(k * 4, 12)) if not c.exhausted][:k]

    def settled_keys(self, since: datetime) -> set[str]:
        """The keys of concerns resolved since ``since``, and the stable bases of the recurring ones."""
        rows = self.conn.execute("SELECT dedup_key, detail_json FROM concerns WHERE status = 'resolved' "
                                 "AND last_touched >= ?", (_ts(since),)).fetchall()
        keys = {row[0] for row in rows}
        keys |= {base for base in (_loads(row[1], {}).get("dedup_base") for row in rows) if base}
        return keys

    def touched_since(self, since: datetime) -> List[Concern]:
        rows = self.conn.execute("SELECT * FROM concerns WHERE last_touched >= ? ORDER BY last_touched DESC",
                                 (_ts(since),)).fetchall()
        return [Concern.from_row(row) for row in rows]

    # -- writes -----------------------------------------------------------------------

    def bump(self, *, drive: str, kind: str, summary: str, dedup_key: str, salience: float = 0.5,
             sources: Iterable[str] = (), detail: Dict[str, Any] | None = None,
             max_thoughts: int | None = None, now: datetime | None = None) -> Tuple[Optional[Concern], str]:
        """Raise a concern; a repeat bumps the existing row.

        The outcome is ``created``, ``bumped``, ``reopened`` (after a
        resolution or a drop), ``noted`` (already under way, nothing new) or
        ``settled`` (resolved less than a week ago: not raised again).
        """
        now = now or self.clock()
        kind = kind if kind in CONCERN_KINDS else "obligation"
        salience = max(0.0, min(1.0, float(salience)))
        sources = [str(s) for s in sources if str(s)][:12]
        existing = self.by_key(dedup_key[:200])
        if existing is None:
            concern = Concern(id=uuid.uuid4().hex[:16], drive=drive, kind=kind, summary=summary[:300],
                              dedup_key=dedup_key[:200], salience=salience, sources=sources,
                              max_thoughts=int(max_thoughts or MAX_THOUGHTS), last_touched=now, created_at=now,
                              detail=dict(detail or {}))
            self.conn.execute(
                "INSERT INTO concerns (id, drive, kind, summary, dedup_key, salience, sources_json, thoughts_spent, "
                "max_thoughts, status, last_touched, created_at, intention_id, detail_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 'open', ?, ?, NULL, ?)",
                (concern.id, drive, kind, concern.summary, concern.dedup_key, salience, _json(sources),
                 concern.max_thoughts, _ts(now), _ts(now), _json(concern.detail)))
            self.conn.commit()
            return concern, "created"
        if existing.status == "resolved" and now - existing.last_touched < SETTLED_FOR:
            return existing, "settled"
        if existing.status == "intended":
            # Work is already under way; the repeat is noted on the concern without re-raising it.
            # New evidence (a source the concern did not have) is ``bumped``: the event that lets
            # the tick reconsider the intention. The same evidence again is only ``noted``.
            merged = list(dict.fromkeys([*existing.sources, *sources]))[-12:]
            new_evidence = set(merged) - set(existing.sources)
            self.conn.execute("UPDATE concerns SET sources_json = ?, detail_json = ?, last_touched = ? WHERE id = ?",
                              (_json(merged), _json({**existing.detail, **(detail or {})}),
                               _ts(now) if new_evidence else _ts(existing.last_touched), existing.id))
            self.conn.commit()
            return self.get(existing.id), "bumped" if new_evidence else "noted"
        reopened = existing.status in {"resolved", "dropped"}
        merged_sources = list(dict.fromkeys([*existing.sources, *sources]))[-12:]
        level = salience if reopened else max(existing.salience, salience)
        self.conn.execute(
            "UPDATE concerns SET salience = ?, sources_json = ?, summary = ?, detail_json = ?, status = 'open', "
            "last_touched = ?, thoughts_spent = CASE WHEN ? THEN 0 ELSE thoughts_spent END, intention_id = NULL "
            "WHERE id = ?",
            (level, _json(merged_sources), summary[:300], _json({**existing.detail, **(detail or {})}), _ts(now),
             int(reopened), existing.id))
        self.conn.commit()
        return self.get(existing.id), "reopened" if reopened else "bumped"

    def decay(self, now: datetime | None = None) -> Dict[str, int]:
        """Half-life decay of every open concern since the last decay, then the floor and the capacity."""
        now = now or self.clock()
        previous, self._last_decay = self._last_decay, now
        if previous is not None and now > previous:
            factor = 0.5 ** ((now - previous).total_seconds() / self.half_life.total_seconds())
            self.conn.execute("UPDATE concerns SET salience = salience * ? WHERE status = 'open'", (factor,))
        open_rows = self.open(limit=10000)
        third = open_rows[BROADCAST - 1].salience if len(open_rows) >= BROADCAST else 0.0
        pinned = 0
        for concern in open_rows:
            # An open goal's concern does not decay below the broadcast set (architecture 4.5).
            if concern.kind == "goal" and concern.salience < third:
                self.conn.execute("UPDATE concerns SET salience = ? WHERE id = ?", (third, concern.id))
                pinned += 1
        evicted = 0
        for concern in self.open(limit=10000):
            if concern.salience < FLOOR and concern.kind != "goal":
                self._set_status(concern.id, "dropped", now, note="faded")
                evicted += 1
        remaining = self.open(limit=10000)
        for concern in reversed(remaining[self.capacity:]):
            if concern.kind != "goal":
                self._set_status(concern.id, "dropped", now, note="over capacity")
                evicted += 1
        self.conn.commit()
        return {"evicted": evicted, "pinned": pinned}

    def intended(self, concern_id: str, intention_id: str, now: datetime | None = None) -> Optional[Concern]:
        now = now or self.clock()
        self.conn.execute("UPDATE concerns SET status = 'intended', intention_id = ?, last_touched = ? WHERE id = ?",
                          (intention_id, _ts(now), concern_id))
        self.conn.commit()
        return self.get(concern_id)

    def progress(self, concern_id: str, *, progressed: bool, note: str = "",
                 now: datetime | None = None) -> Optional[Concern]:
        """One thought spent; salience x0.9 after progress, x0.6 without (anti-rumination)."""
        now = now or self.clock()
        concern = self.get(concern_id)
        if concern is None:
            return None
        factor = PROGRESS_FACTOR if progressed else NO_PROGRESS_FACTOR
        detail = dict(concern.detail)
        if note:
            detail["last_note"] = str(note)[:500]
        self.conn.execute(
            "UPDATE concerns SET thoughts_spent = thoughts_spent + 1, salience = salience * ?, status = 'open', "
            "intention_id = NULL, last_touched = ?, detail_json = ? WHERE id = ?",
            (factor, _ts(now), _json(detail), concern_id))
        self.conn.commit()
        return self.get(concern_id)

    def resolve(self, concern_id: str, note: str = "", now: datetime | None = None) -> Optional[Concern]:
        return self._set_status(concern_id, "resolved", now or self.clock(), note=note, commit=True)

    def drop(self, concern_id: str, note: str = "", now: datetime | None = None) -> Optional[Concern]:
        return self._set_status(concern_id, "dropped", now or self.clock(), note=note, commit=True)

    def prune(self, before: datetime) -> int:
        cursor = self.conn.execute("DELETE FROM concerns WHERE status IN ('resolved', 'dropped') AND last_touched < ?",
                                   (_ts(before),))
        self.conn.commit()
        return cursor.rowcount

    def _set_status(self, concern_id: str, status: str, now: datetime, *, note: str = "",
                    commit: bool = False) -> Optional[Concern]:
        concern = self.get(concern_id)
        if concern is None:
            return None
        detail = dict(concern.detail)
        if note:
            detail["closed"] = str(note)[:300]
        self.conn.execute("UPDATE concerns SET status = ?, last_touched = ?, detail_json = ? WHERE id = ?",
                          (status, _ts(now), _json(detail), concern_id))
        if commit:
            self.conn.commit()
        return self.get(concern_id)


def salience_after(salience: float, *, progressed: bool) -> float:
    """The anti-rumination rule as a pure function (used by the drive tests)."""
    return max(0.0, min(1.0, float(salience) * (PROGRESS_FACTOR if progressed else NO_PROGRESS_FACTOR)))


def decayed(salience: float, elapsed: timedelta, *, half_life: timedelta = HALF_LIFE) -> float:
    if elapsed.total_seconds() <= 0:
        return float(salience)
    return float(salience) * math.pow(0.5, elapsed.total_seconds() / half_life.total_seconds())


__all__ = ["BROADCAST", "CAPACITY", "CONCERN_KINDS", "CONCERN_STATUSES", "Concern", "Concerns", "FLOOR",
           "HALF_LIFE", "MAX_THOUGHTS", "MIND_DB", "MindState", "SETTLED_FOR", "decayed", "open_mind_db",
           "salience_after"]
