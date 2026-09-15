"""Session isolation — per-contact and per-protagine isolated contexts."""

from protagine.sessions.isolated_session import (
    IsolatedSession,
    SessionState,
    ConversationTurn,
)
from protagine.sessions.store import IsolatedSessionStore, InMemorySessionStore
from protagine.sessions.context_loader import SessionContext, SessionContextLoader
from protagine.sessions.federation_session import FederationSession, FederationSessionState

__all__ = [
    "IsolatedSession",
    "SessionState",
    "ConversationTurn",
    "IsolatedSessionStore",
    "InMemorySessionStore",
    "SessionContext",
    "SessionContextLoader",
    "FederationSession",
    "FederationSessionState",
]
