"""Actual provider metadata, reordered arrival, restart and no inferred delivery."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import time

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from protagine.api.middleware import ApiKeyMiddleware
from protagine.api.routers import host, transport
from protagine.contacts.comms import CommsLog
from protagine.commitments.store import CommitmentStore
from protagine.initiatives.temporal_followup import TemporalFollowups
from protagine.turns import TurnIdempotencyLedger, canonical_turn_digest
from onekey import KEY, _principal, _write_keyring


