"""Execute candidate code only inside a fresh offline Hermes Docker container."""
import json
from pathlib import Path
import sys
import signal

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from protagine.qualification.coding_sandbox import execute_checks, seed, verification_environment


def main():
    request = json.load(sys.stdin)
    result = {'checks': {}, 'fresh_verification_container': False, 'cleanup_verified': False}
    def stop(_signal, _frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise InterruptedError('Verifier interrupted')
    signal.signal(signal.SIGTERM, stop)
    try:
        with verification_environment(request['sandbox']) as env:
            result['fresh_verification_container'] = True
            Path('owned-container.json').write_text(json.dumps({'container_id': env._container_id}))
            try:
                seed(env, request['files'])
                result['checks'] = execute_checks(env, request['checks'])
            except BaseException as exc:
                result['error'] = type(exc).__name__
        result['cleanup_verified'] = True
    except BaseException as exc:
        result['error'] = type(exc).__name__
    # Only this record after context exit can establish container cleanup.
    print(json.dumps(result))
    return 1 if result.get('error') else 0


if __name__ == '__main__':
    raise SystemExit(main())
