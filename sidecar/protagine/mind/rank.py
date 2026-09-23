"""The one ranker: score, the feedback multiplier and eligibility (architecture 3.2).

``score = salience x w_drive x feedback_mult x affect_mod x (1 - cost)``.
The multiplier comes from ``TypeFeedbackStore`` keyed by ``(type, drive)`` and,
for outreach, ``reach_out:<contact>``. Eligibility uses the same effective
score, so a lowered multiplier drops a candidate below the act threshold even
when nothing competes with it. Affect arrives with its own milestone; until
then ``affect_mod`` is 1.
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


def score(candidate: Candidate, *, drives: Mapping[str, float] | None = None, feedback: Any = None,
          affect_mod: float = 1.0) -> float:
    weight = float((drives or {}).get(candidate.drive, 1.0))
    salience = max(0.0, min(1.0, float(candidate.salience)))
    cost = max(0.0, min(0.99, float(candidate.cost)))
    return salience * weight * feedback_multiplier(feedback, candidate) * float(affect_mod) * (1.0 - cost)


def rank(candidates: Iterable[Candidate], *, drives: Mapping[str, float] | None = None,
         feedback: Any = None, affect_mod: float = 1.0) -> List[Tuple[Candidate, float]]:
    scored = [(candidate, score(candidate, drives=drives, feedback=feedback, affect_mod=affect_mod))
              for candidate in candidates]
    scored.sort(key=lambda item: (-item[1], item[0].dedup_key))
    return scored


def pick(candidates: Iterable[Candidate], *, threshold: float = DEFAULT_ACT_THRESHOLD,
         drives: Mapping[str, float] | None = None, feedback: Any = None,
         affect_mod: float = 1.0) -> Tuple[Optional[Candidate], float]:
    """The best eligible candidate and its effective score, or (None, best score)."""
    ranked = rank(candidates, drives=drives, feedback=feedback, affect_mod=affect_mod)
    if not ranked:
        return None, 0.0
    best, best_score = ranked[0]
    if best_score >= threshold:
        return best, best_score
    return None, best_score


def eligible(candidates: Iterable[Candidate], *, threshold: float = DEFAULT_ACT_THRESHOLD,
             drives: Mapping[str, float] | None = None, feedback: Any = None,
             affect_mod: float = 1.0) -> List[Tuple[Candidate, float]]:
    """Every candidate at or above the threshold, best first (duty work is not exclusive)."""
    return [(candidate, value) for candidate, value in
            rank(candidates, drives=drives, feedback=feedback, affect_mod=affect_mod) if value >= threshold]


__all__ = ["Candidate", "DEFAULT_ACT_THRESHOLD", "eligible", "feedback_multiplier", "pick", "rank", "score"]
