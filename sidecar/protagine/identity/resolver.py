"""The owner's and the persona's identity, from the deployment's environment.

Which contact a handle or a name refers to is the contact store's job
(``SQLiteContactStore.resolve_messaging_handle`` and ``resolve_reference``).

Owner identity rules:
- The owner is resolved once from ``PROTAGINE_OWNER_CONTACT_ID`` and cached.
- ``PROTAGINE_HOST_CONTACT_ID`` is accepted as a deprecated alias.
- A configured-but-unresolvable owner raises :class:`OwnerIdentityError`.
  There is NO fallback to a default string — the silent ``"owner"``
  default was the bug this module replaces. Callers that filter the owner
  out of generated work must fail closed (generate nothing) rather than
  fall through.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

class OwnerIdentityError(RuntimeError):
    """The owner's identity is missing or cannot be resolved."""


def get_owner_contact_id() -> Optional[str]:
    """Return the configured owner contact ID, honouring the legacy alias.

    ``PROTAGINE_OWNER_CONTACT_ID`` is canonical. ``PROTAGINE_HOST_CONTACT_ID``
    is accepted with a deprecation warning so running deployments keep
    their config. Returns None when neither is set — callers must treat
    that as "owner unknown", never as a default string.
    """
    owner = os.environ.get("PROTAGINE_OWNER_CONTACT_ID")
    if owner:
        return owner
    legacy = os.environ.get("PROTAGINE_HOST_CONTACT_ID")
    if legacy:
        logger.warning(
            "PROTAGINE_HOST_CONTACT_ID is deprecated, use PROTAGINE_OWNER_CONTACT_ID"
        )
        return legacy
    return None


def get_owner_name(default: str = "the owner") -> str:
    """Human-readable owner name for prompts/messages.

    Deployment-agnostic: the generic build must never hardcode a person's name.
    Set ``PROTAGINE_OWNER_NAME`` in the deployment env; otherwise a neutral term
    ("the owner") is used so the public code carries no personal identity.
    """
    return os.environ.get("PROTAGINE_OWNER_NAME") or default


def get_persona_name(default: str = "the assistant") -> str:
    """Human-readable persona/agent name for tool descriptions and prompts.

    Same rule: the deployment sets ``PROTAGINE_PERSONA_NAME``; the generic default
    is neutral so the public code names no specific product.
    """
    return os.environ.get("PROTAGINE_PERSONA_NAME") or default
