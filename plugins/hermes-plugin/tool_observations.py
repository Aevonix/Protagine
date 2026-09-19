"""Nominate a useful completed call; retain its actual bounded native original."""
from collections import OrderedDict
from contextlib import closing
import copy
import hashlib
import json
import logging
import math
import sqlite3
import threading
import time

from httpx import HTTPStatusError, NetworkError, RemoteProtocolError, TimeoutException

from .followups import capture_instruction
from .request_work import replace_context
from .request_tool_visibility import without_tool, without_discovery_tool

MAX_BYTES = 16384
logger = logging.getLogger(__name__)
# Explicit retention needs the same bounded freshness allowance as a model
# request. Local sidecar contention can exceed the background drain's 250 ms.
_ERASURE_REFRESH_SECONDS = 5.0
# Discovery metadata is already available through the current tool catalog.
# Retaining it as an observation makes later recall compete with real findings.
_EXCLUDED = {'session_search', 'tool_search', 'tool_describe', 'protagine_memory_retain_observation'}
_HINT_MARKER = 'protagine-observation-candidates-v1'
_CATALOG_HEADER = 'Deferred tool catalog (call schemas via `tool_describe`, invoke via `tool_call`):'
_RETENTION = 'protagine_memory_retain_observation'


def argument_preview(arguments):
    """A display label from execution arguments, never a replacement for evidence."""
    try:
        text = json.dumps(arguments, sort_keys=True, ensure_ascii=True, separators=(',', ':'))
    except (TypeError, ValueError):
        return {'arguments_preview': None, 'arguments_truncated': False}
    return {'arguments_preview': text[:128], 'arguments_truncated': len(text) > 128}


def _available_retention(request):
    if (request.get('tool_choice') in ('none', {'type': 'none'})
            or request.get('function_call') == 'none'):
        return None
    schemas = request.get('tools') or request.get('functions') or []
    functions = [tool.get('function', tool) for tool in schemas if isinstance(tool, dict)]
    named = {fn['name']: fn for fn in functions if isinstance(fn, dict) and isinstance(fn.get('name'), str)}
    for name in named:
        if name == 'protagine_memory_retain_observation':
            return name, False
    if not {'tool_search', 'tool_describe', 'tool_call'} <= named.keys():
        return None
    description = named['tool_search'].get('description', '')
    if not isinstance(description, str) or _CATALOG_HEADER not in description:
        return None
    for line in description.split(_CATALOG_HEADER, 1)[1].splitlines():
        names = [line[2:].split(':', 1)[0]] if line.startswith('- ') else line.split(',')
        for name in names:
            name = name.strip()
            if name == 'protagine_memory_retain_observation':
                return name, True
    return None


def _ordinary_native_origin(scope):
    """Transport identity cannot turn a native worker into an owner conversation.

    The native session origin survives kanban_complete clearing its claim/run.
    Read that exact session, not a task's current status or model-supplied fields.
    Child execution can inherit a parent's transport and source; its native
    ContextVar also has to agree before borrowing any ordinary-turn receipt.
    """
    try:
        from agent.delegation_context import is_delegated_child_process_context
        from gateway.session_context import get_session_env
        from hermes_state import _default_db_path
        if is_delegated_child_process_context():
            return False
        source = str(get_session_env('HERMES_SESSION_SOURCE', '') or '').strip()
        if source and source != scope.platform:
            return False
        path = _default_db_path().resolve()
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=.25)) as db:
            row = db.execute('SELECT source FROM sessions WHERE id=?', (scope.session_id,)).fetchone()
        return bool(row and row[0] == scope.platform)
    except (ImportError, OSError, sqlite3.Error):
        return False


def _key(scope):
    if (scope is None or not scope.valid_participant or scope.authority_lane not in {'owner', 'system'}
            or scope.platform in {'cron', 'subagent', 'background_review', 'protagine_task'}
            or getattr(scope, 'parent_session_id', '')
            or not isinstance(getattr(scope, 'user_message', None), str)
            or not scope.user_message.strip() or len(scope.user_message) > 32768
            or not all(getattr(scope, name, '') for name in ('contact_id', 'session_id', 'task_id', 'turn_id'))):
        return None
    from .input_provenance import current
    if current() is not None or not _ordinary_native_origin(scope):
        return None
    return scope.contact_id, scope.session_id, scope.task_id, scope.turn_id


def _arguments_hash(arguments):
    try:
        return hashlib.sha256(json.dumps(arguments, sort_keys=True, ensure_ascii=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
    except (TypeError, ValueError):
        return None


def _call_identity(name, arguments):
    """Native dispatch unwraps local calls, while SDK history keeps tool_call.

    Normalization alone grants nothing: checked() still requires the matching
    observed execution arguments, result bytes and current request identity.
    """
    if name != 'tool_call':
        return name, None
    try:
        from tools import tool_search
        arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
        if not isinstance(arguments, dict):
            return None
        underlying, selected, error = tool_search.resolve_underlying_call(arguments)
        if error or not underlying or underlying == getattr(tool_search, 'CONNECTOR_BATCH_SENTINEL', None):
            return None
        digest = _arguments_hash(selected)
        return (underlying, digest) if digest is not None else None
    except Exception:
        return None


def _request_results(request):
    calls, results = {}, {}
    def add(rows, call_id, value):
        # A repeated ID cannot disambiguate one actual native completion.
        rows[call_id] = value if call_id not in rows else None
    for message in request.get('messages', request.get('input', [])):
        if not isinstance(message, dict):
            continue
        if message.get('role') == 'assistant':
            for call in message.get('tool_calls') or []:
                if isinstance(call, dict) and isinstance(call.get('function'), dict):
                    add(calls, call.get('id'), _call_identity(call['function'].get('name'),
                                                           call['function'].get('arguments')))
        elif message.get('type') == 'function_call':
            add(calls, message.get('call_id'), _call_identity(message.get('name'), message.get('arguments')))
        if message.get('role') == 'tool':
            add(results, message.get('tool_call_id'), message.get('content'))
        elif message.get('type') == 'function_call_output':
            add(results, message.get('call_id'), message.get('output'))
        if isinstance(message.get('content'), list):
            for block in message['content']:
                if not isinstance(block, dict):
                    continue
                if message.get('role') == 'assistant' and block.get('type') == 'tool_use':
                    add(calls, block.get('id'), _call_identity(block.get('name'), block.get('input')))
                elif message.get('role') == 'user' and block.get('type') == 'tool_result':
                    add(results, block.get('tool_use_id'), block.get('content'))
    return calls, results


def native_original(scope, call_id, expected):
    """Read the exact completed current-session row; never scan other sessions."""
    from hermes_state import SessionDB, _default_db_path
    path = _default_db_path().resolve()
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=.25)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('''SELECT id,role,content,tool_name,timestamp FROM messages
            WHERE session_id=? AND tool_call_id=? AND role='tool' AND active=1 LIMIT 2''',
            (scope.session_id, call_id)).fetchall()
    if len(rows) != 1:
        raise ValueError('A unique completed native tool message is required')
    row = dict(rows[0])
    content = SessionDB._decode_content(row['content'])
    if (not isinstance(content, str) or len(content.encode()) > MAX_BYTES
            or hashlib.sha256(content.encode()).hexdigest() != expected['sha256']
            or (row['tool_name'] and row['tool_name'] != expected['name'])
            or type(row['timestamp']) not in (int, float) or not math.isfinite(row['timestamp'])
            or row['timestamp'] <= 0):
        raise ValueError('The native original does not match the observed completed call')
    native = {'profile_id': hashlib.sha256(str(path.parent).encode()).hexdigest(),
        'session_id': scope.session_id, 'task_id': scope.task_id, 'turn_id': scope.turn_id,
        'tool_call_id': call_id, 'tool_name': row['tool_name'] or expected['visible_name'], 'message_id': row['id'],
        'api_request_id': expected['api_request_id'],
        'timestamp': row['timestamp'], 'result_sha256': expected['sha256']}
    return content, native


def native_input(scope, call_id, expected, *, result_message_id=None, result_content=''):
    """Recover the executed arguments from the exact native call, not its label.

    Dispatch or completion witnessed the argument hash. The persisted
    assistant call supplies the values only if they still match that witness.
    A request-side retelling or a different same-tool call supplies no input.
    """
    from hermes_state import _default_db_path
    path = _default_db_path().resolve()
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=.25)) as db:
        db.row_factory = sqlite3.Row
        rows = db.execute('''SELECT m.*,c.value FROM messages m,
            json_each(CASE WHEN json_valid(m.tool_calls) THEN m.tool_calls ELSE '[]' END) c
            WHERE m.session_id=? AND m.role='assistant' AND m.active=1 AND (? IS NULL OR m.id<?)
              AND json_extract(c.value,'$.id')=? LIMIT 2''',
            (scope.session_id, result_message_id, result_message_id, call_id)).fetchall()
    if len(rows) != 1:
        raise ValueError('A unique original native call is required to include its input')
    row = rows[0]
    if len(json.loads(row['tool_calls'])) != 1:
        raise ValueError('Original input shares a native row with other calls; retain this result without input')
    try:
        call = json.loads(row['value'])['function']
        name, arguments = call['name'], call['arguments']
        arguments = json.loads(arguments) if isinstance(arguments, str) else arguments
        if name == 'tool_call':
            from tools import tool_search
            name, arguments, error = tool_search.resolve_underlying_call(arguments)
            if error:
                raise ValueError('Invalid original deferred call')
        digest = _arguments_hash(arguments)
        if (name != expected['name'] or not isinstance(arguments, dict)
                or digest is None or digest != expected['arguments_sha256']):
            raise ValueError('Original call arguments differ from the executed input')
        encoded = json.dumps(arguments, sort_keys=True, ensure_ascii=True,
                             separators=(',', ':'), allow_nan=False)
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError('The original executed input is unavailable') from exc
    if len(encoded.encode()) + len(result_content.encode()) > MAX_BYTES:
        raise ValueError('Original input and result exceed the combined 16 KiB retention budget; no input was saved')
    from hermes_state import SessionDB
    if not callable(getattr(SessionDB, 'message_redaction_snapshot', None)):
        raise ValueError('Original input retention requires native snapshot support; retain the result without input')
    return {'arguments': arguments, 'arguments_sha256': digest}, {
        '_row_id':row['id'], 'role':'assistant', 'content':SessionDB._decode_content(row['content']),
        '_native_payload_sha256':SessionDB.message_redaction_snapshot(row)['sha256']}


class ToolObservations:
    def __init__(self, client, outbox, request_memory):
        self.client, self.outbox, self.request_memory = client, outbox, request_memory
        self._lock, self._turns = threading.RLock(), OrderedDict()

    def completed(self, scope, context, value, *, arguments=None):
        key, call_id, name = _key(scope), context.get('tool_call_id'), context.get('tool_name')
        request_id = context.get('api_request_id')
        if (key is None or not isinstance(request_id, str) or not 1 <= len(request_id) <= 256
                or not isinstance(call_id, str) or not 1 <= len(call_id) <= 256
                or not isinstance(name, str) or not 1 <= len(name) <= 128 or name in _EXCLUDED
                or not isinstance(value, str) or not value.strip() or len(value.encode()) > MAX_BYTES):
            return
        with self._lock:
            turn = self._turns.setdefault(key, OrderedDict())
            # Retrying the same observed call cannot replace its bytes.
            turn.setdefault(call_id, {'name': name, 'sha256': hashlib.sha256(value.encode()).hexdigest(),
                                       'visible': {}, 'api_request_id': request_id,
                'arguments': argument_preview(arguments),
                'arguments_sha256': _arguments_hash(arguments),
                'input_sha256': hashlib.sha256(scope.user_message.encode()).hexdigest(), 'sources': self.request_memory.supplied_snapshot(scope)})
            while len(turn) > 16:
                turn.popitem(last=False)
            self._turns.move_to_end(key)
            while len(self._turns) > 64:
                self._turns.popitem(last=False)

    def checked(self, request, scope, request_id, *, api_mode=''):
        key = _key(scope)
        if not isinstance(request, dict):
            return request
        request = replace_context(request, api_mode=api_mode, marker=_HINT_MARKER)
        if key is None or not isinstance(request_id, str) or not request_id:
            return without_tool(request, _RETENTION)
        calls, results = _request_results(request)
        eligible = []
        with self._lock:
            for call_id, record in self._turns.get(key, {}).items():
                text = results.get(call_id)
                record['visible'].pop(request_id, None)
                identity = calls.get(call_id)
                name, arguments_hash = identity if identity is not None else (None, None)
                if (isinstance(name, str) and name == record['name']
                        and (arguments_hash is None or arguments_hash == record['arguments_sha256']) and isinstance(text, str)
                        and hashlib.sha256(text.encode()).hexdigest() == record['sha256']):
                    record['visible'][request_id] = name
                    if (record['sources'] is not None
                            and record['input_sha256'] == hashlib.sha256(scope.user_message.encode()).hexdigest()):
                        eligible.append({'call_id': call_id, 'tool_name': name, **record['arguments']})
                while len(record['visible']) > 8:
                    record['visible'].pop(next(iter(record['visible'])))
        available = _available_retention(request)
        if not eligible or self.request_memory.supplied_snapshot(scope) is None:
            return without_tool(request, _RETENTION)
        if not available:
            return request
        name, deferred = available
        guidance = (f'For durable findings or meaningful outcomes with likely future use, you may retain '
            f'an original tool result using {name}(call_id, reason). Skip incidental output, duplicate '
            'status, transient noise and secrets. These are candidates, not saved memories. '
            'Match the call ID to its execution arguments; truncated previews require checking the original call. '
            'For recipe reuse, set include_input=true on the actual workflow/config call when its arguments matter; '
            'retain a separate final-result call if needed. A locator alone is not a recipe. Inputs may contain secrets; '
            'skip those calls. Input plus result must fit 16 KiB. ')
        if deferred:
            guidance += f'Load {name} with tool_describe, then invoke it with tool_call. '
        guidance += '\nEligible completed calls in this request: '
        listed = []
        wrapper_chars = len(f'[{_HINT_MARKER}]\n\n[/{_HINT_MARKER}]')
        for item in reversed(eligible):
            encoded = json.dumps([*listed, item], ensure_ascii=True).replace('[/', r'\u005b/')
            if wrapper_chars + len(guidance) + len(encoded) > 2048 or len(listed) == 8:
                break
            listed.append(item)
        if not listed:
            return request
        text = guidance + json.dumps(listed, ensure_ascii=True).replace('[/', r'\u005b/')
        return replace_context(request, text, api_mode=api_mode, marker=_HINT_MARKER)

    def discovery(self, value, scope, context):
        """Filter this completed discovery only, using checked request witnesses.

        The native agent executor wraps bridge reads in tool_execution. Its
        session catalog is not an input to this hook, so adjust aggregate
        counts only when the returned named record proves its membership.
        """
        name, request_id = context.get('tool_name'), context.get('api_request_id')
        if name not in {'tool_search', 'tool_describe'} or not isinstance(value, str):
            return value
        key = _key(scope)
        if key is not None and self.request_memory.supplied_snapshot(scope) is not None:
            with self._lock:
                if any(request_id in record['visible'] and record['sources'] is not None
                       and record['input_sha256'] == hashlib.sha256(scope.user_message.encode()).hexdigest()
                       for record in self._turns.get(key, {}).values()):
                    return value
        return without_discovery_tool(value, context, _RETENTION)

    def finish(self, scope):
        with self._lock:
            if scope is not None:
                self._turns.pop(tuple(getattr(scope, name, '') for name in
                    ('contact_id', 'session_id', 'task_id', 'turn_id')), None)

    def handle(self, args, scope, context):
        key = _key(scope)
        if key is None:
            return json.dumps({'accepted': False, 'error': 'Retention requires an ordinary authenticated owner conversation with matching native session origin'})
        if (not isinstance(args, dict) or not {'call_id', 'reason'} <= set(args)
                or set(args) - {'call_id', 'reason', 'include_input'}
                or type(args.get('include_input', False)) is not bool
                or not isinstance(args['call_id'], str) or not 1 <= len(args['call_id']) <= 256
                or not isinstance(args['reason'], str) or not args['reason'].strip() or len(args['reason']) > 512):
            return json.dumps({'accepted': False, 'error': 'Nominate one completed current owner-turn call and a bounded future-use reason'})
        stage = 'source_admission'
        try:
            if self.request_memory.supplied_snapshot(scope) is None:
                raise ValueError('Current request source admission is unavailable; check memory/source readiness before retrying')
            stage = 'request_evidence'
            with self._lock:
                record = copy.deepcopy(self._turns.get(key, {}).get(args['call_id']))
            if (not record or context.get('api_request_id') not in record['visible']
                    or record['input_sha256'] != hashlib.sha256(scope.user_message.encode()).hexdigest()):
                raise ValueError('The exact completed call must be present in this authorized native request')
            references = record['sources']
            if references is None or self.request_memory.supplied_snapshot(scope) is None:
                raise ValueError('Current request source lineage is unavailable')
            if 'payload' in record:
                payload = record['payload']  # First nomination owns immutable retry bytes.
            else:
                stage = 'native_original'
                record['visible_name'] = record['visible'][context['api_request_id']]
                content, native = native_original(scope, args['call_id'], record)
                stage = 'native_input'
                original_input, input_row = (native_input(scope, args['call_id'], record,
                    result_message_id=native['message_id'], result_content=content)
                    if args.get('include_input', False) else (None, None))
                ownership = self.request_memory.ownership
                stage = 'instruction_capture'
                origins = capture_instruction(scope, self.client,
                    retain_origin=(lambda source_id: ownership.retain_origin(scope, source_id,
                        canonical_user_message=scope.user_message)) if ownership is not None else None)
                if len(origins) != 1:
                    raise ValueError('The exact current owner instruction must be retained first')
                source_id = 'native-observation:' + hashlib.sha256(json.dumps(native, sort_keys=True,
                    separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
                origin_id, origin_version = next(iter(origins.items()))
                payload = {'session_id': scope.session_id, 'contact_id': scope.contact_id,
                    'turn_id': source_id, 'require_source_receipt': True,
                    'observation': {'native': native, 'content': content, 'reason': args['reason'],
                        'origin': {'source_id': origin_id, 'source_version': origin_version},
                        'sources': references}}
                if original_input is not None:
                    payload['observation']['input'] = original_input
                if ownership is not None:
                    stage = 'observation_ownership'
                    # Bind exact native rows before enqueue can publish or a
                    # concurrent erasure page can purge the canonical payload.
                    origin_rows = [{'role':'tool', 'content':content, '_row_id':native['message_id']}]
                    if input_row is not None:
                        origin_rows.append(input_row)
                    if not ownership.retain_origin(scope, source_id, messages=origin_rows,
                            row_only_ids=[input_row['_row_id']] if input_row is not None else ()):
                        raise ValueError('Native observation ownership is unavailable; no observation was queued')
                stage = 'request_evidence'
                with self._lock:
                    current = self._turns.get(key, {}).get(args['call_id'])
                    if current is None or context.get('api_request_id') not in current['visible']:
                        raise ValueError('The current observation turn ended')
                    payload = current.setdefault('payload', payload)
            # A delivered receipt is historical, not proof the source still exists.
            stage = 'erasure_refresh'
            deadline = time.monotonic() + _ERASURE_REFRESH_SECONDS
            after = self.outbox.erasure_watermark(scope.contact_id, deadline_monotonic=deadline)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Observation erasure freshness budget expired')
            response = self.client.get('/v1/host/memory/sources/erasures',
                params={'contact_id': scope.contact_id, 'after': after}, timeout=remaining,
                _deadline_monotonic=deadline)
            response.raise_for_status()
            page = response.json()
            self.outbox.apply_erasure_page(scope.contact_id, page, deadline_monotonic=deadline)
            if time.monotonic() >= deadline:
                raise TimeoutError('Observation erasure freshness budget expired')
            if page.get('complete') is not True:
                raise ValueError('Current source erasure state is incomplete')
            stage = 'outbox_enqueue'
            receipt = self.outbox.enqueue(payload['turn_id'], payload)
            if receipt['state'] == 'pending':
                stage = 'outbox_delivery'
                self.outbox.drain(lambda stored, timeout_seconds: self.client.sync_turn(
                    **stored, outbox=self.outbox, timeout_seconds=timeout_seconds), limit=16, timeout_seconds=.25)
                receipt = self.outbox.enqueue(payload['turn_id'], payload)
            return json.dumps({'accepted': receipt['state'] == 'delivered', 'state': receipt['state'],
                'source_id': payload['turn_id'], 'source_recorded': receipt['state'] == 'delivered',
                'kind': 'original_tool_quotation', 'selection_author': 'model',
                'input_included': payload['observation'].get('input') is not None,
                'selected_call': {**{key: payload['observation']['native'][key] for key in
                    ('tool_call_id', 'tool_name', 'message_id', 'result_sha256')}, **record['arguments']}})
        except ValueError as exc:
            return json.dumps({'accepted': False, 'source_recorded': False,
                'failure_stage': stage, 'error': str(exc)})
        except Exception as exc:
            retryable = isinstance(exc, (TimeoutError, TimeoutException, NetworkError, RemoteProtocolError))
            if isinstance(exc, HTTPStatusError):
                retryable = exc.response.status_code == 429 or exc.response.status_code >= 500
            # Exception text, request URLs and tracebacks can contain private
            # source text or credentials. Report only fixed stages and types.
            logger.warning('observation retention unavailable (stage=%s, error=%s)', stage, type(exc).__name__)
            return json.dumps({'accepted': False, 'source_recorded': False,
                'failure_stage': stage, 'error_type': type(exc).__name__, 'retryable': retryable,
                'error': 'Observation persistence is unconfirmed; inspect memory/source readiness before retrying'})
