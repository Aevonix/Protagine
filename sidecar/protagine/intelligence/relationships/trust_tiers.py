"""Protagine's trust tier system — Protagine's own trust levels for people it interacts with.

Trust tiers represent how much Protagine trusts each person based on Protagine's own
observations of interaction patterns over time. "Inner circle" means Protagine has
developed high trust through consistent, positive engagement — not simply that
the owner designated them as important.

The owner can set manual overrides to inform Protagine's initial assessment, but
tiers are ultimately Protagine's independent view of its social world.

Two gatekeeping systems
-----------------------
TIER_CAPABILITIES governs two distinct concerns that must not be conflated:

- ``protagine_*`` keys: what Protagine will do **autonomously** for this relationship
  (reach out proactively, share full context, propose tier changes).
- ``contact_*`` keys: what the contact can **request** from Protagine
  (reminders, task modifications).

Protagine's autonomous behaviors are gated by trust tiers. Contact-facing permissions
are gated by the permissions system (protagine.intelligence.relationships.permissions).
"""
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional


class TrustTier(str, Enum):
    """Protagine's trust levels for people it interacts with.

    Each tier reflects Protagine's accumulated assessment of a person based on
    interaction patterns observed across connected gateways. Tiers gate what
    Protagine is willing to do autonomously on behalf of or in relation to each person.
    """
    INNER_CIRCLE = "inner_circle"
    TRUSTED = "trusted"
    REGULAR = "regular"
    # group_guest: trust granted WITHIN a shared context (a group) only, never 1:1.
    # Sits below regular; gated as strictly as peripheral for disclosure.
    GROUP_GUEST = "group_guest"
    PERIPHERAL = "peripheral"
    SILENCED = "silenced"


TIER_CAPABILITIES = {
    TrustTier.INNER_CIRCLE: {
        # What Protagine does autonomously for this relationship
        "protagine_proactive_reach_out": True,
        "protagine_priority_notifications": True,
        "protagine_full_context_sharing": True,
        "protagine_proposes_tier_changes": True,

        # What the contact can request from Protagine
        "contact_can_request_reminders": True,
        "contact_can_modify_tasks": True,
    },
    TrustTier.TRUSTED: {
        "protagine_proactive_reach_out": False,   # Protagine waits; doesn't initiate
        "protagine_priority_notifications": True,
        "protagine_full_context_sharing": True,
        "protagine_proposes_tier_changes": True,

        "contact_can_request_reminders": True,
        "contact_can_modify_tasks": False,
    },
    TrustTier.REGULAR: {
        "protagine_proactive_reach_out": False,
        "protagine_priority_notifications": False,
        "protagine_full_context_sharing": False,
        "protagine_proposes_tier_changes": False,

        "contact_can_request_reminders": False,
        "contact_can_modify_tasks": False,
    },
    TrustTier.GROUP_GUEST: {
        # Converses only within the shared group; Protagine does nothing autonomously for
        # them and they cannot drive Protagine. Granted by group membership, never 1:1.
        "protagine_proactive_reach_out": False,
        "protagine_priority_notifications": False,
        "protagine_full_context_sharing": False,
        "protagine_proposes_tier_changes": False,

        "contact_can_request_reminders": False,
        "contact_can_modify_tasks": False,
    },
    TrustTier.PERIPHERAL: {
        "protagine_proactive_reach_out": False,
        "protagine_priority_notifications": False,
        "protagine_full_context_sharing": False,
        "protagine_proposes_tier_changes": False,

        "contact_can_request_reminders": False,
        "contact_can_modify_tasks": False,
    },
    TrustTier.SILENCED: {
        "protagine_proactive_reach_out": False,
        "protagine_priority_notifications": False,
        "protagine_full_context_sharing": False,
        "protagine_proposes_tier_changes": False,
        "protagine_excluded_from_digests": True,

        "contact_can_request_reminders": False,
        "contact_can_modify_tasks": False,
    },
}


@dataclass
class TierChangeProposalRecord:
    """A pending tier-change proposal awaiting owner approval."""
    person_id: str
    current_tier: TrustTier
    proposed_tier: TrustTier
    direction: str  # "upward" | "downward"
    evidence_score: float
    reasoning: str
    created_at: datetime = field(default_factory=datetime.now)
    approved: Optional[bool] = None


class TrustTierManager:
    """Manage Protagine's trust tiers and the capabilities they gate.

    Protagine independently maintains trust tiers for each person it interacts with.
    Manual overrides allow the owner to inform Protagine's initial assessment (e.g.
    elevating a new contact Protagine hasn't yet observed), but Protagine's evidence-based
    scoring is the default path for tier assignment.

    Protagine MAY propose tier changes upward or downward based on its own observations.
    Proposals require owner approval and are tracked via pending_proposals.
    """

    def __init__(self):
        self._manual_overrides: Dict[str, TrustTier] = {}
        self._pending_proposals: Dict[str, TierChangeProposalRecord] = {}

    def get_tier(self, score: float, person_id: Optional[str] = None) -> TrustTier:
        """Get tier from score, respecting manual overrides."""
        # Check for manual override
        if person_id and person_id in self._manual_overrides:
            return self._manual_overrides[person_id]

        # Score-based tier
        if score >= 80:
            return TrustTier.INNER_CIRCLE
        elif score >= 60:
            return TrustTier.TRUSTED
        elif score >= 30:
            return TrustTier.REGULAR
        else:
            return TrustTier.PERIPHERAL

    def set_manual_tier(self, person_id: str, tier: TrustTier) -> None:
        """Override Protagine's assessed tier for a person.

        Used when the owner wants to inform Protagine's trust level for someone
        before Protagine has built its own evidence base (e.g. a new contact).
        Protagine's score-based assessment will resume if the override is cleared.
        """
        self._manual_overrides[person_id] = tier

    def clear_manual_tier(self, person_id: str) -> None:
        """Remove manual override, restoring Protagine's evidence-based tier."""
        self._manual_overrides.pop(person_id, None)

    def has_manual_override(self, person_id: str) -> bool:
        """Return True if a manual override is set for this person."""
        return person_id in self._manual_overrides

    def get_capabilities(self, tier: TrustTier) -> Dict[str, bool]:
        """Get capabilities for a tier."""
        return TIER_CAPABILITIES.get(tier, TIER_CAPABILITIES[TrustTier.REGULAR])

    def can(self, tier: TrustTier, capability: str) -> bool:
        """Check if tier has a capability."""
        caps = self.get_capabilities(tier)
        return caps.get(capability, False)

    def persons_by_tier(
        self,
        persons: Dict[str, float]
    ) -> Dict[TrustTier, List[str]]:
        """Group persons by their tier."""
        result = {tier: [] for tier in TrustTier}
        for person_id, score in persons.items():
            tier = self.get_tier(score, person_id)
            result[tier].append(person_id)
        return result

    def tier_change_proposal(
        self,
        person_id: str,
        evidence_score: float,
        days_at_score: int = 0,
        interaction_count: int = 0,
    ) -> Optional[TierChangeProposalRecord]:
        """Generate a tier-change proposal based on Protagine's evidence.

        Protagine MAY propose tier changes upward or downward. Proposals are
        surfaced via the briefing system and require owner approval.

        Upward: score above threshold for 30+ days with ≥20 interactions.
        Downward: score below threshold for 21+ days; no manual override required.

        Protagine MUST NOT change tiers automatically — proposals only.
        """
        current_tier = self.get_tier(evidence_score, person_id)
        # Get what the tier would be without override
        score_tier = self._score_to_tier(evidence_score)

        if person_id in self._manual_overrides:
            override_tier = self._manual_overrides[person_id]
            tier_order = [
                TrustTier.PERIPHERAL,
                TrustTier.REGULAR,
                TrustTier.TRUSTED,
                TrustTier.INNER_CIRCLE,
            ]
            try:
                override_idx = tier_order.index(override_tier)
                score_idx = tier_order.index(score_tier)
            except ValueError:
                return None

            if score_idx == override_idx:
                return None

            direction = "upward" if score_idx > override_idx else "downward"

            if direction == "upward":
                if days_at_score < 30 or interaction_count < 20:
                    return None
                reasoning = (
                    f"Protagine has observed consistent engagement "
                    f"(score {evidence_score:.0f}, {interaction_count} interactions "
                    f"over {days_at_score} days). Consider promoting from "
                    f"{override_tier.value} to {score_tier.value}. Approve?"
                )
            else:
                if days_at_score < 21:
                    return None
                reasoning = (
                    f"Protagine observes reduced engagement "
                    f"(score {evidence_score:.0f} for {days_at_score} days). "
                    f"Score suggests {score_tier.value}; override holds at "
                    f"{override_tier.value}. Suggest clearing override?"
                )

            proposal = TierChangeProposalRecord(
                person_id=person_id,
                current_tier=override_tier,
                proposed_tier=score_tier,
                direction=direction,
                evidence_score=evidence_score,
                reasoning=reasoning,
            )
        else:
            # Natural score movement — no override in place
            # Only propose if we've been at a boundary score for a while
            return None

        self._pending_proposals[person_id] = proposal
        return proposal

    def approve_proposal(self, person_id: str) -> bool:
        """Apply a pending proposal. Returns True if proposal existed."""
        proposal = self._pending_proposals.get(person_id)
        if not proposal:
            return False
        proposal.approved = True
        # Apply the tier change
        self._manual_overrides[person_id] = proposal.proposed_tier
        del self._pending_proposals[person_id]
        return True

    def reject_proposal(self, person_id: str) -> bool:
        """Reject a pending proposal. Returns True if proposal existed."""
        proposal = self._pending_proposals.get(person_id)
        if not proposal:
            return False
        proposal.approved = False
        del self._pending_proposals[person_id]
        return True

    @property
    def pending_proposals(self) -> Dict[str, TierChangeProposalRecord]:
        """All pending tier-change proposals awaiting owner decision."""
        return dict(self._pending_proposals)

    def _score_to_tier(self, score: float) -> TrustTier:
        """Score to tier without checking overrides."""
        if score >= 80:
            return TrustTier.INNER_CIRCLE
        elif score >= 60:
            return TrustTier.TRUSTED
        elif score >= 30:
            return TrustTier.REGULAR
        else:
            return TrustTier.PERIPHERAL
