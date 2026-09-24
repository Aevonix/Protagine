"""The one opinion store: premises, the new-premise rule, the soft limit, audience,
relevance, owner controls, erasure, the queue and the appraisal migration (store level)."""
from dataclasses import replace
import json
import sqlite3
import sys
import time

import pytest

from protagine.self_model.judgments import (
    ENTRY_PREFIX, LIMIT_WINDOW_S, Premise, Proposal, SelfJudgments, content_key, head_key, initialize,
)
from protagine.turns import TurnIdempotencyLedger
from protagine.turns.idempotency import source_message_hash


class Clock:
    def __init__(self):
        # Close to wall time: the ledger hook stamps queued jobs with wall time.
        self.value = float(int(time.time()))

    def __call__(self):
        return self.value


@pytest.fixture
def judgments(tmp_path, monkeypatch):
    monkeypatch.setenv('PROTAGINE_OWNER_CONTACT_ID', 'contact-a')
    clock = Clock()
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    return SelfJudgments(ledger, owner_id='contact-a', clock=clock), clock


def admit_source(judgments, turn, *, memory_kind='substantive_event'):
    """Controlled completed upstream admission for opinion fixtures."""
    from protagine.beliefs.source_claims import validated_claims
    from protagine.beliefs.source_projection import SourceClaimProjection
    from test_source_claim_projection import claim
    with judgments.ledger._connect() as conn:
        row = dict(conn.execute('SELECT * FROM turn_sources WHERE turn_id=?', (turn,)).fetchone())
    for message in json.loads(row['messages_json']):
        text = message.get('content')
        if message.get('role') != 'user' or not isinstance(text, str):
            continue
        first = text.split()[0].strip('.,')
        claims = validated_claims(json.dumps([claim(text, first, subject=first, predicate='reported context',
            memory_kind=memory_kind)]),
            message=text, prior=[], observed_at=None)
        assert claims
        for candidate in claims:
            candidate['admission_review'] = {'version': 'source-claim-review-v1',
                'basis': 'model_judgment_unverified', 'reason': 'Controlled source-admission fixture.',
                'model_provenance': {'model_id': 'fixture-reviewer'}}
        assert SourceClaimProjection(judgments.ledger).commit(row, message, claims, model='fixture-extractor')
    with judgments.ledger._connect() as conn:
        conn.execute("UPDATE source_claim_jobs SET status='complete' WHERE turn_id=?", (turn,))


def source(judgments, turn='first', text='Long local work lost progress after an interruption. Checkpoints could help.', **kwargs):
    admitted = kwargs.pop('admitted', True)
    memory_kind = kwargs.pop('memory_kind', 'substantive_event')
    reply = kwargs.pop('reply', None)
    messages = [{'role': 'user', 'content': text}] + ([{'role': 'assistant', 'content': reply}] if reply else [])
    judgments.ledger.record_source(turn, contact_id=kwargs.pop('contact_id', 'contact-a'),
        session_id=kwargs.pop('session_id', 'session-' + turn), messages=messages, **kwargs)
    if admitted:
        admit_source(judgments, turn, memory_kind=memory_kind)


def premise(kind, ref, text, **kwargs):
    return Premise(kind, ref, text, content_key(text), **kwargs)


def proposal(premises, **kwargs):
    values = {'subject_kind': 'topic', 'subject': '', 'topic': 'local work checkpoints',
              'stance': 'I favor explicit checkpoints for long local work.',
              'reason': 'The reported interruption shows a recovery benefit that outweighs the small cost.',
              'certainty': 'tentative', 'revise_if': 'a measured run shows the cost exceeds the recovery',
              'premises': list(premises), 'source_ref': 'turn:first', 'session_id': 'session-first'}
    return Proposal(**(values | kwargs))


def finding(judgments, intention='int-1', text='Research found that staged checkpoints recover long renders.'):
    turn = f'mind:{intention}:finding'
    judgments.ledger.record_source(turn, contact_id='contact-a', session_id='mind', derive_claims=False,
        messages=[{'role': 'assistant', 'content': text,
                   'metadata': {'origin': 'mind', 'intention_id': intention, 'event': 'finding'}}])
    return turn


def entry(judgments, stance_id, event):
    with judgments.ledger._connect() as conn:
        row = conn.execute('SELECT * FROM turn_sources WHERE turn_id=?', (f'{ENTRY_PREFIX}{stance_id}:{event}',)).fetchone()
        indexed = conn.execute('SELECT count(*) FROM turn_source_search WHERE turn_id=?',
                               (f'{ENTRY_PREFIX}{stance_id}:{event}',)).fetchone()[0]
    return (dict(row) | {'message': json.loads(row['messages_json'])[0], 'indexed': indexed}) if row else None


def job(judgments, ref):
    with judgments.ledger._connect() as conn:
        row = conn.execute('SELECT * FROM opinion_jobs WHERE ref=?', (ref,)).fetchone()
    return dict(row) if row else None


def formed(judgments, turn='first', **kwargs):
    result = judgments.form(proposal(judgments.admitted_premises(turn), source_ref='turn:' + turn,
                                     session_id='session-' + turn, **kwargs))
    assert result.disposition == 'formed', result
    return result.stance_id


def test_contact_turn_forms_an_owner_audience_stance_with_an_entry_that_survives_reopen(judgments):
    state, _ = judgments
    source(state, 'guest-turn', 'Staged exports recovered twice after the build host crashed.', contact_id='contact-b')
    premises = state.admitted_premises('guest-turn')
    assert [(p.kind, p.contact_id) for p in premises] == [('claim', 'contact-b')] and premises[0].ref.startswith('claim:')
    result = state.form(proposal(premises, topic='staged exports', source_ref='turn:guest-turn', session_id='session-guest-turn'))
    assert result.disposition == 'formed'
    row = state.get(result.stance_id)
    assert (row['audience'], row['status'], row['session_id'], row['source_turn_id']) == (
        'owner', 'current', 'session-guest-turn', 'guest-turn')
    assert row['premises'][0]['ref'] == premises[0].ref and row['revise_if']
    written = entry(state, result.stance_id, 'formed')
    assert (written['contact_id'], written['session_id'], written['scope']) == ('contact-a', 'mind', 'person')
    assert written['message']['metadata'] == {'origin': 'mind', 'event': 'opinion_formed', 'opinion_id': result.stance_id,
        'audience': 'owner', 'subject_kind': 'topic', 'subject': ''}
    assert 'I formed a view on staged exports' in written['message']['content'] and written['indexed']
    reopened = SelfJudgments(TurnIdempotencyLedger(state.ledger.db_path), owner_id='contact-a', clock=state.clock)
    assert reopened.revisions() == [row]
    assert reopened.head(subject_kind='topic', topic=' Staged  Exports ')['id'] == result.stance_id


def test_new_premise_rule_refuses_statements_repeats_and_restatements_but_revises_on_new_evidence(judgments):
    state, _ = judgments
    source(state, 'first', 'Inspection s-12 found 3 of 40 seals cracked after the long run without checkpoints.')
    stance = formed(state)
    source(state, 'pushback', 'Are you sure? I think checkpoints are useless.', admitted=False,
           reply='You are right, checkpoints are useless for long work.')
    said = state.statements('pushback')
    assert [p.kind for p in said] == ['statement'] and state.admitted_premises('pushback') == []
    doubt = state.revise(stance, proposal(said, stance='Checkpoints are useless.', new_evidence=[said[0].ref],
                                          source_ref='turn:pushback'))
    assert doubt.disposition == 'no_new_premise'
    cited = state.admitted_premises('first')
    assert state.revise(stance, proposal(cited, new_evidence=[cited[0].ref])).disposition == 'no_new_premise'
    source(state, 'restated', 'Inspection s-40 found 3 of 40 seals cracked after the long run without checkpoints.')
    restated = state.admitted_premises('restated')
    assert restated[0].ref != cited[0].ref and restated[0].key == cited[0].key
    assert state.revise(stance, proposal(restated, new_evidence=[restated[0].ref])).disposition == 'no_new_premise'
    assert [r['id'] for r in state.revisions()] == [stance]
    source(state, 'measured', 'Measured short runs lost nothing and checkpoints tripled their time.')
    new = state.admitted_premises('measured')
    result = state.revise(stance, proposal([replace(p, role='contrary') for p in new] + cited,
        stance='I favor checkpoints for long work only.', new_evidence=[new[0].ref], source_ref='turn:measured'))
    assert result.disposition == 'revised'
    row = state.get(result.stance_id)
    assert row['supersedes'] == stance and {p['ref'] for p in row['premises']} == {cited[0].ref, new[0].ref}
    assert {p['role'] for p in row['premises'] if p['ref'] == new[0].ref} == {'contrary'}
    text = entry(state, result.stance_id, 'revised')['message']['content']
    assert 'I changed my view on local work checkpoints: now I favor checkpoints for long work only.' in text
    assert '(was: I favor explicit checkpoints for long local work.)' in text and 'Measured short runs' in text
    # Re-forming the same topic is a revision, so the rule cannot be dodged.
    assert state.form(proposal(new, source_ref='turn:measured')).disposition == 'no_new_premise'
    assert [r['id'] for r in state.revisions()] == [result.stance_id]


def test_a_revision_rests_first_on_its_new_evidence_and_not_on_the_words_it_replaced(judgments):
    """The revised view is read with what now supports it: the new evidence first, then the data
    the old view rested on (kept, so it can never come back as new). The agent's earlier words
    stay a dependency, so forgetting them still tombstones the chain, but they are not a premise
    of the view that replaced them."""
    state, _ = judgments
    source(state, 'said', 'Which should we use for long local work?', admitted=False,
           reply='I favor explicit checkpoints for long local work.')
    source(state, 'first', 'Inspection s-12 found 3 of 40 seals cracked after the long run without checkpoints.')
    stance = state.form(proposal(state.statements('said') + state.admitted_premises('first'), source_ref='turn:said',
                                 session_id='session-said')).stance_id
    source(state, 'measured', 'Measured short runs lost nothing and checkpoints tripled their time.')
    new = state.admitted_premises('measured')
    revised = state.revise(stance, proposal(new, stance='I favor checkpoints for long work only.',
                                            new_evidence=[new[0].ref], source_ref='turn:measured')).stance_id
    row = state.get(revised)
    assert [(p['kind'], p['ref']) for p in row['premises']] == [
        ('claim', new[0].ref), ('claim', state.admitted_premises('first')[0].ref)]
    # The view it replaced reads as replaced, wherever it is read by id.
    assert state.get(stance)['status'] == 'superseded' and state.get(stance)['stance'] == (
        'I favor explicit checkpoints for long local work.')
    assert [(r['id'], r['status']) for r in state.revisions(history=True)] == [(revised, 'current'), (stance, 'superseded')]
    state.ledger.erase_sources(contact_id='contact-a', turn_ids=['said'])
    assert state.revisions() == [] and state.get(revised)['status'] == 'erased'


@pytest.mark.asyncio
async def test_a_correction_to_a_cited_claim_revises_once_inside_the_limit(tmp_path):
    from protagine.beliefs.source_projection import SourceClaimProjection
    from test_source_claim_subject_basis import CORRECTION, ORIGINAL, add
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path / 'sources.db'))
    state = SelfJudgments(projection.ledger, owner_id='owner', clock=Clock())
    await add(projection, 'original', ORIGINAL, '14', memory_kind='substantive_event')
    first = state.admitted_premises('original')
    stance = state.form(proposal(first, topic='cupboard refills', source_ref='turn:original')).stance_id
    outcome = state.outcome_premise({'id': 'int-7', 'outcome': 'done', 'result': 'Refill scheduled.', 'verified': 'none'})
    used = state.revise(stance, proposal([outcome], topic='cupboard refills', new_evidence=[outcome.ref],
                                         source_ref='intention:int-7'))
    assert used.disposition == 'revised'
    await add(projection, 'correction', CORRECTION, '17', operation='correct', match_prior=True, memory_kind='substantive_event')
    correction = state.admitted_premises('correction')
    assert [p.corrects for p in correction] == [(first[0].ref,)]
    assert state.revisions() == []  # the cited premise is no longer current
    assert state.revisions(history=True)[0]['status'] == 'unsupported_premise'
    assert [r['id'] for r in state.citing(correction[0].corrects)] == [used.stance_id]
    ask = proposal(correction, topic='cupboard refills', stance='Refill when the count drops below 17.',
                   new_evidence=[correction[0].ref], source_ref='turn:correction')
    revised = state.revise(used.stance_id, ask)
    assert revised.disposition == 'revised'
    row = state.get(revised.stance_id)
    assert row['status'] == 'current' and {p['ref'] for p in row['premises']} == {outcome.ref, correction[0].ref}
    assert state.revise(used.stance_id, ask).disposition == 'no_new_premise'
    assert state.revise(revised.stance_id, ask).disposition == 'no_new_premise'
    assert len(state.revisions(history=True)) == 3
    with state.ledger._connect() as conn:
        deps = json.loads(conn.execute('SELECT dependency_json FROM self_judgment_revisions WHERE id=?',
                                       (revised.stance_id,)).fetchone()[0])
    assert {d['turn_id'] for d in deps} >= {'original', 'correction'}  # the subject basis stays a dependency


def test_soft_limit_defers_a_second_revision_and_verified_outcomes_bypass_it(judgments):
    state, clock = judgments
    source(state)
    stance = formed(state)
    source(state, 'second', 'Second interrupted render recovered from its stage checkpoint.')
    second = state.admitted_premises('second')
    first_revision = state.revise(stance, proposal(second, new_evidence=[second[0].ref], source_ref='turn:second'))
    assert first_revision.disposition == 'revised'
    source(state, 'third', 'Third render measured a small checkpoint overhead.')
    third = state.admitted_premises('third')
    limited = state.revise(first_revision.stance_id, proposal(third, new_evidence=[third[0].ref], source_ref='turn:third'))
    created = state.get(first_revision.stance_id)['created_at']
    assert (limited.disposition, limited.stance_id, limited.retry_at) == ('rate_limited', first_revision.stance_id,
                                                                         created + LIMIT_WINDOW_S)
    clock.value += LIMIT_WINDOW_S + 1
    later = state.revise(first_revision.stance_id, proposal(third, new_evidence=[third[0].ref], source_ref='turn:third'))
    assert later.disposition == 'revised'
    unverified = state.outcome_premise({'id': 'int-1', 'outcome': 'failed', 'failed_reason': 'Render timed out.'})
    assert state.revise(later.stance_id, proposal([unverified], new_evidence=[unverified.ref],
                        source_ref='intention:int-1')).disposition == 'rate_limited'
    verified = state.outcome_premise({'id': 'int-2', 'outcome': 'failed', 'failed_reason': 'Render timed out again.',
                                      'verified': 'check'})
    assert verified.verified == 'check'
    assert state.revise(later.stance_id, proposal([verified], new_evidence=[verified.ref],
                        source_ref='intention:int-2')).disposition == 'revised'


def test_a_statement_only_stance_is_refused_beside_a_current_one_from_the_same_session(judgments):
    state, clock = judgments
    source(state, 'first', 'Which render settings should I use for long jobs?', admitted=False,
           reply='I recommend stage checkpoints for renders over an hour.')
    said = state.statements('first')
    first = state.form(proposal(said, topic='long render settings'))
    assert first.disposition == 'formed'
    source(state, 'second', 'And for the export queue?', admitted=False, session_id='session-first',
           reply='I would batch exports overnight.')
    later = proposal(state.statements('second'), topic='export queue batching', source_ref='turn:second')
    assert state.form(later).disposition == 'duplicate_topic'
    assert state.form(proposal(state.statements('second'), topic='export queue batching', source_ref='turn:second',
                               session_id='another-session')).disposition == 'formed'
    clock.value += LIMIT_WINDOW_S + 1
    assert state.form(proposal(said, topic='render queue order')).disposition == 'formed'


def test_audience_is_all_only_for_topic_stances_resting_on_findings_or_outcomes(judgments):
    state, _ = judgments
    found = state.finding_premise(finding(state))
    assert found.kind == 'finding' and found.ref == 'finding:mind:int-1:finding'
    everyone = state.form(proposal([found], topic='render checkpoints', source_ref='finding:mind:int-1:finding', session_id='mind'))
    outcome = state.outcome_premise({'id': 'int-4', 'outcome': 'done', 'result': 'Staged the long render.'})
    also = state.form(proposal([outcome], topic='staged renders', source_ref='intention:int-4', session_id=''))
    source(state)
    claimed = formed(state)
    source(state, 'guest', 'Mira delivered the parts list on time twice.', contact_id='contact-b')
    person = state.form(proposal(state.admitted_premises('guest'), subject_kind='person', subject='contact-b',
                                 topic='delivery reliability', source_ref='turn:guest'))
    approach = state.form(proposal([outcome], subject_kind='approach', subject='research:render-checkpoints',
                                   topic='render checkpoints', stance_class='avoid', source_ref='intention:int-4'))
    audiences = {r['id']: r['audience'] for r in state.revisions()}
    assert audiences == {everyone.stance_id: 'all', also.stance_id: 'all', claimed: 'owner',
                         person.stance_id: 'owner', approach.stance_id: 'owner'}
    assert {r['id'] for r in state.revisions(audience='all')} == {everyone.stance_id, also.stance_id}
    assert {r['id'] for r in state.relevant('render checkpoints delivery reliability', audience='all', limit=10)} <= {
        everyone.stance_id, also.stance_id}
    assert state.get(approach.stance_id)['stance_class'] == 'avoid'
    assert state.form(proposal([outcome], subject_kind='approach', subject='research:x', topic='x',
                               source_ref='intention:int-4')).disposition == 'invalid:stance_class'
    assert state.form(proposal([outcome], subject_kind='person', subject='', topic='x',
                               source_ref='intention:int-4')).disposition == 'invalid:subject'


def test_relevance_ranks_by_entries_boosts_the_session_filters_audience_and_falls_back(judgments):
    state, _ = judgments
    stances = {}
    for turn, topic, text in [('a', 'local work checkpoints', 'Checkpoints saved the interrupted local render.'),
                              ('b', 'garden watering schedule', 'Watering the garden at dawn kept the seedlings alive.'),
                              ('c', 'invoice review order', 'Reviewing invoices by due date caught two late fees.')]:
        source(state, turn, text)
        stances[turn] = formed(state, turn, topic=topic, stance=f'I favor the reported approach for {topic}.')
    assert [r['id'] for r in state.relevant('What garden watering schedule do you suggest?')][:1] == [stances['b']]
    assert [r['id'] for r in state.relevant('invoice review order')][:1] == [stances['c']]
    assert state.relevant('What should I cook for dinner?') == []
    # This session's stance comes first even without shared words (pushback keeps its stance in view).
    assert [r['id'] for r in state.relevant('Are you sure?', session_id='session-a')] == [stances['a']]
    assert state.relevant('garden watering', audience='all') == []
    semantic = state.relevant('unrelated words', semantic_turn_ids=[f'{ENTRY_PREFIX}{stances["c"]}:formed'])
    assert [r['id'] for r in semantic] == [stances['c']]
    with state.ledger._connect() as conn:
        conn.execute("DELETE FROM turn_source_search WHERE turn_id LIKE ?", (f'{ENTRY_PREFIX}{stances["b"]}:%',))
        conn.execute("DELETE FROM turn_sources WHERE turn_id LIKE ?", (f'{ENTRY_PREFIX}{stances["b"]}:%',))
    assert [r['id'] for r in state.relevant('garden watering schedule')] == [stances['b']]  # fallback overlap
    state.correct(stances['b'], action='withdraw', correction_id='w', reason='Stop using this.')
    state.correct(stances['c'], action='reconsider', correction_id='r', reason='Look again.')
    assert state.relevant('garden watering schedule invoices late fees') == []


def test_withdraw_reconsider_and_their_idempotent_owner_controls(judgments):
    state, _ = judgments
    source(state)
    stance = formed(state)
    withdrawn = state.correct(stance, action='withdraw', correction_id='stop', reason='Owner withdrew this view.')
    assert withdrawn['status'] == 'withdrawn' and state.revisions() == []
    assert state.head(subject_kind='topic', topic='local work checkpoints')['status'] == 'withdrawn'
    assert "At the owner's request I withdrew my view on local work checkpoints" in entry(
        state, withdrawn['revision_id'], 'withdrawn')['message']['content']
    assert state.correct(stance, action='withdraw', correction_id='stop', reason='Owner withdrew this view.') == withdrawn
    with pytest.raises(ValueError, match='judgment_correction_id_conflict'):
        state.correct(stance, action='withdraw', correction_id='stop', reason='A different reason.')
    source(state, 'again', 'Another long run recovered from a checkpoint.')
    assert state.form(proposal(state.admitted_premises('again'), source_ref='turn:again')).disposition == 'withdrawn_head'
    control = state.correct(withdrawn['revision_id'], action='reconsider', correction_id='again', reason='Reconsider it.')
    assert control['status'] == 'reconsidering'
    assert job(state, f"reconsider:{control['revision_id']}")['kind'] == 'reconsider'
    view = state.get(control['revision_id'])
    assert view['owner_correction']['action'] == 'reconsider' and view['original_id'] == stance
    assert view['stance'] == 'I favor explicit checkpoints for long local work.' and view['premises']
    original = [Premise.from_dict(p) for p in view['premises']]
    ask = proposal(original, stance='I favor checkpoints for long work, reconsidered.', source_ref=f"reconsider:{control['revision_id']}")
    assert state.revise(control['revision_id'], ask).disposition == 'withdrawn_head'
    assert state.revise(control['revision_id'], proposal([premise('statement', 'turn:x#y', 'Just agree.')],
                        owner_reconsider=True)).disposition == 'invalid:premise'
    revised = state.revise(control['revision_id'], proposal(original, stance='I favor checkpoints for long work, reconsidered.',
                           source_ref=f"reconsider:{control['revision_id']}", owner_reconsider=True))
    assert revised.disposition == 'revised' and state.revisions()[0]['id'] == revised.stance_id
    assert state.end_reconsideration(control['revision_id']).disposition == 'no_new_premise'  # a replay after the revision
    second = state.correct(revised.stance_id, action='reconsider', correction_id='third', reason='Once more.')
    ended = state.end_reconsideration(second['revision_id'])
    assert ended.disposition == 'reconsider_withdrawn'
    assert state.get(second['revision_id'])['status'] == 'withdrawn' and state.revisions() == []
    assert entry(state, second['revision_id'], 'withdrawn')['message']['content'].endswith('Once more.')


def test_a_captured_control_turn_erasure_removes_the_copied_reason_and_fences_new_controls(judgments):
    state, _ = judgments
    source(state, 'basis', 'Long local work benefited from checkpoints.')
    stance = formed(state, 'basis')
    instruction = 'Withdraw the checkpoint view; my correction must remain separate from your opinion.'
    control = state.correct(stance, action='withdraw', correction_id='native-control', reason=instruction,
                            control_turn_id='control')
    assert entry(state, control['revision_id'], 'withdrawn')['indexed']
    source(state, 'control', instruction, admitted=False)
    state.ledger.erase_sources(contact_id='contact-a', turn_ids=['control'])
    history = state.revisions(history=True)
    assert history[0]['status'] == 'withdrawn' and history[0]['owner_correction'] is None and history[0]['stance'] == ''
    assert entry(state, control['revision_id'], 'withdrawn') is None
    with state.ledger._connect() as conn:
        assert conn.execute("SELECT 1 FROM turn_sources WHERE turn_id='basis'").fetchone()
        assert all(instruction not in row[0] for row in conn.execute('SELECT payload_json FROM self_judgment_revisions'))
        assert not conn.execute('SELECT count(*) FROM turn_source_search WHERE content LIKE ?', ('%' + instruction[:30] + '%',)).fetchone()[0]
    source(state, 'fresh', 'A later long render recovered from a stage checkpoint.')
    fresh = formed(state, 'fresh')
    with pytest.raises(ValueError, match='control_turn_erased'):
        state.correct(fresh, action='withdraw', correction_id='late', reason='Withdraw.', control_turn_id='control')


def test_a_reconsidered_revision_inherits_its_control_turn_fence(judgments):
    state, _ = judgments
    source(state)
    stance = formed(state)
    source(state, 'observations', 'Local work checkpoints have measurable overhead in short tasks.')
    control = state.correct(stance, action='reconsider', correction_id='native-reconsider', reason='Reconsider using these observations.',
                            source_id='observations', control_turn_id='native-control')
    original = [Premise.from_dict(p) for p in state.get(control['revision_id'])['premises']]
    assert state.revise(control['revision_id'], proposal(original, owner_reconsider=True,
                        source_ref=f"reconsider:{control['revision_id']}")).disposition == 'revised'
    assert state.revisions()
    source(state, 'native-control', 'Reconsider using these observations.', admitted=False)
    state.ledger.erase_sources(contact_id='contact-a', turn_ids=['native-control'])
    reopened = SelfJudgments(TurnIdempotencyLedger(state.ledger.db_path), owner_id='contact-a', clock=state.clock)
    assert reopened.revisions() == []
    with reopened.ledger._connect() as conn:
        assert all('Reconsider using these observations.' not in row[0]
                   for row in conn.execute('SELECT payload_json FROM self_judgment_revisions'))


@pytest.mark.parametrize('action', ['erase', 'reattribute'])
def test_erasing_a_premise_tombstones_the_chain_and_its_entries_without_reviving(judgments, action):
    state, clock = judgments
    source(state)
    stance = formed(state)
    source(state, 'later', 'Local work checkpoints now have lower overhead on the new disk.')
    later = state.admitted_premises('later')
    revised = state.revise(stance, proposal(later, new_evidence=[later[0].ref], source_ref='turn:later')).stance_id
    if action == 'erase':
        state.ledger.erase_sources(contact_id='contact-a', turn_ids=['first'])
    else:
        from protagine.turns.source_attribution import correct as reattribute
        reattribute(state.ledger, operation_id='fix-speaker', performed_by='operator', old_contact_id='contact-a',
                    contact_id='contact-z', source_ids=['first'], evidence_refs=['operator:correction'])
    assert state.revisions() == []
    assert [r['status'] for r in state.revisions(history=True)] == ['erased', 'erased']
    assert entry(state, stance, 'formed') is None and entry(state, revised, 'revised') is None
    with state.ledger._connect() as conn:
        assert all(r[0] == '{}' and r[1] == '' and r[2] == '[]' for r in conn.execute(
            'SELECT payload_json,topic,premises_json FROM self_judgment_revisions'))
        assert not conn.execute("SELECT count(*) FROM turn_source_search WHERE turn_id LIKE 'mind:opinion:%'").fetchone()[0]
    assert job(state, 'first') is None
    source(state, 'fresh', 'A new local work experiment measured useful checkpoint recovery.')
    fresh = formed(state, 'fresh')
    assert [r['id'] for r in state.revisions()] == [fresh]


def test_queue_rules_waiting_finish_fail_retry_prune_and_unweighed(judgments):
    state, clock = judgments
    source(state, 'owner-turn')
    source(state, 'guest-turn', contact_id='contact-b')
    source(state, 'import', derive_claims=False, admitted=False)
    source(state, 'checkpoint', scope='session', admitted=False)
    found = finding(state)
    with state.ledger._connect() as conn:
        rows = {r['ref']: (r['kind'], r['contact_id']) for r in conn.execute('SELECT * FROM opinion_jobs')}
    assert rows == {'owner-turn': ('turn', 'contact-a'), 'guest-turn': ('turn', 'contact-b'), found: ('finding', 'contact-a')}
    stance = formed(state, 'owner-turn')
    assert entry(state, stance, 'formed') and job(state, f'{ENTRY_PREFIX}{stance}:formed') is None
    for ref in ('owner-turn', 'guest-turn', found):
        state.finish(ref, 'none')
    assert state.next_job() is None
    source(state, 'question', 'Which orchard badge did I ask you to remember?', admitted=False)
    assert state.next_job() is None  # waits for its claim job, without spending attempts
    assert (state.processing()[0]['ref'], state.processing()[0]['disposition']) == ('question', 'waiting_source_claims')
    assert state.unweighed_since(0, contact_id='contact-a') == [
        {'ref': 'question', 'enqueued_at': job(state, 'question')['enqueued_at'], 'claims': 'pending'}]
    with state.ledger._connect() as conn:
        conn.execute("UPDATE source_claim_jobs SET status='complete' WHERE turn_id='question'")
    assert state.unweighed_since(0, contact_id='contact-a') == []  # complete, nothing admitted
    assert state.next_job() == {'ref': 'question', 'kind': 'turn', 'contact_id': 'contact-a',
                                'enqueued_at': job(state, 'question')['enqueued_at'], 'attempts': 0}
    state.fail('question', 'timeout')
    assert job(state, 'question')['next_attempt'] == clock.value + 60 and state.next_job() is None
    clock.value += 61
    state.fail('question', 'timeout')
    clock.value += 121
    state.fail('question', 'invalid_json')
    assert (job(state, 'question')['disposition'], job(state, 'question')['done_at']) == ('failed:invalid_json', clock.value)
    source(state, 'weighed', 'The long render recovered from its checkpoint again.')
    assert state.unweighed_since(0, contact_id='contact-a')[0]['claims'] == 'admitted'
    state.finish('weighed', 'rate_limited', retry_at=clock.value + 100)
    assert job(state, 'weighed')['done_at'] is None and state.next_job() is None
    clock.value += 101
    assert state.next_job()['ref'] == 'weighed'
    source(state, 'waiting', 'Is this a question?', admitted=False)
    clock.value += 48 * 3600 + 1
    assert state.next_job()['ref'] == 'weighed'
    state.finish('weighed', 'none')
    assert state.next_job()['ref'] == 'waiting'  # older than 48 h: surfaces to be finished as stale
    clock.value += 31 * 86400
    state.finish('waiting', 'stale')
    with state.ledger._connect() as conn:
        assert {r[0] for r in conn.execute('SELECT ref FROM opinion_jobs')} == {'waiting'}


def test_appraisal_judgments_migrate_into_person_opinions_and_the_kind_is_deleted(tmp_path, monkeypatch):
    from protagine.self_model import appraisals
    ledger = TurnIdempotencyLedger(tmp_path / 'sources.db')
    text = 'The export report claimed completion before any output existed.'
    ledger.record_source('seen', contact_id='contact-b', session_id='s-seen', messages=[{'role': 'user', 'content': text}])
    with ledger._connect() as conn:
        (session, messages), = conn.execute("SELECT session_id,messages_json FROM turn_sources WHERE turn_id='seen'")
        digest = source_message_hash(session, json.loads(messages)[0])
        dependency = {'source_id': 'seen', 'source_version': 'v', 'source_contact_id': 'contact-b', 'message_hash': digest}
        quote = 'claimed completion before any output existed'
        def record(identifier, topic, status, key):
            item = {'kind': 'judgment', 'dimension': 'skepticism', 'topic': topic, 'text': f'I doubt {topic} until verified.',
                    'reason': 'A premature report.', 'support': [{'handle': digest, 'quote': quote}], 'contrary': [],
                    'intensity': 'low', 'hint': 'verify_before_relying'}
            conn.execute('INSERT INTO appraisal_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (identifier, 'owner', 'contact-b',
                key, 'judgment', json.dumps(item), json.dumps([dependency]), '{"model_id":"old"}', 'seen', 'v', 100.0, None, status, None))
        record('j-old', 'export reports', 'superseded', 'k1')
        record('j-current', 'export reports', 'current', 'k1')
        record('j-withdrawn', 'invoice totals', 'withdrawn', 'k2')
        conn.execute("INSERT INTO appraisal_heads VALUES ('owner','contact-b','k1','j-current')")
        conn.execute("INSERT INTO appraisal_heads VALUES ('owner','contact-b','k2','j-withdrawn')")
        conn.execute("INSERT INTO appraisal_corrections VALUES ('c-1','j-withdrawn',?,1.0)", (json.dumps(
            {'record_id': 'j-withdrawn', 'action': 'withdraw', 'reason': 'Not fair.', 'actor_id': 'owner'}),))
        conn.execute("INSERT INTO appraisal_records VALUES ('a-1','owner','contact-b','k3','appraisal','{}','[]','{}','seen','v',1.0,NULL,'current',NULL)")
        initialize(conn)
        assert conn.execute("SELECT count(*) FROM appraisal_records WHERE kind='judgment'").fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM appraisal_heads').fetchone()[0] == 0
        assert conn.execute('SELECT count(*) FROM appraisal_corrections').fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM appraisal_records").fetchone()[0] == 1
        before = conn.execute('SELECT count(*) FROM self_judgment_revisions').fetchone()[0]
        initialize(conn)
        assert conn.execute('SELECT count(*) FROM self_judgment_revisions').fetchone()[0] == before == 2
    store = SelfJudgments(TurnIdempotencyLedger(ledger.db_path), owner_id='owner')
    current, = store.revisions()
    assert (current['subject_kind'], current['subject'], current['topic'], current['audience'], current['certainty']) == (
        'person', 'contact-b', 'skepticism export reports', 'owner', 'tentative')
    assert current['stance'] == 'I doubt export reports until verified.' and current['processor'] == {'model_id': 'old'}
    assert [(p['kind'], p['text'], p['turn_id'], p['contact_id']) for p in current['premises']] == [
        ('quote', quote, 'seen', 'contact-b')]
    tombstone = store.head(subject_kind='person', subject='contact-b', topic='skepticism invoice totals')
    assert tombstone['status'] == 'withdrawn' and tombstone['owner_correction']['reason'] == 'Not fair.'
    assert store.relevant('export reports', limit=5)[0]['id'] == current['id']  # no entry: the overlap fallback
    assert 'judgment' not in appraisals.KINDS and 'judgment' not in appraisals.DIMENSIONS
    branches = appraisals.RESPONSE_SCHEMA['schema']['properties']['observations']['items']['anyOf']
    assert 'judgment' not in {branch['properties']['kind']['const'] for branch in branches}
    item = {'kind': 'judgment', 'dimension': 'skepticism', 'topic': 'x', 'text': 'x', 'reason': 'x',
            'support': [], 'contrary': [], 'intensity': 'low', 'hint': 'none'}
    with pytest.raises(ValueError, match='invalid_appraisal_record'):
        appraisals.AppraisalStore(ledger, owner_id='owner')._validate(
            json.dumps({'observations': [item], 'incident_decisions': []}), {'evidence': [], 'previous': [], 'incident_ids': []})
    ledger.erase_sources(contact_id='contact-b', turn_ids=['seen'])
    assert store.revisions() == []


@pytest.mark.asyncio
async def test_process_one_is_inert_without_the_opinion_pass(judgments, monkeypatch):
    state, _ = judgments
    source(state)
    monkeypatch.setitem(sys.modules, 'protagine.mind.opinions', None)
    assert await state.process_one(object()) is False
    assert job(state, 'first')['attempts'] == 0 and job(state, 'first')['done_at'] is None


def test_self_judgment_runs_is_retired_and_legacy_heads_keep_their_keys(tmp_path):
    from protagine import init
    assert 'self_judgment_runs' in init.RETIRED_TABLES['turn-idempotency.db']
    path = tmp_path / 'turn-idempotency.db'
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE self_judgment_runs (turn_id TEXT PRIMARY KEY, owner_id TEXT, status TEXT)")
        db.execute("INSERT INTO self_judgment_runs VALUES ('t', 'o', 'pending')")
        db.execute('''CREATE TABLE self_judgment_revisions (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id TEXT NOT NULL,
            topic TEXT NOT NULL, payload_json TEXT NOT NULL, dependency_json TEXT NOT NULL, supersedes INTEGER,
            processor_json TEXT NOT NULL, created_at REAL NOT NULL, status TEXT NOT NULL, source_turn_id TEXT NOT NULL,
            version TEXT NOT NULL, correction_id TEXT)''')
        db.execute('CREATE TABLE self_judgment_heads (owner_id TEXT NOT NULL, topic TEXT NOT NULL, revision_id INTEGER NOT NULL, PRIMARY KEY(owner_id,topic))')
        db.execute('''INSERT INTO self_judgment_revisions (owner_id,topic,payload_json,dependency_json,processor_json,created_at,status,source_turn_id,version)
            VALUES ('contact-a','legacy topic',?, '[]','{}',1.0,'current','t','agent-judgment-v2')''', (json.dumps({'stance': 'Old view.', 'reason': 'r', 'certainty': 'moderate'}),))
        db.execute('INSERT INTO self_judgment_heads VALUES (?,?,1)', ('contact-a', head_key('topic', '', 'legacy topic')))
    assert init.retired_tables_present(tmp_path) == ['turn-idempotency.db:self_judgment_runs']
    init.retire_tables(tmp_path)
    store = SelfJudgments(TurnIdempotencyLedger(path), owner_id='contact-a')
    legacy = store.head(subject_kind='topic', topic='legacy topic')
    assert (legacy['id'], legacy['stance'], legacy['subject_kind'], legacy['audience']) == (1, 'Old view.', 'topic', 'owner')
    assert legacy['status'] == 'unsupported_premise'  # a legacy view without citations supports nothing
    with sqlite3.connect(path) as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='self_judgment_runs'").fetchone()
