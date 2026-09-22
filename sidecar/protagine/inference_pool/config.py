"""Validated deployment configuration for the shared inference boundary."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Reservation:
    requests: int
    tokens: int


@dataclass(frozen=True)
class Endpoint:
    name: str
    base_url: str
    model: str
    max_requests: int
    max_tokens: int
    context_tokens: int
    reservations: dict[str, Reservation] = field(default_factory=dict)
    max_input_tokens: dict[str, int] = field(default_factory=dict)
    api_key_env: str | None = None
    tokenize_path: str | None = None


@dataclass(frozen=True)
class Route:
    name: str
    model: str
    endpoints: tuple[str, ...]
    traffic_class: str
    priority: int = 0
    queue_timeout_seconds: float = 30.0
    default_output_tokens: int = 2048
    max_output_tokens: int = 8192
    media_tokens_per_item: int | None = None
    max_media_items: int = 4


@dataclass(frozen=True)
class PoolConfig:
    endpoints: dict[str, Endpoint]
    routes: dict[str, Route]
    max_queue: int = 128
    aging_seconds: float = 10.0
    cooldown_seconds: float = 10.0
    cancellation_grace_seconds: float = 0.25
    request_timeout_seconds: float = 1800.0


def _object(value: object, label: str) -> dict:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")  # noqa: TRY004 - configuration errors share one API
    return value


def _keys(value: dict, allowed: set[str], label: str) -> None:
    if value.keys() - allowed:
        raise ValueError(
            f"{label} has unknown fields: {', '.join(sorted(value.keys() - allowed))}"
        )


def _name(value: object, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", value
    ):
        raise ValueError(f"{label} must be a short identifier")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _integer(value: object, label: str, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ValueError(f"{label} must be an integer >= {minimum}")
    return value


def _seconds(value: object, label: str, minimum: float = 0.001) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < minimum
    ):
        raise ValueError(f"{label} must be finite and >= {minimum}")
    return float(value)


def parse_config(raw: object) -> PoolConfig:
    """Reject ambiguous capacity and misspelled policy before accepting work.

    One endpoint entry represents one physical serving replica. Multiple routes
    reference that entry rather than defining aliases with independent counters.
    Replica equivalence is a deployment qualification, not inferred from names.
    """
    data = _object(raw, "pool")
    _keys(
        data,
        {
            "version",
            "endpoints",
            "routes",
            "max_queue",
            "aging_seconds",
            "cooldown_seconds",
            "cancellation_grace_seconds",
            "request_timeout_seconds",
        },
        "pool",
    )
    if type(data.get("version", 1)) is not int or data.get("version", 1) != 1:
        raise ValueError("unsupported pool configuration version")
    endpoints: dict[str, Endpoint] = {}
    addresses: set[str] = set()
    for name, value in _object(data.get("endpoints"), "endpoints").items():
        _name(name, "endpoint name")
        item = _object(value, f"endpoint {name}")
        _keys(
            item,
            {
                "base_url",
                "model",
                "max_requests",
                "max_tokens",
                "context_tokens",
                "reservations",
                "max_input_tokens",
                "api_key_env",
                "tokenize_path",
            },
            f"endpoint {name}",
        )
        base_url = _text(item.get("base_url"), "base_url").rstrip("/")
        parsed = urlsplit(base_url)
        try:
            port = parsed.port
        except ValueError:
            raise ValueError("invalid endpoint port") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "endpoint base_url must be HTTP(S), without credentials, query or fragment"
            )
        # Normalize the conventional /v1 suffix so it cannot create a second
        # admission counter for the same server. DNS aliases remain an operator
        # responsibility: declare a physical replica exactly once.
        path = parsed.path.removesuffix("/v1").rstrip("/")
        identity = f"{parsed.scheme}://{parsed.hostname.lower()}:{port or (443 if parsed.scheme == 'https' else 80)}{path}"
        if identity in addresses:
            raise ValueError(
                "duplicate physical endpoint; reference its existing name from each route"
            )
        addresses.add(identity)
        requests = _integer(item.get("max_requests"), "max_requests")
        tokens = _integer(item.get("max_tokens"), "max_tokens")
        context = _integer(item.get("context_tokens"), "context_tokens")
        reservations = {}
        for kind, reserve in _object(
            item.get("reservations", {}), "reservations"
        ).items():
            _name(kind, "traffic class")
            reserve = _object(reserve, "reservation")
            _keys(reserve, {"requests", "tokens"}, "reservation")
            reservations[kind] = Reservation(
                _integer(reserve.get("requests", 0), "reserved requests", 0),
                _integer(reserve.get("tokens", 0), "reserved tokens", 0),
            )
        if (
            sum(r.requests for r in reservations.values()) > requests
            or sum(r.tokens for r in reservations.values()) > tokens
        ):
            raise ValueError(f"endpoint {name} reservations exceed its capacity")
        limits = {
            _name(k, "traffic class"): _integer(v, "max input tokens", 0)
            for k, v in _object(
                item.get("max_input_tokens", {}), "max_input_tokens"
            ).items()
        }
        api_key_env = item.get("api_key_env")
        if api_key_env is not None and (
            not isinstance(api_key_env, str)
            or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", api_key_env)
        ):
            raise ValueError("api_key_env must name an environment variable")
        tokenize_path = item.get("tokenize_path")
        if tokenize_path is not None and (
            not isinstance(tokenize_path, str)
            or not tokenize_path.startswith("/")
            or tokenize_path.startswith("//")
            or any(character in tokenize_path for character in "?#\\")
            or ".." in tokenize_path.split("/")
        ):
            raise ValueError("tokenize_path must be a path on the configured endpoint")
        endpoints[name] = Endpoint(
            name,
            base_url,
            _text(item.get("model"), "model"),
            requests,
            tokens,
            context,
            reservations,
            limits,
            api_key_env,
            tokenize_path,
        )
    if not endpoints:
        raise ValueError("at least one endpoint is required")
    routes: dict[str, Route] = {}
    for name, value in _object(data.get("routes"), "routes").items():
        _name(name, "route name")
        item = _object(value, f"route {name}")
        _keys(
            item,
            {
                "model",
                "endpoints",
                "traffic_class",
                "priority",
                "queue_timeout_seconds",
                "default_output_tokens",
                "max_output_tokens",
                "media_tokens_per_item",
                "max_media_items",
            },
            f"route {name}",
        )
        candidates = item.get("endpoints")
        if (
            not isinstance(candidates, list)
            or not candidates
            or any(not isinstance(n, str) or n not in endpoints for n in candidates)
            or len(candidates) != len(set(candidates))
        ):
            raise ValueError(f"route {name} needs distinct declared endpoints")
        default_output = _integer(
            item.get("default_output_tokens", 2048), "default_output_tokens"
        )
        max_output = _integer(item.get("max_output_tokens", 8192), "max_output_tokens")
        if default_output > max_output:
            raise ValueError("default output budget exceeds maximum output budget")
        media = item.get("media_tokens_per_item")
        if media is not None:
            media = _integer(media, "media_tokens_per_item")
        routes[name] = Route(
            name,
            _text(item.get("model"), "model"),
            tuple(candidates),
            _name(item.get("traffic_class"), "traffic_class"),
            _integer(item.get("priority", 0), "priority", 0),
            _seconds(item.get("queue_timeout_seconds", 30), "queue_timeout_seconds"),
            default_output,
            max_output,
            media,
            _integer(item.get("max_media_items", 4), "max_media_items"),
        )
    if not routes:
        raise ValueError("at least one route is required")
    classes = {route.traffic_class for route in routes.values()}
    for endpoint in endpoints.values():
        if (endpoint.reservations.keys() | endpoint.max_input_tokens.keys()) - classes:
            raise ValueError(
                f"endpoint {endpoint.name} refers to an unknown traffic class"
            )
    return PoolConfig(
        endpoints,
        routes,
        _integer(data.get("max_queue", 128), "max_queue"),
        _seconds(data.get("aging_seconds", 10), "aging_seconds"),
        _seconds(data.get("cooldown_seconds", 10), "cooldown_seconds", 0),
        _seconds(
            data.get("cancellation_grace_seconds", 0.25),
            "cancellation_grace_seconds",
            0,
        ),
        _seconds(data.get("request_timeout_seconds", 1800), "request_timeout_seconds"),
    )


def load_config(path: str | Path) -> PoolConfig:
    with Path(path).open(encoding="utf-8") as stream:
        return parse_config(json.load(stream))
