"""Build isolated adapter artifacts for native Hermes contract tests."""

from pathlib import Path
import os
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]


def run_python(*args, cwd, env=None):
    result = subprocess.run(
        [sys.executable, *map(str, args)], cwd=cwd, env=env,
        text=True, capture_output=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result


@pytest.fixture(scope="session")
def artifacts(tmp_path_factory):
    output = tmp_path_factory.mktemp("adapter-artifacts")
    # Release CI qualifies the same bytes that it will publish. Local tests
    # still build a wheel from the sdist when no prepared artifacts are supplied.
    prepared = os.environ.get("PROTAGINE_DISTRIBUTIONS_DIR")
    distributions = Path(prepared).resolve() if prepared else output
    if not prepared:
        run_python("-m", "build", "--no-isolation", "--outdir", output, cwd=ROOT)
    wheel, = distributions.glob("protagine_hermes-*.whl")
    source, = distributions.glob("protagine_hermes-*.tar.gz")
    installed = output / "installed"
    run_python(
        "-m", "pip", "install", "--no-deps", "--no-index", "--target",
        installed, wheel, cwd=output,
    )
    return output, wheel, source, installed
