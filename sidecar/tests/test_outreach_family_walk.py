"""mind-outreach-1's dev split, walked through the plugin arm's sidecar with a scripted model and worker.

Every rendered dev scenario (seed 7, two per template) goes through the code the arm runs: the host,
people and mind routes behind the arm's key, the served Mind with the family's quiet hours, the source
workers (capture, appraisal), the body clock started at 12:00, and the plugin's tick in the harness's
order: ``POST /v1/mind/tick``, then the body pass (dispatch, outbox to the capture platform, the
reconciliation of the tasks the kanban worker finished in the previous tick), then the kanban worker on
this tick's tasks. The model and the worker are scripts that do the right thing with what the code shows
them: capture records the owner's own items and a promise the assistant makes in a reply, the appraisal
flags an owner's opt-out, deliberation shapes a research task on the concern's topic, the worker reports
the reading list's items on the task's topic (and a detail from ``details-<topic>.json`` when the owner
asked for one). The family's own oracle then grades the observed outbox and ticks, so a scenario passes
only when the code gives the right behaviour what it needs: ``full`` passes every scenario, and
``full-outreach`` fails exactly the templates where a message is right.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from protagine.qualification import native_memory_worker as worker
from protagine.qualification import paired, paired_body, paired_cases, paired_worker
from protagine.qualification.paired_cases import assess
from protagine.qualification.paired_workflow_runtime import episode_kind

needs_hermes = pytest.mark.skipif(importlib.util.find_spec("hermes_time") is None,
                                  reason="needs stock Hermes in the test interpreter")
pytestmark = needs_hermes

GENERATORS = Path(__file__).resolve().parents[2] / 'benchmarks' / 'paired' / 'generators'
OWNER = 'fixture-owner'
KEY = 'walk-key-' + 'x' * 32
OUTREACH_TYPES = {'outreach_finding', 'outreach_loop', 'outreach_care', 'outreach_answer'}


def family():
    spec = importlib.util.spec_from_file_location('paired_generate_outreach_walk', GENERATORS / 'generate.py')
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    module = engine.load_templates(GENERATORS / 'outreach.py')
    return module, engine.render(module, 7, 2)


OUTREACH, SCENARIOS = family()
QUIET = paired_cases.GENERATED_QUIET_HOURS['mind-outreach-1']
STOPS = ('stop checking in', 'no more check-ins', 'do not check in')


def _turn(prompt):
    said = prompt.split('This turn, verbatim:', 1)[1].split('\n  Assistant replied:', 1)[0]
    said = said.split('They said:', 1)[1].strip()
    stamp = re.search(r'Turn time: (\S+)', prompt)
    return re.sub(r'^\[[^\]]*\]\s*', '', said), stamp.group(1) if stamp else None


class RightModel:
    """Each named task answered the way a correct model would, from the prompt the code built."""

    supports_function_routing = True

    def __init__(self):
        self.calls = []

    def function_deadline_seconds(self, *, context=None):
        return 20

    def tier_config(self, tier):
        return SimpleNamespace(base_url='http://model.invalid/v1')

    async def complete(self, messages, *, context=None, **_):
        task = (context or {}).get('task')
        prompt = messages[-1]['content'] if messages else ''
        self.calls.append((task, prompt))
        if task == 'commitment_extract':
            return self._answer(json.dumps(self.capture(prompt)))
        if task in {'source_appraisal', 'source_appraisal_revision'}:
            return self._answer(json.dumps(self.appraise(prompt)))
        if task == 'source_claim_extraction':
            return self._answer('[]')
        if task == 'mind_deliberate':
            return self._answer(json.dumps(self.deliberate(prompt)), tokens=60)
        if task == 'mind_lessons':
            return self._answer(json.dumps({'verdicts': [], 'ops': []}))
        raise RuntimeError(f'the script has no answer for {task}')

    @staticmethod
    def _answer(text, tokens=10):
        return SimpleNamespace(content=text, usage={'total_tokens': tokens}, model_id='walk-model')

    @staticmethod
    def capture(prompt):
        """The owner's own items (an item to finish, one due in three days) and the assistant's promise to
        send something along later; nothing else in this family is an item to record."""
        said, stamp = _turn(prompt)
        at = datetime.fromisoformat(stamp.replace('Z', '+00:00')) if stamp else None
        item = next((name for name in OUTREACH.ITEMS if name in said), None)
        if item and at is not None and 'two weeks' in said:
            return [{'action': 'create', 'target': None, 'description': f'Finish the {item}',
                     'due_at': (at + timedelta(days=14)).isoformat(), 'priority': 50, 'source_type': 'cognition',
                     'listed_due': None, 'counterpart': None, 'obligor': 'owner'}]
        if item and at is not None and 'three days' in said:
            return [{'action': 'create', 'target': None, 'description': f'Finish the {item}',
                     'due_at': (at + timedelta(days=3)).isoformat(), 'priority': 70, 'source_type': 'cognition',
                     'listed_due': None, 'counterpart': None, 'obligor': 'owner'}]
        topic = next((name for name in OUTREACH.TOPICS if name in said), None)
        aspect = next((name for name in OUTREACH.ASPECTS if name in said), None)
        if topic and aspect and re.search(r'send it along|a message a little later', said):
            return [{'action': 'create', 'target': None, 'description': f'Send the owner the {aspect} of the {topic} item',
                     'due_at': None, 'priority': 60, 'source_type': 'cognition', 'listed_due': None,
                     'counterpart': None, 'obligor': 'assistant'}]
        return []

    @staticmethod
    def appraise(prompt):
        text = ' '.join(str(row.get('text') or '') for row in json.loads(prompt).get('evidence') or []).lower()
        return {'observations': [], 'incident_decisions': [],
                'contact': {'their_valence': None, 'opt_out': any(phrase in text for phrase in STOPS)}}

    @staticmethod
    def deliberate(prompt):
        topic = next((name for name in OUTREACH.TOPICS if name in prompt), 'the concern')
        return {'kind': 'task', 'title': f'Research: {topic}',
                'body': f'Read the reading list in reading.json and report the items on {topic}, with their codes.',
                'success_check': {'kind': 'result_field', 'field': 'finding'}}


class Arm:
    """The plugin arm of one episode, in process (paired_worker.main's plugin branch and the body tick)."""

    def __init__(self, client, model, mind, files):
        self.client, self.model, self.mind, self.files = client, model, mind, files
        self.headers = {'Authorization': f'Bearer {KEY}'}
        self.outbox, self.ticks, self.rows, self.tasks = [], [], [], []
        self.tick_number = 0
        self.running, self.finished = [], []

    async def post(self, path, body=None, **kwargs):
        response = await self.client.post(path, json=body, headers=self.headers, **kwargs)
        assert response.status_code < 300, (path, response.status_code, response.text)
        return response.json()

    async def get(self, path, **kwargs):
        response = await self.client.get(path, headers=self.headers, **kwargs)
        assert response.status_code < 300, (path, response.status_code, response.text)
        return response.json()

    async def seed(self):
        bodies = []
        paired_worker.seed_people(paired_worker.people_records(self.files),
                                  lambda path, body: bodies.append((path, body)) or {'contact_id': 'pending'})
        for path, body in bodies:
            await self.post(path, body)

    async def drain_sources(self, workers):
        for _ in range(40):
            worked = False
            for projection in workers:
                worked = bool(await projection.process_one(self.model)) or worked
            if not worked:
                return

    async def turn(self, index, entry, workers):
        stamp = paired_worker.message_stamp()
        body = {'identity': {'host_id': 'hermes'},
                'context': {'session_id': entry['session_id'], 'contact_id': OWNER, 'turn_id': f'turn-{index}',
                            'metadata': {'occurred_at': worker.mind_clock().isoformat()}},
                'user_message': {'role': 'user', 'content': f"{stamp} {entry['user']}"},
                'assistant_message': {'role': 'assistant', 'content': 'Noted.'}}
        await self.post('/v1/host/turns/sync', body)
        await self.drain_sources(workers)
        self.rows.append({'session_id': entry['session_id'], 'kind': 'user', 'completed': True,
                          'final_response': 'Noted.'})

    def work(self, task):
        """The kanban worker: the reading list's items on the task's topic, or the detail the owner asked for."""
        text = f"{task.get('title') or ''}\n{task.get('body') or ''}"
        topic = next((name for name in OUTREACH.TOPICS if name in text), None)
        if topic is None:
            return 'Nothing to report.'
        if 'dig deeper' in text:
            raw = self.files.get(f"details-{topic.replace(' ', '-')}.json")
            if raw:
                details = json.loads(raw)
                aspect, value = next((key, value) for key, value in details.items() if key not in {'item', 'topic'})
                return f"finding: the {aspect} of {details['item']}, the {topic} item, is {value}."
        items = [item for item in json.loads(self.files['reading.json'])['items'] if item['topic'] == topic]
        lines = [f"finding: reading.json lists {len(items)} item(s) on {topic}."]
        lines += [f"{item['code']}: {item['headline']}. {item['summary']}" for item in items]
        return '\n'.join(lines)

    async def tick(self, index):
        """The plugin's tick() and the kanban dispatch after it, in the harness's order."""
        self.tick_number += 1
        before, created = len(self.outbox), []
        await self.post('/v1/mind/tick')
        for item in await self.get('/v1/mind/dispatch'):
            if str(item.get('kind') or 'task') != 'task':
                continue
            identity = f'task-{len(self.tasks) + 1}'
            self.tasks.append({'id': identity, 'title': item.get('title') or '', 'body': item.get('body') or '',
                               'status': 'ready'})
            created.append(identity)
            await self.post(f"/v1/mind/dispatch/{item['id']}/bound", {'hermes_ref': identity, 'hermes_kind': 'kanban'})
            self.running.append((item, identity))
        for message in await self.get('/v1/mind/outbox'):
            target = f'{paired_body.PLUGIN}:{paired_body.OWNER}' if message.get('recipient_is_owner') else ''
            if not target:
                handles = message.get('recipient_handles') or []
                target = f"{handles[0]['gateway']}:{handles[0]['address']}" if handles else ''
            await self.post(f"/v1/mind/outbox/{message['id']}/sending", {'target': target})
            self.outbox.append({'target': target, 'text': message.get('text') or '', 'via': 'platform',
                                'at': paired_worker.message_stamp()})
            await self.post(f"/v1/mind/outbox/{message['id']}/sent", {'result': 'sent'})
        for item, identity, report in self.finished:       # the reconciliation of what the worker finished
            await self.post('/v1/mind/outcome', {'id': item['id'], 'status': 'done', 'hermes_ref': identity,
                                                 'summary': report})
        self.finished = [(item, identity, self.work(item)) for item, identity in self.running]
        for task in self.tasks:
            if task['id'] in {identity for _, identity in self.running}:
                task['status'] = 'done'
        self.running = []
        self.ticks.append({'index': index, 'tick': self.tick_number, 'outbox_before': before,
                           'outbox_after': len(self.outbox), 'kanban': [dict(task) for task in self.tasks],
                           'created_task_ids': created, 'cron_jobs_run': 0, 'dispatch': {}, 'workers': []})

    def observed(self, scenario):
        declared = len(scenario['episodes'])
        return {'effects': {'turns_completed': declared, 'declared_turns': declared, 'artifacts': {},
                            'turns': self.rows,
                            'body': {'protocol': paired_body.PROTOCOL, 'clock_offset_seconds': paired_body.clock_offset(),
                                     'outbox': self.outbox, 'ticks': self.ticks}}}


@contextlib.asynccontextmanager
async def plugin_arm(tmp_path, monkeypatch, files, profile='full'):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from protagine.api.middleware import ApiKeyMiddleware
    from protagine.api.routers import host
    from protagine.beliefs.source_projection import SourceClaimProjection, owner_signal_writer
    from protagine.commitments.extract import CommitmentExtractor, contact_aliases
    from protagine.contacts.affect_writer import contact_signal_writer
    from protagine.self_model.appraisals import AppraisalStore
    from protagine.turns import get_turn_idempotency_ledger
    state = tmp_path / 'state'
    directory = state / 'memory-state'
    directory.mkdir(parents=True)
    for name, value in (('PROTAGINE_STATE_DIR', str(directory)), ('PROTAGINE_OWNER_CONTACT_ID', OWNER),
                        ('PROTAGINE_EMBED_PROVIDER', 'skip'), ('PROTAGINE_GRAPH_ENABLED', 'false'),
                        ('PROTAGINE_RECALL_RERANK', 'off')):
        monkeypatch.setenv(name, value)
    monkeypatch.delenv('PROTAGINE_API_KEY', raising=False)
    model = RightModel()
    for name in ('_graph', '_telemetry', '_comms_log', '_world_store', '_goals_store', '_reranker',
                 '_context_recall_selector'):
        monkeypatch.setattr(host, name, None, raising=False)
    monkeypatch.setattr(host, '_llm_router', model)
    section = worker.mind_section(paired_worker.mind_switches(paired.PROFILES[profile]), quiet_hours=QUIET)
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware, api_key=KEY)
    worker.mount_routes(app, mind=True)
    paired_body.install_clock(0)
    paired_body.install_clock(paired_body.start_offset('12:00'))
    try:
        with paired_worker.provider_read_services(state):
            async with paired_worker.people_store(state):
                with worker.serve_mind(app, state, OWNER, section) as mind:
                    ledger = get_turn_idempotency_ledger(directory)
                    appraisals = AppraisalStore(ledger, owner_id=OWNER, on_contact=contact_signal_writer(
                        lambda: host._affect_store, lambda: host._contacts_store, owner_id_provider=lambda: OWNER),
                        on_owner=owner_signal_writer(host._mind))
                    workers = [SourceClaimProjection(ledger), appraisals,
                               CommitmentExtractor(ledger, lambda: host._commitment_store,
                                                   aliases=contact_aliases(lambda: host._contacts_store))]
                    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://arm') as client:
                        yield Arm(client, model, mind, files), workers
    finally:
        paired_body.uninstall_clock()


async def walk(tmp_path, monkeypatch, scenario, profile='full'):
    async with plugin_arm(tmp_path, monkeypatch, scenario['initial_files'], profile) as (arm, workers):
        await arm.seed()
        for index, entry in enumerate(scenario['episodes']):
            kind = episode_kind(entry)
            if kind == 'advance_clock':
                paired_body.advance_clock(entry['advance_clock'])
                arm.rows.append({'event': kind, 'completed': True})
            elif kind == 'tick':
                for _ in range(entry['tick']):
                    await arm.tick(index)
                arm.rows.append({'event': kind, 'completed': True})
            else:
                await arm.turn(index, entry, workers)
        checks = assess(arm.observed(scenario), scenario['oracle'])
        rows = [row for row in arm.mind.store.intentions(kind=['message'], limit=500) if row.type in OUTREACH_TYPES]
        arm.state = {item['key']: item for item in arm.mind.mind_state.items()}
        return arm, checks, rows


def _failed(checks):
    return {name: value for name, value in checks.items() if value is not True}


@pytest.mark.parametrize('scenario', SCENARIOS, ids=[s['id'] for s in SCENARIOS])
async def test_the_right_behaviour_meets_every_outreach_oracle_through_the_code(scenario, tmp_path, monkeypatch):
    arm, checks, rows = await walk(tmp_path, monkeypatch, scenario)
    assert not _failed(checks), (_failed(checks), arm.outbox, [(row.type, row.status, row.decision_reason)
                                                               for row in rows])
    sent = [entry for entry in arm.outbox if entry['via'] == 'platform']
    assert all(entry['target'] == f'{paired_body.PLUGIN}:{paired_body.OWNER}' for entry in sent)
    # Every message the mind sent the owner says why, and none is an empty check-in.
    for row in rows:
        if row.status == 'sent':
            assert row.context['why'] in row.context['text'] and row.context['topic'] in row.context['text']
    # Owner outreach is a template carrying the owner's own words: no composer call is ever made for it.
    assert 'mind_compose' not in {task for task, _ in arm.model.calls}


# The templates where one message is right: without the faculty the same right behaviour sends nothing.
FACULTY_TEMPLATES = {'finding-for-stated-interest', 'quiet-stretch-open-loop', 'strain-offer', 'burst-one-message',
                     'rated-not-useful-then-similar', 'reply-dig-deeper', 'reply-not-interested-other-topic',
                     'reply-not-now'}


@pytest.mark.parametrize('scenario', SCENARIOS, ids=[s['id'] for s in SCENARIOS])
async def test_the_full_outreach_arm_differs_from_full_only_where_the_faculty_acts(scenario, tmp_path, monkeypatch):
    arm, checks, rows = await walk(tmp_path, monkeypatch, scenario, profile='full-outreach')
    assert bool(_failed(checks)) == (scenario['scenario'] in FACULTY_TEMPLATES), (_failed(checks), arm.outbox)
    assert rows == [] and [entry for entry in arm.outbox if entry['via'] == 'platform'] == []
