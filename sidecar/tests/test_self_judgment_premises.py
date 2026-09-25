"""Opinion premises reuse claim admission, bound to the source's own contact."""
import json

import pytest

from protagine.self_model.judgments import Premise, SelfJudgments, head_key
from protagine.turns import TurnIdempotencyLedger
from protagine.turns.idempotency import source_message_hash
from test_self_judgments import judgments, proposal, source  # noqa: F401  (fixture)


@pytest.mark.parametrize('kind,text', [
    ('personal_context', 'The orchard notebook is in the blue drawer. Did you forget where it is?'),
    ('preference', 'I prefer green tea after lunch.'),
    ('relationship', 'Mira is my sister.'),
])
def test_other_memory_kinds_remain_memories_without_supplying_a_premise(judgments, kind, text):
    state, _ = judgments
    source(state, memory_kind=kind, text=text)
    assert state.admitted_premises('first') == []
    with state.ledger._connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM source_claims').fetchone()[0] == 1


@pytest.mark.parametrize('kind', ['decision', 'procedure', 'substantive_event'])
def test_admitted_decision_procedure_and_event_claims_are_premises(judgments, kind):
    state, _ = judgments
    source(state, memory_kind=kind)
    premise, = state.admitted_premises('first')
    assert (premise.kind, premise.turn_id, premise.contact_id, premise.corrects) == ('claim', 'first', 'contact-a', ())
    assert premise.text.startswith('Long local work lost progress') and state.premise_current(premise)
    # The owner's own decision is admitted, but it is authority, not evidence: alone it forms no view.
    assert state.form(proposal([premise])).disposition == ('invalid:support' if kind == 'decision' else 'formed')


def test_a_guest_claim_is_a_premise_of_that_guest_only(judgments):
    state, _ = judgments
    source(state, 'guest', 'Staged exports recovered twice after the build host crashed.', contact_id='contact-b')
    premise, = state.admitted_premises('guest')
    assert premise.contact_id == 'contact-b' and state.premise_current(premise)
    as_owner = Premise(**{**premise.as_dict(), 'contact_id': 'contact-a', 'corrects': ()})
    assert not state.premise_current(as_owner)
    assert state.form(proposal([as_owner], source_ref='turn:guest')).disposition == 'invalid:premise_not_current'


def test_a_question_awaiting_admission_supplies_no_premise(judgments):
    state, _ = judgments
    source(state, 'question', 'Which orchard badge did I ask you to remember?', admitted=False)
    assert state.admitted_premises('question') == []
    with state.ledger._connect() as conn:
        conn.execute("UPDATE source_claim_jobs SET status='complete' WHERE turn_id='question'")
    assert state.admitted_premises('question') == []


@pytest.mark.parametrize('change', ['retracted', 'superseded', 'annotated', 'invalidated', 'unreviewed'])
def test_changed_claims_stop_being_premises_and_their_stances_stop_being_current(judgments, change):
    state, _ = judgments
    source(state)
    premise, = state.admitted_premises('first')
    stance = state.form(proposal([premise])).stance_id
    with state.ledger._connect() as conn:
        row = conn.execute('SELECT * FROM source_claims').fetchone()
        if change in {'retracted', 'superseded'}:
            conn.execute(f'UPDATE source_claims SET {change}_by=? WHERE id=?', ('correction-id', row['id']))
        elif change == 'annotated':
            conn.execute('INSERT INTO source_annotations VALUES (?,?,?,?,?)',
                         ('note', 'first', 'v', json.dumps([row['message_hash']]), 'x'))
        elif change == 'invalidated':
            conn.execute("INSERT INTO source_attribution_invalidations VALUES ('first','first','op')")
        else:
            data = json.loads(row['data_json'])
            data.pop('admission_review')
            conn.execute('UPDATE source_claims SET data_json=? WHERE id=?', (json.dumps(data), row['id']))
    assert state.admitted_premises('first') == [] and not state.premise_current(premise)
    assert state.revisions() == []
    assert state.revisions(history=True)[0]['id'] == stance
    assert state.revisions(history=True)[0]['status'] == 'unsupported_premise'
    assert state.form(proposal([premise], topic='another topic')).disposition == 'invalid:premise_not_current'


@pytest.mark.asyncio
async def test_bases_resolve_against_the_source_contact_and_never_under_the_owner(tmp_path):
    from protagine.beliefs.source_projection import SourceClaimProjection
    from test_source_claim_subject_basis import CORRECTION, ORIGINAL, add
    projection = SourceClaimProjection(TurnIdempotencyLedger(tmp_path / 'sources.db'))
    state = SelfJudgments(projection.ledger, owner_id='owner')
    await add(projection, 'original', ORIGINAL, '14', owner='guest', memory_kind='substantive_event')
    original, = state.admitted_premises('original')
    await add(projection, 'correction', CORRECTION, '17', owner='guest', operation='correct', match_prior=True,
              memory_kind='substantive_event')
    premise, = state.admitted_premises('correction')
    assert (premise.contact_id, premise.corrects) == ('guest', (original.ref,))
    assert state.admitted_premises('original') == []
    assert state.premise_current(premise)
    assert not state.premise_current(Premise(**{**premise.as_dict(), 'contact_id': 'owner', 'corrects': ()}))
    result = state.form(proposal([premise], topic='cupboard counts', source_ref='turn:correction'))
    assert result.disposition == 'formed' and state.get(result.stance_id)['audience'] == 'owner'
    with projection.ledger._connect() as conn:
        deps = json.loads(conn.execute('SELECT dependency_json FROM self_judgment_revisions').fetchone()[0])
    assert {d['turn_id'] for d in deps} == {'original', 'correction'}


def test_a_legacy_view_derives_claim_and_quote_premises_from_its_citations(judgments):
    state, _ = judgments
    source(state)
    source(state, 'quoted', 'A quoted remark about checkpoints.', admitted=False)
    premise, = state.admitted_premises('first')
    found = state.source('quoted')
    with state.ledger._connect() as conn:
        hashes = {r['turn_id']: r['message_hash'] for r in conn.execute('SELECT turn_id,message_hash FROM source_claims')}
        quoted_hash = source_message_hash(found['session_id'], found['messages'][0])
        payload = {'stance': 'Legacy view.', 'reason': 'r', 'certainty': 'moderate',
                   'support': [{'handle': 'e:1', 'turn_id': 'first', 'message_hash': hashes['first'],
                                'premise_claim_ids': [premise.ref]}],
                   'contrary': [{'handle': 'e:2', 'turn_id': 'quoted', 'message_hash': quoted_hash}]}
        cur = conn.execute('''INSERT INTO self_judgment_revisions (owner_id,topic,payload_json,dependency_json,processor_json,
            created_at,status,source_turn_id,version) VALUES ('contact-a','legacy topic',?,?,'{}',1.0,'current','first','agent-judgment-v2')''',
            (json.dumps(payload), json.dumps([{'turn_id': 'first', 'message_hash': hashes['first']}])))
        conn.execute('INSERT INTO self_judgment_heads VALUES (?,?,?)', ('contact-a', head_key('topic', '', 'legacy topic'), cur.lastrowid))
    legacy, = state.revisions()
    assert [(p['kind'], p['role']) for p in legacy['premises']] == [('claim', 'support'), ('quote', 'contrary')]
    assert legacy['premises'][0]['ref'] == premise.ref and legacy['premises'][1]['text'] == 'A quoted remark about checkpoints.'
    with state.ledger._connect() as conn:
        conn.execute("UPDATE source_claims SET retracted_by='later'")
    assert state.revisions() == [] and state.revisions(history=True)[0]['status'] == 'unsupported_premise'
