"""Protagine Contacts — data model dataclasses."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from protagine.util.temporal import now_utc


# ── Trust tiers ──────────────────────────────────────────────────────────────

# "group_guest" is a *scoped* tier: it is the trust a person is granted WITHIN a
# trust_scope (e.g. a group chat) — converse in-context, but no 1:1 DM rights and
# no proactive reach-out. A contact's global trust_tier is unaffected by it.
TRUST_TIERS = ("inner_circle", "trusted", "regular", "group_guest", "peripheral", "silenced", "acquaintance", "unknown")

# Higher index = more permissive
_TIER_RANK = {t: i for i, t in enumerate(("unknown", "acquaintance", "silenced", "peripheral", "group_guest", "regular", "trusted", "inner_circle"))}

PRIVACY_LEVELS = ("public", "private", "restricted")
GATEWAYS = ("imessage", "telegram", "email", "sms", "signal", "custom", "internal")

# Per-contact outbound permission (architecture 7.4). A tier is standing, never permission:
# only the owner raises ``may_contact``; a contact's opt-out lowers it to ``never``.
MAY_CONTACT = ("never", "ask", "auto")


def tier_rank(tier: str) -> int:
    """The tier's rank; unknown names rank lowest."""
    return _TIER_RANK.get(tier, 0)


def regular_or_above(tier: str) -> bool:
    """``regular``, ``trusted`` or ``inner_circle``: the tiers the social drive considers on their own."""
    return tier_rank(tier) >= _TIER_RANK["regular"]


def more_restrictive_privacy(a: str, b: str) -> str:
    """Return the more restrictive privacy level."""
    rank = {"public": 0, "private": 1, "restricted": 2}
    return a if rank.get(a, 1) >= rank.get(b, 1) else b


# ── Core entities ─────────────────────────────────────────────────────────────

@dataclass
class Contact:
    """A person known to Protagine."""
    contact_id: str
    display_name: Optional[str]
    given_name: Optional[str]
    family_name: Optional[str]
    organization: Optional[str]
    relationship_score: float
    trust_tier: str
    tags: List[str]
    privacy_level: str
    person_node_id: Optional[str]
    notes: Optional[str]
    import_source: str
    first_seen_at: str
    last_interaction_at: Optional[str]
    interaction_count: int
    enrichment_source: List[str]
    enrichment_last_at: Optional[str]
    deleted_at: Optional[str]
    created_at: str
    updated_at: str
    timezone: Optional[str] = None  # IANA tz for this contact (v0.21.0), editable
    # Introduction provenance (social-graph autonomy): the contact_id of whoever
    # introduced this person, and a met_via dict ({channel, scope_id, ...}) for
    # how/where the agent met them. None for contacts not created via an intro.
    introduced_by: Optional[str] = None
    met_via: Optional[Dict[str, Any]] = None
    # People (M5): outbound permission, the owner-set cadence and the per-contact digest.
    may_contact: str = "ask"
    cadence_minutes: Optional[int] = None
    digest: Optional[str] = None
    digest_sources: List[str] = field(default_factory=list)

    @property
    def is_shadow(self) -> bool:
        """A sender the agent remembered on its own (``auto:*``) that the owner has not filed yet
        (the tier is still ``unknown``). Its handles are the sender's own choosing, so they identify
        nobody for the owner: they are never an exact reference, and a confirmed link folds a shadow
        but never an established contact."""
        return str(self.import_source or "").startswith("auto:") and self.trust_tier == "unknown"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "contact_id": self.contact_id,
            "display_name": self.display_name,
            "given_name": self.given_name,
            "family_name": self.family_name,
            "organization": self.organization,
            "relationship_score": self.relationship_score,
            "trust_tier": self.trust_tier,
            "may_contact": self.may_contact,
            "cadence_minutes": self.cadence_minutes,
            "digest": self.digest,
            "digest_sources": self.digest_sources,
            "tags": self.tags,
            "privacy_level": self.privacy_level,
            "person_node_id": self.person_node_id,
            "notes": self.notes,
            "import_source": self.import_source,
            "first_seen_at": self.first_seen_at,
            "last_interaction_at": self.last_interaction_at,
            "interaction_count": self.interaction_count,
            "enrichment_source": self.enrichment_source,
            "enrichment_last_at": self.enrichment_last_at,
            "deleted_at": self.deleted_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "timezone": self.timezone,
            "introduced_by": self.introduced_by,
            "met_via": self.met_via,
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "Contact":
        tags_json = row.get("tags_json", "[]")
        tags = json.loads(tags_json) if isinstance(tags_json, str) else (tags_json or [])
        enrichment_source_json = row.get("enrichment_source", "[]")
        enrichment_source = (
            json.loads(enrichment_source_json)
            if isinstance(enrichment_source_json, str)
            else (enrichment_source_json or [])
        )
        met_via_raw = row.get("met_via_json")
        met_via = json.loads(met_via_raw) if isinstance(met_via_raw, str) and met_via_raw else None
        sources_raw = row.get("digest_sources", "[]")
        digest_sources = json.loads(sources_raw) if isinstance(sources_raw, str) and sources_raw else (sources_raw or [])
        cadence = row.get("cadence_minutes")
        return cls(
            contact_id=row["contact_id"],
            display_name=row.get("display_name"),
            given_name=row.get("given_name"),
            family_name=row.get("family_name"),
            organization=row.get("organization"),
            relationship_score=float(row.get("relationship_score", 0.0)),
            trust_tier=row.get("trust_tier", "unknown"),
            tags=tags,
            privacy_level=row.get("privacy_level", "private"),
            person_node_id=row.get("person_node_id"),
            notes=row.get("notes"),
            import_source=row.get("import_source", "manual"),
            first_seen_at=row.get("first_seen_at", ""),
            last_interaction_at=row.get("last_interaction_at"),
            interaction_count=int(row.get("interaction_count", 0)),
            enrichment_source=enrichment_source,
            enrichment_last_at=row.get("enrichment_last_at"),
            deleted_at=row.get("deleted_at"),
            created_at=row.get("created_at", ""),
            updated_at=row.get("updated_at", ""),
            timezone=row.get("timezone"),
            introduced_by=row.get("introduced_by"),
            met_via=met_via,
            may_contact=row.get("may_contact") or "ask",
            cadence_minutes=int(cadence) if cadence is not None else None,
            digest=row.get("digest"),
            digest_sources=[str(item) for item in digest_sources],
        )


@dataclass
class ContactHandle:
    """A gateway-specific address for a contact."""
    handle_id: str
    contact_id: str
    gateway: str
    address: str
    is_primary: bool
    verified: bool
    confidence: float
    source: str
    created_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "handle_id": self.handle_id,
            "contact_id": self.contact_id,
            "gateway": self.gateway,
            "address": self.address,
            "is_primary": self.is_primary,
            "verified": self.verified,
            "confidence": self.confidence,
            "source": self.source,
            "created_at": self.created_at,
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "ContactHandle":
        return cls(
            handle_id=row["handle_id"],
            contact_id=row["contact_id"],
            gateway=row["gateway"],
            address=row["address"],
            is_primary=bool(row.get("is_primary", 0)),
            verified=bool(row.get("verified", 0)),
            confidence=float(row.get("confidence", 1.0)),
            source=row.get("source", "manual"),
            created_at=row.get("created_at", ""),
        )


@dataclass
class MergeProposal:
    """A pending or resolved merge proposal."""
    id: str
    contact_id_a: str
    contact_id_b: str
    confidence: float
    reason: str
    status: str  # pending, approved, rejected, auto_merged
    proposed_at: str
    resolved_at: Optional[str]

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "MergeProposal":
        return cls(
            id=row["id"],
            contact_id_a=row["contact_id_a"],
            contact_id_b=row["contact_id_b"],
            confidence=float(row["confidence"]),
            reason=row["reason"],
            status=row["status"],
            proposed_at=row["proposed_at"],
            resolved_at=row.get("resolved_at"),
        )


@dataclass
class MergeAuditRecord:
    """Immutable record of a completed merge operation."""
    audit_id: str
    canonical_id: str
    absorbed_id: str
    confidence: float
    merge_reason: str
    triggered_by: str
    contact_a_snapshot: Dict[str, Any]
    contact_b_snapshot: Dict[str, Any]
    merged_at: datetime = field(default_factory=lambda: now_utc())

    def __post_init__(self):
        if self.merged_at is None:
            self.merged_at = now_utc()


# ── Trust scopes (context-scoped trust: group chats, households, project rooms) ──
#
# A trust_scope grants its members a trust tier that applies ONLY inside the scope
# (e.g. a specific group conversation). Membership never confers global 1:1 rights —
# a member's contact.trust_tier / may_contact are independent. This is the
# generic primitive any agent uses to say "trusted in this room, not in my DMs".

SCOPE_TYPES = ("group", "household", "project", "event", "custom")
SCOPE_MEMBER_ROLES = ("owner", "member", "observer")


@dataclass
class TrustScope:
    """A context that grants its members a tier of trust scoped to itself."""
    scope_id: str
    scope_type: str                  # one of SCOPE_TYPES
    platform: Optional[str]          # e.g. "rcs"; NULL for abstract scopes
    external_id: Optional[str]       # platform conversation/group id (relay conv_id, etc.)
    label: Optional[str]             # human name (e.g. a group's display name)
    granted_tier: str                # tier members get WITHIN this scope (default group_guest)
    created_by: str                  # "owner" | "agent" | contact_id
    active: bool
    created_at: str
    updated_at: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scope_id": self.scope_id,
            "scope_type": self.scope_type,
            "platform": self.platform,
            "external_id": self.external_id,
            "label": self.label,
            "granted_tier": self.granted_tier,
            "created_by": self.created_by,
            "active": self.active,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "TrustScope":
        return cls(
            scope_id=row["scope_id"],
            scope_type=row["scope_type"],
            platform=row.get("platform"),
            external_id=row.get("external_id"),
            label=row.get("label"),
            granted_tier=row.get("granted_tier", "group_guest"),
            created_by=row.get("created_by", "agent"),
            active=bool(row.get("active", 1)),
            created_at=row.get("created_at", ""),
            updated_at=row.get("updated_at", ""),
        )


@dataclass
class ScopeMember:
    """A contact's membership in a trust scope (current while left_at is None)."""
    scope_id: str
    contact_id: str
    role: str                        # one of SCOPE_MEMBER_ROLES
    joined_at: str
    left_at: Optional[str]

    @classmethod
    def from_row(cls, row: Dict[str, Any]) -> "ScopeMember":
        return cls(
            scope_id=row["scope_id"],
            contact_id=row["contact_id"],
            role=row.get("role", "member"),
            joined_at=row.get("joined_at", ""),
            left_at=row.get("left_at"),
        )
