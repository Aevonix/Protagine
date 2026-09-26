"""Generic schema migration runner for Protagine SQLite stores.

Discovers numbered .sql files in a migrations directory, tracks which
have been applied via a ``schema_version`` table in the target database,
and applies unapplied ones in order.  Works with both sync (sqlite3)
and async (aiosqlite) connections.

Usage (sync)::

    import sqlite3
    conn = sqlite3.connect("protagine-channels.db")
    applied = run_migrations_sync(conn, Path("channels/migrations"))

Usage (async)::

    import aiosqlite
    db = await aiosqlite.connect("protagine-contacts.db")
    applied = await run_migrations(db, Path("contacts/migrations"))

Migration filenames must match ``NNN_description.sql`` (e.g.
``001_initial_schema.sql``).  The numeric prefix determines order.
Each file runs as one transaction together with its ``schema_version``
row (``_script``): either both land or neither does, so a failure, a
kill or a locked database part way leaves the store at the previous
version for a rerun, never with a migration applied and unrecorded. A
file therefore holds no ``BEGIN``/``COMMIT`` of its own. A file that
drops a column needs SQLite >= 3.35; both runners check that before
applying anything.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

_VERSION_DDL = """\
CREATE TABLE IF NOT EXISTS schema_version (
    version     TEXT PRIMARY KEY,
    filename    TEXT NOT NULL,
    applied_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""

_MIGRATION_RE = re.compile(r"^(\d+)_.+\.sql$")


DROP_COLUMN_SQLITE = (3, 35)
_DROP_COLUMN_RE = re.compile(r"\bDROP\s+COLUMN\b", re.IGNORECASE)


def _check_sqlite(pending: list[tuple[str, Path]]) -> None:
    """Refuse, before any file runs, a migration SQLite cannot apply (ALTER TABLE DROP COLUMN)."""
    if sqlite3.sqlite_version_info >= DROP_COLUMN_SQLITE:
        return
    for version, path in pending:
        if _DROP_COLUMN_RE.search(path.read_text()):
            raise RuntimeError(
                f"migration {version} ({path.name}) needs SQLite >= 3.35 (ALTER TABLE DROP COLUMN); "
                f"this Python links SQLite {sqlite3.sqlite_version}")


def _script(sql: str, *, table: str, version: str, filename: str) -> str:
    """One migration file and its version row as a single transaction for ``executescript``."""
    def quoted(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"
    return (f"BEGIN;\n{sql}\n;\nINSERT INTO {table}(version, filename) "
            f"VALUES ({quoted(version)}, {quoted(filename)});\nCOMMIT;\n")


def _discover(migrations_dir: Path) -> list[tuple[str, Path]]:
    """Return ``(version, path)`` pairs sorted by numeric prefix."""
    found: list[tuple[int, str, Path]] = []
    if not migrations_dir.is_dir():
        return []
    for p in migrations_dir.iterdir():
        m = _MIGRATION_RE.match(p.name)
        if m:
            found.append((int(m.group(1)), m.group(1).zfill(3), p))
    found.sort(key=lambda t: t[0])
    return [(v, p) for _, v, p in found]


# ── Sync API (sqlite3) ──────────────────────────────────────────────────


def run_migrations_sync(
    conn: sqlite3.Connection,
    migrations_dir: Path,
    *,
    table: str = "schema_version",
) -> list[str]:
    """Apply unapplied migrations using a sync sqlite3 connection.

    Returns the list of newly applied version strings.
    """
    ddl = _VERSION_DDL.replace("schema_version", table)
    conn.executescript(ddl)
    conn.commit()

    cur = conn.execute(f"SELECT version FROM {table}")  # noqa: S608
    applied = {row[0] for row in cur.fetchall()}

    pending = [(version, path) for version, path in _discover(migrations_dir) if version not in applied]
    _check_sqlite(pending)
    newly_applied: list[str] = []

    for version, path in pending:
        sql = path.read_text()
        logger.info("Applying migration %s (%s)", version, path.name)
        try:
            conn.executescript(_script(sql, table=table, version=version, filename=path.name))
        except BaseException:
            logger.error("Migration %s failed (%s)", version, path.name)
            conn.rollback()
            raise
        newly_applied.append(version)

    if newly_applied:
        logger.info(
            "Applied %d migration(s): %s",
            len(newly_applied),
            ", ".join(newly_applied),
        )
    return newly_applied


# ── Async API (aiosqlite) ───────────────────────────────────────────────


async def run_migrations(
    db: "aiosqlite.Connection",
    migrations_dir: Path,
    *,
    table: str = "schema_version",
) -> list[str]:
    """Apply unapplied migrations using an async aiosqlite connection.

    Returns the list of newly applied version strings.
    """
    ddl = _VERSION_DDL.replace("schema_version", table)
    await db.executescript(ddl)
    await db.commit()

    async with db.execute(f"SELECT version FROM {table}") as cur:  # noqa: S608
        rows = await cur.fetchall()
    applied = {row[0] for row in rows}

    pending = [(version, path) for version, path in _discover(migrations_dir) if version not in applied]
    _check_sqlite(pending)
    newly_applied: list[str] = []

    for version, path in pending:
        sql = path.read_text()
        logger.info("Applying migration %s (%s)", version, path.name)
        try:
            await db.executescript(_script(sql, table=table, version=version, filename=path.name))
        except BaseException:
            logger.error("Migration %s failed (%s)", version, path.name)
            await db.rollback()
            raise
        newly_applied.append(version)

    if newly_applied:
        logger.info(
            "Applied %d migration(s): %s",
            len(newly_applied),
            ", ".join(newly_applied),
        )
    return newly_applied


def applied_versions_sync(
    conn: sqlite3.Connection,
    *,
    table: str = "schema_version",
) -> set[str]:
    """Return the set of already-applied version strings (sync)."""
    try:
        cur = conn.execute(f"SELECT version FROM {table}")  # noqa: S608
        return {row[0] for row in cur.fetchall()}
    except sqlite3.OperationalError:
        return set()


async def applied_versions(
    db: "aiosqlite.Connection",
    *,
    table: str = "schema_version",
) -> set[str]:
    """Return the set of already-applied version strings (async)."""
    try:
        async with db.execute(f"SELECT version FROM {table}") as cur:  # noqa: S608
            rows = await cur.fetchall()
        return {row[0] for row in rows}
    except Exception:
        return set()
