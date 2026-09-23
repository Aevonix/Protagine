"""Sidecar client: settings, bearer key, short timeouts and a circuit breaker.

The plugin finds the sidecar through the Hermes config keys
``plugins.protagine.sidecar_url`` and ``plugins.protagine.key_file`` (written
by ``protagine init``). The key file's directory is the Protagine instance
directory, which also holds ``protagine.yaml`` and ``identity.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, Mapping

import httpx

logger = logging.getLogger(__name__)

PLUGIN_ID = "protagine"
WORKER_PROFILE = "protagine-act"
DEFAULT_URL = "http://127.0.0.1:7777"
MIND_STATUS_ROUTE = "/v1/mind/status"


class SidecarUnavailable(RuntimeError):
    """The sidecar gave no answer: connection refused, timeout or open breaker."""


def hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home()).expanduser()
    except ImportError:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()


def hermes_config() -> dict[str, Any]:
    """The merged Hermes config; falls back to the raw file outside Hermes."""
    try:
        from hermes_cli.config import load_config_readonly
        loaded = load_config_readonly()
        return loaded if isinstance(loaded, Mapping) else {}
    except Exception:
        return read_yaml(hermes_home() / "config.yaml")


def plugin_section(config: Mapping[str, Any] | None = None) -> dict[str, Any]:
    config = hermes_config() if config is None else config
    plugins = config.get("plugins") if isinstance(config, Mapping) else None
    section = plugins.get(PLUGIN_ID) if isinstance(plugins, Mapping) else None
    return dict(section) if isinstance(section, Mapping) else {}


_YAML_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_YAML_LOCK = threading.Lock()


def read_yaml(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Read a YAML mapping, cached by mtime; missing or invalid files read as {}."""
    path = Path(path)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return {}
    with _YAML_LOCK:
        cached = _YAML_CACHE.get(str(path))
        if cached is not None and cached[0] == mtime:
            return cached[1]
    try:
        import yaml
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        value = dict(loaded) if isinstance(loaded, Mapping) else {}
    except Exception as error:
        logger.warning("could not read %s: %s", path.name, type(error).__name__)
        value = {}
    with _YAML_LOCK:
        _YAML_CACHE[str(path)] = (mtime, value)
    return value


@dataclass
class Settings:
    sidecar_url: str
    key_file: Path
    api_key: str
    home: Path
    hermes_home: Path
    outbox_path: Path
    worker_profile: str = WORKER_PROFILE

    def mind(self) -> dict[str, Any]:
        value = read_yaml(self.home / "protagine.yaml").get("mind")
        return dict(value) if isinstance(value, Mapping) else {}

    def identity(self) -> dict[str, Any]:
        return read_yaml(self.home / "identity.yaml")

    def owner_contact_id(self) -> str:
        """The owner's sidecar contact: ``protagine.yaml`` ``owner.contact_id`` (written by
        ``protagine init``), else the same key in ``identity.yaml``."""
        for source in (read_yaml(self.home / "protagine.yaml"), self.identity()):
            owner = source.get("owner")
            if isinstance(owner, Mapping) and owner.get("contact_id"):
                return str(owner["contact_id"])
        return ""


def load_settings(config: Mapping[str, Any] | None = None) -> Settings:
    section = plugin_section(config)
    home = hermes_home()
    instance = Path(os.environ.get("PROTAGINE_HOME") or Path.home() / ".protagine").expanduser()
    key_file = Path(section.get("key_file") or instance / "api.key").expanduser()
    api_key = os.environ.get("PROTAGINE_API_KEY", "").strip()
    if not api_key:
        try:
            api_key = key_file.read_text(encoding="utf-8").strip().splitlines()[0].strip()
        except (OSError, IndexError):
            api_key = ""
    url = str(section.get("sidecar_url") or os.environ.get("PROTAGINE_URL") or "").strip()
    if not url:
        sidecar = read_yaml(key_file.parent / "protagine.yaml").get("sidecar")
        if isinstance(sidecar, Mapping) and sidecar.get("port"):
            url = f"http://{sidecar.get('host') or '127.0.0.1'}:{sidecar['port']}"
    return Settings(
        sidecar_url=(url or DEFAULT_URL).rstrip("/"), key_file=key_file, api_key=api_key,
        home=key_file.parent, hermes_home=home,
        outbox_path=Path(section.get("turn_outbox_path")
                         or home / "state" / "protagine-turn-outbox.sqlite3"),
        worker_profile=str(section.get("worker_profile") or WORKER_PROFILE),
    )


class ProtagineClient:
    """Synchronous HTTP client with a bearer key, short timeouts and a breaker.

    Three consecutive connection failures open the breaker for ``cooldown``
    seconds; while open, requests raise :class:`SidecarUnavailable` at once.
    """

    def __init__(self, settings: Settings | None = None, *, url: str | None = None,
                 api_key: str | None = None, timeout: float = 5.0, cooldown: float = 30.0):
        self.url = (url or (settings.sidecar_url if settings else DEFAULT_URL)).rstrip("/")
        self.api_key = api_key if api_key is not None else (settings.api_key if settings else "")
        self.timeout = timeout
        self.cooldown = cooldown
        self._lock = threading.Lock()
        self._failures = 0
        self._open_until = 0.0
        self._mind_routes: tuple[float, bool] | None = None

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    @property
    def breaker_open(self) -> bool:
        with self._lock:
            return time.monotonic() < self._open_until

    def request(self, method: str, path: str, *, timeout: float | None = None,
                **kwargs: Any) -> httpx.Response:
        if self.breaker_open:
            raise SidecarUnavailable("circuit breaker open")
        try:
            with httpx.Client(timeout=timeout or self.timeout, trust_env=False) as client:
                response = client.request(method, f"{self.url}{path}", headers=self.headers(),
                                          **kwargs)
        except (httpx.TransportError, OSError) as error:
            with self._lock:
                self._failures += 1
                if self._failures >= 3:
                    self._open_until = time.monotonic() + self.cooldown
            raise SidecarUnavailable(type(error).__name__) from error
        with self._lock:
            self._failures = 0
        return response

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> httpx.Response:
        return self.request("PATCH", path, **kwargs)

    def has_mind_routes(self, *, ttl: float = 300.0) -> bool | None:
        """Whether this sidecar serves ``/v1/mind/*``; None while unreachable."""
        with self._lock:
            cached = self._mind_routes
        if cached is not None and time.monotonic() - cached[0] < ttl:
            return cached[1]
        try:
            present = self.get(MIND_STATUS_ROUTE, timeout=2).status_code != 404
        except SidecarUnavailable:
            return None
        with self._lock:
            self._mind_routes = (time.monotonic(), present)
        return present

    def resolve_contact(self, platform: str, handle: str, *, create: bool = False,
                        timeout: float = 4.0) -> dict[str, Any] | None:
        """The contact behind a messaging handle, or None when there is none."""
        if not handle:
            return None
        params = {"gateway": platform or "", "address": handle}
        if create:
            params["create"] = "true"
        response = self.get("/v1/host/contacts/resolve", params=params, timeout=timeout)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        value = response.json()
        return value if isinstance(value, dict) and value.get("contact_id") else None

    def health(self, timeout: float = 3.0) -> dict[str, Any] | None:
        try:
            response = self.get("/v1/host/health", timeout=timeout)
            return response.json() if response.is_success else None
        except (SidecarUnavailable, ValueError):
            return None


__all__ = [
    "DEFAULT_URL", "PLUGIN_ID", "WORKER_PROFILE", "ProtagineClient", "Settings",
    "SidecarUnavailable", "hermes_config", "hermes_home", "load_settings", "plugin_section",
    "read_yaml",
]
