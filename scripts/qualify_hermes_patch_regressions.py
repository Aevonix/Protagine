"""Run the shipped native patch witnesses with upstream's file isolation."""
from pathlib import Path
import argparse
import os
import subprocess
import sys
import tempfile

from protagine.hermes_patches import describe_patchset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root, output = args.runtime.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    paths = sorted({entry['path'] for patch in describe_patchset()['patches']
                    if patch['purpose'] == 'qualification' for entry in patch['files']
                    if entry['path'].startswith('tests/') and entry['after_sha256']})
    failed = []
    for index, path in enumerate(paths):
        # Hermes tests explicitly require a fresh process for each file.
        with tempfile.TemporaryDirectory(prefix='hermes-regression-') as home:
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(('HERMES_', 'PROTAGINE_', 'PYTHONPATH'))}
            env.update(HOME=home, HERMES_HOME=str(Path(home)/'.hermes'), PYTHON_DOTENV_DISABLED='1')
            result = subprocess.run([sys.executable, '-m', 'pytest', path, '-q', '-ra',
                                     f'--junitxml={output / (str(index) + ".xml")}'],
                                    cwd=root, env=env, timeout=300)
            if result.returncode:
                failed.append(path)
    if failed:
        raise SystemExit('Failed native patch regressions: ' + ', '.join(failed))


if __name__ == '__main__':
    main()
