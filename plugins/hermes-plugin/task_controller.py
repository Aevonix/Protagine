"""Native text task tools backed by one registered gateway adapter.

The controller retains associations and schedules calls onto that adapter's
existing event loop. It owns no executor, listener, scheduler or agent process.
"""
import asyncio
from concurrent.futures import TimeoutError as FutureTimeout
from contextlib import contextmanager
import hashlib
import importlib
import json
import os
from pathlib import Path
import sqlite3
import time

from .client import PrivateSQLitePath
from .task_handoffs import TaskHandoffError, TaskHandoffs, erase_task_handoffs, status_view
from .task_sources import NativeTaskSources, owner_lookup_deadline

try:
    from hermes_cli.tool_completion import FinishTurn
except ImportError:
    FinishTurn = None


TOOL_SCHEMA = {
    'name': 'protagine_task',
    'description': (
        'Run an accepted task in the background while this conversation continues. '
        'Submit the complete deliverable and constraints, including any child work, in one bounded request. '
        'After acceptance, use the actual task_id and let the task perform its completion and verification work. '
        'Inspect, steer, stop or resume the returned task_id from '
        'another conversation belonging to the same owner. Results are retained '
        'for inspection; acceptance is not completion or an outward delivery. '
        'Status includes original-input and recent authorized update source references; '
        'open them with protagine_memory_read_source. Update acknowledgment and request '
        'visibility do not prove that the behavior was applied. '
        'List also reports profile-declared model role names for task submission. '
        'Answer routine questions directly. For difficult reasoning or evidence synthesis, '
        'use a suitable declared model role and keep the conversation responsive while it works. '
        'Resume a failed or interrupted task using the native_turn_id just observed in status '
        'as expected_turn_id; the same task and conversation continue. '
        'Use native delegation for child work within a task.'),
    'parameters': {
        'type': 'object', 'additionalProperties': False,
        'properties': {
            'operation': {'type': 'string', 'enum': ['submit', 'status', 'steer', 'stop', 'resume', 'list']},
            'request': {'type': 'string', 'minLength': 1, 'maxLength': 32768,
                'description': 'For submit: preserve the requested deliverable, destination, '
                    'verification or readback steps, and permission boundaries. Include child work '
                    'inside this task and apply child-only restrictions only to that child. '
                    'A read-only child does not make this task read-only. '
                    'For steer: the complete correction for the existing task_id, including '
                    'conditions that must remain unchanged.'},
            'model_role': {'type': 'string', 'minLength': 1, 'maxLength': 256,
                'description': 'Optional for submit: a task role declared in this profile, '
                    'such as coding or reasoning. Omit to use the configured task default. '
                    'Choose for the work, not merely because it runs in the background. '
                    'The role selects model configuration; it does not change permissions '
                    'or remove deliverables.'},
            'task_id': {'type': 'string', 'pattern': '^[0-9a-f]{64}$'},
            'expected_turn_id': {'type': 'string', 'minLength': 1, 'maxLength': 512,
                'description': 'Required for resume: the exact native_turn_id observed in task status.'},
        },
        'required': ['operation'],
    },
}

if FinishTurn is not None:
    TOOL_SCHEMA['description'] += (
        ' Use handoff when this task carries the remaining request and this foreground turn '
        'should finish with its acceptance receipt. Submit keeps the foreground conversation available '
        'for more work. To hand off a task already accepted in this turn, pass its task_id alone; '
        'this returns the existing acceptance without starting another task. Use request for new work. '
        'Handoff can finish the turn only when it is the sole successful tool call.')
    TOOL_SCHEMA['parameters']['properties']['operation']['enum'].insert(1, 'handoff')
    TOOL_SCHEMA['parameters']['properties']['task_id']['description'] = (
        'For handoff: an existing task accepted in this same turn. Omit request, model_role '
        'and expected_turn_id; use steer to change an existing task.')
    for field in ('request', 'model_role'):
        prop = TOOL_SCHEMA['parameters']['properties'][field]
        prop['description'] = prop['description'].replace('for submit:', 'for submit or handoff:').replace(
            'For submit:', 'For submit or handoff:')


class NativeTasks:
    def __init__(self, client, outbox, owner_contact_id, *, state_path=None,
                 attested_system_platforms=('cli',), database=None, sources=None,
                 adapter_type=None, adapter_resolver=None,
                 error_type=TaskHandoffError, reply_effect='retained_for_transport'):
        if database is not None and state_path is not None:
            raise ValueError('Choose the existing task database or a state path')
        self._database_factory = database
        self.path = None if database is not None else Path(
            state_path or outbox.path.parent / 'protagine-native-tasks.sqlite3').expanduser()
        if self.path is not None and self.path.resolve() == outbox.path.resolve():
            raise ValueError('Task associations cannot replace the turn outbox schema')
        self.storage = PrivateSQLitePath(self.path) if self.path is not None else None
        self.sources = sources or NativeTaskSources(client, outbox, owner_contact_id,
            attested_system_platforms=attested_system_platforms,
            erase=lambda contact, rules: erase_task_handoffs(self.database, contact, rules))
        self.handoffs = TaskHandoffs(self.database, self.sources.resolve_source, self.sources.resolve_owner,
            error_type=error_type, reply_effect=reply_effect)
        self.owner = owner_contact_id
        self.client, self.outbox = client, outbox
        self.execution_observer = None
        self.adapter_type = adapter_type
        self.adapter_resolver = adapter_resolver
        self.adapter = None
        self.gateway = None
        self._draining = False
        self._pending_after = None
        self._updates_after = None

    @contextmanager
    def database(self):
        if self._database_factory is not None:
            with self._database_factory() as db:
                yield db
            return
        db, identity = self.storage.connect(timeout_seconds=1)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA fullfsync=ON')
        try:
            with db:
                yield db
            self.storage.assert_current(identity)
        finally:
            db.close()

    def create_adapter(self, config):
        from .native_task_platform import NativeTaskAdapter
        controller = self
        base = self.adapter_type or NativeTaskAdapter
        if not isinstance(base, type) or not issubclass(base, NativeTaskAdapter):
            raise TypeError('A native task adapter subclass is required')

        class ConnectedTaskAdapter(base):
            @property
            def authorization_is_upstream(adapter):
                # The source controller and correlated handler authenticate
                # the retained real owner
                # before native dispatch, including recovery. Hermes' existing
                # trusted-upstream contract avoids treating a canonical task
                # owner as an unrelated external-channel allowlist entry.
                # A configured transport subclass must authenticate its own
                # HTTP callback. The generic base rejects all external ingress.
                return True

            async def connect(adapter, *, is_reconnect=False):
                connected = await super().connect(is_reconnect=is_reconnect)
                controller.adapter = adapter
                await controller.reconcile()
                return connected

            async def disconnect(adapter):
                if controller.adapter is adapter:
                    controller.adapter = None
                await super().disconnect()

            async def dispatch_http_event(adapter, payload):
                return await controller.dispatch(payload)

        adapter = ConnectedTaskAdapter(config, handoffs=self.handoffs)
        if adapter.platform.value != 'protagine_task':
            raise ValueError('The shared task adapter must retain its registered platform')
        return adapter

    def observe_gateway(self, **kwargs):
        """Capture the runtime supplied by the supported native dispatch hook."""
        gateway, store = kwargs.get('gateway'), kwargs.get('session_store')
        adapter = self.adapter
        if (gateway is not None and adapter is not None
                and store is getattr(adapter, '_session_store', None)
                and callable(getattr(gateway, '_adapter_for_source', None))):
            self.gateway = gateway

    async def dispatch(self, payload):
        """Route a retained ID to its actual native origin, including old adapters."""
        from .native_task_platform import NativeTaskAdapter
        from gateway.platforms.event import MessageEvent
        if (not isinstance(payload, dict) or set(payload) not in (
                {'handoff_id'}, {'handoff_id', 'action'}, {'handoff_id', 'action', 'update_id'},
                {'handoff_id', 'action', 'expected_turn_id'})):
            raise TaskHandoffError('An exact retained task callback is required')
        adapter = self.adapter
        if adapter is None:
            raise TaskHandoffError('The native task gateway is not connected')
        row = await asyncio.to_thread(self.handoffs.control, payload['handoff_id'])
        stopped = self.handoffs.stop_view(row)
        if row['response'] or (stopped and stopped['status'] == 'cancelled'):
            # Retained completion and observed cancellation need no old adapter
            # to be online. These native paths cannot admit or resume work.
            return await adapter.dispatch_native_event(payload)
        store = getattr(adapter, '_session_store', None)
        entry = None
        if store is not None:
            if row['native_session_id']:
                entry = await asyncio.to_thread(store.lookup_by_session_id, row['native_session_id'])
                if entry is None:
                    raise TaskHandoffError('The retained native task origin is unavailable')
            else:
                # A previous callback may have created native work before its
                # first binding hook. Detect the exact existing origin rather
                # than admitting that row again on another platform.
                entries = await asyncio.to_thread(store.list_sessions)
                matches = [candidate for candidate in entries if candidate.origin is not None
                    and candidate.origin.chat_id == row['id']
                    and candidate.origin.user_id == row['source']['contact_id']]
                if len(matches) > 1:
                    raise TaskHandoffError('The retained task has ambiguous native origins')
                entry = matches[0] if matches else None
        if entry is not None:
            origin = entry.origin
            if (origin is None or origin.chat_id != row['id']
                    or origin.user_id != row['source']['contact_id']):
                raise TaskHandoffError('The retained task origin does not match its owner')
            source = adapter.build_source(chat_id=row['id'], chat_type='dm',
                user_id=row['source']['contact_id'], message_id=row['id'])
            key = adapter._event_session_key(MessageEvent(text='', source=source))
            if origin.platform != adapter.platform or entry.session_key != key:
                resolver = self.adapter_resolver or (self.gateway._adapter_for_source if self.gateway else None)
                selected = resolver(origin) if resolver is not None else None
                if (not isinstance(selected, NativeTaskAdapter)
                        or getattr(selected, '_session_store', None) is not store):
                    raise TaskHandoffError('The original native task adapter is unavailable')
                adapter = selected
                source = adapter.build_source(chat_id=row['id'], chat_type='dm',
                    user_id=row['source']['contact_id'], message_id=row['id'])
                key = adapter._event_session_key(MessageEvent(text='', source=source))
            adapter._check_native_origin(entry, source, key)
        return await adapter.dispatch_native_event(payload)

    def bind_native_turn(self, **kwargs):
        from .native_task_platform import ACTIVE, bind_native_turn
        active = ACTIVE.get()
        if active is not None and active['adapter'] is self.adapter:
            return bind_native_turn(**kwargs)

    def finish_native_turn(self, **kwargs):
        from .native_task_platform import ACTIVE, finish_native_turn
        active = ACTIVE.get()
        if active is not None and active['adapter'] is self.adapter:
            return finish_native_turn(**kwargs)

    def settle_native_turn(self, **kwargs):
        from .native_task_platform import ACTIVE, settle_native_turn
        active = ACTIVE.get()
        if active is not None and active['adapter'] is self.adapter:
            return settle_native_turn(**kwargs)

    def execution_experience(self, **kwargs):
        from .native_task_platform import ACTIVE, execution_experience
        active = ACTIVE.get()
        if active is not None and active['adapter'] is self.adapter:
            return execution_experience(**kwargs)

    def native_scope_fields(self, **kwargs):
        """Project the retained real sender into this exact native task turn.

        The adapter's correlated handler establishes ACTIVE after source and
        ownership checks. A synthetic platform name or sender argument alone
        cannot establish authority. Retain the original handle for the existing
        per-tool participant revalidation, including joined child turns.
        """
        from .native_task_platform import ACTIVE
        active = ACTIVE.get()
        exact = {key: str(kwargs.get(key) or '') for key in ('session_id', 'task_id', 'turn_id')}
        if (active is None or active['adapter'] is not self.adapter
                or active['handoffs'] is not self.handoffs or not all(exact.values())
                or active.get('native') != exact or kwargs.get('parent_session_id')
                or kwargs.get('platform') != self.adapter.platform.value):
            raise TaskHandoffError('The exact native task source is unavailable')
        row = self.handoffs.control(active['id'])
        if any(row['native_' + key] != value for key, value in exact.items()):
            raise TaskHandoffError('The native task generation changed')
        owner = self.sources.resolve_owner(row['source'], require_task_grant=True)
        if kwargs.get('sender_id') != owner:
            raise TaskHandoffError('The native task sender does not match its owner')
        fields = self.sources.execution_identity(row['source'])
        if (not isinstance(fields, dict) or set(fields) != {'sender_id', 'contact_id',
                'authority_lane', 'resolution_status', 'authority_gateway'}
                or fields['contact_id'] != owner or fields['authority_lane'] not in {'owner', 'system'}
                or fields['resolution_status'] not in {'resolved', 'attested_system'}
                or any(not isinstance(value, str) for value in fields.values())):
            raise TaskHandoffError('The native task source identity is invalid')
        return fields

    def _call(self, action, identity, *, update_id=None, expected_turn_id=None):
        adapter = self.adapter
        if (adapter is None or adapter.loop is None or not adapter.loop.is_running()
                or adapter.dispatch_context is None):
            return None
        try:
            same_loop = asyncio.get_running_loop() is adapter.loop
        except RuntimeError:
            same_loop = False
        if same_loop:
            raise TaskHandoffError('Native task tools must run outside the gateway event loop')
        # Tool handlers run off the gateway loop. A bounded wait never cancels
        # half-admitted native work; the retained ID resolves an ambiguous result.
        payload = {'handoff_id': identity, 'action': action}
        if update_id is not None:
            payload['update_id'] = update_id
        if expected_turn_id is not None:
            payload['expected_turn_id'] = expected_turn_id
        # This is an independent root. Copy the connected gateway/profile
        # context, not the foreground tool's managed Relay callback ancestry.
        # Its owner and source lineage come from the retained handoff instead.
        future = adapter.dispatch_context.copy().run(asyncio.run_coroutine_threadsafe,
            self.dispatch(payload), adapter.loop)
        try:
            return future.result(timeout=2)
        except FutureTimeout:
            return None

    async def reconcile(self):
        """Retry bounded retained admissions on connect or an existing native tick."""
        adapter = self.adapter
        if adapter is None or self._draining:
            return
        self._draining = True
        try:
            ids = await asyncio.to_thread(self.handoffs.pending, 4, after=self._pending_after)
            self._pending_after = ids[-1] if ids else None
            for identity in ids:
                try:
                    row = await asyncio.to_thread(self.handoffs.control, identity)
                    await self.dispatch({'handoff_id': identity,
                        'action': 'stop' if row['stop'] else 'submit'})
                except Exception:
                    # Keep the same row and its stop intent. Native liveness is
                    # not inferred from this failed transport attempt.
                    continue
            updates = await asyncio.to_thread(self.handoffs.pending_updates, 4, after=self._updates_after)
            self._updates_after = updates[-1]['id'] if updates else None
            for update in updates:
                try:
                    await self.dispatch({'handoff_id': update['handoff_id'], 'action':'steer',
                                         'update_id':update['id']})
                except Exception:
                    continue
        finally:
            self._draining = False

    def reconcile_pending(self, **kwargs):
        if kwargs.get('dry_run') or kwargs.get('board') not in (None, 'default') or os.environ.get('HERMES_KANBAN_TASK'):
            return
        adapter = self.adapter
        if (adapter is not None and adapter.loop is not None and adapter.loop.is_running()
                and adapter.dispatch_context is not None):
            adapter.dispatch_context.copy().run(asyncio.run_coroutine_threadsafe,
                self.reconcile(), adapter.loop)

    @staticmethod
    def _metadata(row):
        result = {'task_id': row['id'], 'executor': 'native_hermes',
                  **{key: row[key] for key in ('native_session_id', 'native_task_id', 'native_turn_id')}}
        if row.get('model_role') is not None:
            result['model_role'] = row['model_role']
        if row['response']:
            return {**result, **TaskHandoffs.response_view(row, include_content=False),
                    'delivery': 'unobserved'}
        stopped = TaskHandoffs.stop_view(row)
        return {**result, **(stopped or TaskHandoffs.failure_view(row)
                            or status_view('unknown', 'native_liveness_unobserved',
                                           reason='native_liveness_unobserved'))}

    def request_revision(self, scope, task_ids, *, deadline_monotonic):
        """Read one accepted update locally; the request boundary checks its sources.

        Current owner/grant resolvers are unchanged. This does not ask the
        native runtime for status, resolve source prose or dispatch a callback.
        """
        from .input_provenance import _refs
        if (getattr(scope, 'contact_id', None) != self.owner
                or not isinstance(task_ids, list) or len(task_ids) > 8
                or any(not isinstance(value, str) or len(value) != 64
                    or any(c not in '0123456789abcdef' for c in value) for value in task_ids)):
            return None
        try:
            for identity in task_ids:
                row = self.handoffs.get(identity)
                updates = self.handoffs.updates(identity)
                if not updates:
                    continue
                update = updates[-1]
                # A missing latest update never promotes an older revision.
                with owner_lookup_deadline(deadline_monotonic):
                    if (not update['instruction'] or self.sources.actor_contact(scope) != self.owner
                            or self.sources.resolve_owner(row['source'], require_task_grant=True) != self.owner
                            or self.sources.resolve_owner(update['source'], require_task_grant=True) != self.owner):
                        return None
                if time.monotonic() > deadline_monotonic:
                    return None
                parents = [row['source'], row['dependencies'] or {}, update['source']]
                def merged(name, digest):
                    values = [ref for source in parents for ref in source.get(name, [])]
                    return (_refs(list({json.dumps(ref, sort_keys=True): ref for ref in values}.values()), digest)
                            if values else [])
                refs = merged('source_refs', 'source_version')
                inputs = merged('input_refs', 'input_message_hash')
                if not inputs:
                    return None
                missing = {ref['source_id'] for ref in inputs} - {ref['source_id'] for ref in refs}
                if missing:
                    # Enrolled speech may retain exact input hashes before it
                    # has source revisions. Reuse the canonical resolver only
                    # for those missing pins; do not derive a revision from text.
                    remaining = deadline_monotonic - time.monotonic()
                    if remaining <= 0:
                        return None
                    after, _ = self.outbox.erasure_state(self.owner,
                        deadline_monotonic=deadline_monotonic)
                    response = self.client.post('/v1/host/memory/sources/erasures',
                        json={'contact_id': self.owner, 'session_id': scope.session_id,
                            'after': after, 'source_refs': refs, 'unannotated_input_refs': inputs},
                        timeout=remaining, _deadline_monotonic=deadline_monotonic)
                    response.raise_for_status()
                    page = response.json()
                    resolved = _refs(page.get('input_source_refs'), 'source_version')
                    if (page.get('complete') is not True
                            or {ref['source_id'] for ref in resolved} != {ref['source_id'] for ref in inputs}
                            or time.monotonic() > deadline_monotonic):
                        return None
                    refs = [*refs, *(ref for ref in resolved if ref['source_id'] in missing)]
                view = self.handoffs.update_view(update)
                return {'task_id': identity, 'instruction': update['instruction'],
                    'update': {key: view[key] for key in ('update_id', 'accepted',
                        'native_control_acknowledged', 'middleware_visible', 'native_request_visible',
                        'provider_delivery', 'behavior_applied')},
                    'contact_id': self.owner,
                    'watermark': max(row['source']['watermark'], update['source']['watermark'],
                        self.outbox.erasure_watermark(self.owner, deadline_monotonic=deadline_monotonic)),
                    'source_refs': refs, 'unannotated_input_refs': inputs}
        except Exception:
            # Request-only context is optional; source or authority uncertainty
            # must not interrupt the native conversation or expose old text.
            return None
        return None

    def request_origin(self, scope, task_ids, *, deadline_monotonic):
        """Retained admission lineage for one actually shown same-owner task.

        Reuse request-memory's final source/annotation checks. This neither
        dispatches task control nor reads other native session transcripts.
        """
        from .input_provenance import _refs
        from .native_task_platform import ACTIVE
        if (getattr(scope, 'contact_id', None) != self.owner
                or not isinstance(task_ids, list) or len(task_ids) > 8):
            return None
        try:
            active = ACTIVE.get()
            with owner_lookup_deadline(deadline_monotonic):
                if scope.platform == 'protagine_task':
                    if (active is None or active['adapter'] is not self.adapter
                            or active['handoffs'] is not self.handoffs
                            or active['id'] not in task_ids
                            or active.get('native') != {key: getattr(scope, key, '') for key in
                                ('session_id', 'task_id', 'turn_id')}):
                        return None
                    task_ids = [active['id']]
                elif self.sources.actor_contact(scope) != self.owner:
                    return None
                for identity in task_ids:
                    row = self.handoffs.get(identity)
                    if self.sources.resolve_owner(row['source'], require_task_grant=True) != self.owner:
                        continue
                    origin = row['source'].get('origin')
                    if not origin:
                        continue
                    parents = [row['source'], row['dependencies'] or {}]
                    def merged(name, digest):
                        refs = [ref for parent in parents for ref in parent.get(name, [])]
                        return _refs(list({json.dumps(ref, sort_keys=True): ref for ref in refs}.values()), digest)
                    refs = merged('source_refs', 'source_version')
                    inputs = merged('input_refs', 'input_message_hash')
                    if {ref['source_id'] for ref in inputs} - {ref['source_id'] for ref in refs}:
                        return None
                    if time.monotonic() > deadline_monotonic:
                        return None
                    return {'task_id': identity, 'origin': origin, 'contact_id': self.owner,
                        'origin_execution_id': row.get('origin_execution_id'),
                        'source_refs': refs, 'unannotated_input_refs': inputs,
                        'watermark': max(row['source']['watermark'],
                            self.outbox.erasure_watermark(self.owner, deadline_monotonic=deadline_monotonic))}
        except Exception:
            return None  # Optional context cannot weaken a failed owner/source check.
        return None

    def handle(self, args, scope):
        identity = None
        try:
            if (not isinstance(args, dict) or scope is None or not scope.valid_participant
                    or scope.contact_id != self.owner or scope.authority_lane not in {'owner', 'system'}):
                raise TaskHandoffError('An attested owner conversation is required')
            operation = args.get('operation')
            if operation == 'handoff' and FinishTurn is None:
                raise TaskHandoffError('This native runtime does not support terminal handoff; use submit to continue normally')
            existing_handoff = operation == 'handoff' and 'task_id' in args
            expected = {'operation'} | ({'task_id'} if existing_handoff else
                {'request'} if operation in {'submit', 'handoff'} else
                {'task_id', 'request'} if operation == 'steer' else
                {'task_id', 'expected_turn_id'} if operation == 'resume' else
                {'task_id'} if operation in {'status', 'stop'} else set())
            if not existing_handoff and operation in {'submit', 'handoff'} and 'model_role' in args:
                expected.add('model_role')
            if set(args) != expected or operation not in {'submit', 'handoff', 'status', 'steer', 'stop', 'resume', 'list'}:
                if existing_handoff:
                    identity = args['task_id']
                    raise TaskHandoffError('Existing task handoff accepts only operation and task_id; use steer to change its request')
                if operation not in TOOL_SCHEMA['parameters']['properties']['operation']['enum']:
                    raise TaskHandoffError('Choose a supported operation: ' + ', '.join(
                        TOOL_SCHEMA['parameters']['properties']['operation']['enum']))
                allowed = expected | ({'model_role'} if operation in {'submit', 'handoff'} else set())
                details = [f'For {operation}, use only these fields: ' + ', '.join(sorted(allowed))]
                if expected - set(args):
                    details.append('Missing fields: ' + ', '.join(sorted(expected - set(args))))
                if set(args) - expected:
                    details.append('Unexpected fields: ' + ', '.join(sorted(set(args) - expected)))
                raise TaskHandoffError('. '.join(details) + '.')
            if existing_handoff:
                identity = args['task_id']
                row = self.handoffs.get(identity)
                self.sources.authorize_control(row['source'], scope)
                origin = row['source'].get('origin') or {}
                if any(origin.get(key) != getattr(scope, key, None)
                       for key in ('session_id', 'turn_id', 'platform')):
                    raise TaskHandoffError('Handoff requires a task accepted in this same turn; use status for other tasks')
                row, _ = self.handoffs.resolve(identity)
                if row['stop'] or row['terminal'] or row['response']:
                    raise TaskHandoffError('This task has stopped or ended; inspect its status')
                # This operation returns an existing acceptance. It does not
                # recapture instructions, dispatch work or change its model.
                return json.dumps({**self._metadata(row), 'accepted': True,
                    'existing_task': True, 'delivery': 'unobserved'})
            if operation == 'list':
                items = []
                for row in self.handoffs.recent(contact_id=self.owner):
                    self.sources.authorize_control(row['source'], scope)
                    items.append(self._metadata(row))
                roles = self.adapter.configured_task_model_roles() if self.adapter is not None else None
                return json.dumps({'items': items, 'view': 'retained_associations',
                    'complete_running_inventory': False,
                    'configured_model_roles': sorted(key for key in roles
                        if isinstance(key, str) and key.strip() and len(key) <= 256)
                        if isinstance(roles, dict) else []})
            if operation in {'submit', 'handoff'}:
                adapter = self.adapter
                if adapter is None or adapter.loop is None or not adapter.loop.is_running():
                    raise TaskHandoffError('The native task gateway is not connected')
                selected = adapter.select_task_model_role(args['model_role']) if 'model_role' in args else None
                source = self.sources.capture(scope)
                request_fields = [args['request'], {
                    key: value for key, value in source.items() if key != 'watermark'}]
                if selected is not None:
                    request_fields.append(args['model_role'])
                request_id = hashlib.sha256(json.dumps(request_fields,
                    sort_keys=True, separators=(',', ':')).encode()).hexdigest()
                origin = source.get('origin') or {}
                # Local owner permissions do not establish ordinary use.
                # Operators classify purpose through trusted admit() instead.
                ordinary = (scope.authority_lane == 'owner'
                    and getattr(scope, 'resolution_status', '') == 'resolved'
                    and origin.get('platform') == getattr(scope, 'platform', '')
                    and origin.get('platform') not in getattr(self.sources, 'attested_system_platforms', {'cli'}))
                observed_origin = (self.execution_observer.origin_context(origin, self.owner, [])
                    if self.execution_observer is not None else None)
                row = self.handoffs.admit(request_id=request_id, request=args['request'],
                    source_input=source, model_role=selected,
                    experience='operational' if ordinary else None,
                    origin_execution_id=(observed_origin['origin_execution_id'] if observed_origin else None))
                identity = row['id']
                observed = self._call('submit', identity)
                return json.dumps({'task_id': identity, 'accepted': True, 'executor': 'native_hermes',
                    **({'model_role': selected} if selected is not None else {}),
                    'native_admission': observed, 'callback_observed': observed is not None,
                    'status': 'queued', 'delivery': 'unobserved'})
            identity = args['task_id']
            row = self.handoffs.get(identity)
            self.sources.authorize_control(row['source'], scope)
            if operation == 'resume':
                observed = self._call('resume', identity, expected_turn_id=args['expected_turn_id'])
                return json.dumps({**self._metadata(self.handoffs.control(identity)),
                    'native_observation': observed, 'callback_observed': observed is not None})
            if operation == 'stop':
                retained = self.handoffs.request_stop(identity)
                if retained['response']:
                    return json.dumps({'task_id': identity, 'status': 'done', 'stop_requested': False})
                observed = self._call('stop', identity)
                return json.dumps({**self._metadata(self.handoffs.control(identity)),
                    'stop_requested': True, 'native_observation': observed})
            if operation == 'steer':
                source = self.sources.capture(scope)
                update = self.handoffs.admit_update(identity, instruction=args['request'],
                    source_input=source, principal=source['principal'])
                if update is None:
                    return json.dumps({**self._metadata(self.handoffs.control(identity)),
                        'accepted': False, 'reason': 'task_stopped_or_native_turn_terminal'})
                observed = self._call('steer', identity, update_id=update['id'])
                return json.dumps({'task_id': identity, 'executor': 'native_hermes',
                    **self.handoffs.update_view(self.handoffs.get_update(identity, update['id'])),
                    'native_observation': observed})
            observed = self._call('status', identity)
            if observed is not None:
                return json.dumps({'task_id': identity, 'executor': 'native_hermes', **observed})
            row, _, sources = self.handoffs.inspect_sources(identity)
            if row['response']:
                return json.dumps({'task_id': identity, **sources, **self.handoffs.response_view(row),
                    'delivery': {'retained': True, 'outward': 'unobserved'}})
            return json.dumps({**self._metadata(row), **sources})
        except Exception as error:
            # Retain a known association in the error so an ambiguous native
            # dispatch can be inspected, rather than submitted as another task.
            return json.dumps({'error': str(error) if isinstance(error, (TaskHandoffError, ValueError))
                else type(error).__name__, **({'task_id': identity} if identity else {}),
                'outcome': 'unconfirmed'})

    def finish_handoff(self, *, scope, tool_name, tool_call_id, tool_arguments, tool_result, **_):
        """Native invokes this only for the current successful persisted single-tool batch."""
        if (FinishTurn is None or tool_name not in {TOOL_SCHEMA['name'], 'tool_call'} or not tool_call_id
                or scope is None or not scope.valid_participant or scope.contact_id != self.owner
                or scope.authority_lane not in {'owner', 'system'}):
            return None
        try:
            args, result = json.loads(tool_arguments), json.loads(tool_result)
            if not isinstance(args, dict):
                return None
            if tool_name == 'tool_call':
                from tools.tool_search import resolve_underlying_call
                tool_name, args, error = resolve_underlying_call(args)
                if error or tool_name != TOOL_SCHEMA['name']:
                    return None
            if (args.get('operation') != 'handoff'
                    or not isinstance(result, dict) or result.get('accepted') is not True
                    or 'error' in result or result.get('executor') != 'native_hermes'):
                return None
            row = self.handoffs.get(result['task_id'])
            origin = row['source'].get('origin') or {}
            if (row['source']['contact_id'] != self.owner
                    or result.get('model_role') != row['model_role']
                    or any(origin.get(key) != getattr(scope, key, None)
                        for key in ('session_id', 'turn_id', 'platform'))):
                return None
            guidance = 'You can inspect or steer it using this task ID.'
            if 'task_id' in args:
                if (set(args) != {'operation', 'task_id'} or args['task_id'] != row['id']
                        or result.get('existing_task') is not True):
                    return None
                self.sources.authorize_control(row['source'], scope)
                row, _ = self.handoffs.resolve(row['id'])
                # The persisted tool result already accepted this handoff.
                # A later completion or stop does not undo that historical
                # receipt; current source and owner authority still apply.
                guidance = 'Inspect this task ID for its current status.'
            elif (row['request'] != args.get('request')
                    or args.get('model_role') != (row['model_role'] or {}).get('role')):
                return None
            return FinishTurn(text=f"Accepted task `{row['id']}`. {guidance}",
                              tool_call_id=tool_call_id)
        except (KeyError, TypeError, ValueError, TaskHandoffError):
            return None


def configured_tasks(client, outbox, owner_contact_id, *, config,
                     attested_system_platforms=('cli',)):
    """Load one explicit deployment factory, without fallback to another store."""
    specification = config.get('factory')
    if specification is None:
        return NativeTasks(client, outbox, owner_contact_id, state_path=config.get('state_path'),
                           attested_system_platforms=attested_system_platforms)
    if (not isinstance(specification, str) or specification.count(':') != 1
            or any(not part.isidentifier() for part in specification.replace(':', '.').split('.'))
            or not specification.split(':')[1].isidentifier()):
        raise ValueError('The native task factory must be an importable module:callable')
    module, name = specification.split(':')
    factory = getattr(importlib.import_module(module), name)
    controller = factory(client, outbox, owner_contact_id, config=dict(config))
    if not isinstance(controller, NativeTasks):
        raise TypeError('The configured native task factory must return NativeTasks')
    return controller
