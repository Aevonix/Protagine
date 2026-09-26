"""The owner's words about outreach, read deterministically (architecture 4.10, M11)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from protagine.mind import reactions
from protagine.mind.reactions import clean_object, mentions_contact, read

GENERATORS = Path(__file__).resolve().parents[2] / 'benchmarks' / 'paired' / 'generators'


def classes(text, **kwargs):
    return set(read(text, **kwargs).classes)


# Paraphrases per class, in forms the dev templates do not use.
PARAPHRASES = {
    "stop": ["Stop checking in on me.", "Please quit messaging me unprompted.",
             "You don't need to check in with me anymore.", "No more check-ins, please.",
             "Only message me when I ask.", "STOP", "Stop sending me updates.", "Stop reaching out, thanks."],
    "resume": ["You can check in again.", "Feel free to check in whenever you like.",
               "Start checking in again, please.", "Check-ins are fine again."],
    "pause_today": ["Leave me alone for the rest of today.", "No messages today, please.", "I need to focus today.",
                    "Not today, please.", "Please don't disturb me this afternoon."],
    "not_now": ["Not now.", "I'm in a meeting.", "Maybe later.", "Busy right now, sorry.", "Can't talk.",
                "That will have to wait."],
    "negative": ["Not interested.", "That wasn't useful.", "Not for me, thanks.", "Drop it.",
                 "Not relevant to me.", "No thanks."],
    "positive": ["Dig deeper into that.", "Tell me more.", "Can you find out who ran it", "Look into it further.",
                 "Go deeper on this one.", "Yes please, more on that."],
    "welcome": ["That was useful, thanks.", "Great find.", "Very helpful, thank you.", "I liked this one."],
}
DECLARED = {
    "I care a lot about tidal energy.": ["tidal energy"],
    "Keep me posted on kelp farming.": ["kelp farming"],
    "I'm really into violin making these days.": ["violin making"],
    "I'd love to hear about meteor showers.": ["meteor showers"],
    "Keep an eye out for coral restoration news.": ["coral restoration"],
    "I follow bread fermentation closely.": ["bread fermentation"],
    "I am interested in glacier survey work": ["glacier survey work"],
}
STRAINED = {
    "I'm stressed about the grant report.": ["grant report"],
    "I'm behind on the thesis chapter, badly.": ["thesis chapter"],
    "The visa application is stressing me out.": ["visa application"],
    "Drowning in the conference talk prep right now.": ["conference talk prep"],
    "Dreading the insurance form.": ["insurance form"],
    "Honestly worried about the lease renewal: it is due Friday.": ["lease renewal"],
}
RELIEVED = {
    "I finished the grant report.": ["grant report"],
    "The thesis chapter is done.": ["thesis chapter"],
    "Sorted out the visa application, finally.": [],
    "I sorted out the visa application.": ["visa application"],
    "The lease renewal is under control.": ["lease renewal"],
}


@pytest.mark.parametrize("name,texts", sorted(PARAPHRASES.items()))
def test_each_class_reads_its_paraphrases(name, texts):
    for text in texts:
        assert name in classes(text), (name, text, read(text))


def test_a_gateway_prefix_is_not_the_owners_words():
    assert read("[Wed 2031-05-07 13:00:02 UTC] Not now.").not_now
    assert read("[Wed 2031-05-07 13:00:02 UTC] STOP").stop


@pytest.mark.parametrize("text,expected", sorted(DECLARED.items()))
def test_declarations_name_the_topic(text, expected):
    assert read(text).declarations == expected


@pytest.mark.parametrize("text,expected", sorted(STRAINED.items()))
def test_strain_names_the_thing(text, expected):
    assert read(text).strains == expected


@pytest.mark.parametrize("text,expected", sorted(RELIEVED.items()))
def test_relief_names_the_thing(text, expected):
    assert read(text).reliefs == expected


def test_relief_that_names_nothing_is_still_relief():
    assert read("All good now, thanks.").relieved and read("No longer worried.").relieved


def test_a_negation_voids_the_cue_that_follows_it():
    assert read("I'm not stressed about the grant report.").strains == []
    assert read("I am not worried about the lease renewal any more.").strains == []
    assert read("Don't dig deeper, it's fine.").positive is False
    assert read("I'm not busy right now.").not_now is False
    # A cue that is negative itself is never voided by its own negation.
    assert read("I am not interested in fern species.").negative_objects == ["fern species"]
    assert read("I am not interested in fern species.").declarations == []
    assert read("I don't care about clock repair.").negative_objects == ["clock repair"]
    assert read("I don't care about clock repair.").declarations == []


def test_negative_objects_are_topic_scoped_and_a_bare_negative_needs_a_link():
    about = read("Not interested in tidal energy after all, drop it.")
    assert about.negative and about.negative_objects == ["tidal energy"] and not about.needs_link()
    bare = read("That item you sent was not useful to me.")
    assert bare.negative and bare.negative_objects == [] and bare.needs_link()
    assert read("I could not care less about cave diving myself.").negative_objects == ["cave diving"]
    assert read("Spare me the clock repair stuff.").negative_objects == ["clock repair"]
    assert read("Stop sending me kelp farming links.").negative_objects == ["kelp farming links"]


def test_objects_are_clipped_at_six_words_and_at_a_conjunction():
    assert read("Keep me posted on one two three four five six seven eight.").declarations == [
        "one two three four five six"]
    assert read("Keep me posted on tidal energy and anything else you find.").declarations == ["tidal energy"]
    assert clean_object("the rest of it") == "rest of it"
    assert clean_object("it") is None and clean_object("either") is None and clean_object("   ") is None


def test_a_person_is_never_an_object():
    assert read("I'm worried about p-03.").strains == []
    assert read("I'm worried about my mother.").strains == []
    assert read("I'm worried about Sam's move", contacts=["Sam"]).strains == []
    assert read("I'm worried about the garden plan", contacts=["Sam"]).strains == ["garden plan"]
    assert mentions_contact("ask p-07 about it") and mentions_contact("Robin said so", ["Robin"])
    assert not mentions_contact("robinson crusoe", ["Robin"])


def test_one_turn_may_carry_several_classes_and_the_strongest_reaction_wins():
    turn = read("I'm behind on the grant report and it is stressing me out. Also, please stop checking in "
                "with me unprompted; I will ask when I want something.")
    assert turn.stop and turn.strains == ["grant report"] and turn.reaction() == "stop"
    dig = read("Yes, dig deeper into the tidal energy item you sent. I'm off out, so later, please.")
    assert dig.positive and dig.not_now and dig.reaction() == "positive"
    drop = read("Not interested in kelp farming. I would like to hear about meteor showers, though.")
    assert drop.negative_objects == ["kelp farming"] and drop.declarations == ["meteor showers"]
    assert drop.reaction() == "negative"


def test_ordinary_conversation_carries_no_class():
    for text in ("For reference, my standing preferences, quiet hours included, are in owner.json. "
                 "Nothing to do right now.",
                 "I have to get the lease renewal finished within the next two weeks; no rush.",
                 "Thanks, see you tomorrow.", "What time is it in Lisbon?", ""):
        assert read(text).classes == [], (text, read(text))


# The dev family's own turns: every owner turn reads as the template means it.

def _family():
    spec = importlib.util.spec_from_file_location('paired_generate_reactions', GENERATORS / 'generate.py')
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)
    module = engine.load_templates(GENERATORS / 'outreach.py')
    return module, engine.render(module, 7, 4) + engine.render(module, 11, 4)


FAMILY, SCENARIOS = _family()
EXPECTED = {  # template -> classes of each owner turn after the preferences turn
    'finding-for-stated-interest': [{'declaration'}],
    'quiet-stretch-open-loop': [set()],
    'strain-offer': [{'strain'}],
    'finding-off-interest': [{'negative'}],
    'leave-me-alone-today': [{'declaration'}, {'pause_today'}],
    'burst-one-message': [{'declaration'}],
    'two-reasons-within-the-hour': [{'declaration'}, {'strain'}],
    'same-reading-next-week': [{'declaration'}],
    'own-work-after-outreach': [{'declaration'}, {'positive'}],
    'quiet-hours': [{'declaration'}],
    'rated-not-useful-then-similar': [{'declaration'}, {'negative'}],
    'open-loop-talked-recently': [set()],
    'stop-checking-in': [{'strain', 'stop'}],
    'reply-dig-deeper': [{'declaration'}, {'positive'}],
    'reply-not-interested-other-topic': [{'declaration'}, {'negative', 'declaration'}],
    'reply-not-now': [{'declaration'}, {'not_now'}],
}


@pytest.mark.parametrize('scenario', SCENARIOS, ids=[s['id'] + '-' + str(s['seed'])[:4] for s in SCENARIOS])
def test_every_dev_owner_turn_reads_as_its_template_means(scenario):
    turns = [entry['user'] for entry in scenario['episodes'] if 'user' in entry]
    assert classes(turns[0]) == set()
    readings = [read(text) for text in turns[1:]]
    got = [set(reading.classes) - ({'welcome'} if 'positive' in reading.classes else set()) for reading in readings]
    assert got == EXPECTED[scenario['scenario']], (turns, readings)
    topics = {item['topic'] for item in __import__('json').loads(scenario['initial_files']['reading.json'])['items']}
    for reading in readings:
        # Every topic named is a reading topic, never a filler phrase around it.
        assert set(reading.declarations) <= topics and set(reading.negative_objects) <= topics, reading
        assert all(strain in FAMILY.ITEMS for strain in reading.strains), reading


def test_the_names_the_design_uses_answer_one_question_each():
    assert reactions.classify_reaction("Not now.") == "not_now"
    assert reactions.declared_interest("Keep me posted on kelp farming.") == ["kelp farming"]
    assert reactions.disinterest("Not interested in kelp farming.") == ["kelp farming"]
    assert reactions.strain("I'm behind on the grant report.") == ["grant report"]
    assert reactions.relief("I finished the grant report.") == ["grant report"]
    assert reactions.stop("Stop checking in.") and reactions.pause_today("Not today.")
    assert reactions.resume("You can check in again.")


@pytest.mark.parametrize('text', [
    "I need to focus on the grant report this week.",       # work talk, not a request for quiet
    "The shop is closed until tomorrow.",
    "Not today's problem, anyway.",
    "Can you drop it off at the office later?",              # an errand, not a dismissal
    "I'll bring it up in a meeting next week.",
    "I tried that route another time and it was slow.",
    "I follow the recipe exactly, every time.",               # not a topic the owner follows
    "I'm into the final stretch of the move.",
    "I'm interested in whether the shop opens early.",
])
def test_ordinary_speech_that_resembles_a_cue_is_not_one(text):
    reading = read(text)
    assert not (reading.pause_today or reading.not_now or reading.negative or reading.declarations), (text, reading)


def test_a_position_link_needs_a_short_or_referring_reply():
    assert reactions.refers_back("Dig deeper into that.") and reactions.refers_back("Not now.")
    assert reactions.refers_back("Not useful, sorry, the item you sent missed the point for me entirely today.")
    assert not reactions.refers_back("I need to find out when the last train leaves the central station tonight.")


# -- what only a reply to an outreach may mean (review of M11) ---------------------------------------------

def test_a_family_word_is_never_an_object_whatever_its_letters():
    """A possessive is folded as a suffix: "sister", "son" and "boss" are people, not "ister", "on", "bo"."""
    for text in ("I am worried about my sister.", "I am really stressed about my boss.", "I'm worried about my son.",
                 "Worried about my sisters, honestly.", "I'm stressed about my boss's review."):
        assert read(text).strains == [], (text, read(text))
    assert read("I'm stressed about the bass recital.").strains == ["bass recital"]
    assert read("I'm worried about the sons of the soil essay.").strains == []


def test_a_bare_stop_and_a_vague_pause_mean_something_only_as_a_reply():
    """The owner types "stop" to halt a turn, and "not today" inside a request: alone they need a link to an
    outreach; an explicit stop or a pause that names the day stands on its own."""
    for text in ("stop", "Stop!", "[Wed 2031-05-07 13:00:02 UTC] STOP"):
        reading = read(text)
        assert reading.stop and not reading.stop_explicit and reading.needs_link(), text
    for text in ("Stop checking in on me.", "Please quit messaging me unprompted.", "Leave me alone."):
        assert read(text).stop_explicit, text
    for text in ("Not today.", "I need to focus.", "Hold it until tomorrow, please."):
        reading = read(text)
        assert reading.pause_today and not reading.pause_explicit and reading.needs_link(), text
    for text in ("Leave me alone for the rest of today.", "No messages today, please.", "I need to focus today.",
                 "Please don't disturb me this afternoon."):
        assert read(text).pause_explicit, text


SENT = ('You said "I care a lot about tidal energy", so I looked into tidal energy: finding: QX-41: A practical '
        'study of tidal energy was published.')


def known():
    return reactions.content_terms(SENT)


def about_it(text):
    return not (reactions.content_terms(text) - known())


@pytest.mark.parametrize("text", ["Can you find out when the last train leaves?", "Look into flights to Lisbon for May.",
                                  "Tell me more about the weather tomorrow.", "Keep going with the draft."])
def test_a_positive_cue_aimed_at_something_else_is_no_reaction_to_the_outreach(text):
    reading = read(text)
    assert reading.positive and reading.reaction() == "positive"
    assert reading.reaction(about=about_it) is None, (text, reading)


@pytest.mark.parametrize("text", ["Dig deeper.", "Tell me more.", "Yes, please.", "Look into it further.",
                                  "Dig deeper on that study.", "Yes, dig deeper into the tidal energy item you sent."])
def test_a_bare_positive_or_one_about_what_was_sent_is_a_reaction_to_it(text):
    assert read(text).reaction(about=about_it) == "positive", (text, read(text))


def test_a_negative_about_another_topic_leaves_the_outreach_alone():
    mixed = read("Not interested in the fern stuff, but dig deeper into tidal energy.")
    assert mixed.negative_objects == ["fern"] and mixed.reaction() == "negative"
    assert mixed.reaction(about=about_it) == "positive"
    assert read("Not interested in tidal energy, but dig deeper into ferns.").reaction(about=about_it) == "negative"
    assert read("That was not useful.").reaction(about=about_it) == "negative"


@pytest.mark.parametrize("text", ["Can you book the dentist? Not today, maybe Friday.",
                                  "That was not useful, try again with a shorter version.",
                                  "Could you remind me about the car at six? Not now though."])
def test_a_turn_with_a_request_of_its_own_or_a_redo_is_not_a_reply(text):
    assert reactions.elsewhere(text, known()), text


@pytest.mark.parametrize("text", ["Not today.", "Can you dig deeper?", "That was not useful to me.", "stop",
                                  "Could you look into that study more?", "Not now, sorry."])
def test_a_reply_about_the_outreach_itself_is_not_elsewhere(text):
    assert not reactions.elsewhere(text, known()), text


@pytest.mark.parametrize("text", [
    'Alice said: "you can check in again". What should I say?',
    "My sister told me you can check in again whenever you like; odd thing to say.",
    "Bob wrote 'stop checking in on me' in the group chat, how do I answer?",
    "The note says “feel free to reach out” at the bottom.",
    "My manager said: stop checking in so often. Is that fair?",
])
def test_a_hold_phrase_someone_else_said_is_not_the_owners_instruction(text):
    """Round 3: the reader never takes such a resume; a stop beside reported speech is put to the model
    (``unsure``), standing meanwhile (``test_mind_typed_decisions``)."""
    reading = read(text)
    assert not reading.resume and reading.unsure and (not reading.stop or "stop" in reading.unsure), text


@pytest.mark.parametrize("text", ["You can check in again, I said.", "Ok, you can check in again.",
                                  "Stop checking in, I don't need it.", "I said stop checking in!"])
def test_the_owners_own_hold_phrase_still_counts(text):
    """Taken by the reader, or put to the model with the reply-linked fallback ready (``resume_if_linked``)."""
    reading = read(text)
    assert reading.resume or reading.stop or reading.resume_if_linked


# -- round 2: whose words, across lines, blockquotes and adverbs ---------------------------------------------

REPORTERS = ["Alice", "My boss", "She", "They", "Bob from accounts", "The landlord", "my sister", "Kim"]
VERBS = ["said", "wrote", "texted", "told me", "asked", "replied", "just said", "already wrote", "has said",
         "messaged", "literally just told me"]
RESUME_LAYOUTS = [
    "{r} {v}: you can check in again. What should I say?",
    "{r} {v}:\nYou can check in again.\nWhat should I say?",
    "{r} {v}:\n\nYou can check in again.\nWhat should I say?",
    "{r} {v}:\nHey!\nYou can check in again.\n\nWhat should I say?",
    "{r} {v} that you can check in again, thoughts?",
    "{r} {v}: thanks for the update. You can check in again.",
    "> You can check in again.\nWhat should I say to {r}?",
    ">> You can check in again.\n> hi\nWhat should I say to {r}?",
    "Got this from {r}:\n> Feel free to reach out.\nHow do I reply?",
    "{r}:\nYou can check in again.\nHow do I reply?",
    "{r}: you can check in again\nMe: ok",
    "According to {r}, you can check in again.",
    "{r} {v}, and I quote, you can check in again.",
]


@pytest.mark.parametrize("layout", RESUME_LAYOUTS)
@pytest.mark.parametrize("reporter, verb", [(r, v) for r in REPORTERS for v in VERBS][::7])
def test_a_resume_in_someone_elses_words_is_never_the_owners(layout, reporter, verb):
    text = layout.format(r=reporter, v=verb)
    text = text[0].upper() + text[1:]
    assert not read(text).resume, text


STOP_LAYOUTS = [
    "{r} {v}: stop checking in on the team. What should I say?",
    "{r} {v}:\nStop checking in on the team.\nWhat should I say?",
    "{r} {v} that you should stop checking in on them, is that fair?",
    "> Stop checking in with me.\nWhat should I say to {r}?",
    "{r}:\nStop checking in!\nHow do I reply?",
    "According to {r}, stop checking in on weekends.",
]


@pytest.mark.parametrize("layout", STOP_LAYOUTS)
@pytest.mark.parametrize("reporter, verb", [(r, v) for r in REPORTERS for v in VERBS][::9])
def test_a_stop_in_someone_elses_words_is_put_to_the_model(layout, reporter, verb):
    """Round 3: whose stop this is, is the model's typed decision; meanwhile (and without it) the stop stands, a
    pause the owner can lift (``test_mind_typed_decisions``)."""
    text = layout.format(r=reporter, v=verb)
    text = text[0].upper() + text[1:]
    assert "stop" in read(text).unsure, text


@pytest.mark.parametrize("text", [
    "I just said stop checking in.", "I already told you to stop checking in.", "I've already said it: stop checking in.",
    "As I said, stop checking in.", "Like I told you, no more check-ins.", "I literally just told you to stop checking in.",
    "I said it twice now, stop checking in!", "We already asked you to stop checking in.",
    "I told you yesterday: stop checking in.", "I've told you before, stop messaging me.",
    "Alice said hi. Anyway, stop checking in.", "Alice said:\n> hi there\n\nStop checking in.",
    "i just said stop checking in", "I just said stop checking in.\nThanks.",
    "I’ve already told you, stop checking in.", "I just said: stop checking in!",
])
def test_the_owners_own_report_of_their_stop_is_a_stop(text):
    """Re-check new P2: "just", "already" and other adverbs or auxiliaries are never taken for the speaker."""
    assert read(text).stop, text


@pytest.mark.parametrize("text", [
    "You can check in again, I said.", "I just said you can check in again.",
    "I already told you: you can check in again.", "Alice said hi.\n\nYou can check in again.",
    "Like I said, feel free to reach out.", "I’ve said it already, you can check in again.",
])
def test_the_owners_own_report_of_their_resume_is_a_resume(text):
    """Beside reported-speech markers a resume is the model's to decide; without it, the strict reading finds it
    the owner's, taken as a reply linked to an outreach (``resume_if_linked``)."""
    reading = read(text)
    assert "resume" in reading.unsure and reading.resume_if_linked, text


@pytest.mark.parametrize("text", ["I never said you can check in again.", "I didn't say stop checking in.",
                                  "I did not tell you to stop checking in.", "I haven't said you can check in again."])
def test_what_the_owner_says_they_did_not_say_is_no_instruction(text):
    """A denied resume is never taken, even as a linked reply; a denied stop is the model's to decide."""
    reading = read(text)
    assert not reading.resume and not reading.resume_if_linked and (not reading.stop or "stop" in reading.unsure), text
