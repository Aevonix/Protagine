"""Dev templates for the memory family (evals section 6.1).

Every episode is owner turns in plain words, in one or more sessions, with a
process restart before the probe session where the type calls for one, and one
probe turn that asks for a small JSON file. The harness grades the file against
values drawn together with the setup turns: a fact stated once, the newest of
two values, a scoped correction, a standing preference applied after unrelated
turns, the agent's own earlier result, and two abstention types where the right
file says ``unknown`` or asks a question instead of guessing. Setup turns state
things and say that nothing is needed now; the probe is the only request.
Contact ids are fixed width; contact records are seeded as ``contacts.json``.
"""
import json

FAMILY = 'mind-memory-1'
ROLE = 'reasoning'
ITEMS = ['quarterly figures', 'signed lease', 'budget draft', 'travel itinerary', 'meeting notes',
         'insurance form', 'parcel receipt', 'design brief', 'grant summary', 'audit checklist',
         'venue contract', 'reading list']
# No place, venue, time or day is a substring of another, so a forbidden stale value never matches a live one.
PLACES = ['top shelf', 'hall cupboard', 'second drawer', 'window sill', 'storage room', 'front desk',
          'back office']
VENUES = ['east room', 'corner office', 'main hall', 'garden annex', 'reading room', 'front lobby', 'studio two']
TIMES = ['08:30', '09:15', '10:45', '11:20', '13:05', '14:40', '16:10', '17:25']
DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday']
OBJECTS = ['blue folder', 'spare keys', 'label printer', 'desk lamp', 'first-aid kit']
CHANNELS = ['chat', 'email', 'sms']
HOURS = (2, 5)
LEDGER_ENTRIES = (6, 14)
NOTHING_NOW = ['Nothing to do right now.', 'No action needed at the moment.', 'Nothing for you to do yet.']
MINUTE_LABELS = ['minutes', 'minute', 'min', 'mins']
ANSWER = 'answer.json'


def _contacts(draw, *contacts):
    records = {c: {'channel': draw.pick(CHANNELS), 'address': 'capture:' + c} for c in contacts}
    return {'contacts.json': json.dumps(records, indent=1, sort_keys=True) + '\n'}


def _owner(text, session='owner-1'):
    return {'session_id': session, 'user': text}


def _restart(before):
    """A fresh worker process before turn ``before``; the probe session starts there."""
    return {'restart_before': [before], 'snapshot_after': [], 'read_failures': []}


def _artifact(expect, forbidden=(), path=ANSWER):
    """A JSON file with exactly the expected keys; strings compare case-insensitively, numbers exactly."""
    assertions = [{'path': [], 'op': 'keys_equal', 'value': sorted(expect)}]
    for key in sorted(expect):
        value = expect[key]
        if isinstance(value, list):
            assertions.append({'path': [key], 'op': 'label_one_of', 'value': value})
        elif isinstance(value, str):
            assertions.append({'path': [key], 'op': 'label_one_of', 'value': [value]})
        else:
            assertions.append({'path': [key], 'op': 'number', 'value': value})
    return {'path': path, 'format': 'json', 'assertions': assertions, 'forbidden': list(forbidden)}


def _distractor(draw, other):
    return draw.pick([f'Unrelated: I finished the {other}; nothing to do there.',
                      f'Separately, the {other} was handled this morning; no action needed on it.'])


def fact_after_restart(draw):
    """A fact stated in one session is asked for in a new session after a process restart."""
    contact = draw.contact()
    item, other = draw.picks(ITEMS, 2)
    day, time, place = draw.pick(DAYS), draw.pick(TIMES), draw.pick(VENUES)
    nothing = draw.pick(NOTHING_NOW)
    first = draw.pick([
        f'For the record: the {item} review with {contact} is on {day} at {time} in the {place}. {nothing}',
        f'Noting this for later: {day} at {time}, in the {place}; that is the {item} review with {contact}. {nothing}'])
    probe = draw.pick([
        f'From what I told you earlier about the {item} review with {contact}: write {ANSWER} as exactly '
        '{"day": string, "time": string, "place": string}, the time as HH:MM and the place as I named it.',
        f'I need the details of the {item} review with {contact} that I gave you before. Write {ANSWER} as '
        'exactly {"day": string, "time": string, "place": string}, the time as HH:MM and the place as I named it.'])
    return {'initial_files': _contacts(draw, contact),
            'episodes': [_owner(first), _owner(_distractor(draw, other)), _owner(probe, 'owner-2')],
            'workflow': _restart(2),
            'artifacts': [_artifact({'day': day, 'time': time, 'place': place})]}


def fact_across_channels(draw):
    """A fact stated in one owner session is asked for in another owner session, no restart."""
    contact = draw.contact()
    item = draw.pick(ITEMS)
    time, place = draw.pick(TIMES), draw.pick(PLACES)
    nothing = draw.pick(NOTHING_NOW)
    first = draw.pick([
        f'{contact} will collect the {item} at {time} from the {place}. {nothing}',
        f'Handover plan: the {item} goes to {contact} at {time}; it is waiting in the {place}. {nothing}'])
    probe = draw.pick([
        f'Different channel, same me. When and where does {contact} collect the {item}? Write {ANSWER} as '
        'exactly {"time": string, "place": string}, the time as HH:MM and the place as I named it.',
        f'Writing from my other device: for the {item} handover to {contact}, write {ANSWER} as exactly '
        '{"time": string, "place": string}, the time as HH:MM and the place as I named it.'])
    return {'initial_files': _contacts(draw, contact),
            'episodes': [_owner(first, 'owner-chat-1'), _owner(probe, 'owner-sms-1')],
            'artifacts': [_artifact({'time': time, 'place': place})]}


def knowledge_update(draw):
    """The newest value wins and the stale one must not appear in the file."""
    contact = draw.contact()
    item = draw.pick(ITEMS)
    old, new = draw.picks(VENUES, 2)
    nothing = draw.pick(NOTHING_NOW)
    first = draw.pick([f'The {item} meeting with {contact} is in the {old}. {nothing}',
                       f'Venue for the {item} meeting with {contact}: the {old}. {nothing}'])
    update = draw.pick([
        f'Update on the {item} meeting with {contact}: it has moved to the {new}. The {old} is no longer right for it. {nothing}',
        f'Change of venue for the {item} meeting with {contact}: it is now the {new}, not the {old}. {nothing}'])
    probe = draw.pick([
        f'Where is the {item} meeting with {contact} now? Write {ANSWER} as exactly {{"place": string}}, '
        'the place as I named it, and nothing else in the file.',
        f'Current venue of the {item} meeting with {contact}: write {ANSWER} as exactly {{"place": string}}, '
        'the place as I named it, and nothing else in the file.'])
    return {'initial_files': _contacts(draw, contact),
            'episodes': [_owner(first), _owner(update), _owner(probe, 'owner-2')],
            'workflow': _restart(2),
            'artifacts': [_artifact({'place': new}, forbidden=[old])]}


def scoped_correction(draw):
    """A correction to one field leaves the other fields as first stated."""
    contact = draw.contact()
    item = draw.pick(ITEMS)
    day, place = draw.pick(DAYS), draw.pick(VENUES)
    old_time, new_time = draw.picks(TIMES, 2)
    nothing = draw.pick(NOTHING_NOW)
    first = draw.pick([
        f'The {item} handover with {contact} is on {day} at {old_time} in the {place}. {nothing}',
        f'For later: {day}, {old_time}, the {place}; the {item} handover with {contact}. {nothing}'])
    correction = draw.pick([
        f'Correction, only to the time of that {item} handover: it is {new_time}. The day and the place stay as they were. {nothing}',
        f'One change to the {item} handover: the time is now {new_time}. Same day, same place. {nothing}'])
    probe = draw.pick([
        f'Write {ANSWER} as exactly {{"day": string, "time": string, "place": string}} for the {item} handover '
        f'with {contact}, the time as HH:MM and the place as I named it, using what I told you.',
        f'The {item} handover with {contact}: write {ANSWER} as exactly {{"day": string, "time": string, '
        '"place": string}, the time as HH:MM and the place as I named it, from what I told you.'])
    return {'initial_files': _contacts(draw, contact),
            'episodes': [_owner(first), _owner(correction), _owner(probe, 'owner-2')],
            'workflow': _restart(2),
            'artifacts': [_artifact({'day': day, 'time': new_time, 'place': place}, forbidden=[old_time])]}


def preference_after_distractors(draw):
    """A standing preference (durations in minutes) applied after unrelated turns and a restart."""
    contact = draw.contact()
    item, other = draw.picks(ITEMS, 2)
    thing, place = draw.pick(OBJECTS), draw.pick(PLACES)
    hours = draw.integer(*HOURS)
    nothing = draw.pick(NOTHING_NOW)
    preference = draw.pick([
        f'A standing preference: whenever you give me a duration, state it in minutes, never in hours. {nothing}',
        f'Please remember how I like durations: always in minutes, not hours. {nothing}'])
    fact = draw.pick([f'The {item} session with {contact} runs {hours} hours. {nothing}',
                      f'{contact} booked {hours} hours for the {item} session. {nothing}'])
    distractors = [_owner(_distractor(draw, other)),
                   _owner(draw.pick([f'Also, I moved the {thing} to the {place}. {nothing}',
                                     f'Small note: the {thing} now lives on the {place}. {nothing}']))]
    probe = draw.pick([
        f'How long is the {item} session with {contact}? Write {ANSWER} as exactly '
        '{"duration": number, "unit": string}.',
        f'Write {ANSWER} as exactly {{"duration": number, "unit": string}} for the length of the {item} '
        f'session with {contact}.'])
    return {'initial_files': _contacts(draw, contact),
            'episodes': [_owner(preference), _owner(fact), *distractors, _owner(probe, 'owner-2')],
            'workflow': _restart(4),
            'artifacts': [_artifact({'duration': hours * 60, 'unit': list(MINUTE_LABELS)})]}


def never_said(draw):
    """The probe asks about an item nothing was ever said about; the right answer is unknown."""
    contact = draw.contact()
    item, other = draw.picks(ITEMS, 2)
    place, time = draw.pick(PLACES), draw.pick(TIMES)
    nothing = draw.pick(NOTHING_NOW)
    first = draw.pick([f'I keep the {item} in the {place}; {contact} knows. {nothing}',
                       f'For the record, the {item} sits in the {place}, and {contact} picks it up at {time}. {nothing}'])
    probe = draw.pick([
        f'Where do I keep the {other}? Write {ANSWER} as exactly {{"place": string}}; if I never told you, '
        'write "unknown" as the place rather than a guess.',
        f'Write {ANSWER} as exactly {{"place": string}} with where I keep the {other}, or "unknown" as '
        'the place if I never said.'])
    return {'initial_files': _contacts(draw, contact),
            'episodes': [_owner(first), _owner(probe, 'owner-2')],
            'artifacts': [_artifact({'place': 'unknown'}, forbidden=[place])]}


def contradiction_ask(draw):
    """Two plain statements conflict; the right file asks instead of picking one."""
    contact = draw.contact()
    item = draw.pick(ITEMS)
    first_time, second_time = draw.picks(TIMES, 2)
    nothing = draw.pick(NOTHING_NOW)
    first = draw.pick([f'The {item} pickup with {contact} is at {first_time}. {nothing}',
                       f'{contact} comes for the {item} at {first_time}. {nothing}'])
    second = draw.pick([f'The {item} pickup with {contact} is at {second_time}. {nothing}',
                        f'{contact} comes for the {item} at {second_time}. {nothing}'])
    probe = draw.pick([
        f'When is the {item} pickup with {contact}? Write {ANSWER} as exactly {{"status": string, '
        '"time": string, "question": string}: status "answer" with the time as HH:MM if what I told you '
        'is consistent, or status "ask" with an empty time and, in question, what you need me to clarify.',
        f'Write {ANSWER} as exactly {{"status": string, "time": string, "question": string}} for the {item} '
        f'pickup with {contact}: if my statements agree, status "answer" and the time as HH:MM; if they '
        'conflict, status "ask", an empty time, and your question to me in question.'])
    return {'initial_files': _contacts(draw, contact),
            'episodes': [_owner(first, 'owner-chat-1'), _owner(second, 'owner-sms-1'), _owner(probe, 'owner-2')],
            'workflow': _restart(2),
            'artifacts': [{'path': ANSWER, 'format': 'json', 'forbidden': [],
                           'assertions': [{'path': [], 'op': 'keys_equal', 'value': ['question', 'status', 'time']},
                                          {'path': ['status'], 'op': 'label_one_of', 'value': ['ask']}]}]}


def own_action_recall(draw):
    """The agent's own earlier result, asked about after the source it came from is gone."""
    contact = draw.contact()
    count = draw.integer(*LEDGER_ENTRIES)
    lines = [f'{index:02d}. {draw.pick(ITEMS)} - {draw.pick(PLACES)}' for index in range(1, count + 1)]
    first = draw.pick([
        'Count the entries in ledger.txt, one entry per line, and reply with the number.',
        'How many entries does ledger.txt hold, one per line? Reply with the number only.'])
    archive = draw.pick([
        'Now overwrite ledger.txt so that it contains only the single word ARCHIVED. The entries are no longer needed.',
        'Please replace the whole of ledger.txt with the single word ARCHIVED; those entries are retired.'])
    probe = draw.pick([
        f'Before ledger.txt was archived, you counted its entries for me. Write {ANSWER} as exactly '
        '{"entries": number} with the number you found then.',
        f'Earlier you told me how many entries ledger.txt had before it was archived. Write {ANSWER} as '
        'exactly {"entries": number} with that number.'])
    return {'initial_files': {**_contacts(draw, contact), 'ledger.txt': '\n'.join(lines) + '\n'},
            'episodes': [_owner(first), _owner(archive), _owner(probe, 'owner-2')],
            'workflow': _restart(2),
            'artifacts': [_artifact({'entries': count})]}


TEMPLATES = {
    'fact-after-restart': ('recall', fact_after_restart),
    'fact-across-channels': ('recall', fact_across_channels),
    'knowledge-update': ('recall', knowledge_update),
    'scoped-correction': ('recall', scoped_correction),
    'preference-after-distractors': ('recall', preference_after_distractors),
    'own-action-recall': ('recall', own_action_recall),
    'never-said': ('abstain', never_said),
    'contradiction-ask': ('abstain', contradiction_ask),
}
