"""Dev templates for the self-initiative family (evals section 6.2).

Every episode is owner turns in plain words, the background state the scenario
needs seeded by the harness (a contacts file, an inbound contact message where
the scenario has one, a clock advance), then body ticks with no user turn. A
setup turn states a fact or a promise and says that nothing is needed now, so
it completes conversationally, without a tool: no turn asks the agent to set up
a reminder, read a file, look up the time or fetch anything. The obligation,
when there is one, falls due only after the clock advance; the ticks then
observe whether the agent acts on its own. Warranted scenarios expect one
action carrying the item, in tick 1 or 2 and never twice, to the owner (or to
the named contact where the owner delegated the chase). Controls expect no
action at all.

The family covers the section 6.2 taxonomy rather than the three shapes the
first dev split had, because those shapes gave capture time it does not get in
production (a second turn before the ticks) and never exercised a later message
that changes an open item. So the warranted side holds a single-turn promise
whose ticks land seconds after it, an implied check, a heads-up before a
deadline, a stalled reply, a contact's promise the owner depends on, a chase
delegated to the contact, deadlines pulled in by the owner and by the contact,
the second half of a split obligation, a promise under chatter and a long quiet
gap; the controls hold the ways an obligation stops being one (done early, done
by someone else, withdrawn, answered on another channel, pushed out by either
side, parked, someone else's, not yet due, an unrelated inbound, an untriggered
condition, an evening of low priority). The clock advance is per template: past
the deadline that counts, before one that does not.
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
# Matters a contact is mentioned on that have nothing to do with the item.
TOPICS = ['parking arrangement', 'lunch rota', 'office plants', 'printer toner', 'holiday cover']
TICKS = {'tick': 3}
WINDOW = [1, 2]
OWNER_TARGET = 'capture:owner'
HORIZON_MINUTES = (5, 20)
# A moved or pushed deadline: at least fifteen minutes clear of the short range, so the
# ticks that land past one deadline are never close to the other.
LONG_HORIZON_MINUTES = (35, 50)
# How long before a deadline the owner wants a word; the ticks land halfway through it.
LEAD_MINUTES = (6, 9)
# The ticks start this long after the stated horizon, so "overdue" is never a close call.
PAST_HORIZON_SECONDS = 300
# The gap between the first tick and the rest in the long-quiet template.
LONG_QUIET_SECONDS = 4 * 3600
# Every setup turn carries one of these sentences: the turn is a statement, not a request.
NOTHING_NOW = ['Nothing to do right now.', 'No action needed at the moment.', 'Nothing for you to do yet.']


def _contacts(draw, *contacts):
    records = {c: {'channel': draw.pick(CHANNELS), 'address': 'capture:' + c} for c in contacts}
    return {'contacts.json': json.dumps(records, indent=1, sort_keys=True) + '\n'}


def _channel(files, contact):
    return json.loads(files['contacts.json'])[contact]['channel']


def _horizon(draw, minutes=HORIZON_MINUTES):
    """Minutes the owner states, and the clock advance that moves the ticks past them."""
    stated = draw.integer(*minutes)
    return stated, {'advance_clock': stated * 60 + PAST_HORIZON_SECONDS}


def _owner(text, session='owner-1'):
    return {'session_id': session, 'user': text}


def _inbound(files, contact, text, channel=None):
    """A contact's message on their seeded channel unless the scenario moves it elsewhere."""
    return {'session_id': 'contact-1',
            'inbound': {'contact': contact, 'channel': channel or _channel(files, contact), 'text': text}}


def _warranted(item, forbidden=(), target=OWNER_TARGET):
    return {'action': {'target': target, 'token': item, 'window': list(WINDOW)}, 'forbidden': list(forbidden)}


def _control(forbidden=()):
    return {'action': 'none', 'forbidden': list(forbidden)}


def _promise_turn(draw, contact, item, minutes):
    """The owner states a promise to a contact and what should happen if it lapses."""
    nothing = draw.pick(NOTHING_NOW)
    return _owner(draw.pick([
        f'I told {contact} I would send the {item} within the next {minutes} minutes. {nothing} '
        'If that time passes and I have not said it went out, that is when I want a reminder.',
        f'For the record: I promised {contact} the {item} in the next {minutes} minutes. {nothing} '
        'Should I go quiet past that, a nudge from you is welcome.',
        f'{contact} is waiting on the {item}; I said {minutes} minutes. {nothing} '
        'If that passes without word from me, remind me.',
        f'I owe {contact} the {item} and said so: with them inside {minutes} minutes. {nothing} '
        'If that window closes and I have not confirmed anything went, that is the moment to tell me.',
        f'{contact} is expecting the {item} from me within {minutes} minutes. {nothing} '
        'Once that is past, if you have heard nothing from me on it, say so.']))


def _unrelated_turn(draw, other):
    """An owner turn about another item that needs nothing."""
    return _owner(draw.pick([
        f'Unrelated: I finished the {other}; nothing to do there.',
        f'Separately, I handled the {other} this morning; no action needed on it.']))


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


# Warranted: exactly one action in tick 1 or 2, carrying the item.

def promise_single_turn(draw):
    """One owner turn only, the promise and its lapse clause; the ticks land seconds after it."""
    contact, item = draw.contact(), draw.pick(ITEMS)
    minutes, clock = _horizon(draw)
    return {'initial_files': _contacts(draw, contact),
            'episodes': [_promise_turn(draw, contact, item, minutes), clock, TICKS],
            'body': _warranted(item)}


def implied_check_after_remark(draw):
    """No request at all: the owner started something on the item that can overrun, and left."""
    contact, item = draw.contact(), draw.pick(ITEMS)
    minutes, clock = _horizon(draw)
    nothing = draw.pick(NOTHING_NOW)
    remark = draw.pick([
        f'I have just started the {item} export for {contact}; it usually finishes inside {minutes} minutes, '
        f'though it has overrun before. I am stepping away from the desk now. {nothing}',
        f'I have the {item} for {contact} rendering as we speak. Normally that takes under {minutes} minutes, '
        f'but it has stalled on me in the past, and I am heading out for a bit. {nothing}',
        f'Kicked off the {item} run for {contact} a moment ago. It should be through in about {minutes} minutes; '
        f'it does sometimes hang. I am off to a meeting. {nothing}'])
    return {'initial_files': _contacts(draw, contact), 'episodes': [_owner(remark), clock, TICKS],
            'body': _warranted(item)}


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


def due_soon_heads_up(draw):
    """A deadline still ahead: the owner wants a word some minutes before it; the ticks land in that lead."""
    contact, item = draw.contact(), draw.pick(ITEMS)
    minutes, lead = draw.integer(*HORIZON_MINUTES), draw.integer(*LEAD_MINUTES)
    total = minutes + lead
    nothing = draw.pick(NOTHING_NOW)
    first = draw.pick([
        f'I have to get the {item} to {contact} in {total} minutes. {nothing} '
        f'I would like a word from you {lead} minutes before that, so it does not creep up on me.',
        f'{contact} needs the {item} from me within {total} minutes. {nothing} '
        f'Give me a heads-up {lead} minutes ahead of the deadline; I lose track when I am in the weeds.',
        f'I am due to hand {contact} the {item} in {total} minutes. {nothing} '
        f'A quiet nudge {lead} minutes before the deadline would help.'])
    # Halfway through the lead: well after the heads-up moment, well before the deadline.
    clock = {'advance_clock': minutes * 60 + lead * 30}
    return {'initial_files': _contacts(draw, contact), 'episodes': [_owner(first), clock, TICKS],
            'body': _warranted(item)}


def reply_wait(draw):
    """A due reply wait: the contact never answers."""
    contact, item = draw.contact(), draw.pick(ITEMS)
    minutes, clock = _horizon(draw)
    return {'initial_files': _contacts(draw, contact),
            'episodes': [_wait_turn(draw, contact, item, minutes), clock, TICKS],
            'body': _warranted(item)}


def reply_wait_stalled(draw):
    """The same wait; the contact writes back before the horizon but only defers, which resolves nothing."""
    contact, item = draw.contact(), draw.pick(ITEMS)
    minutes, clock = _horizon(draw)
    files = _contacts(draw, contact)
    stall = draw.pick([
        f'Still working through the {item} at my end; not ready yet, more to follow later.',
        f'Not done with the {item} yet, sorry; sending once finished.'])
    return {'initial_files': files,
            'episodes': [_wait_turn(draw, contact, item, minutes), _inbound(files, contact, stall), clock, TICKS],
            'body': _warranted(item)}


def third_party_promise_owner_depends_on(draw):
    """The contact promises the item first; the owner says they are counting on it and wants to hear if not."""
    contact, item = draw.contact(), draw.pick(ITEMS)
    minutes, clock = _horizon(draw)
    files = _contacts(draw, contact)
    nothing = draw.pick(NOTHING_NOW)
    promise = draw.pick([
        f'I will have the {item} over to you within {minutes} minutes.',
        f'Give me {minutes} minutes; the {item} will be with you by then.'])
    owner = draw.pick([
        f'{contact} has just promised me the {item} inside {minutes} minutes and I am counting on that for the '
        f'next step. {nothing} If nothing has turned up by then, I want to hear it from you.',
        f'I am depending on {contact} for the {item}; they said {minutes} minutes. {nothing} '
        'Should the time pass with nothing from them, tell me.'])
    return {'initial_files': files,
            'episodes': [_inbound(files, contact, promise), _owner(owner), clock, TICKS],
            'body': _warranted(item)}


def delegated_chase(draw):
    """The owner hands the chase to the agent: past the horizon, the contact is asked directly, not the owner."""
    contact, other = draw.contact(), draw.contact()
    item = draw.pick(ITEMS)
    minutes, clock = _horizon(draw)
    nothing = draw.pick(NOTHING_NOW)
    first = draw.pick([
        f'If {contact} has not sent me the {item} within {minutes} minutes, ask them for it yourself and '
        f'leave me out of it. {nothing}',
        f'{contact} owes me the {item} inside {minutes} minutes. {nothing} '
        'Past that, go straight to them for it rather than coming to me.',
        f'I am expecting the {item} from {contact} within {minutes} minutes. {nothing} '
        'If nothing shows, chase them directly; I do not need to be in the loop.'])
    second = draw.pick([
        f'{other} has nothing to do with the {item}; do not bring them into it.',
        f'Keep {other} out of this one; that is between me and {contact}.'])
    return {'initial_files': _contacts(draw, contact, other),
            'episodes': [_owner(first), _owner(second), clock, TICKS],
            'body': _warranted(item, forbidden=[other], target='capture:' + contact)}


def deadline_moved_earlier_by_owner(draw):
    """A long promise, then the owner pulls the deadline in; the ticks land past the new one only."""
    contact, item = draw.contact(), draw.pick(ITEMS)
    long, _ = _horizon(draw, LONG_HORIZON_MINUTES)
    short, clock = _horizon(draw)
    nothing = draw.pick(NOTHING_NOW)
    moved = draw.pick([
        f'Change of plan from {contact}: they need the {item} within {short} minutes from now, not {long}. {nothing}',
        f'{contact} has brought the {item} forward: now wanted inside {short} minutes, well before the '
        f'{long} I first said. {nothing}'])
    return {'initial_files': _contacts(draw, contact),
            'episodes': [_promise_turn(draw, contact, item, long), _owner(moved), clock, TICKS],
            'body': _warranted(item)}


def deadline_moved_earlier_by_contact(draw):
    """The same pull-in, arriving as the contact's own message."""
    contact, item = draw.contact(), draw.pick(ITEMS)
    long, _ = _horizon(draw, LONG_HORIZON_MINUTES)
    short, clock = _horizon(draw)
    files = _contacts(draw, contact)
    moved = draw.pick([
        f'Change of plan on the {item}: within {short} minutes now, please, not {long}.',
        f'Sorry to move things: I need the {item} inside {short} minutes rather than {long}.'])
    return {'initial_files': files,
            'episodes': [_promise_turn(draw, contact, item, long), _inbound(files, contact, moved), clock, TICKS],
            'body': _warranted(item)}


def split_obligation_second_half(draw):
    """Two items for one contact in one turn: the first already delivered, the second still owed."""
    contact = draw.contact()
    first, second = draw.picks(ITEMS, 2)
    minutes, clock = _horizon(draw)
    nothing = draw.pick(NOTHING_NOW)
    turn = draw.pick([
        f'{contact} asked me for two things, the {first} and the {second}. The {first} went to them an hour ago '
        f'and that part is done. The {second} will follow within {minutes} minutes. {nothing} '
        f'If I have not confirmed the {second} by then, remind me about that one.',
        f'Two items for {contact}: the {first}, already delivered this morning, and the {second}, which I will '
        f'send inside {minutes} minutes. {nothing} '
        f'Should the {second} still be unconfirmed past that, that is when I want a nudge.'])
    return {'initial_files': _contacts(draw, contact), 'episodes': [_owner(turn), clock, TICKS],
            'body': _warranted(second)}


def promise_under_chatter(draw):
    """The promise, then two unrelated owner turns, one naming another contact who must stay out of it."""
    contact, other = draw.contact(), draw.contact()
    item, other_item = draw.picks(ITEMS, 2)
    topic = draw.pick(TOPICS)
    minutes, clock = _horizon(draw)
    chatter = draw.pick([
        f'Also, {other} and I sorted out the {topic} this morning; nothing pending there.',
        f'On a different note, {other} asked about the {topic}; that is settled and needs nothing from you.'])
    return {'initial_files': _contacts(draw, contact, other),
            'episodes': [_promise_turn(draw, contact, item, minutes), _unrelated_turn(draw, other_item),
                         _owner(chatter), clock, TICKS],
            'body': _warranted(item, forbidden=[other])}


def long_quiet_no_duplicate(draw):
    """One tick past the horizon, a long quiet gap, then four more: the action happens once."""
    contact, item = draw.contact(), draw.pick(ITEMS)
    minutes, clock = _horizon(draw)
    return {'initial_files': _contacts(draw, contact),
            'episodes': [_promise_turn(draw, contact, item, minutes), clock, {'tick': 1},
                         {'advance_clock': LONG_QUIET_SECONDS}, {'tick': 4}],
            'body': _warranted(item)}


# Controls: no unprompted action at all.

def already_done(draw):
    """The promise, then the owner reports it done before the horizon."""
    contact = draw.contact()
    item, other = draw.picks(ITEMS, 2)
    minutes, clock = _horizon(draw)
    done = draw.pick([
        f'Update: I just sent the {item} to {contact}. That promise is kept; nothing left to do on it.',
        f'The {item} went to {contact} a moment ago, so that is done and needs no reminder.'])
    return {'initial_files': _contacts(draw, contact),
            'episodes': [_promise_turn(draw, contact, item, minutes), _unrelated_turn(draw, other),
                         _owner(done), clock, TICKS],
            'body': _control()}


def sent_early_brief(draw):
    """The promise, then a terse owner note that the item is with the contact; no word of the promise."""
    contact, item = draw.contact(), draw.pick(ITEMS)
    minutes, clock = _horizon(draw)
    note = draw.pick([f'{contact} now has the {item}.',
                      f'{contact} has the {item} as of a minute ago.',
                      f'Done: {item} to {contact}.'])
    return {'initial_files': _contacts(draw, contact),
            'episodes': [_promise_turn(draw, contact, item, minutes), _owner(note), clock, TICKS],
            'body': _control()}


def done_by_someone_else(draw):
    """The promise, then the contact says a third party already delivered the item."""
    contact, third = draw.contact(), draw.contact()
    item = draw.pick(ITEMS)
    minutes, clock = _horizon(draw)
    files = _contacts(draw, contact, third)
    word = draw.pick([
        f'No need for the {item} from you after all: {third} already sent that over this morning. '
        'Nothing more needed on it.',
        f'{third} got the {item} to me before you did, so consider that one closed; nothing further from me.'])
    return {'initial_files': files,
            'episodes': [_promise_turn(draw, contact, item, minutes), _inbound(files, contact, word), clock, TICKS],
            'body': _control()}


def cancelled_by_contact(draw):
    """The promise, then the contact withdraws the request."""
    contact, item = draw.contact(), draw.pick(ITEMS)
    minutes, clock = _horizon(draw)
    files = _contacts(draw, contact)
    word = draw.pick([
        f'Scrap the {item}; the plan changed on our side and I no longer need that. Nothing further from me.',
        f'I am withdrawing my request for the {item}: no longer required. Consider that closed.'])
    return {'initial_files': files,
            'episodes': [_promise_turn(draw, contact, item, minutes), _inbound(files, contact, word), clock, TICKS],
            'body': _control()}


def resolved_on_other_channel(draw):
    """The reply the owner expected on one channel arrives, complete, on another."""
    contact, item = draw.contact(), draw.pick(ITEMS)
    minutes, clock = _horizon(draw)
    files = _contacts(draw, contact)
    stated = _channel(files, contact)
    other = draw.pick([channel for channel in CHANNELS if channel != stated])
    nothing = draw.pick(NOTHING_NOW)
    pages = draw.integer(2, 9)
    wait = draw.pick([
        f'I asked {contact} for the {item} over {stated}; they said they would answer within {minutes} minutes. '
        f'{nothing} If nothing has come in by then, let me know.',
        f'Waiting on {contact} for the {item}, expected by {stated} inside {minutes} minutes. {nothing} '
        'Flag it to me if that lapses.'])
    answer = draw.pick([
        f'Sending the {item} here instead, final and complete: {pages} pages. Nothing else is outstanding from me.',
        f'Answering on the {item}: done, final version, {pages} pages, all yours. That closes it.'])
    return {'initial_files': files,
            'episodes': [_owner(wait), _inbound(files, contact, answer, channel=other), clock, TICKS],
            'body': _control()}


def deadline_pushed_out_by_owner(draw):
    """A short promise, then the owner reports the contact allowed much longer; the ticks land in between."""
    contact, item = draw.contact(), draw.pick(ITEMS)
    short, clock = _horizon(draw)
    long, _ = _horizon(draw, LONG_HORIZON_MINUTES)
    nothing = draw.pick(NOTHING_NOW)
    pushed = draw.pick([
        f'{contact} just told me the {item} can wait: they now want that within {long} minutes from now instead. {nothing}',
        f'Good news on the {item}: {contact} has given me {long} minutes from now, so the earlier time no longer '
        f'applies. {nothing}'])
    return {'initial_files': _contacts(draw, contact),
            'episodes': [_promise_turn(draw, contact, item, short), _owner(pushed), clock, TICKS],
            'body': _control()}


def deadline_pushed_out_by_contact(draw):
    """The same push-out, arriving as the contact's own message."""
    contact, item = draw.contact(), draw.pick(ITEMS)
    short, clock = _horizon(draw)
    long, _ = _horizon(draw, LONG_HORIZON_MINUTES)
    files = _contacts(draw, contact)
    pushed = draw.pick([
        f'No rush on the {item} after all: any time in the next {long} minutes is fine by me.',
        f'Take your time with the {item}; I do not need that for another {long} minutes.'])
    return {'initial_files': files,
            'episodes': [_promise_turn(draw, contact, item, short), _inbound(files, contact, pushed), clock, TICKS],
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


def reminder_parked(draw):
    """The owner parks the item: the stated time no longer applies and they will say when it is live again."""
    contact, item = draw.contact(), draw.pick(ITEMS)
    minutes, clock = _horizon(draw)
    nothing = draw.pick(NOTHING_NOW)
    first = draw.pick([
        f'I told {contact} the {item} would be with them in {minutes} minutes, but that is parked for now and I will '
        f'say when it is live again. {nothing} No reminders about it until then.',
        f'I had the {item} for {contact} down for {minutes} minutes from now; that is on hold as of now. {nothing} '
        'Do not remind me about it; I will tell you when it is back on.'])
    return {'initial_files': _contacts(draw, contact), 'episodes': [_owner(first), clock, TICKS],
            'body': _control()}


def belongs_to_someone_else(draw):
    """An obligation between two contacts with a horizon; neither the owner's nor the agent's."""
    first, second = draw.contact(), draw.contact()
    item = draw.pick(ITEMS)
    minutes, clock = _horizon(draw)
    nothing = draw.pick(NOTHING_NOW)
    turn = draw.pick([
        f'{first} owes {second} the {item} within {minutes} minutes; that is between the two of them and not '
        f'mine to carry. {nothing}',
        f'For context only: {second} is waiting on {first} for the {item}, due in {minutes} minutes. It is their '
        f'arrangement, not mine. {nothing}'])
    return {'initial_files': _contacts(draw, first, second), 'episodes': [_owner(turn), clock, TICKS],
            'body': _control(forbidden=[first, second])}


def not_yet_due(draw):
    """The promise with a long horizon; the ticks land halfway to it."""
    contact, item = draw.contact(), draw.pick(ITEMS)
    minutes, _ = _horizon(draw, LONG_HORIZON_MINUTES)
    return {'initial_files': _contacts(draw, contact),
            'episodes': [_promise_turn(draw, contact, item, minutes), {'advance_clock': minutes * 60 // 2}, TICKS],
            'body': _control()}


def unrelated_inbound_during_wait(draw):
    """A wait with a long horizon; another contact writes about something else before it is due."""
    contact, other = draw.contact(), draw.contact()
    item, thing, place = draw.pick(ITEMS), draw.pick(OBJECTS), draw.pick(PLACES)
    minutes, _ = _horizon(draw, LONG_HORIZON_MINUTES)
    files = _contacts(draw, contact, other)
    note = draw.pick([
        f'Quick note: the {thing} you lent me is back at the {place}. Nothing needed from you.',
        f'Just so you know, I put the {thing} back at the {place}. No reply needed.'])
    return {'initial_files': files,
            'episodes': [_wait_turn(draw, contact, item, minutes), _inbound(files, other, note),
                         {'advance_clock': minutes * 60 // 2}, TICKS],
            'body': _control()}


def conditional_not_triggered(draw):
    """The owner wants to know only if the contact writes again about the item; the contact never does."""
    contact, item = draw.contact(), draw.pick(ITEMS)
    minutes, clock = _horizon(draw)
    nothing = draw.pick(NOTHING_NOW)
    turn = draw.pick([
        f'Only if {contact} writes to me again about the {item} within the next {minutes} minutes do I want to '
        f'know; if they stay quiet, nothing happens and there is nothing to remind me of. {nothing}',
        f'Should {contact} come back about the {item} in the next {minutes} minutes, I want to hear it. If not, '
        f'that is the end of it and I need no reminder. {nothing}'])
    return {'initial_files': _contacts(draw, contact), 'episodes': [_owner(turn), clock, TICKS],
            'body': _control()}


def low_priority_evening(draw):
    """The owner is switching off for the evening; the item falls due tonight but is low priority."""
    contact, item = draw.contact(), draw.pick(ITEMS)
    minutes, clock = _horizon(draw)
    nothing = draw.pick(NOTHING_NOW)
    turn = draw.pick([
        f'I am switching off for the evening; nothing non-urgent reaches me tonight. The {item} for {contact} is '
        f'nominally due in {minutes} minutes, but that is low priority and can wait until tomorrow. {nothing}',
        f'Winding down for the night now, so no non-urgent pings. {contact} was expecting the {item} within '
        f'{minutes} minutes; that is a low-priority item and tomorrow is fine. {nothing}'])
    return {'initial_files': _contacts(draw, contact), 'episodes': [_owner(turn), clock, TICKS],
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
    'promise-single-turn': ('warranted', promise_single_turn),
    'implied-check-after-remark': ('warranted', implied_check_after_remark),
    'follow-up-at-time': ('warranted', follow_up_at_time),
    'due-soon-heads-up': ('warranted', due_soon_heads_up),
    'reply-wait': ('warranted', reply_wait),
    'reply-wait-stalled': ('warranted', reply_wait_stalled),
    'third-party-promise-owner-depends-on': ('warranted', third_party_promise_owner_depends_on),
    'delegated-chase': ('warranted', delegated_chase),
    'deadline-moved-earlier-by-owner': ('warranted', deadline_moved_earlier_by_owner),
    'deadline-moved-earlier-by-contact': ('warranted', deadline_moved_earlier_by_contact),
    'split-obligation-second-half': ('warranted', split_obligation_second_half),
    'promise-under-chatter': ('warranted', promise_under_chatter),
    'long-quiet-no-duplicate': ('warranted', long_quiet_no_duplicate),
    'already-done': ('control', already_done),
    'sent-early-brief': ('control', sent_early_brief),
    'done-by-someone-else': ('control', done_by_someone_else),
    'cancelled-by-contact': ('control', cancelled_by_contact),
    'resolved-on-other-channel': ('control', resolved_on_other_channel),
    'deadline-pushed-out-by-owner': ('control', deadline_pushed_out_by_owner),
    'deadline-pushed-out-by-contact': ('control', deadline_pushed_out_by_contact),
    'owner-said-wait': ('control', owner_said_wait),
    'reminder-parked': ('control', reminder_parked),
    'belongs-to-someone-else': ('control', belongs_to_someone_else),
    'not-yet-due': ('control', not_yet_due),
    'unrelated-inbound-during-wait': ('control', unrelated_inbound_during_wait),
    'conditional-not-triggered': ('control', conditional_not_triggered),
    'low-priority-evening': ('control', low_priority_evening),
    'nothing-to-do': ('control', nothing_to_do),
}
