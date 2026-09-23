"""An unavailable or bounded reader must preserve independently observed work."""
import json
import sqlite3
import asyncio
import threading

import pytest

from protagine.api.routers import executions
from protagine.turns import board_observations, hermes_work, local_work, reported_workers
from protagine.turns.executions import format_view, request_work_context


def base_view():
    return {'items':[{'execution_id':'real-turn','session_id':'voice-session',
        'parent_execution_id':'','platform':'voice','phase':'model','tool_name':'',
        'liveness':'unknown','observation_age_seconds':1}], 'total':1,'truncated':False,'complete':False}


def isolate(monkeypatch):
    empty = {'items':[], 'recent':[], 'available':False,'reason':'not_selected'}
    for module, name in ((hermes_work,'cron_view'), (local_work,'local_work_view'),
                         (reported_workers,'reported_worker_view')):
        monkeypatch.setattr(module, name, lambda **_: dict(empty))


@pytest.mark.asyncio
@pytest.mark.parametrize('failed', ['native_kanban','local_work'])
async def test_failed_reader_does_not_erase_successful_turn_start_context(monkeypatch, failed):
    isolate(monkeypatch)
    def broken(**_):
        raise RuntimeError('controlled unavailable reader')
    monkeypatch.setattr(board_observations,'kanban_view', lambda **_: {'items':[],'recent':[], 'available':False,'reason':'not_selected'})
    module, name = (board_observations,'kanban_view') if failed=='native_kanban' else (local_work,'local_work_view')
    monkeypatch.setattr(module,name,broken)
    view = await executions.with_queue_work(base_view(), owner=True)
    rendered = format_view(view)
    assert 'voice-session' in rendered
    assert 'unavailable' in rendered and view['work_sources'][failed]['status']=='unavailable'
    projected = request_work_context(view)
    assert 'real-turn' in projected['text']


@pytest.mark.parametrize('source', ['native_cron','native_kanban','local_work'])
@pytest.mark.parametrize('known_total', [True,False])
def test_recent_terminal_rows_are_not_described_as_zero_observed_records(source, known_total):
    recent={'id':'just-finished','status':'completed'}
    group={'items':[], 'recent':[recent], 'available':True,'total':0}
    if known_total:
        group.update(recent_total=5,recent_truncated=True)
    result=request_work_context({'items':[],source:group})
    count='5' if known_total else '1+'
    assert source+'=0 open, '+count+' recent records' in result['text']
    assert 'just-finished' in result['text']
