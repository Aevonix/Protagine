"""Session-safety tracking utilities (v0.13.0).

Tracks the timestamp of the owner's last message to prevent
agent workers from running destructive tasks during active sessions.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from protagine.util.temporal import now_utc

logger = logging.getLogger(__name__)

_LAST_MSG_PATH = Path(os.path.expanduser("~/.protagine/last_user_message_at.json"))


def load_last_user_message_at() -> Optional[str]:
    """Return ISO timestamp of last user message, or None."""
    try:
        with open(_LAST_MSG_PATH) as f:
            return json.load(f).get("timestamp")
    except Exception:
        return None


def save_last_user_message_at() -> None:
    """Persist current UTC time as last user message timestamp."""
    try:
        _LAST_MSG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_LAST_MSG_PATH, "w") as f:
            json.dump({"timestamp": now_utc().isoformat()}, f)
    except Exception as exc:
        logger.debug("Failed to save last_user_message_at: %s", exc)
