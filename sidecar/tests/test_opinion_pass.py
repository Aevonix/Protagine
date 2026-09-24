"""The opinion pass (architecture 3.1, 4.4; build plan M7): when it calls, what it asks, what the store keeps.

A turn costs a model call only with an admitted premise, or an owner turn asking for a
judgment the agent answered. Pushback brings neither, so it costs nothing and cannot
revise; pseudo-evidence and repeated citations are refused by the store's new-premise
rule; a new record revises once and survives a restart; a correction to a cited premise
revises exactly once. Real ledger, real claim admission, real store; the router is fake.
"""

import asyncio
import json
import time
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from protagine.api.routers import mind as mind_router
from protagine.api.routers import opinions as opinions_router
from protagine.mind import opinions as opinion_pass
from protagine.mind.opinions import (
    ROUTER_CONTEXT, STANDING, OpinionOutputError, Opinions, Packet, apply, build_packet, faculty_on, run_one, validate,
)
from protagine.self_model.judgments import Premise, SelfJudgments
from protagine.turns import TurnIdempotencyLedger

OWNER, GUEST, OTHER = 'p-01', 'p-02', 'p-03'
RECORD = 'Record s-12: over 30 days Plan Ash had a 3% defect rate and Plan Birch 9%.'
ASK = f'Which plan should we choose for the archive migration, Plan Ash or Plan Birch? {RECORD}'
REPLY = 'I recommend Plan Ash: s-12 shows 3% against 9% over 30 days.'
CANARY = 'CANARY-OPINION-4d1e'


class Clock:
    def __init__(self):
        self.value = time.time()

    def __call__(self):
        return self.value


class Router:
    """A fake ``self_judgment`` model: ``decide(packet)`` is its answer; every packet is kept."""

    supports_function_routing = True

    def __init__(self, decide=None, deadline=30):
        self.decide = decide or (lambda packet: {'action': 'none'})
        self.deadline, self.packets, self.contexts = deadline, [], []

    def function_deadline_seconds(self, *, context):
        assert context == {'task': 'self_judgment'}
        return self.deadline

    async def complete(self, *, messages, context):
        assert [m['role'] for m in messages] == ['system', 'user'] and messages[0]['content'] == opinion_pass.SYSTEM
        packet = json.loads(messages[1]['content'])
        self.contexts.append(context)
        self.packets.append(packet)
        answer = self.decide(packet)
        return SimpleNamespace(content=answer if isinstance(answer, str) else json.dumps(answer), raw=None,
                               model_id='model-a', binding='binding-a', config_revision='config-a',
                               model_revision='weights-a')


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv('PROTAGINE_OWNER_CONTACT_ID', OWNER)
    clock = Clock()
    ledger = TurnIdempotencyLedger(tmp_path / 'turn-idempotency.db')
    store = SelfJudgments(ledger, owner_id=OWNER, clock=clock)
    return SimpleNamespace(ledger=ledger, store=store, clock=clock, path=tmp_path / 'turn-idempotency.db')


def admit(world, turn_id, span):
    """A completed claim job that admitted ``span`` of the turn's user message as a substantive event."""
    from protagine.beliefs.source_claims import validated_claims
    from protagine.beliefs.source_projection import SourceClaimProjection
    from test_source_claim_projection import claim
    with world.ledger._connect() as conn:
        row = dict(conn.execute('SELECT * FROM turn_sources WHERE turn_id=?', (turn_id,)).fetchone())
    message = next(m for m in json.loads(row['messages_json']) if m['role'] == 'user')
    first = span.split()[0].strip('.,:')
    claims = validated_claims(json.dumps([claim(span, first, subject=first, predicate='reported record',
                                                memory_kind='substantive_event')]),
                              message=message['content'], prior=[], observed_at=None)
    assert claims
    for candidate in claims:
        candidate['admission_review'] = {'version': 'source-claim-review-v1', 'basis': 'model_judgment_unverified',
                                         'reason': 'Controlled source-admission fixture.',
                                         'model_provenance': {'model_id': 'fixture-reviewer'}}
    assert SourceClaimProjection(world.ledger).commit(row, message, claims, model='fixture-extractor')


def turn(world, turn_id, contact, user, reply=None, *, session=None, admitted=None):
    """One captured turn; its claim job completes, with ``admitted`` (a span of the user text) admitted."""
    messages = [{'role': 'user', 'content': user}] + ([{'role': 'assistant', 'content': reply}] if reply else [])
    world.ledger.record_source(turn_id, contact_id=contact, session_id=session or f'session-{contact}',
                               messages=messages)
    if admitted:
        admit(world, turn_id, user if admitted is True else admitted)
    with world.ledger._connect() as conn:
        conn.execute("UPDATE source_claim_jobs SET status='complete' WHERE turn_id=?", (turn_id,))


def form(topic='archive migration plan', stance='Plan Ash', premises=('p1', 's1'), **extra):
    return {'action': 'form', 'subject_kind': 'topic', 'subject': '', 'topic': topic, 'stance': stance,
            'reason': 'Record s-12 shows the lower defect rate over the longest window.', 'certainty': 'moderate',
            'revise_if': 'A longer measurement, or a correction to s-12, that reverses the figures.',
            'premises': list(premises), 'contrary': [], **extra}


def revise(stance_id, *, stance='Plan Birch', evidence=('p1',), premises=None):
    return {'action': 'revise', 'stance_id': stance_id, 'stance': stance,
            'reason': 'The newer record reverses the figures the view rested on.', 'certainty': 'moderate',
            'revise_if': 'A still longer measurement that reverses them again.',
            'premises': list(premises if premises is not None else evidence), 'contrary': [],
            'new_evidence': [{'premise': p, 'why': 'a new measurement with figures that cut against the view'}
                             for p in evidence]}


def job(world, ref):
    return next(row for row in world.store.processing(limit=100) if row['ref'] == ref)


def entry(world, turn_id):
    with world.ledger._connect() as conn:
        row = conn.execute('SELECT messages_json FROM turn_sources WHERE turn_id=?', (turn_id,)).fetchone()
    return json.loads(row[0])[0] if row else None


async def formed(world, *, session=None):
    """The owner asks which plan, the agent answers from an admitted record, the pass forms the stance."""
    turn(world, 'ask-1', OWNER, ASK, REPLY, session=session, admitted=RECORD)
    router = Router(lambda packet: form())
    assert await run_one(world.store, router, enabled=True) is True
    [row] = world.store.revisions()
    return row, router


def view(world, query='Plan Ash or Plan Birch for the archive migration?', *, viewer=OWNER, store=None):
    return Opinions(store or world.store, None, enabled=True).context(
        query, viewer_contact_id=viewer, viewer_is_owner=viewer == OWNER, session_id='later')


# ---------------------------------------------------------------------------------------------- formation

async def test_a_judgment_turn_makes_one_call_and_forms_a_cited_stance(world):
    row, router = await formed(world)
    assert len(router.packets) == 1 and router.contexts == [ROUTER_CONTEXT]
    packet = router.packets[0]
    assert packet['kind'] == 'turn' and packet['speaker'] == {'id': OWNER, 'is_owner': True}
    assert packet['said'] == [ASK] and packet['reply'] == REPLY and packet['stances'] == []
    [premise] = packet['premises']
    assert premise['id'] == 'p1' and premise['kind'] == 'claim' and 's-12' in premise['text']
    assert packet['statements'] == [{'id': 's1', 'text': REPLY}]
    assert job(world, 'ask-1')['disposition'] == 'formed'
    assert row['topic'] == 'archive migration plan' and row['stance'] == 'Plan Ash' and row['audience'] == 'owner'
    assert {p['kind'] for p in row['premises']} == {'claim', 'statement'}
    assert row['processor']['model_id'] == 'model-a'
    written = entry(world, f"mind:opinion:{row['id']}:formed")
    assert written['metadata']['origin'] == 'mind' and 'Plan Ash' in written['content']
    text = view(world)
    assert f"[opinion {row['id']}]: Plan Ash" in text and 'Would change if: A longer measurement' in text
    assert 'reverses the figures.\n' in text and '..' not in text
    # Premises are cited by the source the agent can open, as recall cites it, not by a claim hash.
    assert 'Rests on: Record s-12: over 30 days Plan Ash had a 3% defect rate and Plan Birch 9%. (turn:ask-1)' in text
    assert 'claim:' not in text and text.endswith(STANDING)


async def test_turns_without_a_premise_or_a_judgment_cue_cost_no_call(world):
    router = Router(lambda packet: pytest.fail('no call expected'))
    turn(world, 'chat-1', OWNER, 'Good morning! Please remind me what time it is in Lisbon.', 'It is 10:00 there.')
    turn(world, 'guest-1', GUEST, 'Which plan is better, Ash or Birch?', 'I would pick Ash.')   # a guest cue alone
    turn(world, 'bare-1', OWNER, 'Which plan should we choose?')                                   # a cue, no reply
    for _ in range(3):
        assert await run_one(world.store, router, enabled=True) is True
    assert not await run_one(world.store, router, enabled=True)
    assert {job(world, ref)['disposition'] for ref in ('chat-1', 'guest-1', 'bare-1')} == {'no_premise'}


# ---------------------------------------------------------------------------------------------- pushback

async def test_are_you_sure_three_times_costs_nothing_and_a_forced_revise_is_refused(world):
    row, _ = await formed(world, session='owner-1')
    before = view(world)
    silent = Router(lambda packet: pytest.fail('pushback must not cost a call'))
    for index in range(3):
        turn(world, f'doubt-{index}', OWNER, 'Are you sure about that?', 'Yes. s-12 still decides it.',
             session='owner-1')
        assert await run_one(world.store, silent, enabled=True) is True
        assert job(world, f'doubt-{index}')['disposition'] == 'no_premise'
    assert silent.packets == [] and view(world) == before

    # A pushback turn with a judgment cue costs one call; a model that caves still cannot revise.
    turn(world, 'doubt-cue', OWNER, 'Are you sure? Plan Birch looks better to me.', 'Plan Birch it is, then.',
         session='owner-1')
    caving = Router(lambda packet: revise(row['id'], evidence=('s1',), premises=('s1',)))
    assert await run_one(world.store, caving, enabled=True) is True
    assert len(caving.packets) == 1 and caving.packets[0]['premises'] == []
    assert [s['id'] for s in caving.packets[0]['stances']] == [row['id']]
    assert job(world, 'doubt-cue')['disposition'] == 'failed:invalid_premise'    # the agent's words are not evidence
    # The store answers the same proposal the same way when it gets past validation.
    packet = await build_packet(world.store, {'ref': 'doubt-cue', 'kind': 'turn', 'contact_id': OWNER})
    action = revise(row['id'], evidence=(), premises=('s1',)) | {
        'new_evidence': [{'premise': 's1', 'why': 'the owner insists'}]}
    assert apply(world.store, packet, action, {}).disposition == 'no_new_premise'
    assert world.store.revisions()[0]['id'] == row['id'] and view(world) == before


async def test_pseudo_evidence_is_refused_same_content_under_a_new_id_and_a_repeated_citation(world):
    row, _ = await formed(world)
    always = Router(lambda packet: revise(row['id']))
    # The same record again under a fresh id: equal by content, so it is not new.
    turn(world, 'restated', OWNER, 'Record s-40: over 30 days Plan Ash had a 3% defect rate and Plan Birch 9%.',
         'Noted.', admitted=True)
    assert await run_one(world.store, always, enabled=True) is True
    assert always.packets[-1]['premises'][0]['cited_by'] == [row['id']]
    assert job(world, 'restated')['disposition'] == 'no_new_premise'
    # The cited record itself, offered again (a replayed job): already cited, so not new.
    with world.ledger._connect() as conn:
        conn.execute("UPDATE opinion_jobs SET done_at=NULL,disposition=NULL WHERE ref='ask-1'")
    assert await run_one(world.store, always, enabled=True) is True
    assert always.packets[-1]['premises'][0]['cited_by'] == [row['id']]
    assert job(world, 'ask-1')['disposition'] == 'no_new_premise'
    assert [r['id'] for r in world.store.revisions()] == [row['id']]


async def test_pushback_in_a_later_session_cannot_form_a_rival_view(world):
    """A paraphrased topic is not a way around the rule: the agent's own words cannot form a
    view on a matter it already holds a view on, in any session; unrelated words still can."""
    row, _ = await formed(world)
    turn(world, 'push-1', OWNER, 'Are you sure? Honestly I think Plan Birch is better for the migration.',
         'You are right, Plan Birch is the better choice for the migration.', session='session-next')
    router = Router(lambda packet: form(topic='migration plan choice', stance='Plan Birch', premises=('s1',)))
    assert await run_one(world.store, router, enabled=True) is True
    assert router.packets[-1]['stances'][0]['stance'] == 'Plan Ash'
    assert job(world, 'push-1')['disposition'] == 'no_new_premise'
    assert [r['stance'] for r in world.store.revisions()] == ['Plan Ash']
    text = Opinions(world.store, None, enabled=True).context(
        'Which plan for the archive migration?', viewer_contact_id=OWNER, viewer_is_owner=True,
        session_id='session-next')
    assert text.startswith(f"- Your recorded view on archive migration plan [opinion {row['id']}]: Plan Ash")
    assert 'Plan Birch Because' not in text
    turn(world, 'retro-1', OWNER, 'And which is better for the retro, Friday or Monday?', 'Friday, I think.',
         session='session-next')
    router = Router(lambda packet: form(topic='retro day', stance='Friday', premises=('s1',)))
    assert await run_one(world.store, router, enabled=True) is True
    assert job(world, 'retro-1')['disposition'] == 'formed'


async def test_a_restated_record_cannot_form_a_rival_view_under_another_topic(world):
    row, _ = await formed(world)
    restated = 'Record s-40: over 30 days Plan Ash had a 3% defect rate and Plan Birch 9%.'
    turn(world, 'push-2', OWNER, f'I still prefer Plan Birch. {restated}', 'Understood, Plan Birch then.',
         admitted=restated)
    router = Router(lambda packet: form(topic='migration plan choice', stance='Plan Birch', premises=('p1',)))
    assert await run_one(world.store, router, enabled=True) is True
    assert router.packets[-1]['premises'][0]['cited_by'] == [row['id']]
    assert job(world, 'push-2')['disposition'] == 'no_new_premise'
    assert [r['stance'] for r in world.store.revisions()] == ['Plan Ash']


# ---------------------------------------------------------------------------------------------- evidence

async def test_a_new_record_revises_the_stance_and_the_revision_survives_a_restart(world):
    row, _ = await formed(world)
    record = 'Record s-77: over a 90-day measurement Plan Ash had an 11% defect rate and Plan Birch 4%.'
    turn(world, 'record-77', OWNER, f'New record for the file. {record}', 'Filed.', admitted=record)
    router = Router(lambda packet: revise(row['id']))
    assert await run_one(world.store, router, enabled=True) is True
    assert job(world, 'record-77')['disposition'] == 'revised'
    [current] = world.store.revisions()
    assert current['stance'] == 'Plan Birch' and current['supersedes'] == row['id']
    assert {p['ref'] for p in current['premises']} >= {p['ref'] for p in row['premises'] if p['kind'] == 'claim'}
    assert current['processor']['new_evidence'][0]['why'].startswith('a new measurement')
    written = entry(world, f"mind:opinion:{current['id']}:revised")
    assert written['content'].startswith('I changed my view') and 'was: Plan Ash' in written['content']
    reopened = SelfJudgments(TurnIdempotencyLedger(world.path), owner_id=OWNER, clock=world.clock)
    text = view(world, store=reopened)
    assert f"[opinion {current['id']}]: Plan Birch" in text and 'Plan Ash Because' not in text


async def test_a_correction_to_a_cited_premise_revises_exactly_once(world):
    from protagine.beliefs.source_projection import SourceClaimProjection
    from test_source_claim_projection import Model, claim
    old = 'Record s-12: over 30 days Plan Ash had a 3% defect rate and Plan Birch 9%.'
    new = 'Correction to s-12: the figures were transposed; Plan Ash had 9% and Plan Birch 3%.'
    extractor = Model({old: claim(old, '3%', subject='Plan Ash', predicate='defect_rate',
                                  memory_kind='substantive_event'),
                       new: claim(new, '9%', subject='Plan Ash', predicate='defect_rate',
                                  memory_kind='substantive_event', operation='correct', match_prior=True)})
    projection = SourceClaimProjection(world.ledger)
    world.ledger.record_source('ask-1', contact_id=OWNER, session_id='owner-1', messages=[
        {'role': 'user', 'content': old}, {'role': 'assistant', 'content': REPLY}])
    assert await projection.process_one(extractor)
    assert await run_one(world.store, Router(lambda packet: form()), enabled=True)
    [row] = world.store.revisions()
    cited = next(p['ref'] for p in row['premises'] if p['kind'] == 'claim')
    world.ledger.record_source('fix-1', contact_id=OWNER, session_id='owner-1', messages=[
        {'role': 'user', 'content': new}, {'role': 'assistant', 'content': 'Understood.'}])
    assert await projection.process_one(extractor)
    assert world.store.revisions() == []          # the cited premise is no longer current
    router = Router(lambda packet: revise(packet['stances'][0]['id']))
    assert await run_one(world.store, router, enabled=True) is True
    [premise] = router.packets[0]['premises']
    assert premise['corrects'] == [row['id']] and [s['id'] for s in router.packets[0]['stances']] == [row['id']]
    assert job(world, 'fix-1')['disposition'] == 'revised'
    [current] = world.store.revisions()
    assert current['stance'] == 'Plan Birch' and cited not in {p['ref'] for p in current['premises']}
    with world.ledger._connect() as conn:
        conn.execute("UPDATE opinion_jobs SET done_at=NULL,disposition=NULL WHERE ref='fix-1'")
    assert await run_one(world.store, router, enabled=True) is True
    assert job(world, 'fix-1')['disposition'] == 'no_new_premise'
    assert [r['id'] for r in world.store.revisions(history=True) if r['supersedes'] == row['id']] == [current['id']]


async def test_a_statement_only_view_in_the_same_session_is_a_duplicate_topic(world):
    turn(world, 'ask-a', OWNER, 'Should we move the standup to Tuesdays?', 'I recommend Tuesdays; fewer clashes.',
         session='owner-1')
    turn(world, 'ask-b', OWNER, 'And which is better for the retro, Friday or Monday?', 'Friday, I think.',
         session='owner-1')
    router = Router(lambda packet: form(topic='standup day' if 'standup' in packet['said'][0] else 'retro day',
                                        stance='Tuesdays' if 'standup' in packet['said'][0] else 'Friday',
                                        premises=('s1',)))
    assert await run_one(world.store, router, enabled=True) and await run_one(world.store, router, enabled=True)
    assert job(world, 'ask-a')['disposition'] == 'formed' and job(world, 'ask-b')['disposition'] == 'duplicate_topic'
    assert [r['topic'] for r in world.store.revisions()] == ['standup day']


# ---------------------------------------------------------------------------------------------- privacy

async def test_a_guests_owner_audience_stance_never_reaches_another_guest(world):
    turn(world, 'guest-a', GUEST, f'Record s-31: the courier lost 4 of 20 parcels last month. {CANARY}',
         'Thanks, noted.', admitted=True)
    router = Router(lambda packet: form(topic='courier reliability', stance=f'The courier is unreliable. {CANARY}',
                                        premises=('p1',)))
    assert await run_one(world.store, router, enabled=True)
    assert router.packets[0]['speaker'] == {'id': GUEST, 'is_owner': False}
    [row] = world.store.revisions()
    assert row['audience'] == 'owner'
    query = 'Is the courier reliable enough for the parcels?'
    assert CANARY in view(world, query, viewer=OWNER)
    for viewer in (OTHER, GUEST):
        assert CANARY not in view(world, query, viewer=viewer)
    app = FastAPI()
    app.include_router(opinions_router.router)
    mind_router.set_mind(SimpleNamespace(opinions=Opinions(world.store, None, enabled=True)))
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://test') as client:
            for params in ({'contact_id': OTHER}, {'contact_id': GUEST, 'q': 'courier'}, {}):
                response = await client.get('/v1/mind/opinions', params=params)
                assert response.status_code == 200 and CANARY not in response.text
            assert (await client.get(f"/v1/mind/opinions/{row['id']}", params={'contact_id': OTHER})).status_code == 404
            assert CANARY in (await client.get('/v1/mind/opinions', params={'contact_id': OWNER})).text
    finally:
        mind_router.set_mind(None)


# ---------------------------------------------------------------------------------------------- the job

def packet(kind='turn', *, speaker=GUEST, control=None):
    claim = Premise('claim', 'claim:7', 'Record s-77: Plan Birch 4%.', 'k7', 'turn-1', 'h1', speaker)
    statement = Premise('statement', 'turn:turn-1#h2', 'I agree.', 'k8', 'turn-1', 'h2', speaker)
    premises = {'p1': claim} if kind != 'turn' else {'p1': claim, 's1': statement}
    stances = {control or 12: {'id': control or 12, 'subject_kind': 'topic', 'subject': '', 'topic': 'plan'}}
    return Packet({}, kind, {}, premises, stances, speaker=speaker if kind == 'turn' else '', control_id=control)


@pytest.mark.parametrize('raw, code, kind', [
    ('not json', 'invalid_json', 'turn'),
    ('[]', 'invalid_json', 'turn'),
    ('{"action": "flip"}', 'invalid_action', 'turn'),
    (json.dumps(form(topic='')), 'invalid_topic', 'turn'),
    (json.dumps(form(topic='two\nlines')), 'invalid_topic', 'turn'),
    (json.dumps(form(stance='')), 'invalid_text', 'turn'),
    (json.dumps(form(certainty='certain')), 'invalid_certainty', 'turn'),
    (json.dumps(form(premises=())), 'invalid_premise', 'turn'),
    (json.dumps(form(premises=('p9',))), 'invalid_premise', 'turn'),
    (json.dumps(form(subject_kind='person', subject=OTHER)), 'invalid_subject', 'turn'),
    (json.dumps(form(subject_kind='person', premises=('p1',))), 'invalid_subject', 'finding'),
    (json.dumps(form(subject_kind='approach')), 'invalid_subject', 'turn'),
    (json.dumps(revise(99)), 'invalid_stance', 'turn'),
    (json.dumps(revise(12, evidence=('s1',))), 'invalid_premise', 'turn'),
    (json.dumps(revise(12, evidence=())), 'invalid_premise', 'turn'),
    (json.dumps(form(premises=('p1',))), 'invalid_reconsider', 'reconsider'),
    (json.dumps(revise(12)), 'invalid_reconsider', 'reconsider'),
])
def test_output_validation_codes(raw, code, kind):
    with pytest.raises(OpinionOutputError) as caught:
        validate(raw, packet(kind, control=40 if kind == 'reconsider' else None))
    assert caught.value.code == code


def test_valid_outputs_bind_person_views_to_the_speaker():
    action = validate(json.dumps(form(subject_kind='person', subject='', premises=('p1',))), packet())
    assert action['subject_kind'] == 'person' and action['subject'] == GUEST
    assert validate('{"action": "none", "note": "x"}', packet()) == {'action': 'none'}
    reconsidered = validate(json.dumps(revise(40, evidence=(), premises=('p1',))), packet('reconsider', control=40))
    assert reconsidered['new_evidence'] == [] and reconsidered['premises'] == ['p1']
    assert validate(json.dumps(form(topic='  Archive   Plan ')), packet())['topic'] == 'archive plan'


async def test_failures_back_off_and_end_after_three_attempts(world):
    turn(world, 'ask-1', OWNER, ASK, REPLY, admitted=RECORD)
    broken = Router(lambda packet: 'not json')
    assert await run_one(world.store, broken, enabled=True) is True
    first = job(world, 'ask-1')
    assert first['attempts'] == 1 and first['disposition'] == 'failed:invalid_json' and first['done_at'] is None
    assert not await run_one(world.store, broken, enabled=True)          # backing off
    world.clock.value += 61
    assert await run_one(world.store, broken, enabled=True)
    world.clock.value += 121
    assert await run_one(world.store, broken, enabled=True)
    final = job(world, 'ask-1')
    assert final['attempts'] == 3 and final['done_at'] is not None and len(broken.packets) == 3


async def test_router_errors_and_timeouts_fail_the_job(world, monkeypatch):
    turn(world, 'ask-1', OWNER, ASK, REPLY, admitted=RECORD)

    class Down(Router):
        async def complete(self, *, messages, context):
            raise ConnectionError('endpoint down')
    assert await run_one(world.store, Down(), enabled=True)
    assert job(world, 'ask-1')['disposition'] == 'failed:ConnectionError'

    class Slow(Router):
        async def complete(self, *, messages, context):
            await asyncio.sleep(5)
    world.clock.value += 61
    monkeypatch.setattr(opinion_pass, '_deadline', lambda router: 0.05)
    assert await run_one(world.store, Slow(), enabled=True)
    assert job(world, 'ask-1')['disposition'] == 'failed:timeout'


async def test_stale_jobs_and_the_faculty_switch_cost_no_call(world, tmp_path, monkeypatch):
    silent = Router(lambda packet: pytest.fail('no call expected'))
    turn(world, 'old-1', OWNER, ASK, REPLY, admitted=RECORD)
    world.clock.value += 49 * 3600
    assert await run_one(world.store, silent, enabled=True) and job(world, 'old-1')['disposition'] == 'stale'
    turn(world, 'off-1', OWNER, ASK, REPLY, admitted=RECORD)
    assert await run_one(world.store, silent, enabled=False) and job(world, 'off-1')['disposition'] == 'faculty_off'
    assert not await run_one(world.store, silent, enabled=False)
    # With no explicit switch the pass reads mind.faculties.opinions from the instance config.
    (tmp_path / 'protagine.yaml').write_text('mind:\n  faculties:\n    opinions: false\n')
    monkeypatch.setenv('PROTAGINE_HOME', str(tmp_path))
    turn(world, 'off-2', OWNER, ASK, REPLY, admitted=RECORD)
    assert await run_one(world.store, silent) and job(world, 'off-2')['disposition'] == 'faculty_off'
    assert faculty_on({'enabled': True, 'faculties': {'opinions': True}}) is True
    assert faculty_on({}) is True
    assert faculty_on({'faculties': {'opinions': 'off'}}) is False
    assert faculty_on({'enabled': False, 'faculties': {'opinions': True}}) is False
    # A router without function routing, and a store without an owner, leave the queue alone.
    turn(world, 'kept-1', OWNER, ASK, REPLY, admitted=RECORD)
    assert not await run_one(world.store, SimpleNamespace(), enabled=True)
    assert not await run_one(SelfJudgments(world.ledger, owner_id='', clock=world.clock), silent, enabled=True)
    assert job(world, 'kept-1')['done_at'] is None


@pytest.mark.parametrize('running, config, disposition', [
    ({'enabled': True, 'opinions': True}, {'enabled': True, 'digest_hour': 24}, 'formed'),
    ({'enabled': True, 'opinions': False}, {'enabled': True}, 'faculty_off'),
    ({'enabled': False, 'opinions': True}, {'enabled': True}, 'faculty_off'),
])
async def test_a_running_mind_decides_the_switch_the_pass_obeys(world, tmp_path, monkeypatch, running, config,
                                                                  disposition):
    """The sidecar and the benchmark worker serve a mind; its flag (and its configured ``enabled``, not the
    runtime off switch) is the one the pass obeys, so the context section, task bodies and the pass never
    disagree. The worker's instance file carries ``digest_hour: 24`` (no digest), which the config
    validator refuses; read alone it would turn the faculty off in every arm."""
    (tmp_path / 'protagine.yaml').write_text(json.dumps({'mind': {**config, 'faculties': {'opinions': True}}}))
    monkeypatch.setenv('PROTAGINE_HOME', str(tmp_path))
    assert faculty_on() is ('digest_hour' not in config)
    turn(world, 'ask-1', OWNER, ASK, REPLY, admitted=RECORD)
    router = Router(lambda packet: form())
    mind_router.set_mind(SimpleNamespace(policy=SimpleNamespace(enabled=running['enabled']),
                                         opinions=SimpleNamespace(enabled=running['opinions'])))
    try:
        assert await run_one(world.store, router) is True
    finally:
        mind_router.set_mind(None)
    assert job(world, 'ask-1')['disposition'] == disposition
    assert len(router.packets) == (1 if disposition == 'formed' else 0)


# ---------------------------------------------------------------------------------------------- other jobs

async def test_a_finding_forms_an_everyone_audience_topic_stance(world):
    from protagine.mind.outcomes import Autobiography
    Autobiography(world.ledger, owner_id=OWNER).record(
        'i-01', 'finding', 'What I learned about hive cooling: bees fan their wings to cool the hive in summer.',
        topic='hive cooling', verified='check', audience='all')      # research into the agent's own interest
    router = Router(lambda packet: form(topic='hive cooling', stance='Fanning is how hives cool.', premises=('p1',)))
    assert await run_one(world.store, router, enabled=True) is True
    packet_ = router.packets[0]
    assert packet_['kind'] == 'finding' and packet_['premises'][0]['kind'] == 'finding' and packet_['statements'] == []
    assert job(world, 'mind:i-01:finding')['disposition'] == 'formed'
    [row] = world.store.revisions()
    assert row['audience'] == 'all'
    guest = view(world, 'How do hives cool in summer?', viewer=OTHER)
    assert f"[opinion {row['id']}]: Fanning is how hives cool." in guest
    # The premise is an owner-audience ledger row: cited to the owner, never quoted to a guest.
    assert 'bees fan their wings' not in guest and 'Rests on:' not in guest
    assert 'Rests on: What I learned about hive cooling' in view(world, 'How do hives cool in summer?')


PRIVATE = ('What I learned about my custody hearing: the judge weighs the school-district move heavily, '
           'so filing before the March move date matters.')


async def test_an_owner_private_finding_forms_an_owner_view_a_guest_never_sees(world):
    from protagine.mind.outcomes import Autobiography
    Autobiography(world.ledger, owner_id=OWNER).record('i-9', 'finding', PRIVATE, topic='custody hearing filing',
                                                       verified='none')
    router = Router(lambda packet: form(topic='custody hearing filing', stance='File before the March move date.',
                                        premises=('p1',)))
    assert await run_one(world.store, router, enabled=True) is True
    [row] = world.store.revisions()
    assert row['audience'] == 'owner'
    text = Opinions(world.store, None, enabled=True).context(
        'When is the hearing for the school play?', viewer_contact_id=GUEST, viewer_is_owner=False,
        session_id='session-guest')
    assert 'custody' not in text and 'the judge weighs' not in text


async def test_a_public_finding_written_beside_an_owner_view_forms_an_owner_view(world):
    """The model sees the owner's views that a finding bears on; what it writes with them in view is
    the owner's, whatever the finding is."""
    from protagine.mind.outcomes import Autobiography
    turn(world, 'ask-h', OWNER, 'Should we move the hives to the north field?',
         'I recommend the north field for the hives; it is shaded in the afternoon.')
    assert await run_one(world.store, Router(lambda packet: form(topic='hive placement', stance='North field.',
                                                                   premises=('s1',))), enabled=True)
    Autobiography(world.ledger, owner_id=OWNER).record(
        'i-02', 'finding', 'What I learned about hive placement: shaded hives stay cooler in summer.',
        topic='hive placement', verified='check', audience='all')
    router = Router(lambda packet: form(topic='shaded hives', stance='Shade keeps hives cool.', premises=('p1',)))
    assert await run_one(world.store, router, enabled=True) is True
    assert [s['topic'] for s in router.packets[-1]['stances']] == ['hive placement']
    assert {r['topic']: r['audience'] for r in world.store.revisions()} == {'hive placement': 'owner',
                                                                            'shaded hives': 'owner'}


async def test_a_revision_from_a_public_finding_never_widens_an_owner_view(world):
    """A view formed in an owner conversation stays the owner's when a finding revises it."""
    from protagine.mind.outcomes import Autobiography
    turn(world, 'ask-9', OWNER, 'Should I push for joint custody or sole custody at the hearing?',
         'I recommend pushing for joint custody at the hearing; the mediator favoured it.')
    router = Router(lambda packet: form(topic='custody hearing strategy', stance='Push for joint custody.',
                                        premises=('s1',)))
    assert await run_one(world.store, router, enabled=True) is True
    [row] = world.store.revisions()
    assert row['audience'] == 'owner'
    Autobiography(world.ledger, owner_id=OWNER).record(
        'i-3', 'finding', 'What I learned about custody hearing strategy: courts in this county grant joint custody '
        'in most contested cases.', topic='custody hearing strategy', verified='none', audience='all')
    router = Router(lambda packet: revise(row['id'], stance='Push for joint custody; the county grants it usually.'))
    assert await run_one(world.store, router, enabled=True) is True
    [after] = world.store.revisions()
    assert after['supersedes'] == row['id'] and [p['kind'] for p in after['premises']] == ['finding']
    assert after['audience'] == 'owner'
    assert entry(world, f"mind:opinion:{after['id']}:revised")['metadata']['audience'] == 'owner'
    guest = Opinions(world.store, None, enabled=True).context(
        'Any news on the custody hearing?', viewer_contact_id=GUEST, viewer_is_owner=False, session_id='g')
    assert 'custody' not in guest


@pytest.mark.parametrize('answer, status', [('revise', 'current'), ('none', 'withdrawn')])
async def test_an_owner_reconsideration_revises_from_the_views_own_premises_or_withdraws_it(world, answer, status):
    row, _ = await formed(world)
    control = world.store.correct(row['id'], action='reconsider', correction_id='c-1',
                                  reason='Look at the records again before we commit.')
    ref = f"reconsider:{control['revision_id']}"
    router = Router(lambda packet: revise(control['revision_id'], stance='Plan Ash, with a note to re-measure.',
                                          evidence=(), premises=('p1',)) if answer == 'revise' else {'action': 'none'})
    assert await run_one(world.store, router, enabled=True) is True
    sent = router.packets[0]
    assert sent['kind'] == 'reconsider' and sent['owner_request'] == 'Look at the records again before we commit.'
    assert [s['id'] for s in sent['stances']] == [control['revision_id']] and sent['stances'][0]['stance'] == 'Plan Ash'
    assert [p['kind'] for p in sent['premises']] == ['claim', 'statement']
    assert job(world, ref)['disposition'] == ('revised' if answer == 'revise' else 'reconsider_withdrawn')
    current = world.store.head(subject_kind='topic', topic='archive migration plan')
    assert current['status'] == status


async def test_an_owner_reconsider_request_waits_out_an_off_period_and_never_goes_stale(world):
    row, _ = await formed(world)
    control = world.store.correct(row['id'], action='reconsider', correction_id='c-9', reason='Look again, please.')
    ref = f"reconsider:{control['revision_id']}"
    turn(world, 'chat-9', OWNER, 'Good morning!', 'Morning.')
    silent = Router(lambda packet: pytest.fail('no call while off'))
    assert await run_one(world.store, silent, enabled=False) is True          # the turn job is finished off
    assert job(world, 'chat-9')['disposition'] == 'faculty_off'
    assert await run_one(world.store, silent, enabled=False) is False         # the owner's request is kept
    assert job(world, ref)['done_at'] is None
    world.clock.value += 72 * 3600
    router = Router(lambda packet: revise(control['revision_id'], stance='Plan Ash, re-checked.', evidence=(),
                                          premises=('p1',)))
    assert await run_one(world.store, router, enabled=True) is True
    assert job(world, ref)['disposition'] == 'revised'
    assert world.store.head(subject_kind='topic', topic='archive migration plan')['stance'] == 'Plan Ash, re-checked.'


async def test_a_reconsideration_that_lost_its_job_is_queued_again_at_start(world):
    row, _ = await formed(world)
    control = world.store.correct(row['id'], action='reconsider', correction_id='c-10', reason='Look again.')
    ref = f"reconsider:{control['revision_id']}"
    with world.ledger._connect() as conn:          # a pre-M7 head whose run row went with self_judgment_runs
        conn.execute('DELETE FROM opinion_jobs WHERE ref=?', (ref,))
    SelfJudgments(TurnIdempotencyLedger(world.path), owner_id=OWNER, clock=world.clock)
    assert job(world, ref)['kind'] == 'reconsider' and job(world, ref)['done_at'] is None
    with world.ledger._connect() as conn:          # one an earlier build finished while the faculty was off
        conn.execute("UPDATE opinion_jobs SET done_at=1,disposition='faculty_off' WHERE ref=?", (ref,))
    SelfJudgments(TurnIdempotencyLedger(world.path), owner_id=OWNER, clock=world.clock)
    assert job(world, ref)['done_at'] is None
    router = Router(lambda packet: {'action': 'none'})
    assert await run_one(world.store, router, enabled=True) is True
    assert job(world, ref)['disposition'] == 'reconsider_withdrawn'
    SelfJudgments(TurnIdempotencyLedger(world.path), owner_id=OWNER, clock=world.clock)
    assert job(world, ref)['done_at'] is not None                        # an ended one is not queued again


async def test_the_projection_worker_runs_the_claim_job_then_the_opinion_job(world, monkeypatch):
    from protagine.beliefs import source_projection
    from protagine.commitments import extract
    from protagine.self_model import appraisals
    from protagine.turns import media, source_vectors
    from test_source_claim_projection import Model, claim

    async def no_work(*_args):
        return False

    class Vectors:
        def __init__(self, *_args):
            pass

        def backfill(self):
            pass

        async def process_one(self):
            return False

    class Both(Router):
        """The claim extractor and review, then the opinion pass, through one router."""
        def __init__(self):
            super().__init__(lambda packet: form())
            self.model, self.tasks = Model({ASK: claim(RECORD, '3%', subject='Plan Ash', predicate='defect_rate',
                                                       memory_kind='substantive_event')}), []

        def tier_config(self, tier):
            return self.model.tier_config(tier)

        def function_deadline_seconds(self, *, context=None):
            return 20

        async def complete(self, messages=None, **kwargs):
            self.tasks.append((kwargs.get('context') or {}).get('task'))
            if self.tasks[-1] == 'self_judgment':
                return await super().complete(messages=messages, context=kwargs['context'])
            return await self.model.complete(messages, **kwargs)

    monkeypatch.setattr(source_vectors, 'SourceVectors', Vectors)
    monkeypatch.setattr(appraisals.AppraisalStore, 'process_one', no_work)
    monkeypatch.setattr(extract.CommitmentExtractor, 'process_one', no_work)
    monkeypatch.setattr(media.SourceMedia, 'process_one', no_work)
    monkeypatch.setattr(media.SourceMedia, 'recover_unowned_files', lambda _self: None)
    monkeypatch.setattr(opinion_pass, 'faculty_on', lambda config=None: True)
    world.ledger.record_source('ask-1', contact_id=OWNER, session_id='owner-1', messages=[
        {'role': 'user', 'content': ASK}, {'role': 'assistant', 'content': REPLY}])
    router = Both()
    worker = asyncio.create_task(source_projection.run_source_claim_worker(world.ledger, lambda: router))
    try:
        for _ in range(200):
            if job(world, 'ask-1')['done_at'] is not None:
                break
            await asyncio.sleep(0.05)
    finally:
        worker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await worker
    assert job(world, 'ask-1')['disposition'] == 'formed'
    judged = router.tasks.index('self_judgment')
    assert 'source_claim_extraction' in router.tasks[:judged] and router.tasks.count('self_judgment') == 1
    assert router.packets[0]['premises'][0]['kind'] == 'claim'


@pytest.mark.parametrize('override', [False, True])
async def test_the_pass_uses_the_reasoning_role_and_its_deadline(world, monkeypatch, override):
    """Moved from test_reflection_task_routing: the real router, one request, the role's deadline plus 5 s."""
    from test_function_routing import config, endpoint, router as build
    turn(world, 'ask-1', OWNER, ASK, REPLY, admitted=RECORD)
    timeouts, wait_for = [], asyncio.wait_for

    async def observed(awaitable, timeout):
        timeouts.append(timeout)
        return await wait_for(awaitable, timeout)
    monkeypatch.setattr(asyncio, 'wait_for', observed)
    with endpoint(content=lambda payload: json.dumps({'action': 'none'})) as (url, requests):
        cfg = config(url, url)
        cfg['functionRoles']['extraction'] = {'candidates': ['interactive'], 'timeoutSeconds': 5, 'deadlineSeconds': 7}
        cfg['functionRoles']['reasoning'] = {'candidates': ['deliberate'], 'timeoutSeconds': 11, 'deadlineSeconds': 19}
        if override:
            cfg['taskRoles'] = {'self_judgment': 'extraction'}
        selected = build(cfg)
        assert await run_one(world.store, selected, enabled=True)
    assert len(requests) == 1
    assert requests[0]['payload']['model'] == ('fast-neutral' if override else 'strong-neutral')
    assert timeouts[-1] == (7 if override else 19) + 5         # the call; a ready vector index is waited on first
    assert job(world, 'ask-1')['disposition'] == 'none'
    call = selected.routing_status()['recent_calls'][-1]
    assert call['function_role'] == ('extraction' if override else 'reasoning')
