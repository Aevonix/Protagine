"""API key authentication + request-size middleware for Protagine sidecar."""

from __future__ import annotations

import hmac
import ipaddress
import os
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Match

from protagine.api.auth_telemetry import AuthTelemetry
from protagine.api.contact_grants import ContactGrantRegistry

from protagine.api.authority import (
    KeyringLoader,
    anonymous_authority,
    compatible_scopes,
    legacy_authority,
    query_person_is_granted,
    required_scope,
    scoped_authority,
)


# Default cap on request body size (10 MiB). Oversized uploads are rejected
# at the middleware layer before the handler buffers them. Override via
# PROTAGINE_MAX_BODY_BYTES in the environment.
_DEFAULT_MAX_BODY_BYTES = 10 * 1024 * 1024


# Endpoints reachable without a key, even in "dev mode" (no PROTAGINE_API_KEY).
# Health + docs keep the first-run wizard smooth; everything else (including
# /configure, which accepts LLM credentials) requires an explicit key.
_DEV_MODE_ALLOWED = frozenset({
    "/v1/host/health",
    "/docs",
    "/openapi.json",
    "/redoc",
})

# Routes that must never be served without an API key, regardless of
# PROTAGINE_API_KEY presence — these accept or return credential-grade state
# and must never be anonymously reachable. Agent registration / connect
# endpoints accept caller-supplied `is_primary` and capability lists, so
# they must fail closed in dev mode rather than letting an unauthenticated
# caller register a fully-privileged primary agent.
_ALWAYS_AUTH_REQUIRED = frozenset({
    "/v1/host/configure",
    "/v1/host/agents/register",
    "/v1/host/agents/connect",
    "/v1/host/queue/contract",
})

_DEV_MODE_MESSAGE = (
    "API authentication is not configured, so only loopback clients sending a "
    "loopback Host header (localhost, 127.0.0.1, ::1) are served. Set "
    "PROTAGINE_API_KEY or PROTAGINE_API_KEYRING_PATH in the sidecar "
    "environment to accept other clients."
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

    Dev mode (no API key) serves this case only. Checking the Host header
    as well as the socket peer stops a browser on the same machine from
    being pointed at the API through a DNS-rebinding hostname.
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


class ApiKeyMiddleware(BaseHTTPMiddleware):
    """Reject requests that don't carry the correct Bearer token.

    Without either auth mechanism the API runs in dev mode: only loopback
    requests (see ``is_local_request``) are served, and
    ``_ALWAYS_AUTH_REQUIRED`` paths fail closed with 503 so an operator
    cannot accidentally expose credential-handling endpoints.
    """

    def __init__(
        self,
        app,
        api_key: str | None = None,
        keyring_path: str | None = None,
        auth_telemetry: AuthTelemetry | None = None,
        contact_grants: ContactGrantRegistry | None = None,
    ) -> None:
        super().__init__(app)
        self._api_key = api_key
        self._keyring = KeyringLoader(keyring_path)
        self._telemetry = auth_telemetry or AuthTelemetry()
        self._contact_grants = contact_grants or ContactGrantRegistry(None)

    @staticmethod
    def _route_template(request: Request) -> str:
        """Resolve a framework template without persisting concrete path IDs."""

        matched = request.scope.get("route")
        template = getattr(matched, "path", None)
        if isinstance(template, str) and template:
            return template
        for candidate in getattr(request.app, "routes", ()):
            try:
                match, _ = candidate.matches(request.scope)
            except Exception:
                continue
            if match == Match.FULL:
                template = getattr(candidate, "path", None)
                if isinstance(template, str) and template:
                    return template
        return "<unmatched>"

    @staticmethod
    def _auth_labels(authority) -> tuple[str, str]:
        if authority.legacy:
            return "legacy", "legacy"
        if authority.anonymous:
            return "anonymous", "anonymous-dev"
        return "scoped", authority.principal_id

    def _record(
        self,
        request: Request,
        *,
        authority=None,
        auth_kind: str | None = None,
        principal_id: str | None = None,
        decision: str,
        reason: str,
        scope: str,
        route: str,
    ) -> None:
        if authority is not None:
            auth_kind, principal_id = self._auth_labels(authority)
        self._telemetry.record(
            auth_kind=auth_kind or "unauthenticated",
            principal_id=principal_id or "unauthenticated",
            method=request.method,
            route=route,
            required_scope=scope,
            decision=decision,
            reason=reason,
        )

    async def _call_authorized(
        self,
        request: Request,
        call_next,
        *,
        authority,
        scope: str,
        route: str,
        allow_reason: str = "allowed",
    ):
        try:
            response = await call_next(request)
        except BaseException:
            self._record(
                request, authority=authority, decision="allow",
                reason="handler_exception", scope=scope, route=route,
            )
            raise
        if response.status_code in (401, 403):
            self._record(
                request, authority=authority, decision="deny",
                reason="authority_denied", scope=scope, route=route,
            )
        else:
            self._record(
                request, authority=authority, decision="allow",
                reason=allow_reason, scope=scope, route=route,
            )
        return response

    @staticmethod
    def _query_scope_error(request: Request, authority):
        # Several legacy list endpoints use query-string person selectors.
        # Validate all common names globally so adding a new handler cannot
        # accidentally turn a body-bound principal back into caller authority.
        for field in ("person_id", "contact_id", "viewer_person_id"):
            for value in request.query_params.getlist(field):
                if value and not query_person_is_granted(authority, value):
                    return JSONResponse(
                        status_code=403,
                        content={
                            "detail": {
                                "code": "person_scope_not_granted",
                                "message": f"query parameter {field} exceeds principal authority",
                            }
                        },
                    )
        return None

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        request.state.protagine_authority = anonymous_authority()
        request.state.protagine_auth_telemetry = self._telemetry
        request.state.protagine_contact_grants = self._contact_grants
        request.state.protagine_keyring_status = self._keyring.status()
        request.state.protagine_auth_configuration = {
            "legacy_configured": bool(self._api_key),
            "scoped_configured": self._keyring.configured,
            "dual_accept": bool(self._api_key) and self._keyring.configured,
        }
        route = self._route_template(request)
        scope = required_scope(request.method, path)
        scope_compatibility = compatible_scopes(request.method, path)
        approval_mode = os.environ.get(
            "PROTAGINE_APPROVAL_AUTHORITY_MODE", "shadow"
        ).strip().lower()
        exact_approval_scope = bool(
            approval_mode != "shadow"
            and (
                scope.startswith("approvals:")
                or scope.startswith("charter:approval-")
            )
        )

        if exact_approval_scope and approval_mode not in {"shadow", "enforce"}:
            self._record(
                request, authority=request.state.protagine_authority,
                decision="deny", reason="approval_authority_mode_invalid",
                scope=scope, route=route,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "detail": {
                        "code": "approval_authority_mode_invalid",
                        "message": (
                            "PROTAGINE_APPROVAL_AUTHORITY_MODE must be shadow or enforce"
                        ),
                    }
                },
            )

        if path in _DEV_MODE_ALLOWED:
            return await call_next(request)

        auth_configured = bool(self._api_key) or self._keyring.configured
        if not auth_configured:
            if not is_local_request(request):
                self._record(
                    request, authority=request.state.protagine_authority,
                    decision="deny", reason="dev_mode_not_local",
                    scope=scope, route=route,
                )
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": {
                            "code": "dev_mode_not_local",
                            "message": _DEV_MODE_MESSAGE,
                        }
                    },
                )
            if exact_approval_scope:
                self._record(
                    request, authority=request.state.protagine_authority,
                    decision="deny", reason="exact_scoped_principal_required",
                    scope=scope, route=route,
                )
                return JSONResponse(
                    status_code=403,
                    content={
                        "detail": {
                            "code": "exact_scoped_principal_required",
                            "required_scopes": ["api:access", scope],
                        }
                    },
                )
            if (
                path in _ALWAYS_AUTH_REQUIRED
                # PATCH/DELETE /agents/{id} change the same privileged
                # fields (is_primary, capabilities) as register/connect.
                or (
                    request.method in {"PATCH", "DELETE"}
                    and path.startswith("/v1/host/agents/")
                )
                or path == "/v1/host/queue/work"
                or path.startswith("/v1/host/queue/work/")
                or (
                    path.startswith("/v1/host/queue/workers/")
                    and "/controls" in path
                )
                or path.startswith("/v1/host/queue/inspection/")
                or path.startswith("/v1/host/queue/attestations/")
            ):
                self._record(
                    request, authority=request.state.protagine_authority,
                    decision="deny", reason="auth_not_configured",
                    scope=scope, route=route,
                )
                return JSONResponse(
                    status_code=503,
                    content={
                        "detail": (
                            "Set PROTAGINE_API_KEY or PROTAGINE_API_KEYRING_PATH in "
                            "the sidecar environment to enable this endpoint."
                        )
                    },
                )
            query_error = self._query_scope_error(
                request, request.state.protagine_authority
            )
            if query_error is not None:
                self._record(
                    request, authority=request.state.protagine_authority,
                    decision="deny", reason="person_scope_not_granted",
                    scope=scope, route=route,
                )
                return query_error
            return await self._call_authorized(
                request, call_next, authority=request.state.protagine_authority,
                scope=scope, route=route,
            )

        # Both header styles are in active use: the gateway sends
        # ``Authorization: Bearer``, the poller/queue-worker scripts send
        # ``X-API-Key`` (and advertise it to agents in job payloads).
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else request.headers.get("X-API-Key", "")

        if token:
            scoped_match = self._keyring.authenticate(token)
            authority = None
            if scoped_match is not None and scoped_match.accepts():
                principal = scoped_match.principal
                exact_grants = frozenset()
                if principal.attested_contact_platforms:
                    exact_grants = self._contact_grants.person_ids(
                        principal.principal_id,
                        max_person_ids=principal.attested_contact_limit,
                    )
                authority = scoped_authority(scoped_match, exact_grants)
            elif self._api_key and hmac.compare_digest(
                token.encode("utf-8"), self._api_key.encode("utf-8")
            ):
                authority = legacy_authority()

            if authority is not None:
                claimed_principal = request.headers.get("X-Protagine-Principal", "").strip()
                if claimed_principal and claimed_principal != authority.principal_id:
                    self._record(
                        request, authority=authority, decision="deny",
                        reason="principal_mismatch", scope=scope, route=route,
                    )
                    return JSONResponse(
                        status_code=403,
                        content={
                            "detail": {
                                "code": "principal_mismatch",
                                "message": "claimed principal does not match the credential",
                            }
                        },
                    )
                if scope == "api:access" and not authority.allow_unscoped_api:
                    self._record(
                        request, authority=authority, decision="deny",
                        reason="unscoped_api_denied", scope=scope, route=route,
                    )
                    return JSONResponse(
                        status_code=403,
                        content={
                            "detail": {
                                "code": "unscoped_api_denied",
                                "message": (
                                    "this principal may use only explicitly "
                                    "mapped API routes"
                                ),
                            }
                        },
                    )
                if exact_approval_scope and (
                    authority.legacy
                    or not authority.authenticated
                    or not authority.has_scope("api:access")
                ):
                    self._record(
                        request, authority=authority, decision="deny",
                        reason="exact_scoped_principal_required",
                        scope=scope, route=route,
                    )
                    return JSONResponse(
                        status_code=403,
                        content={
                            "detail": {
                                "code": "exact_scoped_principal_required",
                                "required_scopes": ["api:access", scope],
                            }
                        },
                    )
                exact_scope = authority.has_scope(scope)
                compatible_scope = next(
                    (
                        candidate for candidate in sorted(scope_compatibility)
                        if authority.has_scope(candidate)
                        and not (
                            candidate == "api:access"
                            and not authority.allow_unscoped_api
                        )
                    ),
                    None,
                )
                if (
                    not exact_scope
                    and "api:access" in scope_compatibility
                    and authority.has_scope("api:access")
                    and not authority.allow_unscoped_api
                ):
                    self._record(
                        request, authority=authority, decision="deny",
                        reason="unscoped_api_denied", scope=scope, route=route,
                    )
                    return JSONResponse(
                        status_code=403,
                        content={
                            "detail": {
                                "code": "unscoped_api_denied",
                                "message": (
                                    "this principal may use only explicitly "
                                    "mapped API routes"
                                ),
                            }
                        },
                    )
                if not exact_scope and compatible_scope is None:
                    self._record(
                        request, authority=authority, decision="deny",
                        reason="insufficient_scope", scope=scope, route=route,
                    )
                    return JSONResponse(
                        status_code=403,
                        content={
                            "detail": {
                                "code": "insufficient_scope",
                                "required_scope": scope,
                            }
                        },
                    )
                request.state.protagine_authority = authority
                query_error = self._query_scope_error(request, authority)
                if query_error is not None:
                    self._record(
                        request, authority=authority, decision="deny",
                        reason="person_scope_not_granted", scope=scope, route=route,
                    )
                    return query_error
                return await self._call_authorized(
                    request, call_next, authority=authority,
                    scope=scope, route=route,
                    allow_reason=(
                        "compatible_scope_allowed"
                        if not exact_scope else "allowed"
                    ),
                )

        # If scoped auth is the only configured method and its file is unsafe
        # or malformed, report an operator-fixable outage rather than pretending
        # a valid credential merely failed authentication.
        if self._keyring.configured and self._keyring.error and not self._api_key:
            self._record(
                request, decision="deny", reason="scoped_auth_unavailable",
                scope=scope, route=route,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "detail": {
                        "code": "scoped_auth_unavailable",
                        "message": "Scoped API authentication is unavailable",
                    }
                },
            )

        self._record(
            request, decision="deny",
            reason="invalid_key" if token else "missing_key",
            scope=scope, route=route,
        )
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid or missing API key"},
        )


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests that declare a body larger than ``max_bytes``.

    Short-circuits before the handler reads the payload, so oversized uploads
    cannot be used to exhaust memory. Requests without a ``Content-Length``
    header (e.g. chunked transfer encoding) are allowed to pass — FastAPI's
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
