"""Real native task submission, shared context and cross-session control."""
import asyncio
from contextlib import contextmanager, ExitStack
from contextvars import ContextVar
import json
from pathlib import Path
import sys
import threading
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from protagine.qualification.native_memory_worker import prepare as memory_prepare
from protagine.qualification.native_unified_host import host_state, native_gateway
from protagine.qualification.native_worker import main

ROLE = ContextVar('qualification_unified_role', default='foreground')


@contextmanager
def prepare(request, state, arguments, config):
    import httpx
    from run_agent import AIAgent
    from protagine.api.routers import host
    from protagine.turns import get_turn_idempotency_ledger
    inputs = request['inputs']
    release = threading.Event()
    waiting = threading.Event()
    captured = {'bootstrap': [], 'foreground': [], 'worker': []}
    bootstrap = None
    try:
        with ExitStack() as resources:
            observe_memory = resources.enter_context(memory_prepare(request, state, arguments, config,
                                                                     setup_host=host_state))
            ledger = get_turn_idempotency_ledger(state/'memory-state')
            ledger.record_source('fixture-commitment-source', contact_id=inputs['contact_id'],
                session_id='fixture-earlier-conversation', scope='person', derive_claims=False,
                messages=[{'role': 'user', 'content': inputs['commitment']}])
            commitment = host._commitment_store.create(inputs['contact_id'], inputs['commitment'],
                metadata={'source_turn_id': 'fixture-commitment-source'})
            # Read-only route preflight must pass before any candidate request.
            plugin = config['plugins']['protagine']
            with httpx.Client(base_url=plugin['url'], headers={'Authorization': 'Bearer '+plugin['api_key']}) as client:
                check = client.post(f"/v1/host/commitments/{commitment['id']}/work", json={
                    'operation': 'status', 'contact_id': inputs['contact_id'],
                    'session_id': 'fixture-preflight', 'task_id': 'fixture-preflight', 'turn_id': 'fixture-preflight'})
                if check.status_code != 200 or check.json().get('accepted') is not True:
                    raise RuntimeError('Native commitment coordination preflight failed')
            gateway = resources.enter_context(native_gateway(state))
            adapter = next(adapter for platform, adapter in gateway.adapters.items()
                           if platform.value == 'protagine_task')
            original_conversation = AIAgent.run_conversation
            original_send = httpx.Client.send

            def conversation(agent, *args, **kwargs):
                role = 'worker' if str(agent.platform) == 'protagine_task' else ROLE.get()
                token = ROLE.set(role)
                try:
                    return original_conversation(agent, *args, **kwargs)
                finally:
                    ROLE.reset(token)

            def send(client, outgoing, *args, **kwargs):
                role = ROLE.get()
                is_inference = outgoing.method == 'POST' and str(outgoing.url).startswith(
                    arguments['base_url'].rstrip('/')+'/')
                text = None
                if is_inference:
                    body = json.loads(outgoing.content)
                    if any(key in body for key in ('messages', 'input', 'system')):
                        text = json.dumps({key: body[key] for key in ('messages', 'input', 'system', 'instructions')
                                           if key in body}, ensure_ascii=False)
                        if role == 'worker' and not release.is_set():
                            waiting.set()
                            if not release.wait(180):
                                raise TimeoutError('Synthetic worker rendezvous expired')
                response = original_send(client, outgoing, *args, **kwargs)
                if text is not None:
                    captured[role].append(text)
                return response

            resources.enter_context(patch.object(AIAgent, 'run_conversation', conversation))
            resources.enter_context(patch.object(httpx.Client, 'send', send))
            bootstrap = AIAgent(**{**arguments, 'enabled_toolsets': ['protagine'],
                                   'max_iterations': 8, 'session_id': 'fixture-bootstrap'})
            token = ROLE.set('bootstrap')
            try:
                initial = bootstrap.run_conversation(inputs['bootstrap'],
                    system_message='Use actual task tools. Return after acceptance; do not poll or finish the work here.')
            finally:
                ROLE.reset(token)
                bootstrap.close()
            # A model that did not submit work has a failed behavioral case,
            # not an invented accepted task or an infrastructure success.
            before = adapter.handoffs.recent(contact_id=inputs['contact_id'])
            if before:
                waiting.wait(10)
            arguments.update(enabled_toolsets=['protagine'], max_iterations=8,
                             session_id='fixture-foreground')

            def evidence(agent, response):
                foreground_before_release = waiting.is_set() and not release.is_set()
                release.set()
                until = time.monotonic()+120
                after = adapter.handoffs.recent(contact_id=inputs['contact_id'])
                while len(after) == 1 and time.monotonic() < until:
                    row = after[0]
                    stopped = adapter.handoffs.stop_view(row)
                    if row.get('response') or (stopped and stopped['status'] == 'cancelled'):
                        break
                    time.sleep(.05)
                    after = adapter.handoffs.recent(contact_id=inputs['contact_id'])
                record = after[0] if len(after) == 1 else None
                stopped = adapter.handoffs.stop_view(record) if record else None
                duplicate_suppressed = None
                if inputs['scenario'] == 'stop' and record and stopped and stopped['status'] == 'cancelled':
                    count = len(captured['worker'])
                    replay = asyncio.run_coroutine_threadsafe(
                        adapter.dispatch_native_event({'handoff_id': record['id']}), gateway._gateway_loop)
                    replay.result(timeout=10)
                    duplicate_suppressed = len(captured['worker']) == count and not adapter.handoffs.get(record['id'])['response']
                result = None
                if record and record.get('response'):
                    try:
                        result = json.loads(record['response']['text'])
                    except (ValueError, KeyError):
                        pass
                def rows(values):
                    return [{'task_id': value['id'], 'native_session_id': value.get('native_session_id'),
                             'has_response': bool(value.get('response')), 'has_stop': bool(value.get('stop'))}
                            for value in values]
                from protagine.commitments.work import CommitmentWork
                claims = CommitmentWork(host._commitment_store).for_commitments(
                    [commitment['id']], contact_id=inputs['contact_id'])
                return {**observe_memory(agent, response), 'gateway_connected': True,
                    'distinct_sessions': agent.session_id != bootstrap.session_id and bool(record)
                        and record.get('native_session_id') not in {None, agent.session_id, bootstrap.session_id},
                    'bootstrap_completed': initial.get('completed') is True,
                    'durable_before': rows(before), 'durable_after': rows(after),
                    'foreground_before_release': foreground_before_release,
                    'bootstrap_requests': list(captured['bootstrap']),
                    'foreground_requests': list(captured['foreground']), 'worker_requests': list(captured['worker']),
                    'commitment_status': host._commitment_store.get(commitment['id'])['status'],
                    'commitment_id': commitment['id'],
                    'commitment_claim_recorded': bool(claims.get(commitment['id'])),
                    'updates': adapter.handoffs.updates(record['id']) if record else [],
                    'worker_result': result, 'stop_status': stopped['status'] if stopped else None,
                    'worker_response_absent': record is not None and record.get('response') is None,
                    'duplicate_suppressed': duplicate_suppressed,
                    'rendezvous': 'Controlled pause at the background provider boundary; not a capacity measurement'}

            try:
                yield evidence
            finally:
                release.set()
        (state/'unified-cleanup.json').write_text(json.dumps({'gateway_stopped': True}))
    finally:
        release.set()


if __name__ == '__main__':
    raise SystemExit(main(prepare))
