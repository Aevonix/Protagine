"""The instance id: one random UUID per state directory, created on first use.

It names this Protagine in the agent registry (``agents.protagine_id``) and binds a
backup to the instance it was taken from. It carries no key and signs nothing; the
chain that used to derive an identity from it is gone (M8).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

INSTANCE_ID_FILE = "instance-id"
# The chain wrote the same UUID under this name; it is read once so existing
# registry rows keep matching, then `protagine upgrade` moves it into the backup.
LEGACY_ID_FILE = "protagine-id"


def instance_id(state_dir: str | Path) -> str:
    """Return the instance id stored in ``<state_dir>/instance-id``, creating it if absent."""
    home = Path(state_dir)
    path = home / INSTANCE_ID_FILE
    if path.is_file():
        value = path.read_text().strip()
        if value:
            return value
    legacy = home / LEGACY_ID_FILE
    value = legacy.read_text().strip() if legacy.is_file() else ""
    value = value or str(uuid.uuid4())
    home.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(value + "\n")
    return value


def read_instance_id(state_dir: str | Path) -> str:
    """The instance id when one exists, else ``""`` (never creates one).

    A state directory not yet upgraded still names itself in ``protagine-id``;
    that value is the id :func:`instance_id` would adopt, so it is returned too.
    """
    home = Path(state_dir)
    for name in (INSTANCE_ID_FILE, LEGACY_ID_FILE):
        path = home / name
        value = path.read_text().strip() if path.is_file() else ""
        if value:
            return value
    return ""
