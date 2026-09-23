"""One-key bearer authentication for the sidecar API.

The instance has one API key (``api.key``, or ``PROTAGINE_API_KEY``). A request
that carries it has full API access; the person it acts for comes from the
request body, exactly as the contact identity the plugin resolved. Owner versus
guest is therefore a property of the contact, never of a credential.

Without a key the API serves loopback callers only (a development convenience,
see ``middleware.is_local_request``), and ``protagine start`` refuses to bind a
non-loopback interface.
"""

from __future__ import annotations

import hmac
import ipaddress
import os
from dataclasses import dataclass

from fastapi import HTTPException
from starlette.requests import Request

AUDIENCES = frozenset({"viewer", "owner", "shared", "global"})
KEY_PRINCIPAL = "api-key"
ANONYMOUS_PRINCIPAL = "anonymous-dev"


class AuthConfigError(RuntimeError):
    """The requested bind is unsafe with the configured authentication."""


@dataclass(frozen=True)
class RequestAuthority:
    """Authority attached to ``request.state`` by the auth middleware."""

    principal_id: str
    authenticated: bool
    anonymous: bool = False
    credential_id: str | None = None
    viewer_person_id: str | None = None
    person_ids: frozenset[str] = frozenset()
    audiences: frozenset[str] = frozenset()


def owner_person_id() -> str:
    return (
        os.environ.get("PROTAGINE_OWNER_PERSON_ID", "").strip()
        or os.environ.get("PROTAGINE_OWNER_CONTACT_ID", "").strip()
        or "owner"
    )


def audience_person_ids() -> dict[str, str]:
    return {
        "owner": owner_person_id(),
        "shared": os.environ.get("PROTAGINE_SHARED_PERSON_ID", "shared").strip() or "shared",
        "global": os.environ.get("PROTAGINE_GLOBAL_PERSON_ID", "global").strip() or "global",
    }


def key_authority() -> RequestAuthority:
    """The authority of a request that presented the instance key."""
    owner = owner_person_id()
    return RequestAuthority(
        principal_id=KEY_PRINCIPAL,
        authenticated=True,
        credential_id="api.key",
        viewer_person_id=owner,
        person_ids=frozenset({owner}),
        audiences=AUDIENCES,
    )


def anonymous_authority() -> RequestAuthority:
    """Loopback development mode without a configured key."""
    return RequestAuthority(principal_id=ANONYMOUS_PRINCIPAL, authenticated=False, anonymous=True)


def request_authority(request: Request | None) -> RequestAuthority:
    if request is None:
        # Direct in-process router calls are trusted like other internal calls.
        return key_authority()
    value = getattr(request.state, "protagine_authority", None)
    if isinstance(value, RequestAuthority):
        return value
    # Routers mounted without the middleware in focused unit tests behave like
    # loopback development mode, never like the key.
    return anonymous_authority()


def _authority_error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=403, detail={"code": code, "message": message})


def resolve_request_person(
    request: Request | None,
    *,
    claimed_person_id: str | None = None,
    context_person_id: str | None = None,
    audience: str | None = None,
) -> str | None:
    """Resolve the person a request acts for.

    With the key the body selects the person (``None`` means no filter). The
    ``viewer`` audience is that same body person (the owner when the body
    names nobody), which is how the memory provider marks a guest prefetch;
    the owner, shared and global lanes are fixed persons. In development mode
    a default person applies and the authenticated lanes need the key.
    """
    authority = request_authority(request)
    claimed = (claimed_person_id or "").strip() or None
    context = (context_person_id or "").strip() or None
    if claimed and context and claimed != context:
        raise _authority_error(
            "person_scope_conflict",
            "person_id and context.contact_id must identify the same person",
        )
    audience = (audience or "").strip().lower() or None
    if audience and audience not in AUDIENCES:
        raise _authority_error("invalid_audience", "unknown audience lane")

    mapped = audience_person_ids()
    lane_person: str | None = None
    if audience:
        if authority.anonymous:
            raise _authority_error(
                "audience_not_granted",
                "development mode cannot select authenticated audience lanes",
            )
        if audience == "viewer":
            lane_person = claimed or context or authority.viewer_person_id
        else:
            lane_person = mapped[audience]
        if not lane_person:
            raise _authority_error("audience_unbound", "the selected audience has no person binding")
        body_person = claimed or context
        if body_person and body_person != lane_person:
            raise _authority_error(
                "person_scope_conflict",
                "body person does not match the selected audience lane",
            )

    target = lane_person or claimed or context
    if authority.authenticated:
        return target

    target = target or (os.environ.get("PROTAGINE_DEV_PERSON_ID", "dev-anonymous").strip() or "dev-anonymous")
    if target in set(mapped.values()):
        raise _authority_error(
            "reserved_authority_required",
            "owner, shared, and global memory require the API key",
        )
    return target


def resolve_turn_person(
    request: Request | None,
    *,
    context_person_id: str | None,
    has_sender: bool,
) -> str | None:
    """The initial contact of a turn; a structured sender is resolved server-side later."""
    return resolve_request_person(request, context_person_id=context_person_id)


def query_person_is_granted(authority: RequestAuthority, person_id: str) -> bool:
    """Validate a query-string person selector without trusting it."""
    target = (person_id or "").strip()
    if not target or authority.authenticated:
        return True
    return target not in set(audience_person_ids().values())


# ---------------------------------------------------------------------------
# Key material and bind safety
# ---------------------------------------------------------------------------

def configured_api_key() -> str | None:
    """``PROTAGINE_API_KEY`` when set, else the instance's ``api.key``."""
    from protagine.config import read_api_key
    return read_api_key()


def token_matches(token: str | None, expected: str | None) -> bool:
    if not token or not expected:
        return False
    return hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8"))


def bearer_token(request: Request) -> str:
    """The credential a request presents: ``Authorization: Bearer`` or ``X-API-Key``."""
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:].strip()
    return request.headers.get("X-API-Key", "").strip()


def is_loopback_host(host: str | None) -> bool:
    """True when a bind address only accepts local connections."""
    value = (host or "").strip().lower()
    if value in {"", "localhost"}:
        return True
    try:
        address = ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return (mapped or address).is_loopback


def check_bind(host: str, api_key: str | None) -> None:
    """Refuse a non-loopback bind without a key.

    Without a key the middleware serves loopback callers only, so a public
    bind would answer every remote request with 403; fail at startup instead.
    """
    if is_loopback_host(host) or api_key:
        return
    raise AuthConfigError(
        f"refusing to bind {host} without an API key: write one to api.key in the "
        "instance directory (protagine init does this) or set PROTAGINE_API_KEY"
    )
