"""The one ranker: score, the feedback multiplier and eligibility (architecture 3.2).

``score = salience x w_drive x feedback_mult x affect_mod x (1 - cost)``.
The multiplier comes from ``TypeFeedbackStore`` keyed by ``(type, drive)`` and,
for outreach, ``reach_out:<contact>``. Eligibility uses the same effective
score, so a lowered multiplier drops a candidate below the act threshold even
when nothing competes with it. The configured drive weight orders candidates
and is factored out of the threshold (``base``), so a low-weight drive
(curiosity at 0.5) can still act when nothing outranks it and a weight of 0
turns it off. Satiation is not factored out for the work the agent chooses
for itself (the recurring candidates keyed on a ``dedup_base``: research,
questions, investigations, backlog upkeep): there it lowers the effective
weight in the score and leaves the threshold alone, so a satisfied drive holds
new self-work until the satiety decays. An obligation, a notice or the step of
an adopted goal is owed whatever the drive's satiety; for those satiation only
orders. Affect arrives with its own milestone; until then ``affect_mod`` is 1.
A social check-in is the exception to the multiplier gating: ``evaluate_outreach``
already found it due (its backoff is the brake on silence), so its eligibility uses the
score without feedback and the multiplier only orders it (architecture 4.7 item 6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

DEFAULT_ACT_THRESHOLD = 0.6


@dataclass
class Candidate:
    """What a drive proposes before authority sees it."""

    type: str                 # template name, e.g. commitment_overdue
    drive: str                # duty | social | curiosity | mastery | upkeep
    kind: str                 # task | message | note
    title: str
    dedup_key: str
    dedup_base: Optional[str] = None   # the stable key under a period key: no second instance while one is active
    salience: float = 0.8
    cost: float = 0.1
    recipient: Optional[str] = None
    text: str = ""            # the message text or task body
    rationale: str = ""
    evidence: List[str] = field(default_factory=list)
    concern: str = ""
    invalidates_if: Optional[str] = None
    success_check: Optional[Dict[str, Any]] = None
    due_at: Any = None
    source_type: Optional[str] = None
    source_id: Optional[str] = None
    priority: float = 0.5
    topic: str = ""               # what an open-ended intention is about (research, investigations)
    open_ended: bool = False      # the model forms the intention (one tool-less call); False = template
    concern_kind: str = "obligation"
    parent_goal_id: Optional[str] = None
    goal: Optional[Dict[str, Any]] = None   # a goal proposal (description, success_check, horizon_days, ...)
    cost_tokens: int = 0          # the deliberation or composition call that formed it, if any
    cooldown_hours: Optional[float] = None  # a message's own per-contact cooldown (a check-in's backoff)
    purpose: Optional[str] = None           # check_in | follow_up:<id> | reply_wait:<id>: the composer's enum
    grant: Optional[str] = None             # "owner": a per-commitment owner grant for this recipient only

    def as_detail(self) -> Dict[str, Any]:
        """The candidate as a concern's stored detail (JSON); ``from_detail`` restores it."""
        detail = {}
        for key, value in vars(self).items():
            if value is None or value == "" or value == [] or value == {} or value is False:
                continue
            detail[key] = value.isoformat() if hasattr(value, "isoformat") else value
        return detail

    @classmethod
    def from_detail(cls, detail: Dict[str, Any]) -> "Candidate":
        names = {field_name for field_name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in (detail or {}).items() if key in names})

    def feedback_key(self) -> str:
        return f"{self.type}:{self.drive}"

    def multiplier_keys(self) -> List[str]:
        keys = [self.feedback_key()]
        if self.kind == "message" and self.recipient:
            keys.append(f"reach_out:{self.recipient}")
        return keys


def feedback_multiplier(feedback: Any, candidate: Candidate) -> float:
    """The clamped ``TypeFeedbackStore`` product over the candidate's keys."""
    if feedback is None:
        return 1.0
    value = 1.0
    for key in candidate.multiplier_keys():
        try:
            value *= float(feedback.multiplier(key))
        except Exception:
            continue
    return max(0.25, min(2.25, value))


def weight_of(candidate: Candidate, drives: Mapping[str, float] | None) -> float:
    return max(0.0, float((drives or {}).get(candidate.drive, 1.0)))


def score(candidate: Candidate, *, drives: Mapping[str, float] | None = None, feedback: Any = None,
          affect_mod: float = 1.0) -> float:
    weight = weight_of(candidate, drives)
    salience = max(0.0, min(1.0, float(candidate.salience)))
    cost = max(0.0, min(0.99, float(candidate.cost)))
    return salience * weight * feedback_multiplier(feedback, candidate) * float(affect_mod) * (1.0 - cost)


def threshold_for(candidate: Candidate, threshold: float, drives: Mapping[str, float] | None) -> float:
    """The act threshold scaled by the configured drive weight: the weight orders, it does not gate."""
    return float(threshold) * weight_of(candidate, drives)


def satiable(candidate: Candidate) -> bool:
    """Whether satiation may hold the candidate: the recurring work the agent chooses for itself."""
    return candidate.dedup_base is not None


def _floor(candidate: Candidate, drives: Mapping[str, float] | None,
           base: Mapping[str, float] | None) -> Mapping[str, float] | None:
    return base if base is not None and satiable(candidate) else drives


def rank(candidates: Iterable[Candidate], *, drives: Mapping[str, float] | None = None,
         feedback: Any = None, affect_mod: float = 1.0) -> List[Tuple[Candidate, float]]:
    scored = [(candidate, score(candidate, drives=drives, feedback=feedback, affect_mod=affect_mod))
              for candidate in candidates]
    scored.sort(key=lambda item: (-item[1], item[0].dedup_key))
    return scored


def _gated(candidate: Candidate, value: float, *, drives: Mapping[str, float] | None, affect_mod: float) -> float:
    """What eligibility compares with the threshold: the effective score, or for a social check-in
    the score without feedback (replies and silence order check-ins; they never switch one off)."""
    if candidate.drive == "social":
        return score(candidate, drives=drives, feedback=None, affect_mod=affect_mod)
    return value


def pick(candidates: Iterable[Candidate], *, threshold: float = DEFAULT_ACT_THRESHOLD,
         drives: Mapping[str, float] | None = None, base: Mapping[str, float] | None = None,
         feedback: Any = None, affect_mod: float = 1.0) -> Tuple[Optional[Candidate], float]:
    """The best eligible candidate and its effective score, or (None, best score).

    ``drives`` are the effective (satiated) weights in the score; ``base`` the
    configured weights the threshold of satiable work normalizes by (``drives``
    when omitted, and for every candidate satiation may not hold).
    """
    ranked = rank(candidates, drives=drives, feedback=feedback, affect_mod=affect_mod)
    if not ranked:
        return None, 0.0
    best, best_score = ranked[0]
    if (weight_of(best, drives) > 0 and _gated(best, best_score, drives=drives, affect_mod=affect_mod)
            >= threshold_for(best, threshold, _floor(best, drives, base))):
        return best, best_score
    return None, best_score


def eligible(candidates: Iterable[Candidate], *, threshold: float = DEFAULT_ACT_THRESHOLD,
             drives: Mapping[str, float] | None = None, base: Mapping[str, float] | None = None,
             feedback: Any = None, affect_mod: float = 1.0) -> List[Tuple[Candidate, float]]:
    """Every candidate at or above its threshold, best first (duty work is not exclusive)."""
    return [(candidate, value) for candidate, value in
            rank(candidates, drives=drives, feedback=feedback, affect_mod=affect_mod)
            if weight_of(candidate, drives) > 0
            and _gated(candidate, value, drives=drives, affect_mod=affect_mod)
            >= threshold_for(candidate, threshold, _floor(candidate, drives, base))]


__all__ = ["Candidate", "DEFAULT_ACT_THRESHOLD", "eligible", "feedback_multiplier", "pick", "rank", "satiable",
           "score", "threshold_for", "weight_of"]
