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
