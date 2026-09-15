"""Admitted attachments survive text vision without becoming human assertions."""
import base64
import gc
from contextlib import closing
import copy
import hashlib
import importlib.util
import importlib
import json
import random
import io
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from protagine.api.schemas.host import TurnSyncRequest
from protagine.turns import TurnIdempotencyLedger
from protagine.turns.idempotency import source_message_hash, SourceErased
from protagine.turns.media import SourceMedia
from protagine.turns.source_read import read
from test_source_media import image_bytes, message
from test_turn_source_evidence import source_app
from test_hermes_turn_outbox import _load_client
from test_hermes_general_governance import runtime as plugin_runtime


@pytest.fixture
def transport(monkeypatch, tmp_path):
    path = Path(__file__).resolve().parents[2] / 'plugins/hermes-plugin/transport_media.py'
    spec = importlib.util.spec_from_file_location('transport_media_fixture', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    monkeypatch.setattr(module, '_home', lambda: tmp_path)
    monkeypatch.setattr(module, '_transport', lambda: ('whatsapp', 'fixture-sender', 'fixture-chat'))
    image = tmp_path / 'cache/images/original.png'
    image.parent.mkdir(parents=True); image.write_bytes(image_bytes())
    event = SimpleNamespace(text='Keep this reference drawing.', message_id='provider-image',
        media_urls=[str(image)], media_types=['image/png'],
        source=SimpleNamespace(platform=SimpleNamespace(value='whatsapp'), user_id='fixture-sender', chat_id='fixture-chat'))
    scope = SimpleNamespace(session_id='native-session', task_id='native-task', turn_id='native-turn',
        platform='whatsapp', sender_id='fixture-sender', contact_id='person', valid_participant=True)
    return SimpleNamespace(module=module, carrier=module.TransportMedia(), event=event, scope=scope, image=image)


def native_context(transport, mode='text'):
    content = ('Runtime vision says this is the original famous painting.\n' + transport.event.text
               if mode == 'text' else message()['content'])
    return {'user_message': content, 'conversation_history': [{'role': 'user', 'content': content,
             'platform_message_id': transport.event.message_id}]}


def capture(transport, mode='text'):
    transport.carrier.observe(event=transport.event)
    kwargs = native_context(transport, mode)
    transport.carrier.bind(transport.scope, kwargs)
    media = transport.carrier.for_turn(transport.scope)
    return {'identity': {'host_id': 'hermes'},
        'context': {'session_id': transport.scope.session_id, 'contact_id': 'person', 'turn_id': 'media-turn'},
        'sender': {'platform': 'whatsapp', 'user_id': 'fixture-sender'},
        'user_message': {'role': 'user', 'content': kwargs['user_message']},
        'assistant_message': {'role': 'assistant', 'content': 'The drawing is retained.'},
        'transport_media': media}


@pytest.mark.asyncio
@pytest.mark.parametrize('mode', ['text', 'native'])
async def test_ingress_native_canonical_read_and_forget(transport, source_app, tmp_path, mode):
    body = capture(transport, mode)
    selected = body['transport_media']['images'][0]
    data_url = (body['user_message']['content'][selected['native_block_index']]['image_url']['url']
                if 'native_block_index' in selected else selected['data_url'])
    assert base64.b64decode(data_url.split(',', 1)[1]) == image_bytes()
    native_hash = source_message_hash(transport.scope.session_id, body['user_message'])
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=source_app), base_url='http://fixture') as api:
        first = await api.put('/v2/host/turns/source-media/transport/media-turn', json=body)
        again = await api.put('/v2/host/turns/source-media/transport/media-turn', json=body)
    assert first.status_code == 201, first.text
    assert again.status_code == 200, again.text
    evidence = first.json()['transport_media']
    asset = hashlib.sha256(image_bytes()).hexdigest()
    assert evidence == {'processed': True, 'source_id': 'media-turn', 'provider_message_id': 'provider-image',
                       'all_originals_retained': True, 'attachments': [{'ordinal': 0, 'retained': True, 'asset_hash': asset}]}
    assert again.json()['transport_media'] == evidence
    ledger = TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
    with closing(ledger._connect()) as conn:
        retained = json.loads(conn.execute('SELECT messages_json FROM turn_sources WHERE turn_id=?', ('media-turn',)).fetchone()[0])
        link = conn.execute('SELECT message_hash FROM source_media_links').fetchone()[0]
    user = retained[0]
    assert source_message_hash(transport.scope.session_id, user) == link == native_hash
    assert user['content'][0] == {'type': 'text', 'text': transport.event.text}
    from protagine.turns.audio import claim_message
    assert claim_message(user)['content'] == transport.event.text
    assert user['_transport_provenance']['runtime_prepared_text_kind'] == 'derived_not_author_statement'
    assert ledger.search_sources('famous painting', contact_id='person', session_id='later') == []
    assert ledger.search_sources('reference drawing', contact_id='person', session_id='later')
    ref, = ledger.source_references(['media-turn'], contact_id='person', session_id='later')
    opened = read(ledger, contact_id='person', session_id='later', **ref, view='image', asset_hash=asset)
    assert base64.b64decode(opened['image']['data_url'].split(',', 1)[1]) == image_bytes()
    with pytest.raises(ValueError):
        read(ledger, contact_id='other-person', session_id='later', **ref, view='image', asset_hash=asset)
    note = ledger.append_source_annotation(contact_id='person', session_id='later', annotation_id='drawing-correction',
        **ref, excerpt=transport.event.text, correction='This is a synthetic drawing, not an artwork photograph.', author_principal='owner')
    corrected = read(ledger, contact_id='person', session_id='later', **ref, view='image', asset_hash=asset)
    assert 'synthetic drawing' in corrected['content']
    assert note['source_id'] in {r['source_id'] for r in corrected['source_refs']}
    ledger.erase_sources(contact_id='person', turn_ids=['media-turn'])
    rules = ledger.erasure_feed('person', 0)['events']
    assert any(native_hash in rule['message_hashes'] for rule in rules)
    with pytest.raises(ValueError):
        read(ledger, contact_id='person', session_id='later', **ref, view='image', asset_hash=asset)
    with closing(ledger._connect()) as conn:
        assert conn.execute('SELECT COUNT(*) FROM source_media_links').fetchone()[0] == 0
    with pytest.raises(SourceErased):
        ledger.record_source('late-native-copy', contact_id='person', session_id=transport.scope.session_id,
                             messages=[body['user_message']])


@pytest.mark.parametrize('mismatch', ['message', 'chat', 'sender', 'platform', 'child', 'unresolved'])
def test_wrong_native_identity_never_reads_files(transport, monkeypatch, mismatch):
    transport.carrier.observe(event=transport.event)
    kwargs = native_context(transport)
    if mismatch == 'message':
        kwargs['conversation_history'][0]['platform_message_id'] = 'other-message'
    elif mismatch in {'chat', 'sender', 'platform'}:
        values = ['whatsapp', 'fixture-sender', 'fixture-chat']
        values[{'platform': 0, 'sender': 1, 'chat': 2}[mismatch]] = 'other'
        monkeypatch.setattr(transport.module, '_transport', lambda: tuple(values))
    elif mismatch == 'child': kwargs['parent_session_id'] = 'parent'
    else: transport.scope.valid_participant = False
    def forbidden(*args): raise AssertionError('unmatched input read a file')
    monkeypatch.setattr(transport.module, '_read_image', forbidden)
    transport.carrier.bind(transport.scope, kwargs)
    assert transport.carrier.for_turn(transport.scope) is None


def test_text_paths_do_not_admit_attachments(transport, monkeypatch):
    transport.event.text = '[Image attached at: ' + str(transport.image) + ']'
    transport.event.media_urls = []
    def forbidden(*args): raise AssertionError('user text became a file locator')
    monkeypatch.setattr(transport.module, '_read_image', forbidden)
    body = capture(transport)
    assert body['transport_media'] is None


def test_file_replaced_after_dispatch_does_not_become_the_original(transport):
    transport.carrier.observe(event=transport.event)
    transport.image.write_bytes(image_bytes() + b'changed')
    transport.carrier.bind(transport.scope, native_context(transport))
    assert transport.carrier.for_turn(transport.scope)['images'] == [
        {'ordinal': 0, 'unavailable': 'original_unavailable'}]


def test_generic_cache_suffix_preserves_actual_original_codec(transport):
    renamed = transport.image.with_suffix('.jpg')
    transport.image.rename(renamed)
    transport.event.media_urls = [str(renamed)]
    body = capture(transport)
    assert body['transport_media']['images'][0]['data_url'].startswith('data:image/png;base64,')


@pytest.mark.asyncio
async def test_large_native_image_retains_once_within_existing_limit(transport, source_app):
    from PIL import Image
    output = io.BytesIO()
    Image.frombytes('RGB', (1024, 1080), random.Random(42).randbytes(1024 * 1080 * 3)).save(output, format='PNG')
    data = output.getvalue()
    assert 3 * 1024 * 1024 < len(data) < 4 * 1024 * 1024
    transport.image.write_bytes(data)
    native = message(data)['content']
    transport.carrier.observe(event=transport.event)
    transport.carrier.bind(transport.scope, {'user_message': native, 'conversation_history': [
        {'role': 'user', 'content': native, 'platform_message_id': transport.event.message_id}]})
    media = transport.carrier.for_turn(transport.scope)
    assert media['images'] == [{'ordinal': 0, 'native_block_index': 1}]
    body = {'identity': {'host_id': 'hermes'}, 'context': {'session_id': 'native-session', 'contact_id': 'person',
        'turn_id': 'large-image'}, 'sender': {'platform': 'whatsapp', 'user_id': 'fixture-sender'},
        'user_message': {'role': 'user', 'content': native}, 'transport_media': media}
    TurnSyncRequest.model_validate(body)
    client = _load_client()
    queued = client.TurnOutbox(transport.image.parents[2] / 'large-outbox.sqlite3').enqueue('large-image', {
        'session_id': 'native-session', 'contact_id': 'person', 'turn_id': 'large-image',
        'sender': body['sender'], 'user_message': native, 'transport_media': media}, capture_ordinary=True)
    assert queued['state'] == 'pending'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=source_app), base_url='http://fixture') as api:
        response = await api.put('/v2/host/turns/source-media/transport/large-image', json=body)
    assert response.status_code == 201, response.text
    assert response.json()['transport_media']['all_originals_retained'] is True
    assert response.json()['transport_media']['attachments'][0]['asset_hash'] == hashlib.sha256(data).hexdigest()


@pytest.mark.asyncio
async def test_full_four_mib_jpeg_crosses_carrier_schema_and_canonical_store(transport, source_app):
    from PIL import Image
    output = io.BytesIO()
    Image.new('RGB', (16, 16), 'blue').save(output, format='JPEG')
    original = output.getvalue()
    # Legal JPEG comment segments exercise the exact byte limit without a huge
    # decoded bitmap or non-image trailing bytes.
    comments = bytearray()
    remaining = 4 * 1024 * 1024 - len(original)
    while remaining:
        size = min(65537, remaining)
        if 0 < remaining - size < 4:
            size -= 4
        comments.extend(b'\xff\xfe' + (size - 2).to_bytes(2, 'big') + b'x' * (size - 4))
        remaining -= size
    data = original[:2] + comments + original[2:]
    assert len(data) == 4 * 1024 * 1024
    transport.image.write_bytes(data)
    transport.event.media_types = ['image/jpeg']
    body = capture(transport)
    assert len(body['transport_media']['images'][0]['data_url']) == 5592431
    TurnSyncRequest.model_validate(body)
    client = _load_client()
    queued = client.TurnOutbox(transport.image.parents[2] / 'jpeg-outbox.sqlite3').enqueue('media-turn', {
        'session_id': 'native-session', 'contact_id': 'person', 'turn_id': 'media-turn',
        'sender': body['sender'], 'user_message': body['user_message']['content'],
        'transport_media': body['transport_media']}, capture_ordinary=True)
    assert queued['state'] == 'pending'
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=source_app), base_url='http://fixture') as api:
        response = await api.put('/v2/host/turns/source-media/transport/media-turn', json=body)
    assert response.status_code == 201, response.text
    assert response.json()['transport_media']['attachments'] == [
        {'ordinal': 0, 'retained': True, 'asset_hash': hashlib.sha256(data).hexdigest()}]


def test_active_turns_survive_concurrency_and_long_work(transport, monkeypatch):
    now = [100.0]
    monkeypatch.setattr(transport.module.time, 'monotonic', lambda: now[0])
    scopes = []
    for index in range(17):
        scope = copy.copy(transport.scope)
        scope.turn_id = 'turn-' + str(index)
        transport.event.message_id = 'provider-' + str(index)
        transport.carrier.observe(event=transport.event)
        transport.carrier.bind(scope, native_context(transport))
        scopes.append(scope)
    now[0] += 7200
    for index, scope in enumerate(scopes):
        assert transport.carrier.for_turn(scope)['provider_message_id'] == 'provider-' + str(index)
    transport.carrier.finish(**vars(scopes[0]))
    assert transport.carrier.for_turn(scopes[0]) is None
    assert all(transport.carrier.for_turn(scope) for scope in scopes[1:])


def test_abandoned_native_owner_releases_only_its_carrier(transport, monkeypatch):
    class NativeTurn:
        closed = False
    owners = [NativeTurn()]
    monkeypatch.setattr(transport.module, '_native_turn', lambda scope: owners[0])
    capture(transport)
    assert transport.carrier.for_turn(transport.scope)
    owners.clear()
    gc.collect()
    assert transport.carrier.for_turn(transport.scope) is None


@pytest.mark.parametrize('selector', [0, 9])
def test_native_reference_requires_an_actual_inline_image(transport, selector):
    body = capture(transport, 'native')
    body['transport_media']['images'][0] = {'ordinal': 0, 'native_block_index': selector}
    with pytest.raises(ValueError):
        TurnSyncRequest.model_validate(body)


def test_mixed_transport_media_preserves_existing_path(transport, monkeypatch):
    transport.event.media_urls.append(str(transport.image.with_suffix('.wav')))
    transport.event.media_types.append('audio/wav')
    def forbidden(*args): raise AssertionError('mixed input entered image normalization')
    monkeypatch.setattr(transport.module, '_read_image', forbidden)
    assert capture(transport)['transport_media'] is None


@pytest.mark.parametrize('kind', ['input_audio', 'input_document', 'input_video', 'audio_transcript'])
def test_server_does_not_discard_native_non_image_blocks(transport, kind):
    body = capture(transport, 'native')
    body['user_message']['content'].append({'type': kind, 'fixture': 'retained through its existing media path'})
    with pytest.raises(ValueError, match='cannot replace mixed native media'):
        TurnSyncRequest.model_validate(body)


@pytest.mark.asyncio
async def test_missing_original_is_explicit_and_caption_not_promoted(transport, source_app):
    transport.image.unlink()
    body = capture(transport)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=source_app), base_url='http://fixture') as api:
        result = await api.put('/v2/host/turns/source-media/transport/media-turn', json=body)
    assert result.status_code == 201, result.text
    receipt = result.json()['transport_media']
    assert receipt['processed'] and not receipt['all_originals_retained']
    assert receipt['attachments'] == [{'ordinal': 0, 'retained': False, 'reason': 'original_unavailable'}]


def test_schema_rejects_caller_hash_paths_and_wrong_platform(transport):
    body = capture(transport)
    for field, value in [('native_hash', 'a' * 64), ('path', '/untrusted')]:
        tampered = copy.deepcopy(body); tampered['transport_media'][field] = value
        with pytest.raises(ValueError): TurnSyncRequest.model_validate(tampered)
    body['transport_media']['platform'] = 'email'
    with pytest.raises(ValueError): TurnSyncRequest.model_validate(body)


def test_outbox_erasure_keeps_media_only_with_its_original_user(transport):
    client = _load_client()
    body = capture(transport)
    payload = {'turn_id': 'media-turn', 'session_id': transport.scope.session_id, 'contact_id': 'person',
        'sender': body['sender'], 'user_message': body['user_message']['content'],
        'assistant_message': body['assistant_message']['content'], 'transport_media': body['transport_media']}
    for role in ['user', 'assistant']:
        rules = [{'turn_id': 'different', 'whole_source': False, 'session_id': transport.scope.session_id,
                  'message_hashes': [source_message_hash(transport.scope.session_id, body[role+'_message'])]}]
        survivor = client.redact_source_payload(payload, rules)
        assert ('transport_media' in survivor) == (role == 'assistant')


def test_registered_hooks_retain_matching_transport_input(plugin_runtime, transport, monkeypatch):
    module, context, client, _ = plugin_runtime
    helper = importlib.import_module(module.__package__ + '.transport_media')
    monkeypatch.setattr(helper, '_home', lambda: transport.image.parents[2])
    monkeypatch.setattr(helper, '_transport', lambda: ('sms', '+15550001', 'fixture-chat'))
    transport.event.source.platform.value = 'sms'
    transport.event.source.user_id = '+15550001'
    kwargs = native_context(transport) | {'session_id': 'hook-session', 'task_id': 'hook-task',
        'turn_id': 'hook-turn', 'platform': 'sms', 'sender_id': '+15550001'}
    context.hooks['pre_gateway_dispatch'](event=transport.event)
    context.hooks['pre_llm_call'](**kwargs)
    context.hooks['post_llm_call'](**kwargs, assistant_response='Retained.', model='fixture-text')
    assert len(client.turns) == 1
    written = client.turns[0]
    assert written['user_message'] == kwargs['user_message']
    assert written['transport_media']['caption'] == transport.event.text
    assert base64.b64decode(written['transport_media']['images'][0]['data_url'].split(',', 1)[1]) == image_bytes()
    assert written['summary'] == ''


@pytest.mark.parametrize('terminal', ['completed', 'failed', 'interrupted'])
def test_registered_handoff_and_terminal_release_exact_turn(plugin_runtime, transport, monkeypatch, terminal):
    module, context, client, _ = plugin_runtime
    helper = importlib.import_module(module.__package__ + '.transport_media')
    monkeypatch.setattr(helper, '_home', lambda: transport.image.parents[2])
    monkeypatch.setattr(helper, '_transport', lambda: ('sms', '+15550001', 'fixture-chat'))
    carrier = next(cell.cell_contents for cell in context.hooks['post_llm_call'].__closure__
                   if isinstance(cell.cell_contents, helper.TransportMedia))
    transport.event.source.platform.value = 'sms'
    transport.event.source.user_id = '+15550001'
    kwargs = native_context(transport) | {'session_id': 'hook-session', 'task_id': 'hook-task',
        'turn_id': 'hook-turn', 'platform': 'sms', 'sender_id': '+15550001'}
    context.hooks['pre_gateway_dispatch'](event=transport.event)
    context.hooks['pre_llm_call'](**kwargs)
    scope = SimpleNamespace(**{key: kwargs[key] for key in ('session_id', 'task_id', 'turn_id')})
    assert carrier.for_turn(scope)
    original = module.TurnOutbox.enqueue
    def unavailable(*args, **kwargs):
        raise OSError('fixture disk unavailable')
    monkeypatch.setattr(module.TurnOutbox, 'enqueue', unavailable)
    context.hooks['post_llm_call'](**kwargs, assistant_response='Retained.', model='fixture-text')
    assert carrier.for_turn(scope) and not client.turns
    context.hooks['on_session_end'](**(vars(scope) | {'turn_id': 'other-turn'}), outcome=terminal)
    assert carrier.for_turn(scope)
    if terminal == 'completed':
        monkeypatch.setattr(module.TurnOutbox, 'enqueue', original)
        context.hooks['post_llm_call'](**kwargs, assistant_response='Retained.', model='fixture-text')
        assert client.turns[0]['transport_media']['caption'] == transport.event.text
        assert carrier.for_turn(scope) is None
    context.hooks['on_session_end'](**vars(scope), outcome=terminal)
    assert carrier.for_turn(scope) is None


@pytest.mark.parametrize('server', ['current', 'old', 'missing'])
def test_client_requires_transport_receipt_without_lossy_fallback(transport, monkeypatch, server):
    module = _load_client()
    client = module.ProtagineClient('http://fixture')
    body = capture(transport)
    calls = []
    def put(path, **kwargs):
        calls.append((path, kwargs['json']))
        response = {'accepted': True, 'source_recorded': True}
        if server == 'current':
            response['transport_media'] = {'processed': True, 'source_id': 'media-turn',
                                           'provider_message_id': 'provider-image'}
        return httpx.Response(404 if server == 'missing' else 200, json=response,
                              request=httpx.Request('PUT', 'http://fixture'+path))
    monkeypatch.setattr(client, 'put', put)
    result = client.sync_turn(session_id='native-session', contact_id='person', turn_id='media-turn',
        sender=body['sender'], user_message=body['user_message']['content'], transport_media=body['transport_media'],
        require_source_receipt=True, timeout_seconds=1)
    assert result is (server == 'current')
    assert len(calls) == 1 and calls[0][0] == '/v2/host/turns/source-media/transport/media-turn'
    from protagine.api.authority import required_scope
    assert required_scope('PUT', calls[0][0]) == 'turns:write'
