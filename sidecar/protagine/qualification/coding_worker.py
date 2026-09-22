"""Real native Hermes editing tools, confined to an owned offline container."""
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from protagine.qualification.coding_sandbox import (
    close_environment, configure_environment, inspect_environment, seed, snapshot, terminal_configuration,
    without_host_mounts,
)
from protagine.qualification.native_worker import main


@contextmanager
def prepare(request, state, arguments, config):
    from run_agent import AIAgent
    from tools.terminal_tool import clear_task_env_overrides, ensure_task_env, register_task_env_overrides
    sandbox = request['inputs']['sandbox']
    # Runtime provider credentials were resolved into AIAgent arguments before this hook.
    # Keep no API credentials in the environment Docker might forward implicitly.
    keep = {'PATH', 'LANG', 'LC_ALL', 'SYSTEMROOT', 'HOME', 'HERMES_HOME',
            'HERMES_SKIP_DOTENV', 'PYTHONNOUSERSITE', 'PYTHONUNBUFFERED'}
    for key in list(os.environ):
        if key not in keep:
            os.environ.pop(key)
    configure_environment(sandbox)
    from hermes_cli.config import apply_terminal_config_to_env
    apply_terminal_config_to_env(config=config, override=True)
    task = 'coding-'+uuid.uuid4().hex
    arguments.update(enabled_toolsets=['file', 'terminal'], max_iterations=request['inputs']['max_iterations'],
                     session_id=task, skip_context_files=True, skip_memory=True)
    register_task_env_overrides(task, {'docker_image': sandbox['image'], 'cwd': '/workspace'})
    env = None
    identifier = None
    mounts = ExitStack()
    try:
        mounts.enter_context(without_host_mounts())
        env = ensure_task_env(task)
        if env is None:
            raise RuntimeError('Offline coding container unavailable')
        identifier = getattr(env, '_container_id', None)
        inspect_environment(env)
        seed(env, request['inputs']['repository'])
        captured = {}
        capture_errors = []
        original_cleanup = env.cleanup

        def cleanup(*args, **kwargs):
            # Hermes can release a terminal at conversation completion before
            # the result observer runs. Capture actual files before its release.
            if getattr(env, '_container_id', None) and not captured and not capture_errors:
                try:
                    captured.update(snapshot(env))
                except BaseException as exc:
                    capture_errors.append(type(exc).__name__)
            return original_cleanup(*args, **kwargs)

        mounts.enter_context(patch.object(env, 'cleanup', cleanup))
        original = AIAgent.run_conversation

        def conversation(agent, *args, **kwargs):
            # Bind the public task_id argument to the container seeded above.
            # This does not replace the native loop, executor or model transport.
            kwargs['task_id'] = task
            return original(agent, *args, **kwargs)

        def observe(_agent, response):
            if capture_errors:
                raise RuntimeError('Native repository capture failed')
            calls = [call.get('function', {}).get('name') for message in response.get('messages', [])
                     for call in message.get('tool_calls') or []]
            files = dict(captured) if captured else snapshot(env)
            return {'coding_files': files, 'native_tool_calls': calls,
                    'offline_container_verified': True, 'sandbox_image': sandbox['image']}

        with patch.object(AIAgent, 'run_conversation', conversation):
            yield observe
    finally:
        try:
            if env is not None and identifier:
                close_environment(env, identifier)
                (state/'coding-cleanup.json').write_text(json.dumps({'cleanup_verified': True}))
        finally:
            clear_task_env_overrides(task)
            mounts.close()


if __name__ == '__main__':
    state = Path(sys.argv[1]).parent
    request = json.loads(Path(sys.argv[1]).read_text())
    config = json.loads((state/'config.yaml').read_text())
    config['terminal'] = terminal_configuration(request['inputs']['sandbox'])
    (state/'config.yaml').write_text(json.dumps(config))
    raise SystemExit(main(prepare))
