"""Native semantic recall scenarios; deployment endpoints and holdouts are private."""
from copy import deepcopy
from dataclasses import replace
import math
from urllib.parse import urlsplit

from .native_memory_cases import case, source
from .records import read

VERSION = 'native-semantic-recall-v1'
FAULTS = {'none', 'index_lag', 'embedding_unavailable', 'erase_after_index',
          'identity_mismatch', 'reranker_unavailable', 'cached_text_corruption'}

DEVELOPMENT = {'version': VERSION, 'split': 'development', 'cases': [
    {'id': 'R10.semantic-paraphrase',
     'turns': [source('r10-vessel', 'The hydrofoil registration mark is cedar-417.')],
     'question': 'What identifier belongs to the vessel? Return identifier.',
     'answer': {'identifier': 'cedar-417'}, 'required': ['r10-vessel'],
     'semantic_required': ['r10-vessel'], 'lexical_absent': ['r10-vessel']},
    {'id': 'R11.index-lag-lexical-fallback', 'fault': 'index_lag',
     'turns': [source('r11-code', 'The observatory entrance code is amber-628.')],
     'question': 'What is the observatory entrance code? Return code.',
     'answer': {'code': 'amber-628'}, 'required': ['r11-code'],
     'pending_minimum': 1, 'projected': 0},
    {'id': 'R12.embedding-failure-lexical-fallback', 'fault': 'embedding_unavailable',
     'turns': [source('r12-code', 'The ceramics locker code is violet-369.')],
     'question': 'What is the ceramics locker code? Return code.',
     'answer': {'code': 'violet-369'}, 'required': ['r12-code'], 'semantic_status': 'failed'},
    {'id': 'R13.contact-scoped-semantic-search',
     'turns': [source('r13-mine', 'My hydrofoil identifier is opal-571.'),
               source('r13-other', 'My vessel identifier is saffron-826.', contact='other-person')],
     'question': 'What is my vessel identifier? Return identifier.',
     'answer': {'identifier': 'opal-571'}, 'required': ['r13-mine'],
     'semantic_required': ['r13-mine'], 'forbidden': ['saffron-826', 'r13-other']},
    {'id': 'R14.stale-vector-after-forgetting', 'fault': 'erase_after_index',
     'turns': [source('r14-erased', 'My observatory entrance code is scarlet-734.')],
     'erase_after_index': ['r14-erased'],
     'question': 'What is my observatory entrance code? Return code, or null without evidence.',
     'answer': {'code': None}, 'forbidden': ['scarlet-734', 'r14-erased'],
     'stale_rows_minimum': 1},
    {'id': 'R15.semantic-ranking-with-distractors',
     'turns': [source('r15-answer', 'For the hydrofoil, use identifier quartz-682.'),
               source('r15-dock', 'The harbor dock number is 14.'),
               source('r15-bike', 'My bicycle identifier is moss-195.'),
               source('r15-shelf', 'The workshop shelf label is silver-743.')],
     'question': 'Which identifier belongs to the vessel? Return identifier.',
     'answer': {'identifier': 'quartz-682'}, 'required': ['r15-answer'],
     'semantic_required': ['r15-answer'], 'reranker_required': True},
]}


def validate_retrieval(config):
    if set(config) != {'embedding', 'reranker'}:
        raise ValueError('Declare embedding and reranker snapshots')
    for name in ('embedding', 'reranker'):
        row = config[name]
        url = urlsplit(row['base_url'])
        if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password:
            raise ValueError('Use an explicit credential-free retrieval URL')
        if not isinstance(row.get('model'), str) or not row['model']:
            raise ValueError('Declare the retrieval model')
    if type(config['embedding'].get('dimensions')) is not int or config['embedding']['dimensions'] < 1:
        raise ValueError('Declare positive embedding dimensions')
    cutoff = config['reranker']['cutoff']
    if not isinstance(cutoff, (int, float)) or not math.isfinite(cutoff):
        raise ValueError('Declare a finite reranking cutoff')


def cases(retrieval, pack=None):
    validate_retrieval(retrieval)
    pack = deepcopy(DEVELOPMENT if pack is None else read(pack) if not isinstance(pack, dict) else pack)
    if pack.get('version') != VERSION or pack.get('split') not in {'development', 'held_out'}:
        raise ValueError('Unknown semantic recall pack')
    rows = pack['cases']
    if not 1 <= len(rows) <= 11 or len({row['id'] for row in rows}) != len(rows):
        raise ValueError('Declare distinct bounded semantic recall cases')
    result = []
    for row in rows:
        fault = row.get('fault', 'none')
        if fault not in FAULTS:
            raise ValueError('Unknown declared retrieval fault')
        original = case(row['id'], row['turns'], row['question'], row['answer'],
                        required=row.get('required', []), forbidden=row.get('forbidden', []))
        result.append(replace(original, id=row['id'], version=VERSION,
            consumer='native_semantic_recall', evaluator='native_semantic_recall_outcomes',
            provenance='private' if pack['split'] == 'held_out' else 'public',
            inputs={**deepcopy(original.inputs), 'native_seconds': 180,
                'retrieval': deepcopy(retrieval), 'retrieval_fault': fault,
                'erase_after_index': row.get('erase_after_index', [])},
            oracle={**deepcopy(original.oracle), 'semantic_status': row.get('semantic_status', 'ready'),
                'semantic_required': row.get('semantic_required', []),
                'lexical_absent': row.get('lexical_absent', []),
                'selected_required': row.get('required', []),
                'source_scopes': {turn['id']: turn.get('scope', 'person') for turn in row['turns']},
                'projected': row.get('projected'), 'pending_minimum': row.get('pending_minimum', 0),
                'stale_rows_minimum': row.get('stale_rows_minimum', 0),
                'erase_after_index': row.get('erase_after_index', []), 'fault': fault,
                'reranker_required': row.get('reranker_required', bool(row.get('required'))),
                'require_conflict': row.get('require_conflict', False)}))
    return result
