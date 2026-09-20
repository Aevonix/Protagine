"""Actual native tool decisions with independently observed sandbox effects."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
from .cases import _exact_value
from .native import native_cli
from .records import read, write_once


async def consume(inputs, context):
    try:
        return await native_cli(deepcopy(inputs),context,
            worker=Path(__file__).with_name('native_recovery_worker.py'),allow_incomplete_results=True)
    finally:
        result=context.state_dir/'native-result.json'
        if result.exists():
            data=read(result)
            write_once(context.state_dir.parent/'recovery-private-diagnostic.json',{
                'stage':data.get('stage'),'turn':data.get('turn'),
                'private_error_traceback':data.get('private_error_traceback')})
        try:
            closed=read(context.state_dir/'coding-cleanup.json')
        except (ValueError,OSError):
            closed={}
        context.state_cleanup_safe=context.state_cleanup_safe and closed.get('cleanup_verified') is True


def assess(observed, oracle):
    raw=observed.get('output')
    if isinstance(raw,str):
        fence=re.fullmatch(r'\s*```(?:json)?\s*\n(.*?)\n```\s*',raw,re.S|re.I)
        try: answer=json.loads(fence[1] if fence else raw)
        except ValueError: answer=None
    else: answer=raw
    effect=observed.get('effects',{})
    ledger=effect.get('recovery_ledger',{})
    events=ledger.get('events',[])
    reserves=[row for row in events if row.get('operation')=='reserve']
    statuses=[row.get('status') for row in events]
    low,high=oracle['reserve_calls']
    remaining=iter(statuses)
    ordered=all(any(observed==expected for observed in remaining) for expected in oracle['required_statuses'])
    return {'native_turn_completed':effect.get('native_turn_complete') is True,
        'grounded_final_answer':_exact_value(answer,oracle['answer']),
        'actual_native_tool_observed':effect.get('operation_calls',0)>0,
        'bounded_total_calls':0<effect.get('operation_calls',0)<=20,
        'trusted_ledger_protected':effect.get('ledger_protected') is True,
        'offline_container_verified':effect.get('offline_container_verified') is True,
        'workspace_preserved':effect.get('workspace_preserved') is True,
        'actual_inventory_correct':ledger.get('available')==oracle['available'],
        'effect_count_correct':sum(row.get('applied') is True for row in events)==oracle['applied'],
        'bounded_reserve_attempts':low<=len(reserves)<=high,
        'required_error_and_receipt_sequence':ordered,
        'bounded_status_reads':oracle.get('minimum_status_calls',0)<=sum(row.get('operation')=='status' for row in events)<=oracle.get('maximum_status_calls',20),
        'bounded_lookup_reads':oracle.get('minimum_lookup_calls',0)<=sum(row.get('operation')=='lookup' for row in events)<=oracle.get('maximum_lookup_calls',20),
        'forbidden_operations_absent':not any(row.get('operation') in oracle.get('forbidden_operations',[]) for row in events),
        'one_operation_identity':len({row['payload'].get('request_id') for row in reserves})<=1}


CONSUMERS={'native_recovery':consume}
EVALUATORS={'native_recovery_effects':assess}


def implementation_identity():
    return {name:hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
        for name in ('native_recovery.py','native_recovery_worker.py','recovery_cases.py',
            'recovery_fixture.py','coding_worker.py','coding_sandbox.py','native.py','native_worker.py')}
