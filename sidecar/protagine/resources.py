"""Process limits the sidecar needs: the open-file limit.

LanceDB keeps one descriptor per data file it touches, a running instance
holds a few hundred, and macOS starts a user process with a soft limit of
256. The generated service unit asks the manager for ``OPEN_FILES``, and the
server raises its own soft limit to the same figure at startup where the
hard limit allows, so a plain ``protagine start`` is safe as well.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

#: Open files the sidecar asks for: the service unit's limit and the server's own raise.
OPEN_FILES = 16384


def raise_open_file_limit(target: int = OPEN_FILES) -> tuple[int, int] | None:
    """Raise this process's soft ``RLIMIT_NOFILE`` to ``target`` where the hard limit allows.

    Never lowers a soft limit already above ``target`` and never touches the
    hard limit. Returns the resulting ``(soft, hard)`` pair, or None on a
    platform without ``resource``.
    """
    try:
        import resource
    except ImportError:  # pragma: no cover - not a POSIX platform
        return None
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    ceiling = target if hard == resource.RLIM_INFINITY else min(target, hard)
    if soft != resource.RLIM_INFINITY and soft < ceiling:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (ceiling, hard))
        except (ValueError, OSError) as exc:
            logger.warning("open file limit stays at %d (raise to %d refused: %s)", soft, ceiling, exc)
            return soft, hard
        soft = ceiling
    if soft != resource.RLIM_INFINITY and soft < target:
        logger.warning("open file limit is %d (hard limit %s), below the %d the vector store wants under load; "
                       "raise the hard limit for the service", soft, hard, target)
    else:
        logger.info("open file limit: %s", "unlimited" if soft == resource.RLIM_INFINITY else soft)
    return soft, hard
