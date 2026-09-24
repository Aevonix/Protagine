"""``protagine.yaml``: the one configuration file of a Protagine instance.

An instance lives in one directory (``$PROTAGINE_HOME``, default ``~/.protagine``)
that holds ``protagine.yaml``, ``api.key``, ``identity.yaml`` and the SQLite
stores. ``load_config`` merges the file over the schema defaults, applies a
small set of environment overrides and validates the result. ``apply_environment``
exports the values the sidecar process reads through ``os.environ``.
"""

from __future__ import annotations

import copy
import logging
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import yaml

logger = logging.getLogger(__name__)

CONFIG_FILE = "protagine.yaml"
KEY_FILE = "api.key"
IDENTITY_FILE = "identity.yaml"
LLM_CONFIG_FILE = ".protagine-llm-config.json"

AUTONOMY_LEVELS = ("off", "suggest", "standard", "trusted")

DEFAULTS: dict[str, Any] = {
    "sidecar": {"host": "127.0.0.1", "port": 7777},
    "hermes": {"home": "~/.hermes", "python": ""},
    "router": {"base_url": "", "model": "", "embed_url": "", "embed_model": "", "embed_dims": 0,
               "rerank_url": "", "rerank_model": ""},
    "owner": {"contact_id": ""},
    # PROTAGINE_* settings the sidecar reads that no key above covers (a reranker prompt
    # style, recall thresholds, oversampling, an endpoint credential): exported after the
    # keys above; a value already in the process environment still wins.
    "environment": {},
    "mind": {
        "enabled": True,
        "autonomy": "suggest",
        "deny": {"commands": [], "tools": [], "text": []},
        "worker_toolsets": ["web", "file", "session_search", "memory", "todo"],
        "budgets": {
            "tasks_per_hour": 4,
            "concurrent_tasks": 2,
            "owner_messages_per_day": 3,
            "contact_messages_per_day": 5,
            "per_contact_cooldown_hours": 24,
            "llm_tokens_per_day": 200000,
            "learn_share": 0.25,
            "open_goals": 2,
            "goal_tasks": 4,          # steps an agent-owned goal may spend
            "goal_horizon_days": 7,   # the longest horizon an adopted goal may have
            "task_max_runtime_s": 600,
            "task_max_retries": 1,
        },
        "quiet_hours": "22:00-07:00",
        "ask_expires_hours": 72,
        "breaker": {"failures": 3, "window_hours": 24, "demotion_hours": 72},
        "act_threshold": 0.6,   # the ranker's effective-score floor (architecture 3.2)
        "digest_hour": 8,       # local hour after which the daily digest goes out
        "heads_up_grace_minutes": 30,   # after a heads-up went out, the overdue reminder for that row waits this long
        # Drive weights: 0 turns a drive off (architecture 4.5).
        "drives": {"duty": 1.0, "social": 0.5, "curiosity": 0.5, "mastery": 1.0, "upkeep": 1.0},
        # One binary flag per faculty; each is one benchmark arm (architecture 4, evals section 3).
        "faculties": {
            "initiative": True,
            "drives": True,         # weights, satiation and goal adoption; off = flat priority
            "deliberation": True,   # the one tool-less call per tick; off = templates only
            "goals": True,          # agent-owned goals
            "people": True,
            "affect": True,
            "affect_rules": False,  # the affect mechanism arm: every consumer reads its stateless rule
            "opinions": True,
            "broadcast": True,
            "semantic_recall": True,
            "consolidation": True,
            "self_narrative": True,
            "lessons": True,
            "skills": False,
        },
    },
}

# The whole environment override set. Everything else is configured in the file.
ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "PROTAGINE_SIDECAR_HOST": ("sidecar", "host"),
    "PROTAGINE_SIDECAR_PORT": ("sidecar", "port"),
    "HERMES_HOME": ("hermes", "home"),
    "PROTAGINE_MIND_ENABLED": ("mind", "enabled"),
    "PROTAGINE_AUTONOMY": ("mind", "autonomy"),
}

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_FALSY = frozenset({"0", "false", "no", "off"})

ENVIRONMENT_NAME = re.compile(r"^PROTAGINE_[A-Z][A-Z0-9_]*$")

#: Names the other keys of protagine.yaml (or identity.yaml) already define. The
#: ``environment`` mapping refuses them and names the key, so each setting has one place.
RESERVED_ENVIRONMENT: dict[str, str] = {
    "PROTAGINE_HOME": "the instance directory ($PROTAGINE_HOME)",
    "PROTAGINE_STATE_DIR": "the instance directory ($PROTAGINE_HOME)",
    "PROTAGINE_CONTACTS_DB": "the instance directory ($PROTAGINE_HOME)",
    "PROTAGINE_SIDECAR_HOST": "sidecar.host",
    "PROTAGINE_SIDECAR_PORT": "sidecar.port",
    "PROTAGINE_API_KEY": "api.key",
    "PROTAGINE_MIND_ENABLED": "mind.enabled",
    "PROTAGINE_AUTONOMY": "mind.autonomy",
    "PROTAGINE_OWNER_CONTACT_ID": "owner.contact_id",
    "PROTAGINE_OWNER_NAME": "identity.yaml owner.name",
    "PROTAGINE_PERSONA_NAME": "identity.yaml agent.name",
    "PROTAGINE_AGENT_VALUES": "identity.yaml agent.values",
    "PROTAGINE_AGENT_TIMEZONE": "identity.yaml agent.timezone",
    "PROTAGINE_TIMEZONE": "identity.yaml agent.timezone",
    "PROTAGINE_AGENT_QUIET_HOURS": "identity.yaml agent.quiet_hours",
    "PROTAGINE_EMBED_PROVIDER": "router.embed_url",
    "PROTAGINE_EMBED_BASE_URL": "router.embed_url",
    "PROTAGINE_EMBED_MODEL": "router.embed_model",
    "PROTAGINE_EMBED_DIMS": "router.embed_dims",
    "PROTAGINE_RERANKER_PROVIDER": "router.rerank_url",
    "PROTAGINE_RERANKER_BASE_URL": "router.rerank_url",
    "PROTAGINE_RERANKER_MODEL": "router.rerank_model",
    "PROTAGINE_GRAPH_ENABLED": "nothing: this line opens no graph database",
}
_SECRET_MARKERS = ("KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL")


def looks_secret(name: str) -> bool:
    """A variable whose name says it holds a credential; its value never reaches a log."""
    return any(marker in name.upper() for marker in _SECRET_MARKERS)


def _validate_environment(mapping: Any) -> dict[str, str]:
    if mapping is None:
        return {}
    if not isinstance(mapping, dict):
        raise ConfigError("environment must be a mapping of PROTAGINE_* names to values")
    result: dict[str, str] = {}
    for raw_name, value in mapping.items():
        name = str(raw_name)
        if name == "HERMES_HOME":
            raise ConfigError("environment.HERMES_HOME has its own key: set hermes.home instead")
        if not ENVIRONMENT_NAME.match(name):
            raise ConfigError(f"environment.{name}: names must be PROTAGINE_ followed by capitals, digits "
                              "and underscores")
        if name in RESERVED_ENVIRONMENT:
            raise ConfigError(f"environment.{name} has its own key: set {RESERVED_ENVIRONMENT[name]} instead")
        if isinstance(value, bool):
            raise ConfigError(f"environment.{name}: YAML read the value as a boolean; quote it "
                              f"(\"{'on' if value else 'off'}\") so the sidecar receives the word")
        if value is None or isinstance(value, (dict, list, tuple, set)):
            raise ConfigError(f"environment.{name} must be a string or a number")
        text = str(value)
        if not text.strip():
            raise ConfigError(f"environment.{name} is empty; remove the entry")
        if any(ord(char) < 32 for char in text):
            raise ConfigError(f"environment.{name} must not contain control characters")
        result[name] = text
    return result


class ConfigError(ValueError):
    """``protagine.yaml`` is missing, malformed or holds an invalid value."""


def instance_home(explicit: str | os.PathLike[str] | None = None) -> Path:
    """The instance directory: an explicit path, ``$PROTAGINE_HOME``, else ``~/.protagine``."""
    selected = explicit or os.environ.get("PROTAGINE_HOME") or os.environ.get("PROTAGINE_STATE_DIR")
    return Path(selected or Path.home() / ".protagine").expanduser()


def config_path(home: Path | None = None) -> Path:
    return instance_home(home) / CONFIG_FILE


def key_path(home: Path | None = None) -> Path:
    return instance_home(home) / KEY_FILE


def identity_path(home: Path | None = None) -> Path:
    return instance_home(home) / IDENTITY_FILE


def _merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _parse_bool(value: Any, *, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUTHY:
        return True
    if text in _FALSY:
        return False
    raise ConfigError(f"{field_name} must be true or false, not {value!r}")


def _require_str_list(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ConfigError(f"{field_name} must be a list of strings")
    return list(value)


def validate(data: dict[str, Any]) -> dict[str, Any]:
    """Coerce and check every field Protagine reads; unknown keys are kept as they are."""
    if not isinstance(data, dict):
        raise ConfigError("protagine.yaml must be a mapping")
    for section in ("sidecar", "hermes", "router", "owner", "mind"):
        if not isinstance(data.get(section), dict):
            raise ConfigError(f"{section} must be a mapping")
    data["environment"] = _validate_environment(data.get("environment"))
    sidecar = data["sidecar"]
    sidecar["host"] = str(sidecar.get("host") or "127.0.0.1").strip()
    try:
        sidecar["port"] = int(sidecar.get("port") or 7777)
    except (TypeError, ValueError):
        raise ConfigError("sidecar.port must be an integer") from None
    if not 1 <= sidecar["port"] <= 65535:
        raise ConfigError("sidecar.port must be between 1 and 65535")
    hermes = data["hermes"]
    hermes["home"] = str(hermes.get("home") or "~/.hermes")
    hermes["python"] = str(hermes.get("python") or "")
    router = data["router"]
    for key in ("base_url", "model", "embed_url", "embed_model", "rerank_url", "rerank_model"):
        router[key] = str(router.get(key) or "")
    if router["rerank_url"] and not router["rerank_model"]:
        raise ConfigError("router.rerank_model is required when router.rerank_url is set")
    dims = router.get("embed_dims")
    if dims in (None, ""):
        dims = 0
    if isinstance(dims, bool) or not isinstance(dims, int):
        try:
            dims = int(str(dims).strip())
        except (TypeError, ValueError):
            raise ConfigError("router.embed_dims must be a whole number of dimensions "
                              "(0 learns it from the endpoint's first embedding)") from None
    if dims < 0:
        raise ConfigError("router.embed_dims must not be negative")
    router["embed_dims"] = dims
    data["owner"]["contact_id"] = str(data["owner"].get("contact_id") or "")
    mind = data["mind"]
    mind["enabled"] = _parse_bool(mind.get("enabled", True), field_name="mind.enabled")
    autonomy = str(mind.get("autonomy") or "suggest").strip().lower()
    if autonomy not in AUTONOMY_LEVELS:
        raise ConfigError(f"mind.autonomy must be one of {', '.join(AUTONOMY_LEVELS)}")
    mind["autonomy"] = autonomy
    deny = mind.get("deny")
    if not isinstance(deny, dict):
        raise ConfigError("mind.deny must be a mapping")
    for key in ("commands", "tools", "text"):
        deny[key] = _require_str_list(deny.get(key), field_name=f"mind.deny.{key}")
    mind["worker_toolsets"] = _require_str_list(mind.get("worker_toolsets"), field_name="mind.worker_toolsets")
    faculties = mind.get("faculties")
    if not isinstance(faculties, dict):
        raise ConfigError("mind.faculties must be a mapping")
    for name, value in list(faculties.items()):
        faculties[name] = _parse_bool(value, field_name=f"mind.faculties.{name}")
    budgets = mind.get("budgets")
    if not isinstance(budgets, dict):
        raise ConfigError("mind.budgets must be a mapping")
    for name, value in list(budgets.items()):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConfigError(f"mind.budgets.{name} must be a number")
    drives = mind.get("drives")
    if not isinstance(drives, dict):
        raise ConfigError("mind.drives must be a mapping of drive weights")
    for name, value in list(drives.items()):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ConfigError(f"mind.drives.{name} must be a non-negative number (0 turns the drive off)")
        drives[name] = float(value)
    try:
        mind["act_threshold"] = float(mind.get("act_threshold", 0.6))
    except (TypeError, ValueError):
        raise ConfigError("mind.act_threshold must be a number") from None
    if not 0 < mind["act_threshold"] <= 1:
        raise ConfigError("mind.act_threshold must be between 0 and 1")
    try:
        mind["digest_hour"] = int(mind.get("digest_hour", 8))
    except (TypeError, ValueError):
        raise ConfigError("mind.digest_hour must be an hour of the day") from None
    if not 0 <= mind["digest_hour"] <= 23:
        raise ConfigError("mind.digest_hour must be between 0 and 23")
    grace = mind.get("heads_up_grace_minutes", 30)
    if isinstance(grace, bool) or not isinstance(grace, (int, float)) or grace < 0:
        raise ConfigError("mind.heads_up_grace_minutes must be a non-negative number of minutes")
    mind["heads_up_grace_minutes"] = float(grace)
    return data


def _apply_env_overrides(data: dict[str, Any], environ: dict[str, str]) -> dict[str, Any]:
    for name, path in ENV_OVERRIDES.items():
        raw = environ.get(name)
        if raw is None or not str(raw).strip():
            continue
        target = data
        for segment in path[:-1]:
            target = target.setdefault(segment, {})
        target[path[-1]] = raw.strip()
    return data


@dataclass
class Config:
    """A validated configuration plus the directory it came from."""

    home: Path
    data: dict[str, Any] = field(default_factory=dict)
    exists: bool = False

    def get(self, dotted: str, default: Any = None) -> Any:
        value: Any = self.data
        for segment in dotted.split("."):
            if not isinstance(value, dict) or segment not in value:
                return default
            value = value[segment]
        return value

    @property
    def sidecar_url(self) -> str:
        host = self.get("sidecar.host")
        host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.get('sidecar.port')}"

    @property
    def hermes_home(self) -> Path:
        return Path(self.get("hermes.home")).expanduser()

    @property
    def path(self) -> Path:
        return self.home / CONFIG_FILE


def read_raw(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from None
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from None
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"{path} must contain a mapping")
    return loaded


def load_config(home: str | os.PathLike[str] | None = None, *, environ: dict[str, str] | None = None,
                required: bool = False) -> Config:
    """Merge defaults, the file and the environment overrides; validate the result.

    With ``required`` a missing file raises ``ConfigError``; otherwise the
    defaults are returned with ``exists=False`` so unconfigured library use
    keeps working.
    """
    environ = dict(os.environ if environ is None else environ)
    directory = instance_home(home)
    path = directory / CONFIG_FILE
    raw: dict[str, Any] = {}
    exists = path.is_file()
    if exists:
        raw = read_raw(path)
    elif required:
        raise ConfigError(f"{path} does not exist; run 'protagine init' first")
    data = _merge(DEFAULTS, raw)
    data = _apply_env_overrides(data, environ)
    return Config(home=directory, data=validate(data), exists=exists)


def dump_config(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)


def _atomic_write(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def save_config(data: dict[str, Any], home: str | os.PathLike[str] | None = None) -> Path:
    """Validate and write ``protagine.yaml``; returns its path."""
    validated = validate(copy.deepcopy(data))
    path = config_path(instance_home(home))
    _atomic_write(path, dump_config(validated).encode("utf-8"), mode=0o600)
    return path


def update_config(changes: dict[str, Any], home: str | os.PathLike[str] | None = None) -> Path | None:
    """Merge ``changes`` into ``protagine.yaml`` without expanding it with defaults.

    Returns the path written, or None when the instance has no configuration
    file yet (library use). The merged result is validated before writing.
    """
    directory = instance_home(home)
    path = directory / CONFIG_FILE
    if not path.is_file():
        return None
    raw = _merge(read_raw(path), changes)
    validate(_merge(DEFAULTS, copy.deepcopy(raw)))
    _atomic_write(path, dump_config(raw).encode("utf-8"), mode=0o600)
    return path


def read_api_key(home: str | os.PathLike[str] | None = None, *, environ: dict[str, str] | None = None) -> str | None:
    """The one API key: ``PROTAGINE_API_KEY`` when set, else the first line of ``api.key``."""
    environ = os.environ if environ is None else environ
    explicit = (environ.get("PROTAGINE_API_KEY") or "").strip()
    if explicit:
        return explicit
    path = key_path(instance_home(home))
    try:
        first = path.read_text(encoding="utf-8").splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    return first or None


def write_api_key(key: str, home: str | os.PathLike[str] | None = None) -> Path:
    key = key.strip()
    if not key or any(ord(char) < 33 or ord(char) > 126 for char in key):
        raise ConfigError("the API key must be one printable ASCII token")
    path = key_path(instance_home(home))
    _atomic_write(path, (key + "\n").encode("ascii"), mode=0o600)
    return path


def key_file_is_private(path: Path) -> bool:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return False
    return not mode & 0o077


def load_identity(home: str | os.PathLike[str] | None = None) -> dict[str, Any]:
    path = identity_path(instance_home(home))
    if not path.is_file():
        return {}
    data = read_raw(path)
    owner = data.get("owner") if isinstance(data.get("owner"), dict) else {}
    agent = data.get("agent") if isinstance(data.get("agent"), dict) else {}
    data["owner"], data["agent"] = owner, agent
    return data


def save_identity(data: dict[str, Any], home: str | os.PathLike[str] | None = None) -> Path:
    path = identity_path(instance_home(home))
    _atomic_write(path, dump_config(data).encode("utf-8"), mode=0o600)
    return path


def apply_environment(config: Config, *, environ: dict[str, str] | None = None) -> dict[str, str]:
    """Export the configuration to the environment names the sidecar reads.

    Values already present in the environment win, so a service unit or a
    shell can still pin one setting. The returned mapping lists what was set.
    """
    target = os.environ if environ is None else environ
    home = config.home
    identity = load_identity(home)
    owner = identity.get("owner", {})
    agent = identity.get("agent", {})
    key = read_api_key(home, environ=target)
    values: dict[str, str] = {
        "PROTAGINE_HOME": str(home),
        "PROTAGINE_STATE_DIR": str(home),
        "PROTAGINE_SIDECAR_HOST": str(config.get("sidecar.host")),
        "PROTAGINE_SIDECAR_PORT": str(config.get("sidecar.port")),
        "HERMES_HOME": str(config.hermes_home),
        # 1.9.0 instances hold contacts.db; new ones use the server's default name.
        "PROTAGINE_CONTACTS_DB": str(home / ("contacts.db" if (home / "contacts.db").exists()
                                             else "protagine-contacts.db")),
        "PROTAGINE_EMBED_PROVIDER": "openai_api" if config.get("router.embed_url") else "skip",
        "PROTAGINE_GRAPH_ENABLED": "false",
    }
    if key:
        values["PROTAGINE_API_KEY"] = key
    if config.get("owner.contact_id"):
        values["PROTAGINE_OWNER_CONTACT_ID"] = str(config.get("owner.contact_id"))
    if owner.get("name"):
        values["PROTAGINE_OWNER_NAME"] = str(owner["name"])
    if agent.get("name"):
        values["PROTAGINE_PERSONA_NAME"] = str(agent["name"])
    if agent.get("values"):
        import json
        values["PROTAGINE_AGENT_VALUES"] = json.dumps(list(agent["values"]), ensure_ascii=True)
    if agent.get("timezone"):
        values["PROTAGINE_AGENT_TIMEZONE"] = str(agent["timezone"])
        values["PROTAGINE_TIMEZONE"] = str(agent["timezone"])
    if agent.get("quiet_hours"):
        values["PROTAGINE_AGENT_QUIET_HOURS"] = str(agent["quiet_hours"])
    if config.get("router.embed_url"):
        values["PROTAGINE_EMBED_BASE_URL"] = str(config.get("router.embed_url"))
        if config.get("router.embed_model"):
            values["PROTAGINE_EMBED_MODEL"] = str(config.get("router.embed_model"))
        # An explicit width is validated against every vector; without one the
        # provider learns the width from the endpoint's first embedding.
        if config.get("router.embed_dims"):
            values["PROTAGINE_EMBED_DIMS"] = str(int(config.get("router.embed_dims")))
    if config.get("router.rerank_url"):
        # A reranker endpoint is configured the way the embedding endpoint is:
        # the remote provider, the model it serves, and recall told to use it.
        # The environment still wins, so "shadow" can be pinned to measure first.
        values["PROTAGINE_RERANKER_PROVIDER"] = "openai_api"
        values["PROTAGINE_RERANKER_BASE_URL"] = str(config.get("router.rerank_url"))
        values["PROTAGINE_RERANKER_MODEL"] = str(config.get("router.rerank_model"))
        values["PROTAGINE_RECALL_RERANK"] = "on"
    # The mapping says explicitly what the keys above only imply (validated: PROTAGINE_
    # names, none that a key already owns), so it lands over the derived values and under
    # the process environment.
    mapping = config.get("environment") or {}
    values.update(mapping)
    applied: dict[str, str] = {}
    for name, value in values.items():
        if name in target and str(target[name]).strip():
            continue
        target[name] = value
        applied[name] = value
    exported = [name for name in mapping if name in applied]
    if exported:
        # Names only; a credential's value never reaches a log, and neither does its name.
        named = [name for name in exported if not looks_secret(name)]
        withheld = len(exported) - len(named)
        logger.info("environment from protagine.yaml: %s%s", ", ".join(named) or "(none named)",
                    f" and {withheld} credential entr{'y' if withheld == 1 else 'ies'} (names withheld)"
                    if withheld else "")
    return applied


# ---------------------------------------------------------------------------
# Plain environment switches for the subsystems that still read one.
# ---------------------------------------------------------------------------

def env_choice(name: str, valid: Iterable[str], fallback: str) -> str:
    """One environment switch with an allowed set; unset or invalid gives the fallback."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return fallback
    value = raw.strip().lower()
    return value if value in set(valid) else fallback


def env_bool(name: str, fallback: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return fallback
    return raw.strip().lower() in _TRUTHY
