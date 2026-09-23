"""Truthful read-state contracts consumed by the Operator Deck."""

from __future__ import annotations


from fastapi import HTTPException, Request
import pytest

from protagine.api.routers import host
from onekey import legacy_authority


def _legacy_request() -> Request:
    request = Request({
        "type": "http", "method": "GET", "path": "/v1/host/goals",
        "query_string": b"", "headers": [], "scheme": "http",
        "server": ("test", 80), "client": ("test", 1), "root_path": "",
    })
    request.state.protagine_authority = legacy_authority()
    return request


class _FailingGoals:
    def list_goals(self, **_kwargs):
        raise RuntimeError("secret goals backend detail")


class _EmptyGoals:
    def list_goals(self, **_kwargs):
        return []


@pytest.mark.asyncio
async def test_goals_not_wired_is_not_misreported_as_valid_empty(monkeypatch):
    monkeypatch.setattr(host, "_goals_store", None)

    with pytest.raises(HTTPException) as captured:
        await host.list_goals()

    assert captured.value.status_code == 501
    assert captured.value.detail == host._NOT_WIRED


@pytest.mark.asyncio
async def test_goals_failure_is_fixed_non_success_and_valid_empty_is_preserved(
    monkeypatch,
):
    monkeypatch.setattr(host, "_goals_store", _FailingGoals())
    with pytest.raises(HTTPException) as captured:
        await host.list_goals()
    assert captured.value.status_code == 500
    assert captured.value.detail == {
        "error": {
            "code": "goals_unavailable",
            "message": "Goals backend unavailable",
        },
    }
    assert "secret" not in str(captured.value.detail).lower()

    monkeypatch.setattr(host, "_goals_store", _EmptyGoals())
    value = await host.list_goals()
    assert value.model_dump() == {"goals": []}


