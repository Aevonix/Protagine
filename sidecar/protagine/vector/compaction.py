"""Routine compaction of the vector tables: one background task, one table at a time.

Every Lance commit writes a manifest listing every fragment of the table, and
each append adds a fragment. A table that is never compacted therefore grows
its version history quadratically: an upgraded store's conversations table held
74.7 GB of manifests around 1.67 GB of data. ``optimize`` merges the fragments
and deletes every older version, but it reads every retained manifest to do so.
Its cost is the bytes under ``_versions`` (about 130 MB/s), whatever it prunes:
seconds for a table compacted routinely, about eleven minutes for that store's
first pass. Splitting the pass by version age would read those bytes again for
each piece, so the work is bounded by when it runs instead:

- ``erasure``: after a forget, every table whatever its size, so erased text
  leaves the data files and the old versions (the forget route schedules it
  after it answers).
- ``nightly``: the mind's nightly upkeep schedules it once per night crossed.
  It covers every table with an older version or a pending deletion.
- ``threshold``: ``run`` checks every ``interval`` seconds. A table holding
  ``versions`` or more versions is compacted, unless its manifests exceed
  ``day_bytes``. Such a table waits for the nightly pass for at most
  ``defer_limit`` seconds, so a mind that is off cannot leave it growing.

Nothing here runs inside a request. ``optimize`` runs in Lance's own threads
while the event loop keeps serving. A table's pass holds the store's write
lock throughout, so no commit runs beside it: tables created by earlier
releases carry Lance's own auto-cleanup, which runs inside a commit and would
prune under the pass. Writes wait for the pass; reads do not, and the pass
prunes no version a read begun before it may still be reading
(``VectorStore._purge_deleted``). A pause separates tables. Each table's pass
is logged at info with its reason, its version count and size before and
after, and its duration.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from protagine.vector.collections import Collection

logger = logging.getLogger(__name__)

REASONS = ('erasure', 'nightly', 'threshold')      # strongest first: a coalesced pass takes the strongest
VERSIONS = 1000             # at most ~1000 versions after a compaction: tens of MB of manifests
DAY_BYTES = 1 << 30         # a pass reading more manifests than this (~8 s) waits for the night
DEFER_LIMIT = 24 * 3600.0
INTERVAL = 300.0
PAUSE = 30.0               # the longest breather between two tables


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, '') or default)
    except ValueError:
        return default
    return value if value > 0 else default


def _size(value: int) -> str:
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024 or unit == 'GB':
            return f'{value:.0f} {unit}' if unit == 'B' else f'{value:.2f} {unit}'
        value /= 1024
    return f'{value:.2f} GB'


@dataclass(frozen=True)
class TableFootprint:
    versions: int = 0
    manifest_bytes: int = 0
    deletions: int = 0

    @classmethod
    def read(cls, path: Optional[Path]) -> 'TableFootprint':
        if path is None:
            return cls()
        versions = manifest_bytes = deletions = 0
        try:
            with os.scandir(path / '_versions') as entries:
                for entry in entries:
                    if entry.name.endswith('.manifest'):
                        versions += 1
                        manifest_bytes += entry.stat().st_size
        except OSError:
            pass
        try:
            with os.scandir(path / '_deletions') as entries:
                deletions = sum(1 for _ in entries)
        except OSError:
            pass
        return cls(versions, manifest_bytes, deletions)


def _tree_bytes(path: Optional[Path]) -> int:
    total = 0
    if path is None:
        return total
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


class Compaction:
    """The vector store's single compaction task and its triggers (see the module docstring)."""

    def __init__(self, store: Any, *, versions: int | None = None, day_bytes: int | None = None,
                 defer_limit: float = DEFER_LIMIT, pause: float = PAUSE, clock=time.monotonic) -> None:
        self.store = store
        self.versions = versions or _env_int('PROTAGINE_VECTOR_COMPACT_VERSIONS', VERSIONS)
        self.day_bytes = day_bytes or _env_int('PROTAGINE_VECTOR_COMPACT_DAY_BYTES', DAY_BYTES)
        self.defer_limit = defer_limit
        self.pause = pause
        self.clock = clock
        self._task: Optional[asyncio.Task] = None
        self._loop: Optional[asyncio.Task] = None
        self._wanted: set[str] = set()
        self._deferred: dict[tuple[str, str], float] = {}

    @property
    def task(self) -> Optional[asyncio.Task]:
        return self._task

    def schedule(self, reason: str) -> asyncio.Task:
        """Ask for a pass in the background. One task at a time: a request while a pass runs asks
        for one more pass, so rows deleted after the running pass read a table are purged too."""
        if reason not in REASONS:
            raise ValueError(f'unknown compaction reason {reason!r}')
        self._wanted.add(reason)
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._drain(), name='protagine-vector-compaction')
        return self._task

    def start(self, *, interval: float = INTERVAL) -> asyncio.Task:
        """Start the threshold loop; ``close`` stops it."""
        if self._loop is None or self._loop.done():
            self._loop = asyncio.get_running_loop().create_task(
                self.run(interval=interval), name='protagine-vector-compaction-threshold')
        return self._loop

    async def run(self, *, interval: float = INTERVAL) -> None:
        """The threshold loop, for as long as the store is open."""
        while True:
            await asyncio.sleep(interval)
            try:
                await asyncio.shield(self.schedule('threshold'))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning('vector compaction threshold check failed', exc_info=True)

    async def close(self) -> None:
        """Stop the loop and any pass in flight (a cut pass leaves a consistent table)."""
        tasks = [task for task in (self._loop, self._task) if task is not None and not task.done()]
        self._loop = self._task = None
        self._wanted.clear()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _drain(self) -> None:
        while self._wanted:
            reason = next(name for name in REASONS if name in self._wanted)
            self._wanted.clear()
            try:
                await self.run_pass(reason)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning('vector compaction pass (%s) failed; the next pass retries it', reason, exc_info=True)

    async def _tables(self):
        store = self.store
        if store.catalog is None:
            sources = [('', store._db)] if store._db is not None else []
        else:
            sources = [(generation['id'], await store._generation_db(generation))
                       for generation in await asyncio.to_thread(store.catalog.generations)]
        for generation_id, db in sources:
            names = set(await db.table_names())
            for collection in Collection:
                if collection.value in names:
                    yield generation_id, db, collection.value

    @staticmethod
    def _path(db, name) -> Optional[Path]:
        uri = str(getattr(db, 'uri', '') or '')
        if not uri or ('://' in uri and not uri.startswith('file://')):
            return None
        path = Path(uri.removeprefix('file://')) / (name + '.lance')
        return path if path.is_dir() else None

    def _selected(self, reason: str, key: tuple[str, str], footprint: Optional[TableFootprint]) -> bool:
        if footprint is None:           # not on a local disk: nothing to count, so erasures and nights compact
            return reason != 'threshold'
        if footprint.versions <= 1 and not footprint.deletions:
            return False                # one version holds no older one and no soft-deleted row
        if reason == 'erasure':
            return True                 # every soft delete goes, whatever the size (a retry finishes the job)
        if reason == 'nightly':
            self._deferred.pop(key, None)
            return footprint.versions > 2 or footprint.deletions > 0
        if footprint.versions < self.versions:
            self._deferred.pop(key, None)
            return False
        if footprint.manifest_bytes <= self.day_bytes:
            return True
        now = self.clock()
        if key not in self._deferred:
            self._deferred[key] = now
            logger.info('vector compaction deferred: %s (generation %s) holds %d versions, %s of manifests; '
                        'its pass reads them all, so it waits for the nightly pass (at most %.0f h)',
                        key[1], key[0][:8] or '-', footprint.versions, _size(footprint.manifest_bytes),
                        self.defer_limit / 3600)
        waited = now - self._deferred[key]
        if waited >= self.defer_limit:
            logger.info('vector compaction of %s (generation %s) waited %.1f h for a nightly pass; running it now',
                        key[1], key[0][:8] or '-', waited / 3600)
            return True
        return False

    async def run_pass(self, reason: str) -> list[dict[str, Any]]:
        """One pass over every table, one at a time; the tables it compacted, as logged."""
        done = []
        async for generation_id, db, name in self._tables():
            path = self._path(db, name)
            footprint = None if path is None else await asyncio.to_thread(TableFootprint.read, path)
            key = (generation_id, name)
            if not self._selected(reason, key, footprint):
                continue
            if done and self.pause:
                # Serving gets a breather as long as the previous table took, up to ``pause``.
                await asyncio.sleep(min(self.pause, done[-1].get('seconds', 0.0)))
            done.append(await self._compact(reason, key, db, name, path, footprint or TableFootprint()))
        return done

    async def _compact(self, reason, key, db, name, path, before: TableFootprint) -> dict[str, Any]:
        generation = key[0][:8] or '-'
        size_before = await asyncio.to_thread(_tree_bytes, path)
        logger.info('vector compaction started (%s): %s (generation %s), %d versions, %s on disk, %s of manifests',
                    reason, name, generation, before.versions, _size(size_before), _size(before.manifest_bytes))
        started = time.monotonic()
        try:
            stats = await self.store._purge_deleted(db, name)
        except asyncio.CancelledError:
            logger.info('vector compaction cancelled (%s): %s (generation %s) after %.1f s',
                        reason, name, generation, time.monotonic() - started)
            raise
        except Exception:
            logger.warning('vector compaction failed (%s): %s (generation %s) after %.1f s',
                           reason, name, generation, time.monotonic() - started, exc_info=True)
            return {'table': name, 'generation': key[0], 'reason': reason, 'error': True}
        elapsed = time.monotonic() - started
        after = await asyncio.to_thread(TableFootprint.read, path)
        size_after = await asyncio.to_thread(_tree_bytes, path)
        self._deferred.pop(key, None)
        prune = getattr(stats, 'prune', None)
        logger.info('vector compaction finished (%s): %s (generation %s) in %.1f s, versions %d -> %d, '
                    'on disk %s -> %s, manifests %s -> %s%s', reason, name, generation, elapsed,
                    before.versions, after.versions, _size(size_before), _size(size_after),
                    _size(before.manifest_bytes), _size(after.manifest_bytes),
                    f', {prune.old_versions_removed} old versions pruned' if prune is not None else '')
        return {'table': name, 'generation': key[0], 'reason': reason, 'seconds': elapsed,
                'versions_before': before.versions, 'versions_after': after.versions,
                'bytes_before': size_before, 'bytes_after': size_after}


__all__ = ['Compaction', 'DAY_BYTES', 'DEFER_LIMIT', 'INTERVAL', 'REASONS', 'TableFootprint', 'VERSIONS']
