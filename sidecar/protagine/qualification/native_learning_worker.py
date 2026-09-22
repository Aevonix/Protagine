"""Own a bounded native training session and a fresh transfer session."""
import asyncio
from contextlib import ExitStack, closing
from copy import deepcopy
import json
from pathlib import Path
import signal
import sys
import threading
import time


def main():
    request = json.loads(Path(sys.argv[1]).read_text())
    state, inputs = Path(sys.argv[1]).parent, request['inputs']
    stop, owned = threading.Event(), {}
    result = {'stage': 'preparing', 'worker_stopped': False,
        'agent_construction_started': False, 'agent_close_returned': False,
        'hard_interrupt_requested': False}

    def interrupt(_signum, _frame):
        stop.set()
        if owned.get('agent') is not None:
            result['hard_interrupt_requested'] = True
            owned['agent'].hard_interrupt('Qualification elapsed deadline')

    signal.signal(signal.SIGTERM, interrupt)

    def run():
        resources, agents, closed = ExitStack(), [], set()
        try:
            from hermes_cli.config import load_config
            from hermes_cli.runtime_provider import resolve_runtime_provider
            from hermes_cli.plugins import get_plugin_manager
            from hermes_constants import resolve_reasoning_config
            from run_agent import AIAgent
            from hermes_state import SessionDB
            from protagine.qualification.native_memory_worker import prepare
            from protagine.qualification.runner import ObservedRouter
            from protagine.router import LLMRouter
            from protagine.turns import get_turn_idempotency_ledger
            from protagine.beliefs.source_projection import SourceClaimProjection
            reader_config = load_config()
            training_record = (json.loads((state/'training-config.json').read_text())
                if (state/'training-config.json').exists() else
                {'binding': request['binding'], 'config': reader_config})

            def prepare_phase(config, binding, *, transfer=False):
                config = deepcopy(config)
                (state/'config.yaml').write_text(json.dumps(config))
                model = config['model']['default']
                runtime = resolve_runtime_provider(requested=binding, target_model=model)
                kwargs = {key: runtime[key] for key in ('base_url', 'api_key', 'provider', 'api_mode',
                    'requested_provider', 'request_overrides', 'capabilities') if key in runtime}
                arguments = dict(model=model, **kwargs, platform='cli', max_iterations=1,
                    max_tokens=inputs['max_output_tokens'], enabled_toolsets=[], quiet_mode=True,
                    skip_context_files=True, skip_memory=False, skip_background_review=True,
                    reasoning_config=resolve_reasoning_config(config, model), fallback_model=None,
                    save_trajectories=False)
                lifetime = resources.enter_context(ExitStack())
                observe = lifetime.enter_context(prepare({**request, 'binding': binding}, state, arguments, config))
                if inputs['arm'] == 'base_hermes':
                    config['plugins'] = {'enabled': []}
                    config['memory'] = {'provider': 'builtin'}
                    (state/'config.yaml').write_text(json.dumps(config))
                    get_plugin_manager().discover_and_load(force=True)
                    arguments['skip_memory'] = True
                elif transfer and inputs['arm'] == 'recall_disabled':
                    arguments['skip_memory'] = True
                return arguments, observe, lifetime

            arguments, observe, training_lifetime = prepare_phase(training_record['config'], training_record['binding'])

            def construct():
                if stop.is_set():
                    raise InterruptedError('Qualification stopped between phases')
                result['agent_construction_started'] = True
                arguments['session_db'] = SessionDB(state / 'state.db')
                agent = AIAgent(**arguments)
                agents.append(agent)
                owned['agent'] = agent
                return agent

            phases, cursor = {}, 0
            system = '\n'.join(row['content'] for row in inputs['messages'][:-1])

            def phase(name, agent, question, history=None):
                nonlocal cursor
                if stop.is_set():
                    raise InterruptedError('Qualification stopped between phases')
                started = time.monotonic()
                response = agent.run_conversation(question, system_message=system,
                    conversation_history=history, task_id='learning-' + name)
                if response.get('completed') is not True or any(response.get(key)
                        for key in ('failed', 'interrupted', 'partial')):
                    raise RuntimeError('Native learning phase incomplete: ' + name)
                evidence = observe(agent, response)
                requests = evidence['request_observations']
                phases[name] = {'session_id': agent.session_id, 'output': response.get('final_response'),
                    'request_observations': requests[cursor:],
                    'elapsed_ms': round((time.monotonic() - started) * 1000, 3)}
                cursor = len(requests)
                return response

            training = construct()
            result.update(stage='running', model=training.model)
            first = phase('baseline', training, inputs['baseline_question'])
            feedbacks = inputs.get('feedback_messages') or [inputs['feedback']]
            history = first['messages']
            for index, feedback in enumerate(feedbacks):
                response = phase('feedback' if index == 0 else 'feedback-'+str(index+1), training, feedback, history)
                history = response['messages']
            training_routes = observe(training, response)['context_routes']
            training.close()
            closed.add(id(training))
            owned['agent'] = None
            training_lifetime.close()
            delay_started = time.monotonic()
            delay = inputs.get('delay_seconds', 0)
            if not isinstance(delay, (float, int)) or not 0 <= delay <= 30:
                raise ValueError('Invalid bounded transfer delay')
            if stop.wait(delay):
                raise InterruptedError('Qualification stopped between native hosts')
            delay_elapsed = time.monotonic() - delay_started
            ledger = get_turn_idempotency_ledger(state / 'memory-state')
            projection = SourceClaimProjection(ledger)
            supporting = []
            with closing(ledger._connect()) as db:
                sources = [dict(row) for row in db.execute('SELECT turn_id,session_id,messages_json FROM turn_sources')]
            feedback_ids = [row['turn_id'] for row in sources if any(message.get('role') == 'user'
                and message.get('content') in feedbacks for message in json.loads(row['messages_json']))]
            formation_started = time.monotonic()
            if inputs['arm'] != 'base_hermes':
                router = LLMRouter(tiers={})
                router.configure(json.loads((state / 'support-config.json').read_text()))
                observed_router = ObservedRouter(router, supporting, request['binding'], qualification_role='chat')

                async def form():
                    # One ordinary projection attempt per actually captured source.
                    # Failures remain visible; no adaptive fixture repair or retry.
                    for _ in sources:
                        if stop.is_set():
                            raise InterruptedError('Qualification stopped during projection')
                        if not await projection.process_one(observed_router):
                            break

                asyncio.run(form())
            formation_ms = round((time.monotonic() - formation_started) * 1000, 3)
            jobs = projection.status(inputs['contact_id'])
            with closing(ledger._connect()) as db:
                claims = [dict(json.loads(row['data_json']), source_id=row['turn_id'],
                    superseded_by=row['superseded_by'], retracted_by=row['retracted_by'])
                          for row in db.execute('SELECT * FROM source_claims')]
            arguments, observe, _ = prepare_phase(reader_config, request['binding'], transfer=True)
            cursor = 0
            transfer = construct()
            # Supporting requests can share the endpoint. They are separately
            # attributed and must not become reader-phase request observations.
            cursor = len(observe(transfer, {})['request_observations'])
            final = phase('transfer', transfer, inputs['messages'][-1]['content'])
            evidence = observe(transfer, final)
            result.update(stage='returned', model=transfer.model, output=final.get('final_response'),
                turn={key: final.get(key) for key in ('completed', 'failed', 'interrupted', 'partial')},
                tool_evidence={'consumer': 'native_feedback_procedure_transfer',
                    'arm': inputs['arm'], 'scenario': inputs['scenario'], 'phases': phases,
                    'feedback_source_ids': feedback_ids, 'claims': claims, 'projection_jobs': jobs,
                    'supporting_observations': supporting, 'training_closed': id(training) in closed,
                    'formation_elapsed_ms': formation_ms,
                    'training_host_closed_before_transfer': True,
                    'delay_requested_seconds': delay, 'delay_elapsed_seconds': delay_elapsed,
                    'context_routes': training_routes + evidence['context_routes'],
                    'embedding_and_reranking': 'not_exercised', 'transport_delivery': 'not_exercised'})
        except BaseException as exc:
            result.update(stage='interrupted' if stop.is_set() else 'error', error_type=type(exc).__name__)
            print('Native learning phase:', type(exc).__name__, str(exc)[:1000], flush=True)
        finally:
            for agent in agents:
                if id(agent) not in closed:
                    try:
                        agent.close()
                        closed.add(id(agent))
                    except BaseException as exc:
                        result['close_error_type'] = type(exc).__name__
            result['agent_close_returned'] = bool(agents) and len(closed) == len(agents)
            try:
                resources.close()
            except BaseException as exc:
                result.update(stage='error', fixture_close_error_type=type(exc).__name__)

    worker = threading.Thread(target=run, name='native-learning-qualification', daemon=True)
    worker.start()
    while worker.is_alive():
        worker.join(.05)
    result['worker_stopped'] = True
    with (state / 'native-result.json').open('x') as stream:
        json.dump(result, stream, allow_nan=False)
    return 0 if result['stage'] == 'returned' else 1


if __name__ == '__main__':
    raise SystemExit(main())
