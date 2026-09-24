"""Durable owner corrections.

``FeedbackStore`` persists every correction the owner submits through
``POST /v1/host/learning/correction``; the selfhood benchmark reads them back
as evidence. The in-memory continuous learner that once consumed them is gone
(M8): nothing here adapts weights.
"""

from .feedback_store import FeedbackStore, UserCorrection

__all__ = [
    "FeedbackStore",
    "UserCorrection",
]
