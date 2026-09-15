"""Surprise Engine — anomaly detection when observations deviate from patterns."""

from protagine.surprise.store import SurpriseStore
from protagine.surprise.scorer import compute_surprise

__all__ = ["SurpriseStore", "compute_surprise"]
