"""Exact source replay filtering, with real durable erasure rules."""
import copy
import importlib
import json
from types import SimpleNamespace

import httpx
import pytest

from test_turn_source_evidence import source_app
from protagine.turns import TurnIdempotencyLedger


def packet(contact, watermark, text):
    stamp = json.dumps({'contact_id': contact, 'watermark': watermark})
    return '<memory-context>\n[protagine-recall-v1 ' + stamp + ']\n' + text + '\n[/protagine-recall-v1]\n</memory-context>'


def freshness_response(ledger, path, body):
    """SQLite-backed transport stand-in; API qualification lives in identity continuity."""
    assert path == '/v1/host/memory/sources/erasures'
    page = ledger.erasure_feed(body['contact_id'], body['after'])
    current = ledger.source_references([ref['source_id'] for ref in body['source_refs']],
                                      contact_id=body['contact_id'], session_id=body['session_id'])
    page['sources_current'] = ({(ref['source_id'], ref['source_version']) for ref in current}
                              == {(ref['source_id'], ref['source_version']) for ref in body['source_refs']})
    return httpx.Response(200, json=page, request=httpx.Request('POST', 'http://fixture' + path))


@pytest.mark.asyncio
async def test_context_stamps_before_a_concurrent_forget(source_app, tmp_path, monkeypatch):
    from protagine.api.routers import host
    ledger = TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
    ledger.record_source('stamp-source', contact_id='contact-a', session_id='original',
                         messages=[{'role': 'user', 'content': 'Neutral source'}], derive_claims=False)
    original = host._build_temporal_section
    async def racing(*args, **kwargs):
        ledger.erase_sources(contact_id='contact-a', turn_ids=['stamp-source'])
        return await original(*args, **kwargs)
    monkeypatch.setattr(host, '_build_temporal_section', racing)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=source_app), base_url='http://fixture') as client:
        response = await client.post('/v1/host/context/assemble', json={
            'identity': {'host_id': 'fixture'}, 'context': {'contact_id': 'contact-a', 'session_id': 'resume'},
            'incoming_message': {'role': 'user', 'content': 'Neutral'}})
    assert response.status_code == 200
    assert response.json()['source_erasure_watermark'] == 0
    assert ledger.erasure_watermark('contact-a') == 1
