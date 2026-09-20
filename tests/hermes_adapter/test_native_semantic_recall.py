"""Shipped retrieval and native injection with controlled HTTP responses only."""
import asyncio
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import sys
import threading

import pytest

from protagine.qualification.native import configuration, native_context
from protagine.qualification.native_memory import MemoryRouter
from protagine.qualification.native_semantic_recall import CONSUMERS, EVALUATORS, assess
from protagine.qualification.records import read
from protagine.qualification.runner import evaluate
from protagine.qualification.semantic_recall_cases import cases, validate_retrieval
from protagine.router import LLMRouter
from test_model_qualification_native import configured, endpoint


def topics(text):
    groups = [('hydrofoil', 'vessel'), ('observatory',), ('ceramics',), ('tripod',),
              ('optics',), ('geology',), ('bicycle',), ('shelf',)]
    return {index for index, words in enumerate(groups) if any(word in text.casefold() for word in words)}


@contextmanager
def retrieval_endpoint():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            calls.append({'path': self.path, 'body': data})
            if self.path.endswith('/embeddings'):
                vectors = []
                for text in data['input']:
                    matched = topics(text)
                    vector = [1.0 if index in matched else 0.01 for index in range(8)]
                    vectors.append({'index': len(vectors), 'embedding': vector})
                response = {'model': data['model'], 'data': vectors}
            elif self.path.endswith('/rerank'):
                query_topics = topics(data['query'])
                response = {'results': [{'index': index,
                    'relevance_score': .99 if query_topics & topics(document) else .01}
                    for index, document in enumerate(data['documents'])]}
            else:
                self.send_error(404)
                return
            raw = json.dumps(response).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    snapshot = {'embedding': {'model': 'controlled-eight-dimensional', 'dimensions': 8,
                             'base_url': url, 'revision': 'controlled-v1'},
                'reranker': {'model': 'controlled-relevance', 'base_url': url,
                             'revision': 'controlled-v1', 'cutoff': .8, 'timeout_ms': 1200}}
    try:
        yield snapshot, calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_scoring_requires_retrieval_effects_not_only_a_correct_answer():
    with retrieval_endpoint() as (snapshot, calls):
        case = cases(snapshot)[0]
    assert not calls
    result = assess({'output': json.dumps(case.oracle['answer'])}, case.oracle)
    assert result['correct_final_answer']
    assert not result['semantic_pipeline_initialized']
    assert not result['canonical_collection_observed']
    assert not result['actual_selection_observed']
    assert not result['semantic_candidate.r10-vessel']
    assert result['source_visible.r10-vessel'] is None


def test_private_endpoint_configuration_is_explicit_and_case_hashes_are_stable():
    with retrieval_endpoint() as (snapshot, calls):
        first, second = cases(snapshot), cases(snapshot)
        assert [row.record()['sha256'] for row in first] == [row.record()['sha256'] for row in second]
        assert not calls
        altered = deepcopy(snapshot)
        altered['embedding']['base_url'] = 'http://secret@localhost:1234'
        with pytest.raises(ValueError, match='credential-free'):
            validate_retrieval(altered)


@pytest.mark.parametrize('index', range(6))
def test_real_semantic_pipeline_and_native_request(tmp_path, index):
    with retrieval_endpoint() as (snapshot, retrieval_calls):
        original = cases(snapshot)[index]
        run_controlled_case(tmp_path, original, retrieval_calls)


def run_controlled_case(tmp_path, original, retrieval_calls):
    native_requests = []

    def respond(data):
        if data['model'] == 'native-fixture':
            native_requests.append(deepcopy(data))
            return {'role': 'assistant', 'content': json.dumps(original.oracle['answer'])}
        supplied = json.loads(data['messages'][-1]['content'])
        if 'proposals' in supplied:
            value = {str(row['index']): {'keep': True, 'reason': 'Exact source-grounded fixture.'}
                     for row in supplied['proposals']}
        else:
            message = supplied['message']
            source = next(row for row in original.inputs['turns'] if row['text'] == message)
            code = re.search(r'[a-z]+-\d{3}', message)
            subject = 'survey tripod' if 'survey tripod' in message else source['id']
            scalar = (code[0] if code else 'cedar cabinet' if 'cedar cabinet' in message
                      else 'amber drawer' if 'amber drawer' in message else message)
            value = {'claims': [{'subject': subject, 'predicate': 'location' if 'tripod' in message else 'identifier',
                'value': scalar, 'evidence': message, 'operation': 'assert', 'prior_claim_id': None,
                'memory_kind': 'personal_context', 'recall_reason': 'Recall the supplied identifier when asked.',
                'valid_from_text': None, 'valid_to_text': None, 'event_at_text': None}]}
        return {'role': 'assistant', 'content': json.dumps(value)}

    case = replace(original, timeout_seconds=120,
                   inputs={**deepcopy(original.inputs), 'native_seconds': 90})
    with endpoint(respond=respond) as (url, _, _):
        selected, recipe = configuration(configured(tmp_path, url), 'fixture', hermes_python=sys.executable)
        support = LLMRouter(tiers={})
        support.configure({'provider': 'vllm', 'apiKey': 'controlled-only', 'models': {},
            'modelPool': {'writer': {'model': 'controlled-writer', 'baseUrl': url}},
            'functionRoles': {'extraction': ['writer'], 'judging': ['writer']}})
        directory = tmp_path / 'run'
        asyncio.run(evaluate(directory, recipe, [case], CONSUMERS, EVALUATORS,
            lambda _: MemoryRouter(support, native_context(selected, recipe)),
            evidence_mode='controlled', suite_version='controlled-semantic-recall-v1'))
        result = read(directory / 'attempts' / case.id / 'result.json')
        assert result['outcome'] == 'pass', result
        assert result['primary_outcome'] == 'pass'
        assert result['cleanup'] == 'state_directory_removed'
        assert len(native_requests) == 1
        assert any(row['path'].endswith('/embeddings') for row in retrieval_calls)
        wire = json.dumps(native_requests[0]['messages'])
        for identity in original.oracle['required_source_ids']:
            assert 'turn:' + identity in wire
        for term in original.oracle['forbidden_request_terms']:
            assert term not in wire


@pytest.mark.skipif(not os.environ.get('PROTAGINE_TEST_SEMANTIC_HELDOUT'), reason='Private held-out file must be selected explicitly')
def test_private_heldout_pipeline_oracles_without_candidate_inference(tmp_path):
    with retrieval_endpoint() as (snapshot, calls):
        hidden = cases(snapshot, os.environ['PROTAGINE_TEST_SEMANTIC_HELDOUT'])
        for index, original in enumerate(hidden):
            owned = tmp_path / str(index)
            owned.mkdir(mode=0o700)
            run_controlled_case(owned, original, calls)
