"""A name locates admitted originals, independently of answer-passage scoring."""
import base64
import hashlib
import io
import json

from PIL import Image
import pytest

from pacomind.memory.recall import calibration_fingerprint
from pacomind.memory.search import collect_sources, select_memory
from pacomind.memory.selection import RecallSelector
from pacomind.turns import TurnIdempotencyLedger
from pacomind.turns.media import SourceMedia
from pacomind.turns.source_read import read


QUERY = 'On the reference cobalt-map-v2, what is the shape on the left?'
TEXT = 'Please retain this reference card. Reference: cobalt-map-v2.'


def original(color='red'):
    output = io.BytesIO()
    Image.new('RGB', (24, 16), color).save(output, format='PNG')
    return output.getvalue()


def retain(ledger, identifier='image-a', *, text=TEXT, data=None, contact='person', scope='person'):
    data = data or original()
    ledger.record_source(identifier, contact_id=contact, session_id='original', scope=scope,
        messages=[{'role': 'user', 'content': [{'type': 'text', 'text': text},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,' + base64.b64encode(data).decode()}}]}])
    return data


@pytest.fixture
def calibrated(monkeypatch):
    metadata = {'model': 'neutral-fixture', 'weights_revision': 'fixture'}
    monkeypatch.setenv('PACOMIND_RECALL_RERANK', 'on')
    monkeypatch.setenv('PACOMIND_RECALL_RERANK_MIN_SCORE', '0.8')
    monkeypatch.setenv('PACOMIND_RECALL_RERANK_CALIBRATION', calibration_fingerprint(metadata))
    return metadata


async def search(ledger, metadata, *, query=QUERY, contact='person', session='later', before_score=None, limit=5):
    async def score(query, documents, top_k):
        # The locator must not rewrite this answer-passage score or sneak into
        # its submitted documents. No caption or answering model is called.
        assert all('Named retained original.' not in doc for doc in documents)
        if before_score:
            before_score()
        return [{'index': index, 'score': 0.1} for index in range(len(documents))]
    collected = await collect_sources(ledger, query=query, contact_id=contact, session_id=session)
    return await select_memory(collected, query=query,
        selector=RecallSelector(score, calibration_metadata=lambda: metadata),
        timezone_name='UTC', limit=limit)


def locators(packet):
    return [row for row in packet.selected if row['kind'] == 'media_locator']


@pytest.mark.asyncio
async def test_named_pending_original_has_complete_reader_fields_and_actual_bytes(tmp_path, calibrated):
    ledger = TurnIdempotencyLedger(tmp_path/'source.db')
    data = retain(ledger)
    assert SourceMedia(ledger).status('person')[0]['status'] == 'pending'
    packet = await search(ledger, calibrated)
    assert len(packet.selected) == 1  # Low-scoring plain quotation stays omitted.
    locator = locators(packet)[0]
    assert locator['caption_status'] == 'pending'
    assert locator['matching_attachment_candidates'] == 1
    assert locator['matched_names'] == ['cobalt-map-v2']
    selector = locator['source_read']
    assert selector == {'source_id': 'image-a', 'source_version': packet.source_refs[0]['source_version'],
                        'view': 'image', 'asset_hash': hashlib.sha256(data).hexdigest()}
    # The rendered packet carries exactly the fields accepted by the existing
    # native tool, not just an unusable source URI or a filesystem path.
    rendered, _ = json.JSONDecoder().raw_decode(packet.content.splitlines()[-1][2:])
    assert rendered['source_read'] == selector
    opened = read(ledger, contact_id='person', session_id='later', **selector)
    assert base64.b64decode(opened['image']['data_url'].split(',', 1)[1]) == data
    assert opened['image_bytes_included'] is True
    assert 'does not establish what the image shows' in packet.content


@pytest.mark.asyncio
@pytest.mark.parametrize('query', ['What is on the reference card?', 'What is on cobalt-map-v20?', 'Open cobalt-map-v2-extra', 'Open cobalt-map-v2.png', 'Open /tmp/cobalt-map-v2', 'How is the weather?'])
async def test_keyword_or_similar_name_does_not_bypass_passage_abstention(tmp_path, calibrated, query):
    ledger = TurnIdempotencyLedger(tmp_path/'source.db')
    retain(ledger)
    assert not (await search(ledger, calibrated, query=query)).selected


@pytest.mark.asyncio
async def test_named_distinct_originals_preserve_ambiguity_under_shared_budget(tmp_path, calibrated, monkeypatch):
    ledger = TurnIdempotencyLedger(tmp_path/'source.db')
    retain(ledger, data=original('red'))
    retain(ledger, 'image-b', data=original('blue'))
    packet = await search(ledger, calibrated)
    assert {row['source_read']['source_id'] for row in locators(packet)} == {'image-a', 'image-b'}
    assert len({row['source_read']['asset_hash'] for row in locators(packet)}) == 2
    assert all(row['matching_attachment_candidates'] == 2 for row in locators(packet))
    limited = await search(ledger, calibrated, limit=1)
    assert len(limited.selected) == 1 and locators(limited)[0]['matching_attachment_candidates'] == 2
    monkeypatch.setenv('PACOMIND_RECALL_CONTEXT_MAX_CHARS', '400')
    small = await search(ledger, calibrated)
    assert len(small.content) <= 400 and not small.selected  # No clipped identity.


@pytest.mark.asyncio
async def test_text_path_or_filename_does_not_admit_an_attachment(tmp_path, calibrated):
    ledger = TurnIdempotencyLedger(tmp_path/'source.db')
    ledger.record_source('text-only', contact_id='person', session_id='original', messages=[{
        'role': 'user', 'content': TEXT + ' Open /tmp/cobalt-map-v2.png.'}])
    for query in (QUERY, 'Open cobalt-map-v2.png'):
        assert not (await search(ledger, calibrated, query=query)).selected
    assert SourceMedia(ledger).status('person') == []


@pytest.mark.asyncio
async def test_exact_names_stay_scoped_and_erasures_cannot_revive_locator(tmp_path, calibrated):
    ledger = TurnIdempotencyLedger(tmp_path/'source.db')
    retain(ledger, scope='session')
    assert not (await search(ledger, calibrated)).selected
    assert not (await search(ledger, calibrated, contact='other', session='original')).selected
    assert locators(await search(ledger, calibrated, session='original'))
    ledger.erase_sources(contact_id='person', turn_ids=['image-a'])
    assert not (await search(ledger, calibrated, session='original')).selected


@pytest.mark.asyncio
@pytest.mark.parametrize('change', ['erase', 'invalidate', 'annotate'])
async def test_current_source_change_during_reranking_omits_stale_locator(tmp_path, calibrated, change):
    ledger = TurnIdempotencyLedger(tmp_path/'source.db')
    retain(ledger)
    def mutate():
        if change == 'erase':
            ledger.erase_sources(contact_id='person', turn_ids=['image-a'])
        elif change == 'invalidate':
            with ledger._connect() as conn:
                conn.execute('INSERT INTO source_attribution_invalidations VALUES (?,?,?)',
                             ('image-a', 'parent', 'fixture'))
        else:
            ref = ledger.source_references(['image-a'], contact_id='person', session_id='later')[0]
            ledger.append_source_annotation(contact_id='person', session_id='later', annotation_id='note',
                **ref, excerpt='cobalt-map-v2', correction='That label refers to a different image.',
                author_principal='operator')
    assert not (await search(ledger, calibrated, before_score=mutate)).selected
    if change == 'annotate':
        # A later read can include the current correction, without silently
        # presenting the old supplied name as the only account.
        packet = await search(ledger, calibrated)
        assert locators(packet) and 'different image' in packet.content


@pytest.mark.asyncio
@pytest.mark.parametrize('text,query', [
    ('Here is cobalt-map.png for later.', 'What is on cobalt-map.png?'),
    ('Here is the board named "Cobalt spring board".', 'Open Cobalt spring board'),
    ('Reference: cobalt-map-v2.\\n', QUERY),
])
async def test_supplied_filename_quoted_name_and_literal_newline_are_exact_locators(tmp_path, calibrated, text, query):
    ledger = TurnIdempotencyLedger(tmp_path/'source.db')
    retain(ledger, text=text)
    assert locators(await search(ledger, calibrated, query=query))


@pytest.mark.asyncio
async def test_literal_path_is_not_a_name_even_beside_an_admitted_image(tmp_path, calibrated):
    ledger = TurnIdempotencyLedger(tmp_path/'source.db')
    retain(ledger, text='Reference: /tmp/cobalt-map.png')
    for query in ('Open cobalt-map.png', 'Open map.png'):
        assert not (await search(ledger, calibrated, query=query)).selected


@pytest.mark.asyncio
async def test_name_on_sibling_message_does_not_bind_its_image(tmp_path, calibrated):
    ledger = TurnIdempotencyLedger(tmp_path/'source.db')
    data = original()
    ledger.record_source('mixed-source', contact_id='person', session_id='original', messages=[
        {'role': 'user', 'content': TEXT},
        {'role': 'user', 'content': [{'type': 'image_url', 'image_url': {
            'url': 'data:image/png;base64,' + base64.b64encode(data).decode()}}]}])
    assert not (await search(ledger, calibrated)).selected


@pytest.mark.asyncio
async def test_quoted_speech_is_not_an_attachment_name(tmp_path, calibrated):
    ledger = TurnIdempotencyLedger(tmp_path/'source.db')
    retain(ledger, text='Retain this screenshot. He said "thanks" after the meeting.')
    assert not (await search(ledger, calibrated, query='Why does he keep saying thanks?')).selected
