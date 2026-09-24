"""``protagine init``, ``protagine upgrade`` and ``protagine init --uninstall``.

Stock Hermes plus one package plus one command plus a restart::

    pipx install protagine  && protagine init    && hermes gateway restart   # install
    pipx upgrade protagine  && protagine upgrade && hermes gateway restart   # update

``init`` performs seven idempotent steps: it asks for the identity and the
autonomy level, writes ``protagine.yaml``, ``identity.yaml`` and ``api.key``,
points the router at the endpoint Hermes uses, installs the matching
``protagine-hermes`` adapter into the Python of the ``hermes`` executable and
runs ``pip check`` there, writes the Hermes configuration keys the adapter
needs, creates the ``protagine-act`` worker profile and installs the sidecar
user service. It writes no Hermes admin lists. Nothing here restarts a running
gateway; the command to do that is printed at the end.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as _dt
import getpass
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

import yaml

from protagine import __version__
from protagine import config as configuration
from protagine.config import (
    AUTONOMY_LEVELS,
    CONFIG_FILE,
    IDENTITY_FILE,
    KEY_FILE,
    LLM_CONFIG_FILE,
    Config,
    ConfigError,
    load_config,
    load_identity,
    read_api_key,
    save_config,
    save_identity,
    write_api_key,
)

ADAPTER_DISTRIBUTION = "protagine-hermes"
HERMES_DISTRIBUTION = "hermes-agent"
SUPPORTED_HERMES_MIN = (0, 21, 3)
SUPPORTED_HERMES_MAX_EXCLUSIVE = (0, 22, 0)
SUPPORTED_HERMES_RANGE = ">=0.21.3,<0.22"
WORKER_PROFILE = "protagine-act"
PROTECTED_PATTERNS = (CONFIG_FILE, IDENTITY_FILE, KEY_FILE)
PLUGIN_NAME = "protagine"
MEMORY_PROVIDER = "protagine-memory"
SKILLS_DIR = "skills"
BACKUPS_DIR = "backups"
LEGACY_MANIFEST = "instance.json"
LEGACY_KEYRING = "api-keyring.json"

_MODEL_ROLES = ("small", "medium", "large")


class InitError(RuntimeError):
    """A step cannot proceed; nothing after it ran."""


def _say(message: str = "") -> None:
    print(message)


def _utc_stamp() -> str:
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _run(command: list[str], *, timeout: int = 900, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(part) for part in command], capture_output=True, text=True, timeout=timeout,
        env=env, check=False,
    )


# ---------------------------------------------------------------------------
# Hermes discovery
# ---------------------------------------------------------------------------

def parse_version(text: str) -> tuple[int, ...]:
    parts = re.findall(r"\d+", text or "")[:3]
    return tuple(int(part) for part in parts) + (0,) * (3 - len(parts))


def hermes_version_supported(version: str) -> bool:
    parsed = parse_version(version)
    return SUPPORTED_HERMES_MIN <= parsed < SUPPORTED_HERMES_MAX_EXCLUSIVE


def find_hermes_executable(explicit: str | None = None) -> Path | None:
    candidate = explicit or shutil.which("hermes")
    if not candidate:
        return None
    path = Path(candidate).expanduser()
    return path if path.is_file() else None


def python_of_executable(executable: Path) -> Path | None:
    """The interpreter a console script runs with, read from its shebang."""
    try:
        with open(executable, "rb") as stream:
            first = stream.readline(512)
    except OSError:
        return None
    if not first.startswith(b"#!"):
        return None
    line = first[2:].decode("utf-8", "replace").strip()
    if not line:
        return None
    parts = line.split()
    # ``#!/usr/bin/env python`` resolves through PATH; everything else is a path.
    if parts[0].endswith("/env") and len(parts) > 1:
        found = shutil.which(parts[1])
        return Path(found) if found else None
    # pipx and uv write ``#!<venv>/bin/python`` (optionally with flags).
    path = Path(parts[0])
    return path if path.exists() else None


def hermes_python_version(python: Path) -> str | None:
    """The installed ``hermes-agent`` version in *python*, or ``None``."""
    result = _run([python, "-I", "-c",
                   "import importlib.metadata as m; print(m.version('hermes-agent'))"], timeout=60)
    if result.returncode != 0:
        return None
    return result.stdout.strip().splitlines()[-1] if result.stdout.strip() else None


def resolve_hermes_python(explicit: str | None, configured: str | None) -> tuple[Path, str]:
    """Find the Python that runs ``hermes`` and check its version."""
    candidates: list[Path] = []
    for value in (explicit, configured):
        if value:
            candidates.append(Path(value).expanduser())
    executable = find_hermes_executable()
    if executable is not None:
        python = python_of_executable(executable)
        if python is not None:
            candidates.append(python)
    for python in candidates:
        if not python.exists():
            continue
        version = hermes_python_version(python)
        if not version:
            continue
        if not hermes_version_supported(version):
            raise InitError(
                f"Hermes {version} in {python} is outside the supported range {SUPPORTED_HERMES_RANGE}"
            )
        return python, version
    raise InitError(
        "no Hermes installation found: put the 'hermes' executable on PATH, or pass "
        "--hermes-python with the interpreter that runs it"
    )


def resolve_hermes_home(explicit: str | None, configured: str | None) -> Path:
    selected = explicit or os.environ.get("HERMES_HOME") or configured or "~/.hermes"
    return Path(selected).expanduser()


def profiles_root(hermes_home: Path) -> Path:
    """Named profiles live under the Hermes root even when the home is itself a profile."""
    if hermes_home.parent.name == "profiles":
        return hermes_home.parent
    return hermes_home / "profiles"


# ---------------------------------------------------------------------------
# Adapter install in Hermes' environment
# ---------------------------------------------------------------------------

def installed_adapter_version(python: Path) -> str | None:
    result = _run([python, "-I", "-c",
                   f"import importlib.metadata as m; print(m.version('{ADAPTER_DISTRIBUTION}'))"], timeout=60)
    if result.returncode != 0:
        return None
    return result.stdout.strip().splitlines()[-1] if result.stdout.strip() else None


def _package_manager(python: Path) -> Callable[..., list[str]]:
    """A command builder: ``pip`` inside *python* when it has one, else ``uv pip`` targeting it."""
    if _run([python, "-m", "pip", "--version"], timeout=60).returncode == 0:
        return lambda verb, *args: [str(python), "-m", "pip", verb, *args]
    uv = shutil.which("uv")
    if uv:
        return lambda verb, *args: [uv, "pip", verb, "--python", str(python), *args]
    raise InitError(
        f"{python} has no pip and 'uv' is not on PATH; install one of them to manage Hermes' environment"
    )


def install_adapter(python: Path, version: str, *, source: str | None = None) -> bool:
    """Install ``protagine-hermes==version`` (or *source*) into *python*; True when it changed."""
    current = installed_adapter_version(python)
    if source is None and current == version:
        return False
    manager = _package_manager(python)
    requirement = source or f"{ADAPTER_DISTRIBUTION}=={version}"
    commands = [manager("install", "--quiet", requirement)]
    if source is not None:
        # A wheel of the same version must still replace what is installed; do
        # that without touching dependencies, which Hermes pins exactly.
        commands.insert(0, manager("install", "--quiet", "--no-deps", "--force-reinstall", requirement))
    for command in commands:
        result = _run(command, timeout=900)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise InitError("adapter install failed in Hermes' environment: "
                            + (detail[-1] if detail else "unknown error"))
    return source is not None or installed_adapter_version(python) != current


def uninstall_adapter(python: Path) -> bool:
    if installed_adapter_version(python) is None:
        return False
    manager = _package_manager(python)
    command = manager("uninstall", ADAPTER_DISTRIBUTION)
    if command[1] == "-m":  # pip asks for confirmation; uv does not
        command.insert(4, "--yes")
    result = _run(command, timeout=300)
    return result.returncode == 0


def pip_check(python: Path) -> tuple[bool, str]:
    result = _run(_package_manager(python)("check"), timeout=300)
    output = (result.stdout + result.stderr).strip()
    return result.returncode == 0, output


def protagine_in_hermes_environment(python: Path) -> bool:
    result = _run([python, "-I", "-c",
                   "import importlib.metadata as m; m.version('protagine')"], timeout=60)
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Hermes configuration
# ---------------------------------------------------------------------------

def read_hermes_config(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise InitError(f"{path} must contain a YAML mapping")
    return loaded


def _mapping(parent: dict, key: str) -> dict:
    value = parent.get(key)
    if value is None:
        value = parent[key] = {}
    if not isinstance(value, dict):
        raise InitError(f"Hermes config key '{key}' must be a mapping")
    return value


def _string_list(parent: dict, key: str) -> list:
    value = parent.get(key)
    if value is None:
        value = parent[key] = []
    if not isinstance(value, list):
        raise InitError(f"Hermes config key '{key}' must be a list")
    return value


def reconcile_hermes_config(config: dict[str, Any], *, sidecar_url: str, key_file: Path,
                            skills_dir: Path) -> tuple[dict[str, Any], list[str]]:
    """Return the config with every key the adapter needs, and the changes made."""
    result = copy.deepcopy(config)
    changes: list[str] = []
    plugins = _mapping(result, "plugins")
    enabled = _string_list(plugins, "enabled")
    if PLUGIN_NAME not in enabled:
        enabled.append(PLUGIN_NAME)
        changes.append("plugins.enabled += protagine")
    disabled = plugins.get("disabled")
    if isinstance(disabled, list) and PLUGIN_NAME in disabled:
        disabled.remove(PLUGIN_NAME)
        changes.append("plugins.disabled -= protagine")
    if plugins.get("hook_callback_timeout") != 0:
        plugins["hook_callback_timeout"] = 0
        changes.append("plugins.hook_callback_timeout: 0")
    settings = _mapping(plugins, PLUGIN_NAME)
    if settings.get("sidecar_url") != sidecar_url:
        settings["sidecar_url"] = sidecar_url
        changes.append("plugins.protagine.sidecar_url")
    if settings.get("key_file") != str(key_file):
        settings["key_file"] = str(key_file)
        changes.append("plugins.protagine.key_file")
    memory = _mapping(result, "memory")
    if memory.get("provider") != MEMORY_PROVIDER:
        memory["provider"] = MEMORY_PROVIDER
        changes.append("memory.provider: protagine-memory")
    kanban = _mapping(result, "kanban")
    if kanban.get("dispatch_in_gateway") is not True:
        kanban["dispatch_in_gateway"] = True
        changes.append("kanban.dispatch_in_gateway: true")
    skills = _mapping(result, "skills")
    external = _string_list(skills, "external_dirs")
    if str(skills_dir) not in external:
        external.append(str(skills_dir))
        changes.append("skills.external_dirs += " + str(skills_dir))
    security = _mapping(result, "security")
    patterns = _string_list(security, "protected_instruction_extra_patterns")
    for pattern in PROTECTED_PATTERNS:
        if pattern not in patterns:
            patterns.append(pattern)
            changes.append("security.protected_instruction_extra_patterns += " + pattern)
    return result, changes


def strip_hermes_config(config: dict[str, Any], *, skills_dir: Path | None) -> tuple[dict[str, Any], list[str]]:
    """Remove every key ``init`` wrote; the rest of the file is untouched."""
    result = copy.deepcopy(config)
    changes: list[str] = []
    plugins = result.get("plugins")
    if isinstance(plugins, dict):
        enabled = plugins.get("enabled")
        if isinstance(enabled, list) and PLUGIN_NAME in enabled:
            enabled.remove(PLUGIN_NAME)
            changes.append("plugins.enabled -= protagine")
        if PLUGIN_NAME in plugins:
            del plugins[PLUGIN_NAME]
            changes.append("plugins.protagine removed")
        if plugins.get("hook_callback_timeout") == 0:
            del plugins["hook_callback_timeout"]
            changes.append("plugins.hook_callback_timeout removed")
        for key in ("enabled",):
            if plugins.get(key) == []:
                del plugins[key]
        if not plugins:
            del result["plugins"]
    memory = result.get("memory")
    if isinstance(memory, dict) and memory.get("provider") == MEMORY_PROVIDER:
        del memory["provider"]
        changes.append("memory.provider removed")
        if not memory:
            del result["memory"]
    kanban = result.get("kanban")
    if isinstance(kanban, dict) and "dispatch_in_gateway" in kanban:
        del kanban["dispatch_in_gateway"]
        changes.append("kanban.dispatch_in_gateway removed")
        if not kanban:
            del result["kanban"]
    skills = result.get("skills")
    if isinstance(skills, dict):
        external = skills.get("external_dirs")
        if isinstance(external, list) and skills_dir is not None and str(skills_dir) in external:
            external.remove(str(skills_dir))
            changes.append("skills.external_dirs -= " + str(skills_dir))
            if not external:
                del skills["external_dirs"]
        if not skills:
            del result["skills"]
    security = result.get("security")
    if isinstance(security, dict):
        patterns = security.get("protected_instruction_extra_patterns")
        if isinstance(patterns, list):
            kept = [pattern for pattern in patterns if pattern not in PROTECTED_PATTERNS]
            if kept != patterns:
                changes.append("security.protected_instruction_extra_patterns trimmed")
                if kept:
                    security["protected_instruction_extra_patterns"] = kept
                else:
                    del security["protected_instruction_extra_patterns"]
        if not security:
            del result["security"]
    return result, changes


def write_hermes_config(path: Path, config: dict[str, Any], *, backup_dir: Path) -> None:
    """Atomically replace the Hermes config, keeping the previous bytes in *backup_dir*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise InitError(f"{path} is a symlink; point it at a regular file first")
    if path.is_file():
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copy2(path, backup_dir / f"hermes-config-{_utc_stamp()}.yaml")
    mode = path.stat().st_mode & 0o777 if path.is_file() else 0o600
    temporary = path.with_name(f".{path.name}.protagine-{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(yaml.safe_dump(config, sort_keys=False, allow_unicode=True))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Worker profile
# ---------------------------------------------------------------------------

def worker_profile_config(main_config: dict[str, Any], cfg: Config, *, sidecar_url: str,
                          key_file: Path) -> dict[str, Any]:
    """The ``protagine-act`` profile: the main model, the mind toolsets and the deny list."""
    profile: dict[str, Any] = {}
    model = main_config.get("model")
    if model:
        profile["model"] = copy.deepcopy(model)
    profile["toolsets"] = list(cfg.get("mind.worker_toolsets") or [])
    profile["approvals"] = {"deny": list(cfg.get("mind.deny.commands") or [])}
    profile["memory"] = {"provider": MEMORY_PROVIDER}
    profile["plugins"] = {
        "enabled": [PLUGIN_NAME],
        "hook_callback_timeout": 0,
        PLUGIN_NAME: {"sidecar_url": sidecar_url, "key_file": str(key_file)},
    }
    profile["security"] = {"protected_instruction_extra_patterns": list(PROTECTED_PATTERNS)}
    return profile


def render_worker_profile(profile: dict[str, Any]) -> str:
    return yaml.safe_dump(profile, sort_keys=False, allow_unicode=True)


def worker_profile_pending(root: Path, profile: dict[str, Any]) -> bool:
    """True when ``profiles/protagine-act/config.yaml`` is missing or differs from *profile*."""
    config_path = root / WORKER_PROFILE / "config.yaml"
    try:
        return config_path.read_text(encoding="utf-8") != render_worker_profile(profile)
    except OSError:
        return True


def write_worker_profile(root: Path, profile: dict[str, Any]) -> bool:
    """Create or update ``profiles/protagine-act``; True when something changed."""
    directory = root / WORKER_PROFILE
    tombstone = root / ".deleted" / WORKER_PROFILE
    changed = False
    if tombstone.exists():
        tombstone.unlink()
        changed = True
    for name in ("memories", "sessions", "skills", "logs", "workspace"):
        (directory / name).mkdir(parents=True, exist_ok=True)
    config_path = directory / "config.yaml"
    content = render_worker_profile(profile)
    if not config_path.is_file() or config_path.read_text(encoding="utf-8") != content:
        config_path.write_text(content, encoding="utf-8")
        os.chmod(config_path, 0o600)
        changed = True
    env_path = directory / ".env"
    if not env_path.exists():
        env_path.write_text("# Profile-scoped credentials; the model's key is inherited from the main profile.\n",
                            encoding="utf-8")
        os.chmod(env_path, 0o600)
        changed = True
    return changed


def remove_worker_profile(root: Path, *, backup_dir: Path) -> bool:
    """Move ``profiles/protagine-act`` (its sessions, memories and logs included) into
    ``backup_dir/profiles`` and leave Hermes' tombstone; True when the profile existed."""
    directory = root / WORKER_PROFILE
    if not directory.exists():
        return False
    destination = backup_dir / "profiles" / WORKER_PROFILE
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    shutil.move(str(directory), str(destination))
    tombstone = root / ".deleted" / WORKER_PROFILE
    tombstone.parent.mkdir(parents=True, exist_ok=True)
    tombstone.write_text("deleted\n", encoding="utf-8")
    return True


def adapter_users(hermes_home: Path) -> list[str]:
    """The other configs under the same Hermes root that still enable the adapter.

    Profiles share the ``hermes`` executable's Python, so the package stays installed
    while any of them lists the plugin or selects the memory provider.
    """
    root = profiles_root(hermes_home)
    candidates = [root.parent / "config.yaml", *sorted(root.glob("*/config.yaml"))]
    users: list[str] = []
    for path in candidates:
        if path.parent in {hermes_home, root / WORKER_PROFILE}:
            continue
        try:
            config = read_hermes_config(path)
        except InitError:
            continue
        plugins = config.get("plugins") if isinstance(config.get("plugins"), dict) else {}
        memory = config.get("memory") if isinstance(config.get("memory"), dict) else {}
        enabled = plugins.get("enabled") if isinstance(plugins.get("enabled"), list) else []
        if PLUGIN_NAME in enabled or memory.get("provider") == MEMORY_PROVIDER:
            users.append(str(path.parent))
    return users


# ---------------------------------------------------------------------------
# Router and identity
# ---------------------------------------------------------------------------

def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if sep and key.strip():
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    return values


def hermes_model_endpoint(hermes_home: Path, main_config: dict[str, Any]) -> dict[str, str]:
    """The model endpoint Hermes uses: ``model`` block first, then ``.env``."""
    model = main_config.get("model")
    env = _read_env_file(hermes_home / ".env")
    found = {"base_url": "", "model": "", "api_key": ""}
    if isinstance(model, dict):
        found["base_url"] = str(model.get("base_url") or "")
        found["model"] = str(model.get("default") or model.get("model") or "")
        found["api_key"] = str(model.get("api_key") or "")
    elif isinstance(model, str):
        found["model"] = model
    if not found["base_url"]:
        found["base_url"] = env.get("OPENAI_BASE_URL", "")
    if not found["api_key"]:
        found["api_key"] = env.get("OPENAI_API_KEY", "")
    for key, value in list(found.items()):
        if value.startswith("${") and value.endswith("}"):
            found[key] = os.environ.get(value[2:-1], "")
    return found


def write_llm_config(home: Path, *, base_url: str, model: str, api_key: str) -> bool:
    """Persist the router's host config once; an existing file is the operator's."""
    path = home / LLM_CONFIG_FILE
    if path.is_file():
        return False
    from protagine.setup import apply_llm_config_fixes
    document = {
        "provider": "local",
        "baseUrl": base_url,
        "apiKey": api_key or "local-no-key",
        "models": {role: model for role in _MODEL_ROLES},
    }
    fixed, _ = apply_llm_config_fixes(document)
    configuration._atomic_write(path, (json.dumps(fixed, indent=2) + "\n").encode("utf-8"), mode=0o600)
    return True


def ensure_owner_contact(home: Path, cfg: Config, identity: dict[str, Any]) -> tuple[str, bool]:
    """Create the owner contact once and return ``(contact_id, created)``."""
    from protagine.contacts.config import ContactsConfig
    from protagine.contacts.store import SQLiteContactStore
    from protagine.setup import build_owner_contact

    db_path = home / "contacts.db"
    if not db_path.exists():
        db_path = home / "protagine-contacts.db"
    existing = str(cfg.get("owner.contact_id") or "")
    owner = identity.get("owner", {})
    handles = [(str(item.get("platform") or ""), str(item.get("id") or ""))
               for item in owner.get("handles") or [] if isinstance(item, dict)]

    async def work() -> tuple[str, bool]:
        store = SQLiteContactStore(ContactsConfig(sqlite_path=str(db_path)))
        await store.connect()
        try:
            if existing and await store.get(existing) is not None:
                return existing, False
            contact_id = await build_owner_contact(store, str(owner.get("name") or "Owner"), handles)
            return contact_id, True
        finally:
            await store.close()

    return asyncio.run(work())


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

def _service(cfg: Config):
    from protagine.services.instance import InstanceService
    return InstanceService(cfg.home, cfg.hermes_home, host=cfg.get("sidecar.host"), port=cfg.get("sidecar.port"))


def install_service(cfg: Config) -> str:
    """Install and start the sidecar user service; a message when no manager exists."""
    from protagine.services.instance import ServiceError
    if os.environ.get("PROTAGINE_INIT_NO_SERVICE"):
        return "sidecar service skipped (PROTAGINE_INIT_NO_SERVICE)"
    try:
        service = _service(cfg)
    except ServiceError as exc:
        return f"sidecar service skipped: {exc}"
    try:
        service._manager_ready()
    except Exception:
        manager = "launchd" if sys.platform == "darwin" else "systemd --user"
        return f"sidecar service skipped: {manager} is not available in this session; start it with 'protagine start'"
    try:
        status = service.install()
    except (ServiceError, OSError) as exc:
        return f"sidecar service not installed: {exc}"
    installed = f"sidecar service installed ({status['manager']}: {status['label']})"
    try:
        started = service.start()  # enable alone starts nothing before the next login
    except (ServiceError, OSError) as exc:
        return f"{installed}, not running: {exc}; start it with 'protagine service start'"
    return f"{installed} and running{_health_words(started)}"


def _health_words(result: Any) -> str:
    """The served health verdict when it is not ``ok``: the status and its reasons, in words."""
    health = (result or {}).get("health") if isinstance(result, dict) else None
    if not health or health == "ok":
        return ""
    problems = "; ".join(str(item) for item in result.get("problems") or []) or "run 'protagine doctor'"
    return f"; health {health}: {problems}"


def service_status(cfg: Config) -> dict[str, Any] | None:
    from protagine.services.instance import ServiceError
    try:
        service = _service(cfg)
        if not service._owned():
            return None
        return service.status()
    except (ServiceError, OSError):
        return None


def restart_service(cfg: Config) -> str:
    from protagine.services.instance import ServiceError
    status = service_status(cfg)
    if status is None:
        return "sidecar service is not installed; restart the sidecar yourself if it is running"
    if not status.get("running"):
        return "sidecar service is installed but not running; start it with 'protagine service start'"
    try:
        started = _service(cfg).start(restart=True)
    except (ServiceError, OSError) as exc:
        return f"sidecar service restart failed: {exc}"
    return f"sidecar service restarted{_health_words(started)}"


def uninstall_service(cfg: Config) -> str:
    from protagine.services.instance import ServiceError
    try:
        service = _service(cfg)
        if not service._owned():
            return "sidecar service was not installed"
        service.uninstall()
    except (ServiceError, OSError) as exc:
        return f"sidecar service removal failed: {exc}"
    return "sidecar service removed"


# ---------------------------------------------------------------------------
# Backups and migrations
# ---------------------------------------------------------------------------

def backup_instance(home: Path) -> Path:
    """Copy every store and configuration file into ``backups/<stamp>/``."""
    destination = home / BACKUPS_DIR / _utc_stamp()
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    for path in sorted(home.iterdir()):
        if path.is_dir():
            continue
        if path.suffix == ".db":
            with sqlite3.connect(path) as source, sqlite3.connect(destination / path.name) as target:
                source.backup(target)
            continue
        if path.name in {CONFIG_FILE, IDENTITY_FILE, KEY_FILE, LLM_CONFIG_FILE, ".env", LEGACY_MANIFEST, LEGACY_KEYRING}:
            shutil.copy2(path, destination / path.name)
    return destination


def _migration_stores() -> tuple[tuple[str, Path], ...]:
    package = Path(__file__).resolve().parent
    return (
        ("contacts.db", package / "contacts" / "migrations"),
        ("protagine-contacts.db", package / "contacts" / "migrations"),
        ("protagine-channels.db", package / "channels" / "migrations"),
    )


def pending_store_migrations(home: Path) -> list[str]:
    """The numbered migrations the existing stores have not applied yet."""
    from protagine.migrations import _discover, applied_versions_sync
    pending: list[str] = []
    for name, migrations in _migration_stores():
        path = home / name
        if not path.is_file():
            continue
        with sqlite3.connect(path) as connection:
            applied = applied_versions_sync(connection)
        pending.extend(f"{name}:{version}" for version, _ in _discover(migrations) if version not in applied)
    return pending


def run_store_migrations(home: Path) -> list[str]:
    """Apply the numbered SQL migrations to the stores that exist."""
    from protagine.migrations import run_migrations_sync
    applied: list[str] = []
    for name, migrations in _migration_stores():
        path = home / name
        if not path.is_file():
            continue
        with sqlite3.connect(path) as connection:
            for version in run_migrations_sync(connection, migrations):
                applied.append(f"{name}:{version}")
    return applied


# State owned by code that no longer exists: the autonomy scheduler, the queue
# approval ledger, standing grants, the proactive delivery bridge, the task
# queue, the project engine, the governed action ledger, the directive store,
# directed tasks, the response guard's ledgers, the agent bridge poller's
# seen-lists and (since the drives milestone) the cognitive workspace, the
# cognition spine, its evidence and drive-governance ledgers, the external
# event inbox and the surprise store. An upgrade moves them into the backup
# instead of leaving orphans behind. A directory entry names a whole tree.
RETIRED_STATE = (
    "approval_authority.db",
    "schedules.db",
    "standing_approvals.json",
    "protagine-delivery-rate-limit.db",
    "protagine-governed-gateway-outcomes.db",
    "task_queue.db",
    "protagine-projects.db",
    "protagine-workspace.db",
    "protagine-cognition.db",
    "protagine-cognition-evidence.db",
    "cognition-drive-governance.db",
    "external-cognition-events.db",
    "protagine-surprise.db",
    "governed-actions",
    "protagine-directives.db",
    "protagine-directed.db",
    "protagine-guard-audit.db",
    "protagine-context-provenance.db",
    "protagine-tom2-taint.db",
    "bridge",
)
# Tables inside surviving stores whose code was deleted: the goal subtask and DAG
# tables (agent goals are intention rows) and the legacy perspective tables (the
# automatic opinion revisions and the attention snapshot). The backup taken before
# the migrations keeps their rows; the upgrade drops them from the live store.
RETIRED_TABLES: dict[str, tuple[str, ...]] = {
    "protagine-goals.db": ("subtasks", "goal_dag_versions"),
    "turn-idempotency.db": ("self_opinion_revisions", "self_attention"),
}
INITIATIVES_DB = "initiatives.db"


def retired_state_present(home: Path) -> list[str]:
    return [name for name in RETIRED_STATE if (home / name).exists()]


def retire_state(home: Path, backup_dir: Path) -> list[str]:
    """Move the retired stores (and SQLite side files) into ``backup_dir/retired``."""
    notes: list[str] = []
    destination = backup_dir / "retired"
    for name in retired_state_present(home):
        destination.mkdir(parents=True, exist_ok=True, mode=0o700)
        if (home / name).is_dir():
            shutil.move(str(home / name), str(destination / name))
        else:
            for suffix in ("", "-wal", "-shm", "-journal"):
                source = home / (name + suffix)
                if source.exists():
                    shutil.move(str(source), str(destination / (name + suffix)))
        notes.append(f"retired {name} (moved to {destination})")
    return notes


def retired_tables_present(home: Path) -> list[str]:
    """``store:table`` for every retired table that still exists in a surviving store."""
    present: list[str] = []
    for name, tables in RETIRED_TABLES.items():
        path = home / name
        if not path.is_file():
            continue
        try:
            with sqlite3.connect(path) as connection:
                existing = {row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'")}
        except sqlite3.DatabaseError:
            continue
        present.extend(f"{name}:{table}" for table in tables if table in existing)
    return present


def retire_tables(home: Path) -> list[str]:
    """Drop the retired tables (the backup taken first keeps their rows)."""
    notes: list[str] = []
    for item in retired_tables_present(home):
        name, table = item.split(":", 1)
        with sqlite3.connect(home / name) as connection:
            rows = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            connection.execute(f"DROP TABLE IF EXISTS {table}")  # noqa: S608
        notes.append(f"retired table {table} in {name} ({rows} rows kept in the backup)")
    return notes


def pending_initiative_columns(home: Path) -> list[str]:
    """The intention and audit columns an existing initiatives store still lacks."""
    path = home / INITIATIVES_DB
    if not path.is_file():
        return []
    from protagine.initiatives.store import missing_mind_columns
    with sqlite3.connect(path) as connection:
        return [f"{INITIATIVES_DB}:{column}" for column in missing_mind_columns(connection)]


def migrate_initiatives(home: Path) -> list[str]:
    """Add the intention and audit columns in place (architecture 5.2)."""
    pending = pending_initiative_columns(home)
    if not pending:
        return []
    from protagine.initiatives.store import InitiativeStore
    store = InitiativeStore(state_dir=home)
    store.close()
    return [f"migration applied: {item}" for item in pending]


# ---------------------------------------------------------------------------
# 1.9.0 migration
# ---------------------------------------------------------------------------

def is_legacy_instance(home: Path) -> bool:
    return (home / LEGACY_MANIFEST).is_file() and not (home / CONFIG_FILE).is_file()


def _keyring_accepts(entry: dict[str, Any], now: _dt.datetime) -> bool:
    """The 1.9.0 rule for a principal or credential: active or retiring, and not past
    ``accept_until``."""
    if str(entry.get("status") or "active").lower() not in {"active", "retiring"}:
        return False
    until = entry.get("accept_until")
    if not until:
        return True
    try:
        parsed = _dt.datetime.fromisoformat(str(until).replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_dt.timezone.utc)
    return now < parsed


def _legacy_key(home: Path, env: dict[str, str]) -> str:
    """The secret the 1.9.0 sidecar still accepted: the configured client credential when
    its principal and credential are both live, else the first live one, else nothing."""
    configured = env.get("PROTAGINE_CLIENT_API_KEY") or env.get("PROTAGINE_API_KEY") or ""
    keyring = home / LEGACY_KEYRING
    if not keyring.is_file():
        return configured
    now = _dt.datetime.now(_dt.timezone.utc)
    usable: list[str] = []
    try:
        document = json.loads(keyring.read_text(encoding="utf-8"))
        for principal in document.get("principals", []):
            if not _keyring_accepts(principal, now):
                continue
            for credential in principal.get("credentials", []):
                if _keyring_accepts(credential, now) and credential.get("secret"):
                    usable.append(str(credential["secret"]))
    except (OSError, ValueError, AttributeError, TypeError):
        return ""
    if configured in usable:
        return configured
    return usable[0] if usable else ""


def prepared_runtime(python: str | Path | None) -> bool:
    """True when *python* belongs to a 1.9.0 prepared (patched) Hermes runtime."""
    if not python:
        return False
    path = Path(python).expanduser()
    for parent in (path, *path.parents):
        if (parent / ".protagine-runtime.json").is_file() or (parent / ".protagine-patch-receipt.json").is_file():
            return True
    return "/.local/share/protagine/hermes/" in str(path)


def archive_legacy_forwarders(home: Path, hermes_home: Path, backup_dir: Path) -> list[str]:
    """Move the 1.9.0 private-directory forwarders out of ``<hermes_home>/plugins``.

    Stock Hermes takes a memory provider from that directory before the installed
    entry point, so a forwarder left behind keeps loading the 1.9.0 adapter copy.
    """
    markers = {str(home / "adapter"), str(home.resolve() / "adapter")}
    moved: list[str] = []
    for name in (PLUGIN_NAME, MEMORY_PROVIDER):
        directory = hermes_home / "plugins" / name
        try:
            source = (directory / "__init__.py").read_text(encoding="utf-8")
        except OSError:
            continue
        if not any(marker in source for marker in markers):
            continue
        destination = backup_dir / "hermes-plugins" / name
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.move(str(directory), str(destination))
        moved.append(f"{hermes_home / 'plugins' / name} moved to {destination} (a 1.9.0 forwarder)")
    return moved


def migrate_legacy_instance(home: Path, *, backup_dir: Path | None = None) -> list[str]:
    """Convert a 1.9.0 instance in place; returns the notes to print."""
    notes: list[str] = []
    manifest = json.loads((home / LEGACY_MANIFEST).read_text(encoding="utf-8"))
    env = _read_env_file(home / ".env")
    key = _legacy_key(home, env)
    if key:
        write_api_key(key, home)
        notes.append("api.key written from the 1.9.0 keyring")
    else:
        write_api_key(secrets.token_urlsafe(32), home)
        notes.append("api.key generated (no live 1.9.0 credential)")
    data = copy.deepcopy(configuration.DEFAULTS)
    data["sidecar"]["port"] = int(env.get("PROTAGINE_SIDECAR_PORT") or 7777)
    data["sidecar"]["host"] = env.get("PROTAGINE_SIDECAR_HOST") or "127.0.0.1"
    data["hermes"]["home"] = str(manifest.get("hermes_home") or "~/.hermes")
    python = str(manifest.get("hermes_python") or "")
    if prepared_runtime(python):
        data["hermes"]["python"] = ""
        stock = find_hermes_executable()
        stock_python = python_of_executable(stock) if stock else None
        target = str(stock_python) if stock_python else "<python of your stock hermes>"
        notes.append(
            "this instance used a prepared (patched) Hermes runtime; install into stock Hermes with:\n"
            f"    protagine upgrade --hermes-python {target}"
        )
    else:
        data["hermes"]["python"] = python
    data["owner"]["contact_id"] = env.get("PROTAGINE_OWNER_CONTACT_ID") or str(manifest.get("owner_id") or "")
    data["mind"]["autonomy"] = "suggest"
    llm_path = home / LLM_CONFIG_FILE
    if llm_path.is_file():
        try:
            llm = json.loads(llm_path.read_text(encoding="utf-8"))
            data["router"]["base_url"] = str(llm.get("baseUrl") or "")
            models = llm.get("models") or {}
            data["router"]["model"] = str(models.get("medium") or models.get("large") or manifest.get("model") or "")
        except (OSError, ValueError, AttributeError):
            pass
    if not data["router"]["base_url"]:
        data["router"]["base_url"] = str(manifest.get("endpoint") or "")
        data["router"]["model"] = str(manifest.get("model") or "")
    data["mind"]["faculties"]["semantic_recall"] = env.get("PROTAGINE_EMBED_PROVIDER", "skip") != "skip"
    save_config(data, home)
    notes.append("protagine.yaml written; autonomy starts at 'suggest' (edit mind.autonomy to choose "
                 "'standard' or 'trusted')")
    preferences = manifest.get("agent_preferences") or {}
    try:
        values = json.loads(env.get("PROTAGINE_AGENT_VALUES") or "[]")
    except ValueError:
        values = []
    identity = {
        "owner": {"name": env.get("PROTAGINE_OWNER_NAME") or "Owner", "handles": []},
        "agent": {
            "name": env.get("PROTAGINE_PERSONA_NAME") or str(manifest.get("agent_name") or "Assistant"),
            "values": values or list(preferences.get("values") or []),
            "timezone": env.get("PROTAGINE_AGENT_TIMEZONE") or str(preferences.get("timezone") or ""),
            "quiet_hours": env.get("PROTAGINE_AGENT_QUIET_HOURS") or str(preferences.get("quiet_hours") or ""),
        },
    }
    save_identity(identity, home)
    notes.append("identity.yaml written from the 1.9.0 environment")
    for name in (".env", LEGACY_KEYRING):
        path = home / name
        if path.is_file():
            path.rename(home / f"{name}.1.9.0")
            notes.append(f"{name} kept as {name}.1.9.0 (no longer read)")
    notes.extend(archive_legacy_forwarders(home, Path(data["hermes"]["home"]).expanduser(),
                                           backup_dir or home / BACKUPS_DIR / _utc_stamp()))
    return notes


# ---------------------------------------------------------------------------
# The steps
# ---------------------------------------------------------------------------

def _ask(prompt: str, default: str, non_interactive: bool, ask: Callable[[str], str] | None = None) -> str:
    from protagine.setup import _prompt
    value = _prompt(prompt, default, non_interactive, ask=ask).strip()
    if any(ord(char) < 32 for char in value):
        raise InitError("configuration values must fit on one line")
    return value


def _parse_handles(values: list[str] | None) -> list[dict[str, str]]:
    handles = []
    for item in values or []:
        platform, sep, address = item.partition("=")
        if not sep or not platform.strip() or not address.strip():
            raise InitError(f"--owner-handle expects PLATFORM=ID, got {item!r}")
        handles.append({"platform": platform.strip().lower(), "id": address.strip()})
    return handles


def _collect_identity(args, existing: dict[str, Any], non_interactive: bool) -> dict[str, Any]:
    owner = dict(existing.get("owner") or {})
    agent = dict(existing.get("agent") or {})
    owner_name = _ask("Your name", getattr(args, "owner_name", None) or owner.get("name") or os.environ.get("USER", "Owner"),
                      non_interactive)
    handles = _parse_handles(getattr(args, "owner_handle", None)) or list(owner.get("handles") or [])
    if not non_interactive and not handles:
        raw = _ask("Your messaging handles, PLATFORM=ID separated by commas (optional)", "", non_interactive)
        handles = _parse_handles([part.strip() for part in raw.split(",") if part.strip()])
    agent_name = _ask("Agent name", getattr(args, "agent_name", None) or agent.get("name") or "Assistant", non_interactive)
    values_default = ", ".join(agent.get("values") or [])
    values_raw = getattr(args, "agent_values", None) or _ask("Guiding values, comma separated (optional)",
                                                             values_default, non_interactive)
    values = [part.strip() for part in str(values_raw).split(",") if part.strip()]
    timezone = _ask("Time zone (optional)", getattr(args, "timezone", None) or agent.get("timezone") or "", non_interactive)
    quiet = _ask("Quiet hours HH:MM-HH:MM (optional)", getattr(args, "quiet_hours", None) or agent.get("quiet_hours") or "",
                 non_interactive)
    if quiet and not re.fullmatch(r"\d{2}:\d{2}-\d{2}:\d{2}", quiet):
        raise InitError("quiet hours must look like 22:00-07:00")
    return {
        "owner": {"name": owner_name, "handles": handles},
        "agent": {"name": agent_name, "values": values, "timezone": timezone, "quiet_hours": quiet},
    }


def _collect_autonomy(args, current: str, non_interactive: bool, *, fresh: bool) -> str:
    explicit = getattr(args, "autonomy", None)
    if explicit:
        level = str(explicit).strip().lower()
    elif fresh and not non_interactive:
        level = _ask("Autonomy level (off, suggest, standard, trusted)", current or "suggest", non_interactive).lower()
    else:
        level = current or "standard"
    if level not in AUTONOMY_LEVELS:
        raise InitError(f"autonomy must be one of {', '.join(AUTONOMY_LEVELS)}")
    return level


def _reconcile(cfg: Config, hermes_home: Path, *, backup_dir: Path) -> list[str]:
    """Steps 5 and 6: the Hermes keys and the worker profile. Returns the changes."""
    config_path = hermes_home / "config.yaml"
    current = read_hermes_config(config_path)
    skills_dir = cfg.home / SKILLS_DIR
    skills_dir.mkdir(parents=True, exist_ok=True)
    key_file = cfg.home / KEY_FILE
    updated, changes = reconcile_hermes_config(current, sidecar_url=cfg.sidecar_url, key_file=key_file,
                                               skills_dir=skills_dir)
    if changes:
        write_hermes_config(config_path, updated, backup_dir=backup_dir)
    profile = worker_profile_config(updated, cfg, sidecar_url=cfg.sidecar_url, key_file=key_file)
    if write_worker_profile(profiles_root(hermes_home), profile):
        changes.append(f"profiles/{WORKER_PROFILE} written")
    return changes


def _adapter_step(python: Path, *, source: str | None) -> list[str]:
    notes: list[str] = []
    if install_adapter(python, __version__, source=source):
        notes.append(f"{ADAPTER_DISTRIBUTION} {installed_adapter_version(python)} installed into {python}")
    ok, output = pip_check(python)
    if ok:
        notes.append(f"pip check clean in {python}")
    else:
        # An incompatibility that predates the adapter is Hermes' to fix; say so and go on.
        detail = " | ".join(line for line in output.splitlines() if line.strip())
        notes.append(f"pip check reports an incompatibility in {python} ({detail}); "
                     "'protagine doctor' keeps flagging it until Hermes' environment is repaired")
    return notes


def run_init(args) -> int:
    """``protagine init``. Returns the exit code."""
    if getattr(args, "uninstall", False):
        return run_uninstall(args)
    non_interactive = bool(getattr(args, "non_interactive", False))
    try:
        home = configuration.instance_home(getattr(args, "home", None))
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        if is_legacy_instance(home):
            for note in migrate_legacy_instance(home):
                _say("  " + note)
        cfg = load_config(home)
        fresh = not cfg.exists
        data = copy.deepcopy(cfg.data)

        # 1. Identity and autonomy.
        identity = _collect_identity(args, load_identity(home), non_interactive)
        data["mind"]["autonomy"] = _collect_autonomy(args, data["mind"].get("autonomy", "standard"),
                                                     non_interactive, fresh=fresh)
        hermes_home = resolve_hermes_home(getattr(args, "hermes_home", None), data["hermes"].get("home"))
        python, hermes_version = resolve_hermes_python(getattr(args, "hermes_python", None),
                                                       data["hermes"].get("python"))
        data["hermes"]["home"] = str(hermes_home)
        data["hermes"]["python"] = str(python)
        if getattr(args, "host", None):
            data["sidecar"]["host"] = str(args.host)
        if getattr(args, "port", None):
            data["sidecar"]["port"] = int(args.port)

        # 3. Router: the endpoint Hermes uses, plus an embedding endpoint when one exists.
        main_config = read_hermes_config(hermes_home / "config.yaml")
        endpoint = hermes_model_endpoint(hermes_home, main_config)
        base_url = getattr(args, "model_url", None) or data["router"].get("base_url") or endpoint["base_url"]
        model = getattr(args, "model", None) or data["router"].get("model") or endpoint["model"]
        api_key = getattr(args, "model_key", None) or os.environ.get("PROTAGINE_MODEL_API_KEY") or endpoint["api_key"]
        data["router"]["base_url"] = base_url
        data["router"]["model"] = model
        embed_url = getattr(args, "embed_url", None) or data["router"].get("embed_url") or ""
        data["router"]["embed_url"] = embed_url
        if getattr(args, "embed_model", None):
            data["router"]["embed_model"] = str(args.embed_model)
        if getattr(args, "embed_dims", None) is not None:
            data["router"]["embed_dims"] = int(args.embed_dims)
        data["mind"]["faculties"]["semantic_recall"] = bool(embed_url)

        # 2. protagine.yaml, identity.yaml and api.key.
        save_identity(identity, home)
        if read_api_key(home, environ={}) is None:
            write_api_key(secrets.token_urlsafe(32), home)
            _say(f"  api.key written ({home / KEY_FILE}, mode 600)")
        save_config(data, home)
        cfg = load_config(home)
        contact_id, created = ensure_owner_contact(home, cfg, identity)
        if created or cfg.get("owner.contact_id") != contact_id:
            cfg.data["owner"]["contact_id"] = contact_id
            save_config(cfg.data, home)
            cfg = load_config(home)
            _say(f"  owner contact {contact_id} recorded")
        if base_url and write_llm_config(home, base_url=base_url, model=model, api_key=api_key):
            _say(f"  router pointed at {base_url} ({model or 'model chosen by Hermes'})")
        elif not base_url:
            _say("  no model endpoint found in Hermes' config; pass --model-url to point the router at one")
        _say(f"  semantic recall {'on' if embed_url else 'off'} "
             f"({'embedding endpoint ' + embed_url if embed_url else 'no embedding endpoint recorded'})")

        # 4. The adapter in Hermes' environment.
        _say(f"  Hermes {hermes_version} at {python}")
        for note in _adapter_step(python, source=getattr(args, "adapter_source", None)):
            _say("  " + note)

        # 5 and 6. Hermes keys and the worker profile.
        for change in _reconcile(cfg, hermes_home, backup_dir=home / BACKUPS_DIR):
            _say("  " + change)
        _say("  plugins.hook_callback_timeout is 0: plugin callbacks run inline for every plugin, so "
             "overlapping capture and guard calls are never skipped")
        _say("  protected_instruction_extra_patterns match by basename in any directory: writes to any "
             "protagine.yaml, identity.yaml or api.key need a human")

        # 7. The sidecar user service.
        if getattr(args, "no_service", False):
            _say("  sidecar service skipped (--no-service)")
        else:
            _say("  " + install_service(cfg))
    except (InitError, ConfigError) as exc:
        _say(f"protagine init failed: {exc}")
        return 1
    except (OSError, subprocess.SubprocessError) as exc:
        _say(f"protagine init failed: {type(exc).__name__}: {exc}")
        return 1
    _say(f"Protagine {__version__} is configured in {home}.")
    _say("Restart the gateway to load the adapter: hermes gateway restart")
    return 0


def run_upgrade(args) -> int:
    """``protagine upgrade``: backup, migrations, adapter, config reconcile, service restart."""
    try:
        home = configuration.instance_home(getattr(args, "home", None))
        notes: list[str] = []
        if is_legacy_instance(home):
            backup = backup_instance(home)
            notes.append(f"backup taken in {backup}")
            notes.extend(migrate_legacy_instance(home, backup_dir=backup))
        cfg = load_config(home, required=True)
        python, hermes_version = resolve_hermes_python(getattr(args, "hermes_python", None),
                                                       cfg.get("hermes.python"))
        hermes_home = resolve_hermes_home(getattr(args, "hermes_home", None), cfg.get("hermes.home"))
        binding_changed = (str(python) != cfg.get("hermes.python")) or (str(hermes_home) != cfg.get("hermes.home"))
        adapter_pending = installed_adapter_version(python) != __version__ or getattr(args, "adapter_source", None)
        current = read_hermes_config(hermes_home / "config.yaml")
        skills_dir = cfg.home / SKILLS_DIR
        updated, config_changes = reconcile_hermes_config(current, sidecar_url=cfg.sidecar_url,
                                                          key_file=cfg.home / KEY_FILE, skills_dir=skills_dir)
        profile_pending = worker_profile_pending(
            profiles_root(hermes_home),
            worker_profile_config(updated, cfg, sidecar_url=cfg.sidecar_url, key_file=cfg.home / KEY_FILE))
        migrations_pending = (pending_store_migrations(home) + pending_initiative_columns(home)
                              + retired_state_present(home) + retired_tables_present(home))
        if not (notes or binding_changed or adapter_pending or config_changes or profile_pending
                or migrations_pending):
            _say(f"Protagine {__version__}: nothing to do.")
            return 0
        if not any(note.startswith("backup taken") for note in notes):
            backup = backup_instance(home)
            notes.insert(0, f"backup taken in {backup}")
        notes.extend("migration applied: " + item for item in run_store_migrations(home))
        notes.extend(migrate_initiatives(home))
        notes.extend(retire_state(home, backup))
        notes.extend(retire_tables(home))
        if binding_changed:
            cfg.data["hermes"]["python"] = str(python)
            cfg.data["hermes"]["home"] = str(hermes_home)
            save_config(cfg.data, home)
            cfg = load_config(home)
            notes.append(f"Hermes binding recorded: {python} ({hermes_version}) in {hermes_home}")
        notes.extend(_adapter_step(python, source=getattr(args, "adapter_source", None)))
        notes.extend(_reconcile(cfg, hermes_home, backup_dir=home / BACKUPS_DIR))
        if protagine_in_hermes_environment(python):
            notes.append(
                "the 'protagine' sidecar package is installed inside Hermes' environment; move it out with:\n"
                f"    {python} -m pip uninstall protagine\n    pipx install protagine"
            )
        notes.append(restart_service(cfg))
    except (InitError, ConfigError) as exc:
        _say(f"protagine upgrade failed: {exc}")
        return 1
    except (OSError, subprocess.SubprocessError) as exc:
        _say(f"protagine upgrade failed: {type(exc).__name__}: {exc}")
        return 1
    for note in notes:
        _say("  " + note)
    _say(f"Protagine {__version__} is up to date in {home}.")
    _say("Restart the gateway to load the adapter: hermes gateway restart")
    return 0


def _mind_off(cfg: Config) -> None:
    """Best effort off-switch cleanup through the sidecar; the route arrives with the mind."""
    key = read_api_key(cfg.home)
    if not key:
        return
    try:
        import httpx
        httpx.post(cfg.sidecar_url + "/v1/mind/off", headers={"Authorization": "Bearer " + key},
                   timeout=5, trust_env=False)
    except Exception:
        pass


def run_uninstall(args) -> int:
    """``protagine init --uninstall``: leave stock Hermes behind and keep the instance data."""
    try:
        home = configuration.instance_home(getattr(args, "home", None))
        cfg = load_config(home, required=True)
        hermes_home = resolve_hermes_home(getattr(args, "hermes_home", None), cfg.get("hermes.home"))
        _mind_off(cfg)
        _say("  " + uninstall_service(cfg))
        config_path = hermes_home / "config.yaml"
        current = read_hermes_config(config_path)
        updated, changes = strip_hermes_config(current, skills_dir=home / SKILLS_DIR)
        if changes:
            write_hermes_config(config_path, updated, backup_dir=home / BACKUPS_DIR)
        for change in changes:
            _say("  " + change)
        backup_dir = home / BACKUPS_DIR / _utc_stamp()
        if remove_worker_profile(profiles_root(hermes_home), backup_dir=backup_dir):
            _say(f"  profiles/{WORKER_PROFILE} moved to {backup_dir / 'profiles' / WORKER_PROFILE}")
        python_value = getattr(args, "hermes_python", None) or cfg.get("hermes.python")
        users = adapter_users(hermes_home)
        if users:
            _say(f"  {ADAPTER_DISTRIBUTION} kept in {python_value}: still enabled by {', '.join(users)}")
        elif python_value and Path(python_value).exists() and uninstall_adapter(Path(python_value)):
            _say(f"  {ADAPTER_DISTRIBUTION} removed from {python_value}")
    except (InitError, ConfigError) as exc:
        _say(f"protagine init --uninstall failed: {exc}")
        return 1
    except (OSError, subprocess.SubprocessError) as exc:
        _say(f"protagine init --uninstall failed: {type(exc).__name__}: {exc}")
        return 1
    _say(f"Hermes in {hermes_home} is stock again; the instance data in {home} is kept.")
    _say("Restart the gateway to drop the adapter: hermes gateway restart")
    return 0


# ---------------------------------------------------------------------------
# Argument parsers
# ---------------------------------------------------------------------------

def add_parsers(sub) -> None:
    init_p = sub.add_parser("init", help="Configure this instance and attach it to stock Hermes")
    init_p.add_argument("--home", default=None, help="Instance directory (default: $PROTAGINE_HOME or ~/.protagine)")
    init_p.add_argument("--non-interactive", "-n", action="store_true", help="Run without prompts")
    init_p.add_argument("--uninstall", action="store_true",
                        help="Remove the adapter, its Hermes keys and the worker profile; keep the instance data")
    init_p.add_argument("--owner-name", help="Your name")
    init_p.add_argument("--owner-handle", action="append", metavar="PLATFORM=ID",
                        help="One of your messaging handles; repeat per account")
    init_p.add_argument("--agent-name", help="The agent's name")
    init_p.add_argument("--agent-values", help="Comma-separated guiding values")
    init_p.add_argument("--timezone", help="Named time zone, for example Europe/Paris")
    init_p.add_argument("--quiet-hours", help="Local quiet window, HH:MM-HH:MM")
    init_p.add_argument("--autonomy", choices=AUTONOMY_LEVELS, help="Autonomy level (default: suggest)")
    init_p.add_argument("--hermes-home", help="Hermes home (default: $HERMES_HOME or ~/.hermes)")
    init_p.add_argument("--hermes-python", help="Interpreter that runs 'hermes' (default: read from the executable)")
    init_p.add_argument("--host", help="Sidecar bind address (default 127.0.0.1)")
    init_p.add_argument("--port", type=int, help="Sidecar port (default 7777)")
    init_p.add_argument("--model-url", help="OpenAI-compatible API root (default: the one Hermes uses)")
    init_p.add_argument("--model", help="Model identifier at that endpoint")
    init_p.add_argument("--model-key", help="API key for that endpoint")
    init_p.add_argument("--embed-url", help="OpenAI-compatible embeddings root; turns semantic recall on")
    init_p.add_argument("--embed-model", help="Embedding model at that endpoint")
    init_p.add_argument("--embed-dims", type=int,
                        help="Vector width of that model (default: learned from its first embedding)")
    init_p.add_argument("--adapter-source", help="Install the adapter from this wheel, directory or requirement")
    init_p.add_argument("--no-service", action="store_true", help="Do not install the sidecar user service")

    upgrade_p = sub.add_parser("upgrade", help="Backup, migrate, upgrade the adapter, reconcile config, restart")
    upgrade_p.add_argument("--home", default=None, help="Instance directory (default: $PROTAGINE_HOME or ~/.protagine)")
    upgrade_p.add_argument("--hermes-home", help="Hermes home when it moved")
    upgrade_p.add_argument("--hermes-python", help="Interpreter that runs 'hermes' when it moved")
    upgrade_p.add_argument("--adapter-source", help="Install the adapter from this wheel, directory or requirement")
