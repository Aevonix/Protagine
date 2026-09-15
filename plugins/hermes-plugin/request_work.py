"""Refresh operational context at the native model-request boundary.

This reads the existing work view. It does not repeat memory recall or write
work, conversation history, commitments or source evidence.
"""
from __future__ import annotations

import json
import math
import re
import time


_OPEN = '[pacomind-work-request-v1]'
_CLOSE = '[/pacomind-work-request-v1]'
_BLOCK = re.compile(r'(?:\n\n)?\[pacomind-work-request-v1\].*?\[/pacomind-work-request-v1\]', re.S)
_UNAVAILABLE = ('Current shared work is unavailable for this model request. '
                'The turn-start snapshot may be stale; this does not establish '
                'that previously observed work has stopped.')
_BACKGROUND_HANDOFF = (
    "When the user asks for background work and a prompt return, call "
    "pacomind_task(operation='handoff') alone with the full deliverable, verification and child work. "
    "Actual acceptance completes this foreground request; the worker still owns execution and verification. "
    "Do not duplicate or poll that work here. Acceptance is not task completion. "
    "Workers must finish their assigned deliverables. Use ordinary foreground work or submit "
    "when this turn has other work to do."
)


def _owned_message(row, opening=_OPEN, closing=_CLOSE):
    return (isinstance(row, dict) and row.get('role') in ('system', 'developer')
            and isinstance(row.get('content'), str)
            and row['content'].startswith(opening + '\n')
            and row['content'].endswith('\n' + closing))


def replace_context(request, text=None, *, api_mode='', marker='pacomind-work-request-v1'):
    """Replace our request-only block without changing user or tool content."""
    result = dict(request)
    opening, closing = '[' + marker + ']', '[/' + marker + ']'
    pattern = _BLOCK if opening == _OPEN else re.compile(
        r'(?:\n\n)?' + re.escape(opening) + '.*?' + re.escape(closing), re.S)
    for name in ('messages', 'input'):
        if isinstance(request.get(name), list):
            result[name] = [row for row in request[name] if not _owned_message(row, opening, closing)]
    for name in ('instructions', 'system'):
        value = request.get(name)
        if isinstance(value, str):
            result[name] = pattern.sub('', value)
        elif name == 'system' and isinstance(value, list):
            result[name] = [row for row in value if not (
                isinstance(row, dict) and row.get('type') == 'text'
                and isinstance(row.get('text'), str)
                and row['text'].startswith(opening + '\n')
                and row['text'].endswith('\n' + closing))]
    if text is None:
        return result
    block = opening + '\n' + text + '\n' + closing
    if 'input' in result:
        instructions = result.get('instructions') or ''
        if isinstance(instructions, str):
            result['instructions'] = instructions + ('\n\n' if instructions else '') + block
    elif isinstance(result.get('system'), str):
        result['system'] += ('\n\n' if result['system'] else '') + block
    elif isinstance(result.get('system'), list):
        result['system'].append({'type': 'text', 'text': block})
    elif api_mode == 'anthropic_messages':
        # Native Anthropic kwargs omit system entirely when it is empty.
        result['system'] = block
    elif isinstance(result.get('messages'), list):
        messages = result['messages']
        # Hermes has already converted the leading system role for models
        # that use developer instructions before invoking request middleware.
        role = ('developer' if messages and isinstance(messages[0], dict)
                and messages[0].get('role') == 'developer' else 'system')
        messages.append({'role': role, 'content': block})
    return result


class RequestWork:
    def __init__(self, client, native_tasks=None):
        self.client = client
        self.native_tasks = native_tasks

    def _revision(self, value, scope, deadline, *, max_chars=4000):
        """Join only a locally retained revision of an actually shown task."""
        if self.native_tasks is None or not value.get('native_task_ids'):
            return value
        revision = self.native_tasks.request_revision(scope, value['native_task_ids'],
            deadline_monotonic=deadline)
        if revision is None or time.monotonic() > deadline:
            return value
        instruction = revision['instruction']
        def line(length):
            update = {**revision['update'], 'excerpt': instruction[:length],
                      'partial': length < len(instruction)}
            return json.dumps({'task_id': revision['task_id'],
                'latest_accepted_update': update}, ensure_ascii=True).replace(
                    _CLOSE, r'\u005b/pacomind-work-request-v1\u005d') + '\n'
        length = min(240, len(instruction))
        while length and len(line(length)) > 640:
            length -= 1
        if not length:
            return value
        addition = line(length)
        # Reserve one of the existing eight records only when a revision is
        # present. With no update the normal full projection stays unchanged.
        selected = value.get('reserved')
        if (not isinstance(selected, dict) or selected.get('schema') != 'PacoMindRequestWorkV1'
                or revision['task_id'] not in selected.get('native_task_ids', [])
                or not isinstance(selected.get('text'), str)
                or len(selected['text']) + len(addition) > max_chars):
            return value
        supplied = selected.get('input_provenance')
        if supplied is None:
            # A retained task can have no current excerpt. Its exact original
            # and update parents still pass the same dispatch-time checks.
            supplied = {key: revision[key] for key in ('contact_id', 'watermark',
                'source_refs', 'unannotated_input_refs')}
        if (not isinstance(supplied, dict) or supplied.get('contact_id') != scope.contact_id
                or revision['contact_id'] != scope.contact_id
                or type(supplied.get('watermark')) is not int
                or supplied['watermark'] < revision['watermark']):
            return value
        # Preserve the fresh view's watermark. Older admission watermarks do
        # not replace it; the existing dispatch check validates every parent.
        supplied = dict(supplied)
        for key in ('source_refs', 'unannotated_input_refs'):
            refs = [*supplied[key], *revision[key]]
            supplied[key] = list({json.dumps(ref, sort_keys=True): ref for ref in refs}.values())
        return {**selected, 'text': selected['text'] + addition, 'input_provenance': supplied}

    def __call__(self, request, scope, *, api_mode=''):
        return self.prepare(request, scope, api_mode=api_mode)[0]

    def prepare(self, request, scope, *, api_mode=''):
        """Return request, optional input lineage, and successful-refresh status.

        The registered adapter passes that lineage to its existing source
        freshness check before dispatch. No text marker nominates a source.
        """
        if (scope is None or not scope.valid_participant
                or not (scope.authority_lane == 'owner'
                        or (scope.authority_lane == 'system'
                            and scope.resolution_status == 'attested_system'))
                or scope.platform in ('cron', 'background_review')):
            return replace_context(request, api_mode=api_mode), None, False
        session = json.dumps(scope.session_id, ensure_ascii=True).replace(
            _CLOSE, r'\u005b/pacomind-work-request-v1\u005d')
        identity = f'Current request session: {session}.\n'
        text = identity + _UNAVAILABLE
        provenance = None
        current = False
        guidance = ''
        if self.native_tasks is not None:
            from .task_controller import FinishTurn
            if FinishTurn is not None:
                guidance = '\n\n' + _BACKGROUND_HANDOFF
        max_chars = 4000 - len(guidance)
        deadline = time.monotonic() + .25
        try:
            response = self.client.get("/v1/host/executions",
                params={'contact_id': scope.contact_id, 'session_id': scope.session_id,
                        'limit': 8, 'projection': 'request', 'input_context': True,
                        **({'reserve_chars': 640 + len(guidance)} if self.native_tasks is not None else {})},
                timeout=.25, _deadline_monotonic=deadline)
            response.raise_for_status()
            value = response.json()
            observed = value.get('observed_at')
            if (value.get('schema') != 'PacoMindRequestWorkV1'
                    or not isinstance(value.get('text'), str)
                    or not 1 <= len(value['text']) <= 4000
                    or type(observed) not in (int, float) or not math.isfinite(observed)
                    or time.monotonic() > deadline):
                raise ValueError('Invalid or late operational view')
            value = self._revision(value, scope, deadline, max_chars=max_chars)
            if len(value['text']) > max_chars:
                value = value.get('reserved')
                if (not isinstance(value, dict) or value.get('schema') != 'PacoMindRequestWorkV1'
                        or not isinstance(value.get('text'), str) or not 1 <= len(value['text']) <= max_chars):
                    raise ValueError('Operational view exceeds its reserved budget')
            text = identity + f"Observed at {observed:.3f}.\n" + value['text']
            supplied = value.get('input_provenance')
            if supplied is not None:
                refs = supplied.get('source_refs')
                input_refs = supplied.get('unannotated_input_refs')
                if (supplied.get('contact_id') != scope.contact_id
                        or type(supplied.get('watermark')) is not int or supplied['watermark'] < 0
                        or not isinstance(refs, list) or not 1 <= len(refs) <= 512
                        or any(not isinstance(ref, dict) or set(ref) != {'source_id', 'source_version'}
                            or not isinstance(ref['source_id'], str) or not 1 <= len(ref['source_id']) <= 256
                            or not isinstance(ref['source_version'], str)
                            or not re.fullmatch('[a-f0-9]{64}', ref['source_version']) for ref in refs)):
                    raise ValueError('Invalid operational input provenance')
                if (not isinstance(input_refs, list) or not 1 <= len(input_refs) <= 512
                        or any(not isinstance(ref, dict) or set(ref) != {'source_id', 'input_message_hash'}
                            or ref['source_id'] not in {source['source_id'] for source in refs}
                            or not isinstance(ref['input_message_hash'], str)
                            or not re.fullmatch('[a-f0-9]{64}', ref['input_message_hash']) for ref in input_refs)):
                    raise ValueError('Operational input requires current annotation checks')
                provenance = {**supplied, 'text': _OPEN + '\n' + text + '\n' + _CLOSE}
            current = True
        except Exception:
            # A temporary work-service failure must not stall a conversation
            # or advertise the previous request's operational state as fresh.
            text, provenance = identity + _UNAVAILABLE, None
        text += guidance
        if provenance is not None:
            provenance['text'] = _OPEN + '\n' + text + '\n' + _CLOSE
            provenance['handoff_guidance'] = guidance
        return replace_context(request, text, api_mode=api_mode), provenance, current
