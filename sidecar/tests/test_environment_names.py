"""Environment names stay consistent between workflows, tests and readers."""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / '.github' / 'workflows'


def test_hermes_qualification_env_names_match_between_workflows_and_tests():
    workflows = ''.join(path.read_text() for path in sorted(WORKFLOWS.glob('*.yml')))
    adapter_tests = ''.join(path.read_text() for path in sorted((ROOT / 'tests' / 'hermes_adapter').rglob('*.py')))
    expected = {'PROTAGINE_HERMES_TEST_PYTHON', 'PROTAGINE_HERMES_TEST_SOURCE'}
    prefixed = re.compile(r'\b([A-Z0-9]+_HERMES_TEST_(?:PYTHON|SOURCE))\b')
    assert set(prefixed.findall(workflows)) == expected
    assert set(prefixed.findall(adapter_tests)) == expected


def test_no_tautological_environment_fallbacks():
    read = r"os\.environ(?:\.get\(|\[)\s*(['\"])(\w+)\1"
    tautology = re.compile(read + r"[^\n]*?os\.environ(?:\.get\(|\[)\s*(['\"])\2\3")
    offenders = []
    for folder in ('sidecar', 'plugins', 'hostworker', 'tests', 'benchmarks', 'scripts'):
        for path in sorted((ROOT / folder).rglob('*.py')):
            if '.venv' in path.parts:
                continue
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if tautology.search(line):
                    offenders.append(f'{path.relative_to(ROOT)}:{number}')
    assert offenders == []
