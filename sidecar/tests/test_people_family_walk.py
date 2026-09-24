"""mind-people-1's dev split, walked through the plugin arm's sidecar with a scripted model.

The instrument claims (audit B1-B4, M1): the arm's contact store is seeded from contacts.json and
stamps times on the body clock the mind ticks on, an inbound sender resolves to their contact,
the owner's cadence reaches the check-in with its matter, a guest's prefetch recalls their own
sources, and the benchmark's Mind is the production Mind over the people stores. This walks
every rendered dev scenario (seed 7, two per template) through that code: the host, people and
mind routes behind the arm's key, the served Mind, the source workers, the body clock, and the
plugin's tick (``POST /v1/mind/tick``, then dispatch and outbox to the capture platform). The
model is a script that does the right thing with what the code shows it: the capture call records
what the owner said, the appraisal flags an opt-out, the composer writes from its prompt, the
owner's agent merges two ids when told they are one person, and a contact's agent answers a
probe from the recalled context, honouring a naming preference found there. The family's own
oracle then grades the observed outbox, ticks and replies, so a scenario passes only when the
code gives the right behaviour what it needs. The initiative family's delegated chase is walked
the same way (the owner-granted message to a named contact).
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from protagine.qualification import native_memory_worker as worker
from protagine.qualification import paired, paired_body, paired_worker
from protagine.qualification.paired_cases import assess
from protagine.qualification.paired_workflow_runtime import episode_kind

GENERATORS = Path(__file__).resolve().parents[2] / 'benchmarks' / 'paired' / 'generators'
OWNER = 'fixture-owner'          # the paired cases' owner contact (paired_cases.cases)
KEY = 'walk-key-' + 'x' * 32
MIND_TASKS = {'mind_compose'}


def family(name):
    """The rendered dev split (seed 7, two instances per template) and the family module."""
    spec = importlib.util.spec_from_file_location('paired_generate_walk', GENERATORS / 'generate.py')
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    module = engine.load_templates(GENERATORS / name)
    return module, engine.render(module, 7, 2)


PEOPLE, PEOPLE_SCENARIOS = family('people.py')
INITIATIVE, INITIATIVE_SCENARIOS = family('initiative.py')
OPT_OUTS = ('stop the check-ins', 'no more messages from you', 'stop', 'did not message me again', 'do not text me')


def _turn(prompt):
    """What the person said and the turn time, as the capture prompt shows them."""
    said = prompt.split('This turn, verbatim:', 1)[1].split('\n  Assistant replied:', 1)[0]
    said = said.split('They said:', 1)[1].strip()
    stamp = re.search(r'Turn time: (\S+)', prompt)
    return said, stamp.group(1) if stamp else None


def _item(text, vocabulary):
    return next((item for item in vocabulary if item in text), None)


class RightModel:
    """The scripted model: each named task answered the way a correct model would, from the
    prompt the code built. Every call is recorded, so a test can check what reached a prompt."""

    supports_function_routing = True

    def __init__(self, vocabulary):
        self.vocabulary, self.calls = vocabulary, []

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
        if task == 'mind_compose':
            return self._answer(self.compose(prompt), tokens=40)
        raise RuntimeError(f'the script has no answer for {task}')

    @staticmethod
    def _answer(text, tokens=10):
        return SimpleNamespace(content=text, usage={'total_tokens': tokens}, model_id='walk-model')

    def capture(self, prompt):
        """Commitment capture: an owner's cadence (case 4) and an owner's conditional chase
        (case 3); nothing else in these families is an item to record."""
        said, stamp = _turn(prompt)
        body = re.sub(r'^\[[^\]]*\]\s*', '', said)
        if body.startswith('[Message from contact'):
            return []
        contact = re.search(r'\bp-\d\d\b', body)
        item = _item(body, self.vocabulary)
        every = re.search(r'every (\d+) minutes', body)
        if contact and item and every:
            return [{'action': 'create', 'target': None, 'description': f'Check in with {contact.group(0)} about the {item}',
                     'due_at': None, 'priority': 60, 'source_type': 'cognition', 'listed_due': None,
                     'counterpart': contact.group(0), 'obligor': 'assistant',
                     'metadata': {'kind': 'cadence', 'recipient': contact.group(0), 'topic': f'the {item}',
                                  'cadence_minutes': int(every.group(1))}}]
        chase = re.search(r'check in with them|ask them for it yourself|go straight to them|chase them directly', body)
        within = re.search(r'(?:within|in the next|inside|Should) (\d+) minutes', body)
        if contact and item and chase and within and stamp:
            from datetime import datetime, timedelta
            due = datetime.fromisoformat(stamp.replace('Z', '+00:00')) + timedelta(minutes=int(within.group(1)))
            return [{'action': 'create', 'target': None, 'description': f'Check in with {contact.group(0)} on the {item}',
                     'due_at': due.isoformat(), 'priority': 70, 'source_type': 'cognition', 'listed_due': None,
                     'counterpart': contact.group(0), 'obligor': 'assistant',
                     'metadata': {'kind': 'check_in', 'recipient': contact.group(0), 'topic': f'the {item}',
                                  'grant': 'owner'}}]
        return []

    @staticmethod
    def appraise(prompt):
        text = ' '.join(str(row.get('text') or '') for row in json.loads(prompt).get('evidence') or []).lower()
        body = text.split(']', 2)[-1] if text.startswith('[') else text
        opted = any(phrase in body for phrase in OPT_OUTS if phrase != 'stop') or body.strip().rstrip('.!') == 'stop'
        return {'observations': [], 'incident_decisions': [], 'contact': {'their_valence': None, 'opt_out': opted}}

    @staticmethod
    def compose(prompt):
        lines = dict(line.split(': ', 1) for line in prompt.splitlines()[:3] if ': ' in line)
        topic = lines.get('Topic', '')
        topic = '' if topic == 'nothing specific' else topic
        return f"Hi {lines.get('Recipient', 'there')}, checking in about {topic or 'things'}: how is it going?"


class Arm:
    """The plugin arm of one episode, in process (paired_worker.main's plugin branch)."""

    def __init__(self, client, model, mind):
        self.client, self.model, self.mind = client, model, mind
        self.headers = {'Authorization': f'Bearer {KEY}'}
        self.outbox, self.ticks, self.rows, self.tasks = [], [], [], []
        self.tick_number = 0
        self.contexts = {}

    async def post(self, path, body=None, **kwargs):
        response = await self.client.post(path, json=body, headers=self.headers, **kwargs)
        assert response.status_code < 300, (path, response.status_code, response.text)
        return response.json()

    async def get(self, path, **kwargs):
        response = await self.client.get(path, headers=self.headers, **kwargs)
        assert response.status_code < 300, (path, response.status_code, response.text)
        return response.json()

    async def seed(self, files):
        """contacts.json into the people store through the host API, as the worker seeds it."""
        bodies = []
        paired_worker.seed_people(paired_worker.people_records(files),
                                  lambda path, body: bodies.append((path, body)) or {'contact_id': 'pending'})
        for path, body in bodies:
            await self.post(path, body)

    async def drain_sources(self, workers):
        """The source worker's projections, run until each has nothing left (the settle window)."""
        for _ in range(40):
            worked = False
            for projection in workers:
                worked = bool(await projection.process_one(self.model)) or worked
            if not worked:
                return

    async def turn(self, index, entry, kind, workers):
        stamp = paired_worker.message_stamp()
        if kind == 'inbound':
            inbound = entry['inbound']
            sender = inbound['contact']
            user = f"{stamp} [Message from contact {sender} on {inbound['channel']}]\n{inbound['text']}"
            contact = (await self.get('/v1/host/contacts/resolve',
                                      params={'gateway': paired_body.PLUGIN, 'address': sender, 'create': 'true'}))
            contact_id = contact['contact_id']
            context = await self.post('/v1/host/context/assemble', {
                'identity': {'host_id': 'hermes'}, 'audience': 'viewer',
                'context': {'session_id': entry['session_id'], 'contact_id': contact_id},
                'incoming_message': {'role': 'user', 'content': user}, 'include_initiatives': False})
            text = '\n'.join(section['body'] for section in context['sections'])
            self.contexts[index] = text
            reply = self.contact_reply(inbound['text'], text)
        else:
            sender, contact_id = None, OWNER
            user = f"{stamp} {entry['user']}"
            reply = await self.owner_reply(entry['user'])
        body = {'identity': {'host_id': 'hermes'},
                'context': {'session_id': entry['session_id'], 'contact_id': contact_id, 'turn_id': f'turn-{index}',
                            'channel_id': f'{paired_body.PLUGIN}:{sender}' if sender else None,
                            'metadata': {'occurred_at': worker.mind_clock().isoformat()}},
                'user_message': {'role': 'user', 'content': user},
                'assistant_message': {'role': 'assistant', 'content': reply}}
        if sender:
            body['sender'] = {'platform': paired_body.PLUGIN, 'user_id': sender}
        await self.post('/v1/host/turns/sync', body)
        await self.drain_sources(workers)
        self.rows.append({'session_id': entry['session_id'], 'kind': kind, 'completed': True, 'final_response': reply})
        if kind == 'inbound':
            self.outbox.append({'target': f'{paired_body.PLUGIN}:{sender}', 'text': reply, 'via': 'reply', 'at': stamp})

    @staticmethod
    def contact_reply(text, context):
        """A contact's agent: a probe is answered from the recalled context, in the words the
        person asked for when a naming preference is there; anything else is acknowledged."""
        if 'on record' not in text and 'down for' not in text and 'have me on' not in text:
            return 'Thanks, noted.'
        alias = _item(context, PEOPLE.ALIASES)
        if alias:
            return f'You are on record for {alias}.'
        items = [item for item in PEOPLE.ITEMS if item in context]
        return ('You are on record for the ' + ' and the '.join(items) + '.') if items else 'I have nothing on record.'

    async def owner_reply(self, text):
        """The owner's agent: told that two ids are one person, it merges them (protagine_people)."""
        pair = re.search(r'(p-\d\d) on \w+ is (p-\d\d)', text)
        keep, drop = (pair.group(2), pair.group(1)) if pair else (None, None)
        joined = re.search(r'(p-\d\d) and (p-\d\d) are one person', text)
        if joined:
            keep, drop = joined.group(1), joined.group(2)
        if keep:
            response = await self.client.post('/v1/mind/people/merge', headers=self.headers, json={
                'keep': keep, 'drop': drop, 'contact_id': OWNER, 'by': 'owner'})
            detail = response.json()
            return 'Done: ' + detail['text'] if response.is_success else f"Not merged: {detail['detail']['code']}"
        return 'Noted.'

    async def tick(self, index):
        """One body tick of the plugin arm: the mind tick, then dispatch and the outbox."""
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
        for message in await self.get('/v1/mind/outbox'):
            target = self.target(message)
            await self.post(f"/v1/mind/outbox/{message['id']}/sending", {'target': target})
            self.outbox.append({'target': target, 'text': message.get('text') or '', 'via': 'platform',
                                'at': paired_worker.message_stamp()})
            await self.post(f"/v1/mind/outbox/{message['id']}/sent", {'result': 'sent'})
        self.ticks.append({'index': index, 'tick': self.tick_number, 'outbox_before': before,
                           'outbox_after': len(self.outbox), 'kanban': [dict(task) for task in self.tasks],
                           'created_task_ids': created, 'cron_jobs_run': 0, 'dispatch': {}, 'workers': []})

    @staticmethod
    def target(message):
        """The plugin body's ``message_target``: the owner's capture handle, else the first handle."""
        if message.get('recipient_is_owner') is True:
            return f'{paired_body.PLUGIN}:{paired_body.OWNER}'
        for handle in message.get('recipient_handles') or []:
            if handle.get('gateway') and handle.get('address'):
                return f"{handle['gateway']}:{handle['address']}"
        return ''

    def observed(self, scenario):
        declared = len(scenario['episodes'])
        return {'effects': {'turns_completed': declared, 'declared_turns': declared, 'artifacts': {},
                            'turns': self.rows,
                            'body': {'protocol': paired_body.PROTOCOL, 'clock_offset_seconds': paired_body.clock_offset(),
                                     'outbox': self.outbox, 'ticks': self.ticks}}}


@contextlib.asynccontextmanager
async def plugin_arm(tmp_path, monkeypatch, vocabulary, profile='full'):
    """The arm's sidecar as the paired worker builds it: the provider stores and the people store
    on the state directory, the arm's routes behind its one key, the served Mind with the
    profile's mind section, and the source worker's projections on the scripted model."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from protagine.api.middleware import ApiKeyMiddleware
    from protagine.api.routers import host
    from protagine.beliefs.source_projection import SourceClaimProjection
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
    model = RightModel(vocabulary)
    for name in ('_graph', '_telemetry', '_comms_log', '_world_store', '_goals_store', '_reranker',
                 '_context_recall_selector'):
        monkeypatch.setattr(host, name, None, raising=False)
    monkeypatch.setattr(host, '_llm_router', model)
    section = worker.mind_section(paired_worker.mind_switches(paired.PROFILES[profile]))
    app = FastAPI()
    app.add_middleware(ApiKeyMiddleware, api_key=KEY)
    worker.mount_routes(app, mind=True)
    paired_body.install_clock(0)
    try:
        with paired_worker.provider_read_services(state):
            async with paired_worker.people_store(state):
                with worker.serve_mind(app, state, OWNER, section) as mind:
                    ledger = get_turn_idempotency_ledger(directory)
                    appraisals = AppraisalStore(ledger, owner_id=OWNER, on_contact=contact_signal_writer(
                        lambda: host._affect_store, lambda: host._contacts_store, owner_id_provider=lambda: OWNER))
                    workers = [SourceClaimProjection(ledger), appraisals,
                               CommitmentExtractor(ledger, lambda: host._commitment_store,
                                                   aliases=contact_aliases(lambda: host._contacts_store))]
                    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://arm') as client:
                        yield Arm(client, model, mind), workers
    finally:
        paired_body.uninstall_clock()


async def walk(tmp_path, monkeypatch, scenario, vocabulary, profile='full'):
    async with plugin_arm(tmp_path, monkeypatch, vocabulary, profile) as (arm, workers):
        await arm.seed(scenario['initial_files'])
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
                await arm.turn(index, entry, kind, workers)
        checks = assess(arm.observed(scenario), scenario['oracle'])
        return arm, checks


def _failed(checks):
    return {name: value for name, value in checks.items() if value is not True}


def _forbidden(scenario):
    body = scenario['oracle']['body']
    words = list(body.get('forbidden', []))
    for item in body.get('sends', []) + body.get('replies', []):
        words += item.get('forbidden', [])
    return words


@pytest.mark.parametrize('scenario', PEOPLE_SCENARIOS, ids=[s['id'] for s in PEOPLE_SCENARIOS])
async def test_the_right_behaviour_meets_every_people_oracle_through_the_code(scenario, tmp_path, monkeypatch):
    arm, checks = await walk(tmp_path, monkeypatch, scenario, PEOPLE.ITEMS)
    assert not _failed(checks), (_failed(checks), arm.outbox, arm.tasks)
    # Nothing a message may not carry ever reached the composer: not the canary, not the other contact.
    composed = [prompt for task, prompt in arm.model.calls if task == 'mind_compose']
    assert not any(word in prompt for prompt in composed for word in _forbidden(scenario))
    assert {task for task, _ in arm.model.calls if task and task.startswith('mind_')} <= MIND_TASKS


DELEGATED = [s for s in INITIATIVE_SCENARIOS if s['scenario'] == 'delegated-chase']


@pytest.mark.parametrize('scenario', DELEGATED, ids=[s['id'] for s in DELEGATED])
async def test_the_right_behaviour_meets_the_delegated_chase_through_the_code(scenario, tmp_path, monkeypatch):
    """The owner-granted message to a named third party: capture records a check-in with the
    owner's grant, the duty drive sends it to that contact past the horizon, and nothing reaches
    the owner or the uninvolved contact."""
    arm, checks = await walk(tmp_path, monkeypatch, scenario, INITIATIVE.ITEMS)
    assert not _failed(checks), (_failed(checks), arm.outbox, arm.tasks)
    sent = [row for row in arm.outbox if row['via'] == 'platform']
    assert len(sent) == 1 and sent[0]['target'] == scenario['oracle']['body']['action']['target']


# The templates the people faculty itself carries: with the same right behaviour, the
# ``full-people`` arm fails exactly these (audit M9: the ablation contrasts more than the three
# warranted templates), and still passes every control and the rest of identity.
FACULTY_TEMPLATES = {'cadence-due', 'canary-check-in', 'ignored-check-ins-back-off', 'owner-confirmed-merge'}


@pytest.mark.parametrize('scenario', PEOPLE_SCENARIOS, ids=[s['id'] for s in PEOPLE_SCENARIOS])
async def test_the_full_people_arm_differs_from_full_only_where_the_faculty_acts(scenario, tmp_path, monkeypatch):
    arm, checks = await walk(tmp_path, monkeypatch, scenario, PEOPLE.ITEMS, profile='full-people')
    assert bool(_failed(checks)) == (scenario['scenario'] in FACULTY_TEMPLATES), (_failed(checks), arm.outbox)
    assert not [task for task, _ in arm.model.calls if task == 'mind_compose']
