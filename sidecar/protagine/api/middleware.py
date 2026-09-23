"""API key authentication and request-size middleware for the Protagine sidecar."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from protagine.api.auth import (
    anonymous_authority,
    bearer_token,
    key_authority,
    query_person_is_granted,
    token_matches,
)


# Default cap on request body size (10 MiB). Oversized uploads are rejected
# at the middleware layer before the handler buffers them. Override via
# PROTAGINE_MAX_BODY_BYTES in the environment.
_DEFAULT_MAX_BODY_BYTES = 10 * 1024 * 1024


# Endpoints reachable without a key, even in development mode (no key).
# Health + docs keep the first run smooth; everything else requires the key
# or a loopback caller.
_DEV_MODE_ALLOWED = frozenset({
    "/v1/host/health",
    "/docs",
    "/openapi.json",
    "/redoc",
})

# Routes that are never served without a key, whatever the caller: they
# accept or return credential-grade state.
_ALWAYS_AUTH_REQUIRED = frozenset({
    "/v1/host/configure",
    "/v1/host/agents/register",
    "/v1/host/agents/connect",
    "/v1/host/queue/contract",
})

_DEV_MODE_MESSAGE = (
    "No API key is configured, so only loopback clients sending a loopback "
    "Host header (localhost, 127.0.0.1, ::1) are served. Write the key to "
    "api.key in the instance directory or set PROTAGINE_API_KEY to accept "
    "other clients."
)


def _is_loopback_name(value: str | None) -> bool:
    """True for ``localhost`` or a literal loopback IP (v4, v6, v4-mapped)."""
    if not value:
        return False
    if value.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return (mapped or address).is_loopback


def is_local_request(request: Request) -> bool:
    """True when both the peer address and the Host header are loopback.

    Development mode (no key) serves this case only. Checking the Host header
    as well as the socket peer stops a browser on the same machine from being
    pointed at the API through a DNS-rebinding hostname.
    """
    client = request.client
    if client is None or not _is_loopback_name(client.host):
        return False
    # ``//host:port`` parsing strips the port and IPv6 brackets for us.
    try:
        parts = urlsplit("//" + request.headers.get("host", ""))
        hostname = parts.hostname
    except ValueError:
        return False
    if parts.username is not None or parts.password is not None:
        return False
    return _is_loopback_name(hostname)


def _dev_mode_requires_key(request: Request, path: str) -> bool:
    return (
        path in _ALWAYS_AUTH_REQUIRED
        # PATCH/DELETE /agents/{id} change the same privileged fields
        # (is_primary, capabilities) as register/connect.
        or (request.method in {"PATCH", "DELETE"} and path.startswith("/v1/host/agents/"))
        or path == "/v1/host/queue/work"
        or path.startswith("/v1/host/queue/work/")
        or (path.startswith("/v1/host/queue/workers/") and "/controls" in path)
        or path.startswith("/v1/host/queue/inspection/")
        or path.startswith("/v1/host/queue/attestations/")
    )


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Reject requests that do not carry the instance key.

    Without a key the API runs in development mode: only loopback requests
    (see ``is_local_request``) are served, and credential-handling paths fail
    closed with 503 so an operator cannot accidentally expose them.
    """

    def __init__(self, app, api_key: str | None = None) -> None:
        super().__init__(app)
        self._api_key = (api_key or "").strip() or None

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    @staticmethod
    def _query_scope_error(request: Request, authority):
        # Several list endpoints use query-string person selectors. Validate
        # the common names globally so a new handler cannot accidentally turn
        # a body-bound person back into caller authority in development mode.
        for field in ("person_id", "contact_id", "viewer_person_id"):
            for value in request.query_params.getlist(field):
                if value and not query_person_is_granted(authority, value):
                    return JSONResponse(
                        status_code=403,
                        content={
                            "detail": {
                                "code": "person_scope_not_granted",
                                "message": f"query parameter {field} exceeds caller authority",
                            }
                        },
                    )
        return None

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        request.state.protagine_authority = anonymous_authority()
        request.state.protagine_auth_configuration = {"key_configured": self.configured}

        if path in _DEV_MODE_ALLOWED:
            return await call_next(request)

        if not self._api_key:
            if not is_local_request(request):
                return JSONResponse(
                    status_code=403,
                    content={"detail": {"code": "dev_mode_not_local", "message": _DEV_MODE_MESSAGE}},
                )
            if _dev_mode_requires_key(request, path):
                return JSONResponse(
                    status_code=503,
                    content={
                        "detail": (
                            "Write the API key to api.key in the instance directory or set "
                            "PROTAGINE_API_KEY in the sidecar environment to enable this endpoint."
                        )
                    },
                )
            query_error = self._query_scope_error(request, request.state.protagine_authority)
            if query_error is not None:
                return query_error
            return await call_next(request)

        # Both header styles are in active use: the plugin sends
        # ``Authorization: Bearer``, scripts may send ``X-API-Key``.
        token = bearer_token(request)
        if token_matches(token, self._api_key):
            request.state.protagine_authority = key_authority()
            return await call_next(request)

        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing API key"},
        )


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests that declare a body larger than ``max_bytes``.

    Short-circuits before the handler reads the payload, so oversized uploads
    cannot be used to exhaust memory. Requests without a ``Content-Length``
    header (e.g. chunked transfer encoding) are allowed to pass; FastAPI's
    own buffer limits still apply downstream.
    """

    def __init__(self, app, max_bytes: int = _DEFAULT_MAX_BODY_BYTES) -> None:
        super().__init__(app)
        self._max_bytes = max_bytes

    async def dispatch(self, request: Request, call_next):
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                length = int(cl)
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Invalid Content-Length header"},
                )
            if length > self._max_bytes:
                return JSONResponse(
                    status_code=413,
                    content={
                        "detail": (
                            f"Request body exceeds limit "
                            f"({length} > {self._max_bytes} bytes)"
                        )
                    },
                )
        return await call_next(request)
