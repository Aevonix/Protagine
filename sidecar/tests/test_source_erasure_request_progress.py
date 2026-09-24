"""Canonical forgetting must leave unrelated HTTP requests responsive."""
import asyncio
import threading
import time

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from protagine.api.routers import host
from protagine.turns import TurnIdempotencyLedger


@pytest.mark.asyncio
async def test_http_validation_progresses_during_real_canonical_erasure(tmp_path, monkeypatch, record_property):
    ledger = TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
    ledger.record_source('diagnostic', contact_id='contact-fixture', session_id='fixture',
                         messages=[{'role': 'user', 'content': 'A disposable diagnostic fact.'}])
    monkeypatch.setattr('protagine.turns.get_turn_idempotency_ledger', lambda _: ledger)
    monkeypatch.setattr('protagine.vector.get_store', lambda: None)
    for name in ('_facts_store', '_affect_store', '_graph', '_world_store', '_comms_log'):
        monkeypatch.setattr(host, name, None)
    monkeypatch.setenv('PROTAGINE_STATE_DIR', str(tmp_path))
    entered, release = threading.Event(), threading.Event()
    read_rules = ledger._erasure_rules
    worker_threads = set()

    def held_read(conn, contact):
        worker_threads.add(threading.get_ident())
        if not entered.is_set():
            entered.set()
            assert release.wait(5), 'unrelated HTTP could not progress during canonical erasure'
        return read_rules(conn, contact)

    monkeypatch.setattr(ledger, '_erasure_rules', held_read)
    app = FastAPI()
    app.include_router(host.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://fixture') as client:
        deletion = asyncio.create_task(client.post('/v1/host/memory/sources/forget',
            json={'contact_id': 'contact-fixture', 'source_ids': ['diagnostic']}))
        try:
            assert await asyncio.to_thread(entered.wait, 2), deletion.result().text if deletion.done() else 'erase did not start'
            assert not deletion.done()
            started = time.perf_counter()
            # Real route validation, while the real ledger's database read is
            # held. No deletion result or HTTP handler is substituted.
            response = await asyncio.wait_for(client.post('/v1/host/memory/sources/forget', json={}), 1)
            record_property('unrelated_http_seconds', time.perf_counter() - started)
            assert response.status_code == 422
            assert not release.is_set() and not deletion.done()
        finally:
            release.set()
            result = await deletion
    assert result.status_code == 200 and result.json()['source_erased']
    assert threading.get_ident() not in worker_threads
    assert ledger.is_source_erased('diagnostic', 'contact-fixture')
    with ledger._connect() as conn:
        assert conn.execute("SELECT count(*) FROM turn_sources WHERE turn_id='diagnostic'").fetchone()[0] == 0
