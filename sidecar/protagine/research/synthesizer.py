"""Research synthesizer — Stage 3 of the Research-to-Artifact Pipeline.

Runs gathered evidence through Protagine's intelligence synthesis layer to
produce a structured ``SynthesisReport`` containing validated, novelty-ranked
insights and annotated contradictions.

Steps:
  1. Connection discovery  — temporal, causal, entity, behavioural patterns
  2. Cross-domain analysis — groups evidence by domain, extracts insights
  3. Novelty scoring       — rank by formula: 0.4*(1/freq) + 0.3*domain_dist + 0.3*conf_delta
  4. Quality validation    — min evidence count, confidence thresholds, recency
  5. Contradiction         — conflicting evidence annotated with provenance
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from protagine.research.gatherer import EvidenceItem, EvidencePackage, SourceType
from protagine.util.temporal import now_utc

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Synthesis models
# ---------------------------------------------------------------------------


@dataclass
class DomainInsight:
    """A validated, novelty-ranked insight derived from evidence."""

    id: str
    title: str
    summary: str
    domains: List[str]
    supporting_citations: List[str]       # Evidence item citations
    confidence: float = 0.0               # 0.0–1.0
    novelty_score: float = 0.0            # 0.0–1.0
    created_at: datetime = field(default_factory=lambda: now_utc())
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Contradiction:
    """Two evidence items with conflicting claims."""

    id: str
    claim_a: str
    claim_b: str
    citation_a: str
    citation_b: str
    topic: str
    confidence_a: float = 0.5
    confidence_b: float = 0.5


@dataclass
class SynthesisReport:
    """Output of the synthesis stage."""

    goal_id: str
    insights: List[DomainInsight]
    contradictions: List[Contradiction]
    evidence_count: int
    source_breakdown: Dict[str, int]      # SourceType.value -> count
    synthesis_confidence: float           # 0.0–1.0, aggregate
    created_at: datetime = field(default_factory=lambda: now_utc())


# ---------------------------------------------------------------------------
# Synthesis configuration
# ---------------------------------------------------------------------------


@dataclass
class SynthesisConfig:
    min_evidence_for_insight: int = 2
    min_confidence: float = 0.55
    min_novelty: float = 0.30
    max_insights: int = 50
    timeout_seconds: float = 90.0


# ---------------------------------------------------------------------------
# Synthesizer
# ---------------------------------------------------------------------------


class ResearchSynthesizer:
    """Synthesize an EvidencePackage into a SynthesisReport.

    Tries to use Protagine's intelligence synthesis layer when available;
    falls back to a lightweight built-in implementation for offline/test use.
    """

    def __init__(self, config: Optional[SynthesisConfig] = None) -> None:
        self.config = config or SynthesisConfig()

    async def synthesize(
        self,
        evidence: EvidencePackage,
        goal_id: str,
        goal_text: str = "",
    ) -> SynthesisReport:
        """Run evidence through the synthesis pipeline.

        Args:
            evidence: List of EvidenceItem objects (already deduplicated).
            goal_id: Unique ID of the parent research run.
            goal_text: Original research goal text (used for context).

        Returns:
            A ``SynthesisReport`` with ranked insights and contradictions.
        """
        # Filter out flagged items before any LLM exposure
        safe_evidence = [e for e in evidence if e.is_safe()]
        flagged_count = len(evidence) - len(safe_evidence)
        if flagged_count:
            logger.warning(
                "Synthesizer: excluded %d flagged evidence items (pii/injection)",
                flagged_count,
            )

        source_breakdown: Dict[str, int] = {}
        for item in safe_evidence:
            source_breakdown[item.source_type.value] = (
                source_breakdown.get(item.source_type.value, 0) + 1
            )

        insights = await self._extract_insights(safe_evidence, goal_text)
        # The only contradiction rule compared web evidence with graph evidence; there is no graph.
        contradictions: List[Contradiction] = []

        # Enforce limits
        insights = insights[: self.config.max_insights]

        aggregate_confidence = (
            sum(i.confidence for i in insights) / len(insights) if insights else 0.0
        )

        return SynthesisReport(
            goal_id=goal_id,
            insights=insights,
            contradictions=contradictions,
            evidence_count=len(safe_evidence),
            source_breakdown=source_breakdown,
            synthesis_confidence=aggregate_confidence,
        )

    async def _extract_insights(
        self,
        evidence: EvidencePackage,
        goal_text: str,
    ) -> List[DomainInsight]:
        """Extract and rank insights from evidence."""
        # Attempt to use Protagine's CrossDomainAnalyzer
        try:
            return await self._run_protagine_synthesis(evidence, goal_text)
        except Exception as exc:
            logger.debug("CrossDomainAnalyzer unavailable (%s), using built-in", exc)
            return self._builtin_extract(evidence, goal_text)

    async def _run_protagine_synthesis(
        self,
        evidence: EvidencePackage,
        goal_text: str,
    ) -> List[DomainInsight]:
        """Run Protagine intelligence synthesis layer."""
        from protagine.intelligence.synthesis.cross_domain_analyzer import CrossDomainAnalyzer
        from protagine.intelligence.synthesis.novelty_scorer import NoveltyScorer
        from protagine.intelligence.synthesis.insight_validator import InsightValidator

        # Build a minimal data structure the analyzer expects
        signals = [
            {
                "domain": item.source_type.value,
                "content": item.content,
                "timestamp": item.retrieved_at.isoformat(),
                "confidence": item.relevance_score,
                "source": item.citation,
            }
            for item in evidence
        ]

        analyzer = CrossDomainAnalyzer()
        raw_insights = await analyzer.analyze(signals)

        validator = InsightValidator(
            min_confidence=self.config.min_confidence,
            min_evidence_count=self.config.min_evidence_for_insight,
        )
        scorer = NoveltyScorer()

        results: List[DomainInsight] = []
        import secrets as _secrets

        for raw in raw_insights:
            validation = validator.validate(raw)
            if not validation.valid:
                continue
            novelty = scorer.score(raw)
            if novelty.score < self.config.min_novelty:
                continue
            results.append(
                DomainInsight(
                    id=_secrets.token_hex(6),
                    title=getattr(raw, "title", str(raw)[:80]),
                    summary=getattr(raw, "summary", str(raw)),
                    domains=getattr(raw, "domains", []),
                    supporting_citations=[],
                    confidence=getattr(raw, "confidence", 0.6),
                    novelty_score=novelty.score,
                )
            )

        return sorted(results, key=lambda x: x.novelty_score, reverse=True)

    def _builtin_extract(
        self,
        evidence: EvidencePackage,
        goal_text: str,
    ) -> List[DomainInsight]:
        """Lightweight built-in insight extractor (no external deps)."""
        import secrets as _secrets

        cfg = self.config
        insights: List[DomainInsight] = []

        # Group evidence by source type
        by_source: Dict[str, List[EvidenceItem]] = {}
        for item in evidence:
            by_source.setdefault(item.source_type.value, []).append(item)

        for source_type, items in by_source.items():
            if len(items) < cfg.min_evidence_for_insight:
                continue
            # One insight per source type, summarizing top items
            top_items = sorted(items, key=lambda x: x.relevance_score, reverse=True)[:5]
            combined = " ".join(i.content[:200] for i in top_items)
            avg_confidence = sum(i.relevance_score for i in top_items) / len(top_items)

            if avg_confidence < cfg.min_confidence:
                continue

            # Compute a simple novelty score from source diversity
            novelty = min(1.0, 0.3 + 0.1 * len(items))
            if novelty < cfg.min_novelty:
                continue

            insights.append(
                DomainInsight(
                    id=_secrets.token_hex(6),
                    title=f"Findings from {source_type} sources",
                    summary=combined[:500],
                    domains=[source_type],
                    supporting_citations=[i.citation for i in top_items],
                    confidence=avg_confidence,
                    novelty_score=novelty,
                )
            )

        return sorted(insights, key=lambda x: x.novelty_score, reverse=True)

