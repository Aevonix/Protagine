"""PDF readback uses actual normalized ownership and stored, fallible pages."""
import base64
import copy
import hashlib
import importlib
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from protagine.api.middleware import ApiKeyMiddleware
from protagine.api.schemas.host import MemoryReadRequest
from protagine.turns import TurnIdempotencyLedger
from protagine.turns.documents import disposition
from protagine.turns.idempotency import source_message_hash
from protagine.turns.source_read import read
from test_native_request_erasure import freshness_response
from onekey import KEY, _principal, _write_keyring
from test_source_documents import pdf_bytes
from test_turn_source_evidence import source_app


CAPTION = 'Retain this PDF reference for the workshop.'
PAGE_TEXT = 'Page one: isolate the pump. ' + 'Inspect the seal; ' * 600 + 'Then restore power.'
PDF = pdf_bytes((PAGE_TEXT, '', 'The original third page.'))
ASSET = hashlib.sha256(PDF).hexdigest()


def message():
    return {'role': 'user', 'content': [{'type': 'text', 'text': CAPTION},
        {'type': 'input_document', 'input_document': {
            'mime_type': 'application/pdf', 'data': base64.b64encode(PDF).decode()}}]}


def retained(ledger, *, identifier='document', contact='person', session='original', scope='person'):
    original = message()
    ledger.record_source(identifier, contact_id=contact, session_id=session,
                         messages=[original], scope=scope, derive_claims=False)
    with ledger._connect() as conn:
        canonical = json.loads(conn.execute('SELECT messages_json FROM turn_sources WHERE turn_id=?',
                                            (identifier,)).fetchone()[0])[0]
    assert canonical['content'][1] == {'type': 'document', 'asset_id': 'sha256:' + ASSET,
                                      'mime_type': 'application/pdf'}
    assert source_message_hash(session, canonical) == source_message_hash(session, original)
    return ledger.source_references([identifier], contact_id=contact, session_id=session)[0]


def derivative(ledger, *, status='partial', reason='no_extractable_text_on_some_pages', pages=None,
               page_count=3, parser_version='fixture-parser-a'):
    if pages is None:
        pages = [{'page': 1, 'text': PAGE_TEXT, 'status': 'text'},
                 {'page': 2, 'text': '', 'status': 'no_extractable_text'},
                 {'page': 3, 'text': 'The original third page.', 'status': 'text'}]
    result = disposition(status, reason, parser_version=parser_version, page_count=page_count, pages=pages)
    row_status = 'complete' if status in {'complete', 'partial'} else 'document_' + status
    with ledger._connect() as conn, conn:
        conn.execute('UPDATE source_media SET status=?,media_metadata_json=? WHERE asset_hash=?',
                     (row_status, json.dumps({'document': result}), ASSET))


@pytest.fixture
def original(tmp_path):
    ledger = TurnIdempotencyLedger(tmp_path/'turn-idempotency.db')
    ref = retained(ledger)
    derivative(ledger)
    return ledger, ref


def opened(original, **changes):
    ledger, ref = original
    return read(ledger, **({'contact_id': 'person', 'session_id': 'later', **ref,
                           'view': 'document', 'asset_hash': ASSET, 'page': 1} | changes))


def all_content(original, **changes):
    result = opened(original, **changes)
    parts = [result['content']]
    while not result['complete']:
        result = opened(original, **changes, offset=result['next_offset'], read_revision=result['read_revision'])
        assert len(result['content']) <= 4096
        parts.append(result['content'])
    return ''.join(parts), result


def test_page_chunks_keep_original_page_numbers_without_parsing_or_returning_original_bytes(original, monkeypatch):
    from protagine.turns.media import SourceMedia
    from protagine.turns import documents
    def forbidden(*args, **kwargs):
        raise AssertionError('readback must not invoke a parser or generic asset read')
    monkeypatch.setattr(SourceMedia, 'read', forbidden)
    monkeypatch.setattr(documents, 'extract_document', forbidden)
    first = opened(original)
    assert len(first['content']) == 4096 and not first['complete']
    assert first['offset_unit'] == 'characters' and first['document']['page_count'] == 3
    text, result = all_content(original)
    assert json.loads(text)['document']['text'] == PAGE_TEXT
    assert result['source_refs'] == [original[1]]
    blank = json.loads(opened(original, page=2)['content'])['document']
    assert blank['page'] == 2 and blank['page_status'] == 'no_extractable_text' and blank['text'] == ''
    third = json.loads(opened(original, page=3)['content'])['document']
    assert third['page'] == 3 and third['text'] == 'The original third page.'
    with pytest.raises(ValueError, match='page_unavailable'):
        opened(original, page=4)
    assert base64.b64encode(PDF).decode() not in text and 'data_url' not in text


@pytest.mark.parametrize('status,page_count', [('pending', None), ('unsupported', 3),
                                              ('unsupported', 0), ('failed', None)])
def test_unavailable_extraction_has_honest_disposition_and_no_stale_page_text(original, status, page_count):
    derivative(original[0], status=status, page_count=page_count, reason='fixture_' + status)
    result = opened(original)
    document = json.loads(result['content'])['document']
    assert document['status'] == status and document['reason'] == 'fixture_' + status
    assert document['text'] == '' and document['ocr_performed'] is False
    assert 'isolate the pump' not in result['content']


def test_image_only_pdf_page_reports_no_extractable_text(original):
    derivative(original[0], status='unsupported', reason='no_extractable_text')
    document = json.loads(opened(original, page=2)['content'])['document']
    assert document['page_status'] == 'no_extractable_text' and document['text'] == ''
    assert document['epistemic_state'] == 'derived_unverified'


def test_derivative_and_attributed_message_corrections_fence_every_continuation(original):
    ledger, ref = original
    first = opened(original)
    derivative(ledger, parser_version='fixture-parser-b')
    with pytest.raises(ValueError, match='restart_at_zero'):
        opened(original, offset=first['next_offset'], read_revision=first['read_revision'])
    first = opened(original)
    note = ledger.append_source_annotation(contact_id='person', session_id='later', annotation_id='correction',
        **ref, excerpt=CAPTION, correction='The workshop PDF is a draft; confirm the procedure before relying on it.',
        author_principal='operator')
    with pytest.raises(ValueError, match='restart_at_zero'):
        opened(original, offset=first['next_offset'], read_revision=first['read_revision'])
    text, corrected = all_content(original)
    assert 'attributed_correction' in text and 'PDF is a draft' in text
    assert note['source_id'] in {item['source_id'] for item in corrected['source_refs']}
    # The existing annotation API anchors canonical messages, not fallible
    # extracted page wording. A companion caption qualifies all selected pages.
    with pytest.raises(ValueError, match='excerpt_mismatch'):
        ledger.append_source_annotation(contact_id='person', session_id='later', annotation_id='page-wording',
            **ref, excerpt='Page one: isolate the pump.', correction='Unverified.', author_principal='operator')
    assert 'PDF is a draft' in opened(original, page=3)['content']
    ledger.erase_sources(contact_id='person', turn_ids=[note['source_id']])
    with pytest.raises(ValueError, match='unavailable'):
        opened(original)


def test_canonical_handle_cannot_borrow_another_owner_and_erase_respects_shared_bytes(original):
    ledger, ref = original
    foreign = retained(ledger, identifier='foreign', contact='other')
    private = retained(ledger, identifier='private', scope='session', session='private')
    ledger.record_source('unlinked', contact_id='person', session_id='original', messages=[{
        'role': 'user', 'content': [{'type': 'document', 'asset_id': 'sha256:' + ASSET,
                                    'mime_type': 'application/pdf'}]}], derive_claims=False)
    unlinked = ledger.source_references(['unlinked'], contact_id='person', session_id='later')[0]
    for changes in ({'contact_id': 'stranger'}, {'source_version': '0'*64},
                    {'asset_hash': '0'*64}, private, foreign, unlinked):
        with pytest.raises(ValueError, match='unavailable'):
            opened(original, **changes)
    assert opened(original, **private, session_id='private')['document']['page'] == 1
    ledger.erase_sources(contact_id='person', turn_ids=[ref['source_id']])
    with pytest.raises(ValueError, match='unavailable'):
        opened(original)
    assert opened(original, **foreign, contact_id='other')['document']['status'] == 'partial'


def test_derivative_change_racing_readback_is_withheld(original, monkeypatch):
    module = importlib.import_module('protagine.turns.source_read')
    previous = module._document_page
    changed = False
    def racing(*args, **kwargs):
        nonlocal changed
        result = previous(*args, **kwargs)
        if not changed:
            changed = True
            derivative(original[0], parser_version='changed-during-read')
        return result
    monkeypatch.setattr(module, '_document_page', racing)
    with pytest.raises(ValueError, match='changed_during_read'):
        opened(original)


@pytest.mark.parametrize('damage', ['missing', 'corrupt', 'oversized'])
def test_missing_or_corrupt_original_never_serves_normal_document_evidence(original, damage):
    from protagine.turns.documents import MAX_DOCUMENT_BYTES
    from protagine.turns.media import SourceMedia
    first = opened(original, page=3)
    path = SourceMedia(original[0]).store._original_path(ASSET, 'application/pdf')
    if damage == 'missing':
        path.unlink()
    else:
        path.write_bytes(b'x' * (MAX_DOCUMENT_BYTES + 1) if damage == 'oversized' else b'%PDF-corrupt')
    for revision in (None, first['read_revision']):
        with pytest.raises(ValueError, match='source_document_original'):
            opened(original, page=3, read_revision=revision)


