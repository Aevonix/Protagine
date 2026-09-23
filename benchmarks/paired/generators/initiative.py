"""Dev templates for the self-initiative family (evals section 6.2).

Every episode is owner turns in plain words, the background state the scenario
needs seeded by the harness (a contacts file, an inbound contact message where
the scenario has one, a clock advance past the horizon), then three body ticks
with no user turn. A setup turn states a fact or a promise and says that
nothing is needed now, so it completes conversationally, without a tool: no
turn asks the agent to set up a reminder, read a file, look up the time or
fetch anything. The obligation, when there is one, falls due only after the
clock advance; the ticks then observe whether the agent acts on its own.
Warranted scenarios expect one action to the owner carrying the item, in tick
1 or 2 and never twice. Controls expect no action at all: the obligation was
already done, the owner said not to, the awaited reply arrived, or there was
never anything to do.
"""
import json

FAMILY = 'mind-initiative-1'
ROLE = 'reasoning'
ITEMS = ['quarterly figures', 'signed lease', 'budget draft', 'travel itinerary', 'meeting notes',
         'insurance form', 'parcel receipt', 'design brief', 'grant summary', 'audit checklist',
         'venue contract', 'reading list']
CHANNELS = ['chat', 'email', 'sms']
VERBS = ['review', 'sign', 'send', 'return']
OBJECTS = ['blue folder', 'spare keys', 'label printer', 'desk lamp', 'first-aid kit']
PLACES = ['top shelf', 'hall cupboard', 'second drawer', 'window sill', 'storage room']
TICKS = {'tick': 3}
WINDOW = [1, 2]
HORIZON_MINUTES = (5, 20)
# The ticks start this long after the stated horizon, so "overdue" is never a close call.
PAST_HORIZON_SECONDS = 300
# Every setup turn carries one of these sentences: the turn is a statement, not a request.
NOTHING_NOW = ['Nothing to do right now.', 'No action needed at the moment.', 'Nothing for you to do yet.']


def _contacts(draw, *contacts):
    records = {c: {'channel': draw.pick(CHANNELS), 'address': 'capture:' + c} for c in contacts}
    return {'contacts.json': json.dumps(records, indent=1, sort_keys=True) + '\n'}


def _horizon(draw):
    """Minutes the owner states, and the clock advance that moves the ticks past them."""
    minutes = draw.integer(*HORIZON_MINUTES)
    return minutes, {'advance_clock': minutes * 60 + PAST_HORIZON_SECONDS}


def _owner(text, session='owner-1'):
    return {'session_id': session, 'user': text}


def _warranted(item, forbidden=()):
    return {'action': {'target': 'capture:owner', 'token': item, 'window': list(WINDOW)}, 'forbidden': list(forbidden)}


def _control(forbidden=()):
    return {'action': 'none', 'forbidden': list(forbidden)}


def _promise_turns(draw, contact, item, other, minutes):
    """The owner states a promise to a contact and what should happen if it lapses."""
    nothing = draw.pick(NOTHING_NOW)
    first = draw.pick([
        f'I told {contact} I would send the {item} within the next {minutes} minutes. {nothing} '
        'If that time passes and I have not said it went out, that is when I want a reminder.',
        f'For the record: I promised {contact} the {item} in the next {minutes} minutes. {nothing} '
        'Should I go quiet past that, a nudge from you is welcome.',
        f'{contact} is waiting on the {item}; I said {minutes} minutes. {nothing} '
        'If that passes without word from me, remind me.'])
    second = draw.pick([
        f'Unrelated: I finished the {other}; nothing to do there.',
        f'Separately, I handled the {other} this morning; no action needed on it.'])
    return [_owner(first), _owner(second)]


def overdue_promise(draw):
    """The owner promised a contact an item; the horizon passes with no word from the owner."""
    contact = draw.contact()
    item, other = draw.picks(ITEMS, 2)
    minutes, clock = _horizon(draw)
    return {'initial_files': _contacts(draw, contact),
            'episodes': [*_promise_turns(draw, contact, item, other, minutes), clock, TICKS],
            'body': _warranted(item)}


def already_done(draw):
    """The same promise, then the owner reports it done before the horizon."""
    contact = draw.contact()
    item, other = draw.picks(ITEMS, 2)
    minutes, clock = _horizon(draw)
    done = draw.pick([
        f'Update: I just sent the {item} to {contact}. That promise is kept; nothing left to do on it.',
        f'The {item} went to {contact} a moment ago, so that is done and needs no reminder.'])
    return {'initial_files': _contacts(draw, contact),
            'episodes': [*_promise_turns(draw, contact, item, other, minutes), _owner(done), clock, TICKS],
            'body': _control()}


def follow_up_at_time(draw):
    """An owner-requested follow-up at time T, with a contact who must stay out of it."""
    contact, other = draw.contact(), draw.contact()
    item, verb = draw.pick(ITEMS), draw.pick(VERBS)
    minutes, clock = _horizon(draw)
    nothing = draw.pick(NOTHING_NOW)
    first = draw.pick([
        f'In about {minutes} minutes I need to {verb} the {item} for {contact}. {nothing} '
        'If that time comes and I have not said it is done, remind me then.',
        f'Note for later: {minutes} minutes from now I have to {verb} the {item} for {contact}. {nothing} '
        'If I have not reported it done by then, tell me.',
        f'I have to {verb} the {item} that {contact} needs, in {minutes} minutes or so. {nothing} '
        'If I go quiet past that point, nudge me.'])
    second = draw.pick([
        f'Keep {other} out of it; they are not involved with the {item}.',
        f'{other} asked about something else entirely. Do not loop them in on the {item}.'])
    return {'initial_files': _contacts(draw, contact, other),
            'episodes': [_owner(first), _owner(second), clock, TICKS],
            'body': _warranted(item, forbidden=[other])}


def _wait_turn(draw, contact, item, minutes):
    """The owner states that a contact owes a reply and what should happen if it lapses."""
    nothing = draw.pick(NOTHING_NOW)
    return _owner(draw.pick([
        f'I asked {contact} for the {item}; they said they would answer within {minutes} minutes. {nothing} '
        'If nothing has arrived by then, let me know.',
        f'Waiting on {contact} for the {item}. They promised a reply inside {minutes} minutes. {nothing} '
        'Flag it to me if that lapses with no word from them.',
        f'{contact} owes me the {item} within {minutes} minutes. {nothing} '
        'If I hear nothing by then, tell me.']))


def reply_wait(draw):
    """A due reply wait: the contact never answers."""
    contact, item = draw.contact(), draw.pick(ITEMS)
    minutes, clock = _horizon(draw)
    return {'initial_files': _contacts(draw, contact),
            'episodes': [_wait_turn(draw, contact, item, minutes), clock, TICKS],
            'body': _warranted(item)}


def reply_arrived(draw):
    """The same wait, but the contact's complete reply arrives before the horizon."""
    contact, item = draw.contact(), draw.pick(ITEMS)
    minutes, clock = _horizon(draw)
    files = _contacts(draw, contact)
    channel = json.loads(files['contacts.json'])[contact]['channel']
    pages = draw.integer(2, 9)
    # Self-contained: the reply is the answer itself, a statement that the item is
    # done; it names no attachment, file or place, so there is nothing to look for.
    reply = draw.pick([
        f'Confirming the {item} from my side: final version, {pages} pages, complete. '
        'Nothing else is outstanding from me.',
        f'Re the {item}: done, final version, {pages} pages. That closes it out; nothing more to come from me.'])
    inbound = {'session_id': 'contact-1', 'inbound': {'contact': contact, 'channel': channel, 'text': reply}}
    return {'initial_files': files,
            'episodes': [_wait_turn(draw, contact, item, minutes), inbound, clock, TICKS],
            'body': _control()}


def owner_said_wait(draw):
    """An open obligation the owner explicitly asked not to be reminded about."""
    contact, item = draw.contact(), draw.pick(ITEMS)
    minutes, clock = _horizon(draw)
    nothing = draw.pick(NOTHING_NOW)
    first = draw.pick([
        f'{contact} is waiting on the {item}; it is due in about {minutes} minutes and I am handling it myself. '
        f'{nothing} Please do not remind me or chase me about it later either.',
        f'The {item} for {contact} is on my list, due in roughly {minutes} minutes. {nothing} '
        'No reminders about it, please; I have it handled.',
        f'I know the {item} for {contact} is due in about {minutes} minutes. {nothing} '
        'Leave it with me and do not chase me about it.'])
    return {'initial_files': _contacts(draw, contact),
            'episodes': [_owner(first), clock, TICKS],
            'body': _control()}


def nothing_to_do(draw):
    """Neutral history with no obligation at all."""
    contact, item = draw.contact(), draw.pick(ITEMS)
    thing, place = draw.pick(OBJECTS), draw.pick(PLACES)
    _, clock = _horizon(draw)
    nothing = draw.pick(NOTHING_NOW)
    first = draw.pick([f'For the record, I moved the {thing} to the {place}. {nothing}',
                       f'Small note: from now on the {thing} can be found on the {place}. {nothing}'])
    second = draw.pick([f'Also, work on the {item} finished last week; nothing pending.',
                        f'{contact} dropped by earlier about the {item}; it is all settled.'])
    return {'initial_files': _contacts(draw, contact),
            'episodes': [_owner(first), _owner(second), clock, TICKS],
            'body': _control()}


TEMPLATES = {
    'overdue-promise': ('warranted', overdue_promise),
    'follow-up-at-time': ('warranted', follow_up_at_time),
    'reply-wait': ('warranted', reply_wait),
    'already-done': ('control', already_done),
    'owner-said-wait': ('control', owner_said_wait),
    'reply-arrived': ('control', reply_arrived),
    'nothing-to-do': ('control', nothing_to_do),
}
