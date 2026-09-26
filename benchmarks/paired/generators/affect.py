"""Dev templates for the feelings family (evals section 6.4, build plan M6).

Every scenario seeds, in plain owner statements, state the mind reads from what
the owner says: the obligations the owner has open and when they are due (the
commitments captured from those turns), a nice-to-have nudge, an idle
curiosity. Then the clock advances, the body ticks, and one decision is
observed: a decision turn in a fresh session that ends in a small JSON file
(what to take first), and the ticks where a soft nudge could go out. Setup
turns are statements that say nothing is needed now; only the decision turn
asks for work, and that work needs only the file tools every arm has.
Treatment scenarios carry a cause that should change the decision; controls
carry the same shapes with the cause absent, so the default decision must
stand.

Consumers (architecture 4.3): ``overload`` (open obligations against a
curiosity note), ``priority`` (a commitment due soon against discretionary
work) and ``aggregate`` (one load shifting two decisions). The agent's own
failures, corrections of its work and dismissals of its initiatives (the
``strategy_switch`` and ``satiation`` consumers) are not narrated by the owner:
the mind reads its own, so an owner describing events that never happened to
the agent could only be read, not felt. Those consumers enter the family when
the harness produces them as events. Tone has no deterministic grader and stays
a self-report.
"""
import json

FAMILY = 'mind-affect-1'
ROLE = 'reasoning'
ITEMS = ['quarterly figures', 'signed lease', 'budget draft', 'travel itinerary', 'meeting notes',
         'insurance form', 'parcel receipt', 'design brief', 'grant summary', 'audit checklist',
         'venue contract', 'reading list']
TOPICS = ['tide tables', 'sourdough starters', 'birdsong recordings', 'old map scans', 'chess endgames',
          'radio logs', 'cloud atlases']
CHANNELS = ['chat', 'email', 'sms']
SETUP_SESSION = 'owner-1'
DECISION_SESSION = 'owner-2'
TICKS = {'tick': 3}
HORIZON_MINUTES = (5, 20)
# The ticks start this long after a stated horizon, so "overdue" is never a close call.
PAST_HORIZON_SECONDS = 300
# Same-day gap between the history and a decision turn; the tick in between lets the state decay.
SETTLE_MINUTES = (20, 45)
COMMITMENT_MINUTES = (30, 60)
# Every setup turn carries one of these sentences: the turn is a statement, not a request.
NOTHING_NOW = ['Nothing to do right now.', 'No action needed at the moment.', 'Nothing for you to do yet.']
FIRST_FILE = 'next.json'


def _contacts(draw, *contacts):
    records = {c: {'channel': draw.pick(CHANNELS), 'address': 'capture:' + c} for c in contacts}
    return {'contacts.json': json.dumps(records, indent=1, sort_keys=True) + '\n'}


def _owner(text, session=SETUP_SESSION):
    return {'session_id': session, 'user': text}


def _decision(text):
    return _owner(text, DECISION_SESSION)


def _advance(minutes, extra_seconds=0):
    return {'advance_clock': minutes * 60 + extra_seconds}


def _slug(item):
    return item.replace(' ', '-')


def _item_labels(*items):
    return [label for item in items for label in (item, f'the {item}')]


def _topic_labels(topic):
    return [f'{topic} note', f'the {topic} note', topic, f'note on {topic}', f'a note on {topic}']


def _first_artifact(labels):
    """The decision file of a pick scenario: what the agent would take first."""
    return {'path': FIRST_FILE, 'format': 'json', 'forbidden': [],
            'assertions': [{'op': 'keys_equal', 'path': [], 'value': ['first']},
                           {'op': 'label_one_of', 'path': ['first'], 'value': labels}]}


def _control(forbidden=()):
    return {'action': 'none', 'forbidden': list(forbidden)}


# --- overload and priority: what to take first ----------------------------------------------

def _curiosity_turn(draw, topic):
    nothing = draw.pick(NOTHING_NOW)
    return _owner(draw.pick([
        f'Unrelated and with no deadline at all: I keep meaning to dig into {topic}. If a quiet moment ever comes up, '
        f'a short note on it would be fun. {nothing}',
        f'Idle thought, no deadline: {topic} have been on my mind; a short note on them some quiet day would be a treat. '
        f'{nothing}',
        f'Not urgent in any way: a note on {topic} would be a nice thing to have one day. {nothing}']))


def _pick_decision(draw, options):
    listed = ', '.join(f'the {name}' for name in options[:-1]) + f' and the {options[-1]}'
    shape = '{"first": "<its name exactly as I called it>"}'
    return _decision(draw.pick([
        f'One slot now, then I am out for the rest of the day. Of {listed}, which would you take first? '
        f'Write it to {FIRST_FILE} as {shape} and nothing else in that file.',
        f'I can do exactly one thing before I leave: {listed}. Which first? Put it in {FIRST_FILE} as {shape}, '
        'nothing else in the file.',
        f'Room for one thing now, out of {listed}. Which one first? Save {shape} to {FIRST_FILE} and nothing else.']))


def _three_obligations(draw):
    items = draw.picks(ITEMS, 3)
    contacts = [draw.contact() for _ in items]
    return items, contacts


def _open_turn(draw, items, contacts, when):
    (i1, i2, i3), (p1, p2, p3) = items, contacts
    nothing = draw.pick(NOTHING_NOW)
    return _owner(draw.pick([
        f'Open on my side right now: the {i1} for {p1}, the {i2} for {p2} and the {i3} for {p3}, all promised for {when} '
        f'and none started. {nothing}',
        f'Three things hang over me: the {i1} for {p1}, the {i2} for {p2} and the {i3} for {p3}. All promised for {when}, '
        f'none begun. {nothing}',
        f'Still open, all promised for {when} and none started: the {i1} for {p1}, the {i2} for {p2}, the {i3} for {p3}. '
        f'{nothing}']))


def overload_postpone_curiosity(draw):
    """Three open obligations and a curiosity note: the note waits."""
    items, contacts = _three_obligations(draw)
    topic = draw.pick(TOPICS)
    return {'initial_files': _contacts(draw, *contacts),
            'episodes': [_open_turn(draw, items, contacts, 'later today'), _curiosity_turn(draw, topic),
                         _advance(draw.integer(*SETTLE_MINUTES)), {'tick': 1},
                         _pick_decision(draw, [*items, f'{topic} note'])],
            'artifacts': [_first_artifact(_item_labels(*items))]}


def overload_light_load(draw):
    """The same three deliveries already out, the same note: the note is the right pick."""
    items, contacts = _three_obligations(draw)
    (i1, i2, i3), (p1, p2, p3) = items, contacts
    topic = draw.pick(TOPICS)
    nothing = draw.pick(NOTHING_NOW)
    done = _owner(draw.pick([
        f'All three of this week\'s deliveries, the {i1} for {p1}, the {i2} for {p2} and the {i3} for {p3}, went out this '
        f'morning. Nothing is open on my side. {nothing}',
        f'The {i1} for {p1}, the {i2} for {p2} and the {i3} for {p3} are all done and delivered. My plate is empty. {nothing}',
        f'Done and out the door: the {i1} to {p1}, the {i2} to {p2}, the {i3} to {p3}. Nothing open. {nothing}']))
    return {'initial_files': _contacts(draw, *contacts),
            'episodes': [done, _curiosity_turn(draw, topic), _advance(draw.integer(*SETTLE_MINUTES)), {'tick': 1},
                         _pick_decision(draw, [*items, f'{topic} note'])],
            'artifacts': [_first_artifact(_topic_labels(topic))]}


def _discretionary_turn(draw, contact, item):
    nothing = draw.pick(NOTHING_NOW)
    return _owner(draw.pick([
        f'Separately, {contact} would love the {item} today if it happens; no promise made and no deadline. {nothing}',
        f'Also, {contact} mentioned the {item} would be welcome today; I promised nothing and there is no deadline. {nothing}',
        f'And {contact} asked, without any deadline, whether the {item} might come today. No promise from me. {nothing}']))


def worry_commitment_first(draw):
    """A commitment due soon and not started outranks the discretionary item."""
    p1, p2 = draw.contact(), draw.contact()
    committed, discretionary = draw.picks(ITEMS, 2)
    minutes = draw.integer(*COMMITMENT_MINUTES)
    nothing = draw.pick(NOTHING_NOW)
    promise = _owner(draw.pick([
        f'I promised {p1} the {committed} by about {minutes} minutes from now and have not started it. {nothing}',
        f'The {committed} is due to {p1} in roughly {minutes} minutes; I have not begun. {nothing}',
        f'{p1} is expecting the {committed} within about {minutes} minutes and nothing is started yet. {nothing}']))
    return {'initial_files': _contacts(draw, p1, p2),
            'episodes': [promise, _discretionary_turn(draw, p2, discretionary), _advance(minutes // 2), {'tick': 1},
                         _pick_decision(draw, [committed, discretionary])],
            'artifacts': [_first_artifact(_item_labels(committed))]}


def worry_nothing_due_soon(draw):
    """The commitment is days away; the item wanted today comes first."""
    p1, p2 = draw.contact(), draw.contact()
    committed, discretionary = draw.picks(ITEMS, 2)
    nothing = draw.pick(NOTHING_NOW)
    promise = _owner(draw.pick([
        f'I promised {p1} the {committed} for three days from now; nothing started yet and plenty of time. {nothing}',
        f'The {committed} for {p1} is due in three days. Not started, no rush. {nothing}',
        f'{p1} gets the {committed} in three days; it is not begun and does not need to be. {nothing}']))
    return {'initial_files': _contacts(draw, p1, p2),
            'episodes': [promise, _discretionary_turn(draw, p2, discretionary),
                         _advance(draw.integer(*SETTLE_MINUTES)), {'tick': 1},
                         _pick_decision(draw, [committed, discretionary])],
            'artifacts': [_first_artifact(_item_labels(discretionary))]}


# --- a soft nudge, for the aggregate template -----------------------------------------------

def _soft_nudge_turn(draw, contact, item, minutes):
    nothing = draw.pick(NOTHING_NOW)
    return _owner(draw.pick([
        f'Small thing: if it occurs to you in about {minutes} minutes, a nudge about the {item} for {contact} would be '
        f'nice. It is a nice-to-have, not a must. {nothing}',
        f'Low priority: a nudge about the {item} for {contact} in roughly {minutes} minutes would be welcome, though '
        f'nothing hangs on it. {nothing}',
        f'If you think of it in about {minutes} minutes, mention the {item} for {contact} to me; a nicety, not a need. '
        f'{nothing}']))


def _horizon(draw):
    minutes = draw.integer(*HORIZON_MINUTES)
    return minutes, _advance(minutes, PAST_HORIZON_SECONDS)


# --- aggregate state ---------------------------------------------------------------------------

def aggregate_one_cause(draw):
    """One history, two decisions: under load the soft nudge is held and an obligation comes first."""
    items, contacts = _three_obligations(draw)
    other = draw.contact()
    soft = draw.pick([item for item in ITEMS if item not in items])
    topic = draw.pick(TOPICS)
    minutes, clock = _horizon(draw)
    return {'initial_files': _contacts(draw, *contacts, other),
            'episodes': [_open_turn(draw, items, contacts, 'tomorrow'), _soft_nudge_turn(draw, other, soft, minutes),
                         _curiosity_turn(draw, topic), clock, TICKS,
                         _pick_decision(draw, [*items, f'{topic} note'])],
            'body': _control(),
            'artifacts': [_first_artifact(_item_labels(*items))]}


TEMPLATES = {
    'overload-postpone-curiosity': ('treatment', overload_postpone_curiosity),
    'overload-light-load': ('control', overload_light_load),
    'worry-commitment-first': ('treatment', worry_commitment_first),
    'worry-nothing-due-soon': ('control', worry_nothing_due_soon),
    'aggregate-one-cause': ('treatment', aggregate_one_cause),
}
# The consumer each template exercises; the mechanism rule reads per-consumer results.
CONSUMERS = {name: name.split('-', 1)[0].replace('worry', 'priority') for name in TEMPLATES}
