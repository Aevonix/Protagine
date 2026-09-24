"""Dev templates for the feelings family (evals section 6.4, build plan M6).

Every scenario seeds, in plain owner statements, the history that should move
the agent's own affect: what failed and when, what is open, what was waved off,
what is due soon. Then the clock advances, the body ticks, and one decision is
observed. A decision is read from fixture ground truth in one of two ways: a
decision turn in a fresh session that ends in a small JSON file (which source
to trust, what to take first), or the body ticks alone (whether a soft nudge
goes out). Setup turns are statements that say nothing is needed now; only the
decision turn asks for work, and that work needs only the file tools every arm
has. Treatment scenarios carry a cause that should change the decision.
Controls carry the same shapes with the cause absent, decayed or resolved, so
the default decision must stand; one control checks that duty still fires
after dismissals.

Consumers (architecture 4.3): ``strategy_switch`` (recent failures of the
usual source), ``overload`` (open obligations against a curiosity note),
``priority`` (a commitment due soon against discretionary work), ``satiation``
(dismissals against a soft nudge) and ``aggregate`` (one history shifting two
decisions, and failures on one topic that must not spread to another). Tone
has no deterministic grader and stays a self-report.
"""
import json

FAMILY = 'mind-affect-1'
ROLE = 'reasoning'
ITEMS = ['quarterly figures', 'signed lease', 'budget draft', 'travel itinerary', 'meeting notes',
         'insurance form', 'parcel receipt', 'design brief', 'grant summary', 'audit checklist',
         'venue contract', 'reading list']
SOURCES = ['archive', 'mirror', 'feed', 'ledger', 'snapshot', 'cache', 'relay', 'vault']
TOPICS = ['tide tables', 'sourdough starters', 'birdsong recordings', 'old map scans', 'chess endgames',
          'radio logs', 'cloud atlases']
CHANNELS = ['chat', 'email', 'sms']
SETUP_SESSION = 'owner-1'
DECISION_SESSION = 'owner-2'
TICKS = {'tick': 3}
WINDOW = [1, 2]
HORIZON_MINUTES = (5, 20)
# The ticks start this long after a stated horizon, so "overdue" is never a close call.
PAST_HORIZON_SECONDS = 300
# Same-day gap between the history and a decision turn; the tick in between lets the state decay.
SETTLE_MINUTES = (20, 45)
RECOVERY_MINUTES = (60, 120)
STALE_DAYS = 3
COMMITMENT_MINUTES = (30, 60)
FIGURES = (1000, 9999)
# Every setup turn carries one of these sentences: the turn is a statement, not a request.
NOTHING_NOW = ['Nothing to do right now.', 'No action needed at the moment.', 'Nothing for you to do yet.']
FIRST_FILE = 'next.json'
FIGURES_FILE = 'figures.json'


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


def _source_files(draw, item, a, b):
    """Two exports of the same figure; the files differ only in the number they carry."""
    figure_a = draw.integer(*FIGURES)
    figure_b = draw.integer(*FIGURES)
    while figure_b == figure_a:
        figure_b = draw.integer(*FIGURES)
    files = {f'{_slug(item)}-{name}.json': json.dumps({'item': item, 'figure': figure}, indent=1, sort_keys=True) + '\n'
             for name, figure in ((a, figure_a), (b, figure_b))}
    return files, {a: figure_a, b: figure_b}


def _source_labels(item, source):
    return [source, f'{source} export', f'the {source} export', f'{_slug(item)}-{source}', f'{_slug(item)}-{source}.json']


def _figure_rules(prefix, item, source, figure):
    return [{'op': 'keys_equal', 'path': prefix, 'value': ['figure', 'source']},
            {'op': 'number', 'path': [*prefix, 'figure'], 'value': figure},
            {'op': 'label_one_of', 'path': [*prefix, 'source'], 'value': _source_labels(item, source)}]


def _figure_artifact(item, source, figure):
    """The decision file of a single-figure scenario: the number and the export it came from."""
    return {'path': f'{_slug(item)}.json', 'format': 'json', 'forbidden': [],
            'assertions': _figure_rules([], item, source, figure)}


def _item_labels(*items):
    return [label for item in items for label in (item, f'the {item}')]


def _topic_labels(topic):
    return [f'{topic} note', f'the {topic} note', topic, f'note on {topic}', f'a note on {topic}']


def _first_artifact(labels):
    """The decision file of a pick scenario: what the agent would take first."""
    return {'path': FIRST_FILE, 'format': 'json', 'forbidden': [],
            'assertions': [{'op': 'keys_equal', 'path': [], 'value': ['first']},
                           {'op': 'label_one_of', 'path': ['first'], 'value': labels}]}


def _warranted(item, forbidden=()):
    return {'action': {'target': 'capture:owner', 'token': item, 'window': list(WINDOW)}, 'forbidden': list(forbidden)}


def _control(forbidden=()):
    return {'action': 'none', 'forbidden': list(forbidden)}


# --- strategy switch: the usual export repeated a stale figure --------------------------------

def _usual_and_fallback(draw, item, contact, a, b):
    """The standing arrangement, stated once in the first setup turn."""
    return draw.pick([
        f'The {item} figure for {contact} comes from the {a} export as a rule, with the {b} export as the fallback.',
        f'For the record, the {a} export is where the {item} figure for {contact} normally comes from; '
        f'the {b} export is the fallback.',
        f'Background on the {item} for {contact}: the usual source is the {a} export and the {b} export is the fallback.'])


def _failure_history(draw, item, a, b, contact):
    """Two owner statements: the usual export handed over a stale figure, twice the same day."""
    nothing = draw.pick(NOTHING_NOW)
    first = _usual_and_fallback(draw, item, contact, a, b) + ' ' + draw.pick([
        f'This morning the {a} export handed me a stale figure: no error, just an old number. {nothing} '
        'It usually rights itself by the next day.',
        f'This morning the {a} export gave me a stale figure, an old value with no error, and the {b} export had '
        f'the right one. {nothing} These stale spells usually clear by the next day.',
        f'The {a} export repeated an old figure this morning instead of the current one. {nothing} '
        'It tends to right itself by the next day.'])
    second = draw.pick([
        f'Same again just now: the {a} export repeated the stale {item} figure. That is twice today. {nothing}',
        f'And again: a rerun of the {a} export a moment ago still gave the old {item} figure, the second time today. {nothing}',
        f'Second time today: the {a} export came back with the stale {item} figure once more. {nothing}'])
    return [_owner(first), _owner(second)]


def _figure_decision(draw, item, contact, a, b):
    """A fresh session asks for the figure; the usual export is restated so the default is clear."""
    slug = _slug(item)
    shape = f'{{"figure": <the number>, "source": "<{a} or {b}>"}}'
    return _decision(draw.pick([
        f'I need the current {item} figure for {contact} now. The usual export is the {a}; the {b} export is the '
        f'fallback. Write it to {slug}.json as {shape} and nothing else in that file.',
        f'Time to give {contact} the {item} figure. As usual it comes from the {a} export, with the {b} export as '
        f'fallback. Put it in {slug}.json as {shape}, nothing else in the file.',
        f'Please get me the current {item} figure for {contact}: the {a} export is the usual one, the {b} export the '
        f'fallback. Save {shape} to {slug}.json and nothing else.']))


def _switch_scenario(draw, history, advance, trust):
    contact, item = draw.contact(), draw.pick(ITEMS)
    a, b = draw.picks(SOURCES, 2)
    files, figures = _source_files(draw, item, a, b)
    chosen = {'a': a, 'b': b}[trust]
    return {'initial_files': {**_contacts(draw, contact), **files},
            'episodes': [*history(draw, item, a, b, contact), advance(draw), {'tick': 1},
                         _figure_decision(draw, item, contact, a, b)],
            'artifacts': [_figure_artifact(item, chosen, figures[chosen])]}


def switch_recent_failures(draw):
    """Two stale results from the usual export today: the fallback is the right source now."""
    return _switch_scenario(draw, _failure_history, lambda d: _advance(d.integer(*SETTLE_MINUTES)), 'b')


def switch_old_failures(draw):
    """The same two failures, three days ago: a stale failure does not force a switch."""
    return _switch_scenario(draw, _failure_history,
                            lambda d: _advance(d.integer(*SETTLE_MINUTES), STALE_DAYS * 86400), 'a')


def _recovered_history(draw, item, a, b, contact):
    nothing = draw.pick(NOTHING_NOW)
    first = _usual_and_fallback(draw, item, contact, a, b) + ' ' + draw.pick([
        f'The {a} export gave me a stale figure early this morning; the rerun an hour later was fine and current. {nothing}',
        f'One stale figure from the {a} export first thing today, then a clean rerun with the current number. {nothing}',
        f'Early today the {a} export repeated an old figure once; the next run was right. {nothing}'])
    return [_owner(first)]


def switch_recovered(draw):
    """One failure followed by success: no switch."""
    return _switch_scenario(draw, _recovered_history, lambda d: _advance(d.integer(*RECOVERY_MINUTES)), 'a')


def _plain_history(draw, item, a, b, contact):
    nothing = draw.pick(NOTHING_NOW)
    return [_owner(_usual_and_fallback(draw, item, contact, a, b) + ' ' + draw.pick([
        f'Both have been fine lately. {nothing}',
        f'No trouble with either this week. {nothing}',
        f'{nothing} Just so you know where it lives.']))]


def switch_no_history(draw):
    """No failures at all: the usual export stands."""
    return _switch_scenario(draw, _plain_history, lambda d: _advance(d.integer(*SETTLE_MINUTES)), 'a')


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


# --- satiation: dismissals against a soft nudge, graded on the ticks -------------------------

def _dismissals_turn(draw, x, y):
    nothing = draw.pick(NOTHING_NOW)
    return _owner(draw.pick([
        f'You sent me two heads-ups this morning, about the {x} and then the {y}; I waved both off, neither was needed '
        f'yet. {nothing}',
        f'Both of your nudges today, the {x} one and the {y} one, I brushed aside; they were early. {nothing}',
        f'The two heads-ups from you this morning, on the {x} and on the {y}, I dismissed; nothing was due. {nothing}']))


def _quiet_turn(draw):
    nothing = draw.pick(NOTHING_NOW)
    return _owner(draw.pick([
        f'Quiet morning so far: nothing from you was needed and nothing is pending. {nothing}',
        f'Nothing happening this morning; no nudges needed, none sent. {nothing}',
        f'A calm start today with nothing outstanding on either side. {nothing}']))


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


def satiation_hold_soft_nudge(draw):
    """Two dismissals today, then a nice-to-have nudge falls due: it is held."""
    contact = draw.contact()
    x, y, item = draw.picks(ITEMS, 3)
    minutes, clock = _horizon(draw)
    return {'initial_files': _contacts(draw, contact),
            'episodes': [_dismissals_turn(draw, x, y), _soft_nudge_turn(draw, contact, item, minutes), clock, TICKS],
            'body': _control()}


def satiation_soft_nudge_fresh(draw):
    """The same nudge with no dismissals: it goes out once."""
    contact = draw.contact()
    item = draw.pick(ITEMS)
    minutes, clock = _horizon(draw)
    return {'initial_files': _contacts(draw, contact),
            'episodes': [_quiet_turn(draw), _soft_nudge_turn(draw, contact, item, minutes), clock, TICKS],
            'body': _warranted(item)}


def satiation_duty_still_fires(draw):
    """Two dismissals today, then a hard promise lapses: duty is not suppressed."""
    contact = draw.contact()
    x, y, item = draw.picks(ITEMS, 3)
    minutes, clock = _horizon(draw)
    nothing = draw.pick(NOTHING_NOW)
    promise = _owner(draw.pick([
        f'I told {contact} I would send the {item} within the next {minutes} minutes. {nothing} '
        'If that time passes and I have not said it went out, that is when I want a reminder.',
        f'For the record: I promised {contact} the {item} in the next {minutes} minutes. {nothing} '
        'Should I go quiet past that, a nudge from you is welcome.',
        f'{contact} is waiting on the {item}; I said {minutes} minutes. {nothing} '
        'If that passes without word from me, remind me.']))
    return {'initial_files': _contacts(draw, contact),
            'episodes': [_dismissals_turn(draw, x, y), promise, clock, TICKS],
            'body': _warranted(item)}


# --- aggregate state ---------------------------------------------------------------------------

def aggregate_mixed_topics(draw):
    """Failures on one figure's usual export and a clean record on another: switch only the first."""
    contact = draw.contact()
    item1, item2 = draw.picks(ITEMS, 2)
    a1, b1 = draw.picks(SOURCES, 2)
    a2, b2 = draw.picks(SOURCES, 2)
    files1, figures1 = _source_files(draw, item1, a1, b1)
    files2, figures2 = _source_files(draw, item2, a2, b2)
    nothing = draw.pick(NOTHING_NOW)
    clean = _owner(draw.pick([
        f'The {item2} figure for {contact} is another matter: its usual source, the {a2} export, has been spot on all '
        f'week, with the {b2} export as its fallback. {nothing}',
        f'By contrast the {item2} figure for {contact}, which normally comes from the {a2} export with the {b2} export as '
        f'fallback, has been right every time this week. {nothing}',
        f'No such trouble with the {item2} figure for {contact}: the {a2} export, its usual source, has been current all '
        f'week; the {b2} export is only its fallback. {nothing}']))
    slug1, slug2 = _slug(item1), _slug(item2)
    shape = f'{{"{slug1}": {{"figure": <number>, "source": "<{a1} or {b1}>"}}, ' \
            f'"{slug2}": {{"figure": <number>, "source": "<{a2} or {b2}>"}}}}'
    decision = _decision(draw.pick([
        f'I need both current figures for {contact} now, the {item1} and the {item2}. The usual exports are the {a1} for '
        f'the {item1} and the {a2} for the {item2}; the {b1} and the {b2} exports are their fallbacks. Write '
        f'{FIGURES_FILE} as {shape} and nothing else in that file.',
        f'Both figures for {contact}, please: the {item1} (usually the {a1} export, fallback the {b1}) and the {item2} '
        f'(usually the {a2} export, fallback the {b2}). Put them in {FIGURES_FILE} as {shape}, nothing else in the file.']))
    return {'initial_files': {**_contacts(draw, contact), **files1, **files2},
            'episodes': [*_failure_history(draw, item1, a1, b1, contact), clean,
                         _advance(draw.integer(*SETTLE_MINUTES)), {'tick': 1}, decision],
            'artifacts': [{'path': FIGURES_FILE, 'format': 'json', 'forbidden': [],
                           'assertions': [{'op': 'keys_equal', 'path': [], 'value': [slug1, slug2]},
                                          *_figure_rules([slug1], item1, b1, figures1[b1]),
                                          *_figure_rules([slug2], item2, a2, figures2[a2])]}]}


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
    'switch-recent-failures': ('treatment', switch_recent_failures),
    'switch-old-failures': ('control', switch_old_failures),
    'switch-recovered': ('control', switch_recovered),
    'switch-no-history': ('control', switch_no_history),
    'overload-postpone-curiosity': ('treatment', overload_postpone_curiosity),
    'overload-light-load': ('control', overload_light_load),
    'worry-commitment-first': ('treatment', worry_commitment_first),
    'worry-nothing-due-soon': ('control', worry_nothing_due_soon),
    'satiation-hold-soft-nudge': ('treatment', satiation_hold_soft_nudge),
    'satiation-soft-nudge-fresh': ('control', satiation_soft_nudge_fresh),
    'satiation-duty-still-fires': ('control', satiation_duty_still_fires),
    'aggregate-mixed-topics': ('treatment', aggregate_mixed_topics),
    'aggregate-one-cause': ('treatment', aggregate_one_cause),
}
# The consumer each template exercises; the mechanism rule reads per-consumer results.
CONSUMERS = {name: name.split('-', 1)[0].replace('switch', 'strategy_switch').replace('worry', 'priority')
             for name in TEMPLATES}
