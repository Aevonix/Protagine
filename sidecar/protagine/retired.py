"""State left behind by the subsystems M8 deleted, moved aside by ``protagine upgrade``.

The belief engine (``protagine-beliefs.db``), the chain (``chain.db``, the protagine
and node identity files and key directories, the genesis and protagine manifests)
and the world model (``protagine_world_model.db``) each kept a file in the state
directory. The Neo4j graph memory and the continuous learner wrote nothing local.
``init.run_upgrade`` calls :func:`retire_state` right after it takes the backup; the
files land in ``<backup>/retired/`` instead of staying behind as orphans.
"""

from __future__ import annotations

from pathlib import Path
import shutil

RETIRED_FILES = (
    "protagine-beliefs.db",
    "chain.db",
    "protagine-id",
    "node-id",
    "node-cert.json",
    "genesis.json",
    "protagine-manifest.json",
    "protagine_world_model.db",
)
RETIRED_DIRS = ("protagine-keys", "node-keys")
RETIRED_STATE = RETIRED_FILES + RETIRED_DIRS
_SQLITE_SIDE_FILES = ("-wal", "-shm", "-journal")


def retired_state_present(home: str | Path) -> list[str]:
    """The retired files and directories that still exist under ``home``."""
    root = Path(home)
    return [name for name in RETIRED_STATE if (root / name).exists()]


def retire_state(home: str | Path, backup_dir: str | Path) -> list[str]:
    """Move every retired file (with its SQLite side files) into ``backup_dir/retired``.

    ``protagine-id`` is copied into ``instance-id`` before it moves, so the agent
    registry rows that name this instance keep matching. Returns one note per item;
    an already clean state directory returns ``[]`` and creates nothing.
    """
    root = Path(home)
    present = retired_state_present(root)
    if not present:
        return []
    if "protagine-id" in present:
        from protagine.instance import instance_id
        instance_id(root)
    destination = Path(backup_dir) / "retired"
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    notes: list[str] = []
    for name in present:
        source = root / name
        if source.is_dir():
            shutil.move(str(source), str(destination / name))
        else:
            for suffix in ("",) + _SQLITE_SIDE_FILES:
                candidate = root / (name + suffix)
                if candidate.exists():
                    shutil.move(str(candidate), str(destination / (name + suffix)))
        notes.append(f"retired {name} (moved to {destination})")
    return notes
