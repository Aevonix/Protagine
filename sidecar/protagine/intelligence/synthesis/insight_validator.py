"""Insight Validator — ensure insights meet quality thresholds before surfacing.

Validates insights against configurable criteria:
- Minimum evidence count (don't surface guesses)
- Confidence threshold (minimum certainty)

Only insights passing all checks should be delivered to the user.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, List, Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@dataclass
class ValidationResult:
    """Result of insight validation.

    Attributes:
        valid: Whether the insight passed all checks
        reasons: List of failure reasons (empty if valid)
        evidence_count: Number of supporting evidence items
        confidence: The insight's confidence score
    """

    valid: bool
    reasons: List[str]
    evidence_count: int
    confidence: float


@runtime_checkable
class Validatable(Protocol):
    """Protocol for objects that can be validated as insights.

    Insight objects must expose confidence, supporting evidence,
    and optionally actionability flags.
    """

    @property
    def confidence(self) -> float: ...

    @property
    def supporting_evidence(self) -> List[str]: ...


class InsightValidator:
    """Validate insights before surfacing to the user.

    Applies configurable quality gates. An insight must pass all
    checks to be considered valid for delivery.

    Args:
        min_evidence: Minimum number of supporting evidence items
        min_confidence: Minimum confidence score (0-1)
    """

    def __init__(
        self,
        min_evidence: int = 2,
        min_confidence: float = 0.6,
    ) -> None:
        self.min_evidence = min_evidence
        self.min_confidence = min_confidence

    async def validate(self, insight: Validatable) -> ValidationResult:
        """Check if an insight meets all quality thresholds.

        Evaluates evidence count and confidence. Returns a ValidationResult
        with pass/fail and detailed reasons.

        Args:
            insight: The insight to validate

        Returns:
            ValidationResult indicating whether the insight is fit
            for delivery, with failure reasons if not.
        """
        reasons: List[str] = []

        # Evidence count
        evidence_count = len(insight.supporting_evidence)
        if evidence_count < self.min_evidence:
            reasons.append(
                f"Insufficient evidence ({evidence_count} < {self.min_evidence})"
            )

        # Confidence check
        confidence = insight.confidence
        if confidence < self.min_confidence:
            reasons.append(
                f"Low confidence ({confidence:.2f} < {self.min_confidence})"
            )

        return ValidationResult(
            valid=len(reasons) == 0,
            reasons=reasons,
            evidence_count=evidence_count,
            confidence=confidence,
        )

    async def validate_batch(
        self,
        insights: List[Validatable],
    ) -> List[tuple[Any, ValidationResult]]:
        """Validate multiple insights, returning paired results.

        Args:
            insights: List of insights to validate

        Returns:
            List of (insight, ValidationResult) tuples.
        """
        results = []
        for insight in insights:
            result = await self.validate(insight)
            results.append((insight, result))
        return results
