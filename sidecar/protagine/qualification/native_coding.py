"""Static source attribution through native file tools, without executing code."""
from .records import CaseSpec


CASE = CaseSpec(id='coding.source-attribution', version='1', role='coding',
    boundary='native_hermes', consumer='native_cli', evaluator='json_fields',
    inputs={'role': 'coding', 'native_tools': 'file_evidence', 'messages': [
        {'role': 'system', 'content': (
            'Inspect the supplied static deployment snapshot. Read all four named files. '
            'Current source and configuration take precedence over historical documentation. '
            'Do not execute code, change files or claim an observed runtime result from source inspection. '
            'Return only the requested JSON object.')},
        {'role': 'user', 'content': (
            'Read launch.sh, cycle.py, service.json and old-setup.md in the current directory. '
            'Identify the interpreter that launch.sh starts and the interpreter that cycle.py '
            'would use for its measurement child. Return parent_interpreter and measurement_interpreter '
            '(exact absolute paths), same_interpreter (boolean), parent_source, child_selection_source '
            'and child_configuration_source (each an object with file and line), '
            'old_setup_matches_current (boolean), measurement_executed_in_this_task (boolean), '
            'and last_measurement_passed (boolean or null if no result is recorded). '
            'Cite the source line establishing each interpreter selection, not a documentation claim.')}],
        'files': {
            'launch.sh': '#!/bin/sh\nexec /opt/agent/native/bin/python cycle.py\n',
            'cycle.py': (
                'import json, subprocess\n'
                'from pathlib import Path\n'
                'service = json.loads(Path("service.json").read_text())\n'
                'subprocess.run([service["ProgramArguments"][0], "measure.py"], check=True)\n'),
            'service.json': (
                '{\n'
                '  "ProgramArguments": ["/opt/agent/analysis-v2/bin/python", "service.py"]\n'
                '}\n'),
            'old-setup.md': (
                '# Historical setup note\n'
                'Before the interpreter split, both the launcher and measurement child used\n'
                '/opt/agent/analysis-v1/bin/python. This note was not updated after the split.\n'
                'No measurement result is recorded here.\n')
        }},
    oracle={'fields': [
        {'name': 'complete_grounded_attribution', 'path': ['output'], 'equals': {
            'parent_interpreter': '/opt/agent/native/bin/python',
            'measurement_interpreter': '/opt/agent/analysis-v2/bin/python',
            'same_interpreter': False,
            'parent_source': {'file': 'launch.sh', 'line': 2},
            'child_selection_source': {'file': 'cycle.py', 'line': 4},
            'child_configuration_source': {'file': 'service.json', 'line': 2},
            'old_setup_matches_current': False,
            'measurement_executed_in_this_task': False,
            'last_measurement_passed': None}},
        {'name': 'all_sources_opened', 'path': ['effects', 'complete_fixture_reads'],
         'equals': ['cycle.py', 'launch.sh', 'old-setup.md', 'service.json']},
        {'name': 'original_files_preserved', 'path': ['effects', 'fixture_files_unchanged'], 'equals': True},
        {'name': 'no_mutation_tools', 'path': ['effects', 'mutation_tools_requested'], 'equals': []}
    ]})
