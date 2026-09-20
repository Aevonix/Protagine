"""Base Hermes comparison: concurrent isolated CLI sessions without Protagine."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
from pathlib import Path
import sys
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from protagine.qualification.native_worker import main

IS_WORKER = ContextVar('qualification_base_worker', default=False)


@contextmanager
def prepare(request, state, arguments, config):
    import httpx
    from run_agent import AIAgent
    ready, release = threading.Event(), threading.Event()
    observed = {}
    original_send = httpx.Client.send
    worker = AIAgent(**{**arguments, 'session_id': 'base-worker', 'enabled_toolsets': [], 'max_iterations': 1})

    def send(client, outgoing, *args, **kwargs):
        if (IS_WORKER.get() and outgoing.method == 'POST'
                and str(outgoing.url).startswith(arguments['base_url'].rstrip('/')+'/')):
            body = json.loads(outgoing.content)
            if 'messages' in body or 'input' in body:
                ready.set()
                if not release.wait(180):
                    raise TimeoutError('Base worker rendezvous expired')
        return original_send(client, outgoing, *args, **kwargs)

    def run():
        token = IS_WORKER.set(True)
        try:
            observed['response'] = worker.run_conversation(request['inputs']['worker_instruction'])
        except BaseException as exc:
            observed['error'] = type(exc).__name__
        finally:
            worker.close()
            observed['closed'] = True
            IS_WORKER.reset(token)

    with patch.object(httpx.Client, 'send', send):
        thread = threading.Thread(target=run, name='qualification-base-worker', daemon=True)
        thread.start()
        arguments.update(session_id='base-foreground', enabled_toolsets=[])
        try:
            if not ready.wait(20):
                raise RuntimeError('Base native worker did not reach its request')
            def evidence(agent, response):
                concurrent = not release.is_set() and thread.is_alive()
                release.set(); thread.join(120)
                return {'consumer': 'base_hermes_independent_sessions',
                    'foreground_before_release': concurrent,
                    'distinct_sessions': agent.session_id != worker.session_id,
                    'worker_completed': observed.get('response', {}).get('completed') is True,
                    'worker_closed': observed.get('closed') is True,
                    'shared_task_context_available': False,
                    'native_task_control_available': False}
            yield evidence
        finally:
            release.set()
            if thread.is_alive():
                worker.hard_interrupt('Qualification cleanup')
                thread.join(25)
            if thread.is_alive():
                raise RuntimeError('Base native worker cleanup is unconfirmed')
    (state/'unified-cleanup.json').write_text(json.dumps({'gateway_stopped': True, 'base_worker_stopped': True}))


if __name__ == '__main__':
    raise SystemExit(main(prepare))
