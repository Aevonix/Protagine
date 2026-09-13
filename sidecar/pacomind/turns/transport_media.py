"""Normalize transport originals while preserving native correction identity."""
import json
from contextlib import closing

from .idempotency import source_message_hash


def apply(messages, media, *, session_id):
    result = []
    for message in messages:
        if message['role'] != 'user':
            result.append(message)
            continue
        blocks = [{'type': 'text', 'text': media.caption}] if media.caption else []
        attachments = []
        for image in media.images:
            attachments.append({'ordinal': image.ordinal, 'block_index': len(blocks)})
            blocks.append({'type': 'image_url', 'image_url': {'url': image.data_url}}
                          if image.data_url else {'type': 'image_unretained', 'reason': image.unavailable})
        native = message['content']
        # The old prepared text is retained as runtime interpretation, never
        # indexed/extracted as the person's words. No caller-supplied hash wins.
        prepared = (native if isinstance(native, str) else '\n'.join(
            b['text'] for b in native if isinstance(b, dict) and b.get('type') in {'text', 'input_text'}
            and isinstance(b.get('text'), str)))
        result.append({**message, 'content': blocks,
            '_source_message_hash': source_message_hash(session_id, message),
            '_transport_provenance': {'platform': media.platform, 'provider_message_id': media.provider_message_id,
                'caption_origin': 'transport', 'attachments': attachments,
                'runtime_prepared_text': prepared, 'runtime_prepared_text_kind': 'derived_not_author_statement'}})
    return result


def receipt(ledger, source_id):
    with closing(ledger._connect()) as conn:
        row = conn.execute('SELECT messages_json FROM turn_sources WHERE turn_id=?', (source_id,)).fetchone()
        if row is None:
            return {'processed': False, 'source_id': source_id, 'attachments': []}
        attachments, provider_id = [], None
        for message in json.loads(row[0]):
            provenance = message.get('_transport_provenance')
            if not provenance:
                continue
            provider_id = provenance['provider_message_id']
            for item in provenance['attachments']:
                block = message['content'][item['block_index']]
                retained = block.get('type') == 'image' and conn.execute('''SELECT 1 FROM source_media_links
                    WHERE turn_id=? AND message_hash=? AND block_index=? AND asset_hash=?''',
                    (source_id, message['_source_message_hash'], item['block_index'], block['asset_id'][7:])).fetchone()
                attachments.append({'ordinal': item['ordinal'], 'retained': bool(retained),
                    **({'asset_hash': block['asset_id'][7:]} if retained else {'reason': block.get('reason', 'original_unavailable')})})
        return {'processed': provider_id is not None, 'source_id': source_id,
                'provider_message_id': provider_id, 'attachments': attachments,
                'all_originals_retained': bool(attachments) and all(item['retained'] for item in attachments)}
