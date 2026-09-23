"""Protagine cognition server for agent runtimes.

A standalone FastAPI server that agent hosts (Hermes plugin, MCP harnesses,
REST integrations) mount via the ``/v1/host`` API surface.
"""

from pathlib import Path
import os


try:  # single source of truth: the installed package metadata (pyproject)
    from importlib.metadata import version as _pkg_version
    __version__ = _pkg_version("protagine")
except Exception:  # editable/unbuilt checkouts without installed metadata
    __version__ = "0.0.0+unknown"


def get_state_dir() -> Path:
    """Return the selected state directory without moving existing state.

    The instance directory (``$PROTAGINE_HOME``, default ``~/.protagine``)
    holds the state once ``protagine init`` has written ``protagine.yaml``.
    Unconfigured library use defaults to ``~/.protagine/data``.

    Creates the directory if it does not exist.
    """
    explicit = os.environ.get("PROTAGINE_STATE_DIR") or os.environ.get("PROTAGINE_HOME")
    if explicit:
        path = Path(explicit).expanduser()
    elif (Path.home() / ".protagine" / "protagine.yaml").is_file():
        path = Path.home() / ".protagine"
    else:
        path = Path.home() / ".protagine" / "data"
    path.mkdir(parents=True, exist_ok=True)
    return path
