"""ParticipantResolver -- per-message sender -> contact attribution.

The attribution chokepoint for relationship intelligence (docs/RELATIONSHIPS.md):
every synced turn that carries a ``sender`` resolves to a real contact here,
server-side, regardless of client caching. Unknown people become shadow
contacts so history accrues from first contact; machines resolve to the
reserved ``system`` sentinel and never touch relationship stores.

Resolution ladder (first hit wins):
  1. exact / cross-gateway messaging handle (an E.164 number is one identity on
     any gateway, emails normalize; C1)
  2. scoped display-name: propose a candidate association for the owner to
     confirm, without attributing the sender to that person's identity or
     private history
  3. shadow contact (tier ``unknown``, ``may_contact='ask'``, provenance
     recorded). Meeting people is the design, not an option (architecture 4.7
     item 1): the social drive ignores a shadow until the owner sets a cadence
     or a tier, so a group chat never becomes a stream of check-in asks.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Reserved contact id for machine-origin turns. Comms may record it for ops
#: visibility; ToM/interactions/relationship surfaces must always exclude it.
SYSTEM_CONTACT_ID = "system"

# Markers that a source turn is system / skill / non-conversational, i.e. not
# genuine conversation: such a senderless turn is a machine's, never a person's.
_SYSTEM_ORIGIN = re.compile(
    r"\binvoked\b.{0,60}?\bskill\b"          # "... invoked the <name> skill ..."
    r"|previous turn was interrupted"
    r"|\bsystem note\b"
    r"|\bsystem[- ]?generated\b"
    r"|conversation (?:was )?(?:interrupted|truncated|reset)"
    r"|<\s*/?\s*system[\s>]",                 # <system> ... </system> style tags
    re.IGNORECASE | re.DOTALL,
)


def is_system_origin(text: Optional[str]) -> bool:
    """True if the raw source text is a system/skill/non-conversational turn."""
    return bool(text) and bool(_SYSTEM_ORIGIN.search(str(text)))


def machine_channel_prefixes() -> tuple:
    raw = os.environ.get("PROTAGINE_IDENTITY_MACHINE_CHANNELS", "cron,api,internal")
    return tuple(p.strip().lower() for p in raw.split(",") if p.strip())


def is_machine_turn(channel_id: str, user_text: str, has_sender: bool) -> bool:
    """A turn is machine-origin when it has no human sender AND either rides
    a machine channel or carries system-origin text markers. A turn WITH a
    resolved human sender is never reclassified (a human talking on an api
    channel is still a human)."""
    if has_sender:
        return False
    ch = (channel_id or "").strip().lower()
    for prefix in machine_channel_prefixes():
        if ch == prefix or ch.startswith(prefix + ":"):
            return True
    return is_system_origin(user_text)


@dataclass
class Resolution:
    contact_id: Optional[str]
    method: str          # contact_id | handle | shadow | none
    created: bool = False
    proposal_filed: bool = False
    candidate_contact_id: Optional[str] = None
    proposal_id: Optional[str] = None


class ParticipantResolver:
    def __init__(self, contacts_store: Any) -> None:
        self._store = contacts_store

    async def resolve(self, *, platform: str, user_id: str,
                      display_name: str = "", group_id: str = "",
                      channel_id: str = "") -> Resolution:
        """Resolve a sender to a contact id (see module doc for the ladder)."""
        platform = (platform or "").strip().lower()
        user_id = (user_id or "").strip()
        if not user_id:
            return Resolution(None, "none")

        # 0. Already-canonical contact id (e.g. the voice channel, where the
        # speaker was resolved to a contact upstream and passed as user_id).
        # Confirm it exists rather than minting a shadow for it.
        if user_id.startswith("cid-"):
            try:
                if await self._store.get(user_id) is not None:
                    return Resolution(user_id, "contact_id")
            except Exception:
                pass

        # 1. Handle match (exact, then the canonical phone or email identity).
        try:
            c = await self._store.resolve_messaging_handle(platform, user_id)
        except Exception:
            logger.debug("participant handle resolve failed", exc_info=True)
            c = None
        if c is not None:
            return Resolution(c.contact_id, "handle")

        # 2. A name can suggest a link; it cannot establish attribution. The
        # candidate becomes an owner ask (link_proposal) and is confirmed or
        # rejected through the store, never by the resolver.
        candidate, proposal = None, None
        if display_name and group_id:
            match = await self._scoped_name_match(platform, group_id, display_name)
            if match is not None:
                proposal = await self._file_handle_proposal(
                    match, platform, user_id, display_name)
                candidate = match

        # 3. Shadow contact: remembered, permission 'ask', no standing. A shadow
        # without its handle would be an orphan minted on every turn, so one
        # whose handle cannot be attached (a number the owner split between two
        # people) is discarded and the sender stays unresolved.
        contact = None
        try:
            contact = await self._store.create(
                display_name=display_name or user_id,
                trust_tier="unknown",
                may_contact="ask",
                import_source="auto:sender",
                notes=(f"Auto-created from first contact on {channel_id or platform}"
                       + (f" (group {group_id})" if group_id else "")),
            )
            await self._store.add_handle(
                contact.contact_id, platform, user_id,
                is_primary=True, confidence=0.9, source="auto:sender")
            logger.info("Shadow contact %s created for %s:%s (%r)",
                        contact.contact_id, platform, user_id,
                        display_name or "?")
            return Resolution(contact.contact_id, "shadow", created=True,
                              proposal_filed=bool(proposal), candidate_contact_id=candidate,
                              proposal_id=proposal)
        except Exception:
            logger.warning("shadow contact creation failed for %s:%s",
                           platform, user_id, exc_info=True)
            if contact is not None:
                try:
                    await self._store.hard_delete(contact.contact_id, performed_by="auto:sender")
                except Exception:
                    logger.warning("could not discard the handle-less shadow %s", contact.contact_id)
            return Resolution(None, "none")

    # -- rung 2 helpers ----------------------------------------------------
    async def _scoped_name_match(self, platform: str, group_id: str,
                                 display_name: str) -> Optional[str]:
        """The display name matches exactly ONE member of this group's scope."""
        try:
            scope = await self._store.get_scope(
                platform=platform, external_id=str(group_id))
            if scope is None:
                return None
            members = await self._store.scope_members(scope.scope_id)
        except Exception:
            return None
        hits = []
        want = display_name.strip().lower()
        for m in members or []:
            cid = getattr(m, "contact_id", None) or (
                m.get("contact_id") if isinstance(m, dict) else None)
            if not cid:
                continue
            try:
                c = await self._store.get(cid)
            except Exception:
                continue
            if c is None:
                continue
            names = {str(c.display_name or "").strip().lower(),
                     str(getattr(c, "given_name", "") or "").strip().lower()}
            if want and (want in names
                         or any(n and want.split()[0] == n.split()[0]
                                for n in names if n)):
                hits.append(c.contact_id)
        return hits[0] if len(hits) == 1 else None

    async def _file_handle_proposal(self, contact_id: str, platform: str,
                                    user_id: str, display_name: str) -> Optional[str]:
        """Keep the hypothesis outside the handle index used for attribution."""
        try:
            result = await self._store.propose_handle_link(contact_id, platform, user_id)
            return result['candidate_id'] if result['status'] == 'pending' else None
        except Exception:
            logger.debug("handle proposal failed", exc_info=True)
            return None
