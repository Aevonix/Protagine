"""Test support for the one-key API: every authenticated request carries ``KEY``.

The sidecar has one API key. Fixtures that used to mint per-viewer scoped
principals now install ``ApiKeyMiddleware(api_key=KEY)`` and select the person
in the request body, which is how the plugin identifies owner and guests.
The helpers below keep older fixtures readable; none of them grants scopes.
"""

from __future__ import annotations

from protagine.api.auth import (  # noqa: F401  (re-exported for fixtures)
    AUDIENCES,
    RequestAuthority as _RequestAuthority,
    anonymous_authority,
    key_authority,
    owner_person_id,
)

KEY = "test-api-key"
AUTH = {"Authorization": "Bearer " + KEY}

_FIELDS = frozenset(
    {"principal_id", "authenticated", "anonymous", "credential_id", "viewer_person_id", "person_ids", "audiences"}
)


def _principal(**fields):
    """A principal description kept only for readability of older fixtures."""
    return dict(fields)


def _write_keyring(path, principals, *, mode: int = 0o600) -> None:
    """The keyring is gone; the file keeps the old shape for fixtures that still read it."""
    import json
    path.write_text(json.dumps({"version": 1, "principals": list(principals)}))
    path.chmod(mode)


def keyed_host_app(*_ignored, api_key: str = KEY, legacy_key: str | None = None, **_kw):
    """A host-router app behind the one key (what older fixtures built from a keyring)."""
    from fastapi import FastAPI
    from protagine.api.middleware import ApiKeyMiddleware
    from protagine.api.routers import host
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware, api_key=legacy_key or api_key)
    app.include_router(host.router)
    app.include_router(host.v2_router)
    return app


def install_key(app, key: str = KEY):
    from protagine.api.middleware import ApiKeyMiddleware
    app.add_middleware(ApiKeyMiddleware, api_key=key)
    return app


def legacy_authority() -> _RequestAuthority:
    """The one key: what the old global bearer used to be."""
    return key_authority()


def RequestAuthority(**fields) -> _RequestAuthority:  # noqa: N802  (factory named like the class)
    """Build an authority from the old keyring vocabulary.

    Scopes, legacy flags and grants no longer exist; an authenticated
    non-anonymous authority is the key, bound to the owner when no viewer is
    given, and any viewer/person binding is kept for the assertions.
    """
    anonymous = bool(fields.get("anonymous", False))
    authenticated = bool(fields.get("authenticated", not anonymous))
    base = anonymous_authority() if anonymous or not authenticated else key_authority()
    values = {
        "principal_id": fields.get("principal_id", base.principal_id),
        "authenticated": authenticated,
        "anonymous": anonymous,
        "credential_id": fields.get("credential_id", base.credential_id),
        "viewer_person_id": fields.get("viewer_person_id", base.viewer_person_id),
        "person_ids": frozenset(fields.get("person_ids", base.person_ids) or ()),
        "audiences": frozenset(fields.get("audiences", base.audiences) or ()),
    }
    return _RequestAuthority(**values)


def required_scope(method: str, path: str) -> str:
    """Every route needs the one key; the old per-route scope names are gone."""
    return "api"


def compatible_scopes(method: str, path: str) -> frozenset[str]:
    return frozenset({"api"})
