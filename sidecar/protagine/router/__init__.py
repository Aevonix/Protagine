"""Protagine LLM Cost Router — intelligent model tier selection.

Route LLM requests to the cheapest capable model based on prompt complexity.
Expected cost savings: 30–40% vs always using the largest model.

Usage::

    from protagine.router import LLMRouter, ModelTier

    router = LLMRouter()
    response = await router.complete(messages, context={"task": "summarise"})
"""

from protagine.router.tiers import ModelTier, TierConfig, DEFAULT_TIERS
from protagine.router.complexity_scorer import ComplexityScorer, ComplexitySignals
from protagine.router.router import LLMRouter, LLMResponse
from protagine.router.self_learning import RouterSelfLearner
from protagine.router.fallback import FallbackHandler

__all__ = [
    "LLMRouter",
    "LLMResponse",
    "ModelTier",
    "TierConfig",
    "DEFAULT_TIERS",
    "ComplexityScorer",
    "ComplexitySignals",
    "RouterSelfLearner",
    "FallbackHandler",
]
