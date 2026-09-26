"""Channel registration API -- /v1/channels/ endpoints."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel

from protagine.channels.manifest import ChannelManifest
from protagine.channels.store import ChannelStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/channels", tags=["channels"])

_channel_store: Optional[ChannelStore] = None


def set_channel_store(store: ChannelStore) -> None:
    global _channel_store
    _channel_store = store


def get_channel_store() -> Optional[ChannelStore]:
    return _channel_store


def _require_store() -> ChannelStore:
    if _channel_store is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Channel store not initialized",
        )
    return _channel_store


# ── Request / Response models ────────────────────────────────────────────


class RegisterResponse(BaseModel):
    channel_key: str
    registered_at: str
    channel_token: str


class ChannelInfo(BaseModel):
    channel_key: str
    display_name: str
    gateway_family: str
    status: str
    registered_at: str
    last_seen_at: Optional[str] = None
    supports_media: bool = False
    supports_reactions: bool = False
    supports_voice: bool = False
    supports_rich_text: bool = False
    max_message_length: Optional[int] = None
    provides_channel_id: bool = False
    delivery_protocol: str = "hermes"
    delivery_aliases: list[str] = []
    home_chat_id: Optional[str] = None


# ── Endpoints ────────────────────────────────────────────────────────────


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register_channel(
    manifest: ChannelManifest,
    x_channel_token: Optional[str] = Header(None),
) -> RegisterResponse:
    store = _require_store()

    try:
        registered = store.register(manifest, channel_token=x_channel_token)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        )

    return RegisterResponse(
        channel_key=registered.channel_key,
        registered_at=registered.registered_at,
        channel_token=registered.channel_token,
    )


@router.get("", response_model=list[ChannelInfo])
async def list_channels() -> list[ChannelInfo]:
    store = _require_store()
    return [_to_info(ch) for ch in store.list_all()]


@router.get("/{channel_key}", response_model=ChannelInfo)
async def get_channel(channel_key: str) -> ChannelInfo:
    store = _require_store()
    ch = store.get(channel_key)
    if ch is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel '{channel_key}' not found",
        )
    return _to_info(ch)


@router.put("/{channel_key}", response_model=ChannelInfo)
async def update_channel(
    channel_key: str,
    manifest: ChannelManifest,
    x_channel_token: str = Header(...),
) -> ChannelInfo:
    store = _require_store()

    if manifest.channel_key != channel_key:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="channel_key in body must match URL path",
        )

    if not store.verify_token(channel_key, x_channel_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid channel token",
        )

    try:
        registered = store.register(manifest, channel_token=x_channel_token)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=str(exc)
        )

    return _to_info(registered)


@router.delete("/{channel_key}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(
    channel_key: str,
    x_channel_token: str = Header(...),
) -> None:
    store = _require_store()

    if not store.verify_token(channel_key, x_channel_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid channel token",
        )
    if not store.revoke(channel_key):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Channel '{channel_key}' not found",
        )


# ── Helpers ──────────────────────────────────────────────────────────────


def _to_info(ch) -> ChannelInfo:
    m = ch.manifest
    return ChannelInfo(
        channel_key=ch.channel_key,
        display_name=ch.display_name,
        gateway_family=ch.gateway_family,
        status=ch.status,
        registered_at=ch.registered_at,
        last_seen_at=ch.last_seen_at,
        supports_media=m.supports_media,
        supports_reactions=m.supports_reactions,
        supports_voice=m.supports_voice,
        supports_rich_text=m.supports_rich_text,
        max_message_length=m.max_message_length,
        provides_channel_id=m.provides_channel_id,
        delivery_protocol=m.delivery_protocol,
        delivery_aliases=m.delivery_aliases,
        home_chat_id=m.home_chat_id,
    )
