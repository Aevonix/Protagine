"""Labelled sets for the decision points (``protagine.decisions.POINTS``), built from the repository's own words.

Every item is ``{"point", "fields", "gold", "source"}``: ``fields`` are what the point's state is written from,
``gold`` its label (a choice label, or "yes"/"no"), ``source`` where the words come from. Sources:

- ``generator:<family>/<template>``: an owner turn of a dev family, rendered from the dev seeds below (never a
  held-out split), labelled by what the template means;
- ``test:<module>``: a phrase the sidecar suite labels (the reactions paraphrases and ordinary speech, the
  opt-out family, close variants and near misses, the capture and lesson fixtures);
- ``novel``: paraphrases written for this measurement in forms neither the phrase tables nor the templates use,
  so the sets also say what the phrase tables miss. They are reported separately.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[2]
GENERATORS = ROOT / "benchmarks" / "paired" / "generators"
TESTS = ROOT / "sidecar" / "tests"
DEV_SEEDS = (7, 11, 13)
PER_TEMPLATE = 3


def _load(path: Path, name: str):
    if str(path.parent) not in sys.path:
        sys.path.insert(0, str(path.parent))        # a test module imports its siblings
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _engine():
    return _load(GENERATORS / "generate.py", "decision_sets_generate")


def _scenarios(family: str) -> List[Dict[str, Any]]:
    engine = _engine()
    module = engine.load_templates(GENERATORS / f"{family}.py")
    rendered = []
    for seed in DEV_SEEDS:
        rendered += engine.render(module, seed, PER_TEMPLATE)
    return rendered


def _owner_turns(scenario) -> List[Dict[str, Any]]:
    return [entry for entry in scenario["episodes"] if "user" in entry]


def item(point: str, gold: str, source: str, **fields: Any) -> Dict[str, Any]:
    return {"point": point, "fields": fields, "gold": gold, "source": source}


def _unique(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen, result = set(), []
    for entry in items:
        key = (entry["point"], tuple(sorted(entry["fields"].items())))
        if key not in seen:
            seen.add(key)
            result.append(entry)
    return result


# -- outreach_reply ------------------------------------------------------------------------------------------

# The reactions module's classes as the decision's labels: a pause for the day is a "not now" to the message.
REPLY_LABEL = {"welcome": "engaged", "positive": "dig_deeper", "negative": "not_interested", "not_now": "not_now",
               "pause_today": "not_now", "stop": "stop", None: "engaged"}
NOVEL_REPLIES = {
    "engaged": ["Oh nice, thanks.", "Cool, noted.", "Ha, fascinating stuff.", "Good to know.", "Appreciated!",
                "Neat. Saved it for the weekend."],
    "dig_deeper": ["Who ran the study?", "Can you send me the full paper?", "What did they actually find?",
                   "Any numbers behind that?", "Get me the details when you can.", "Keep digging on this one."],
    "not_interested": ["Meh, not my thing.", "Pass.", "Skip these ones from now on.", "I stopped following that.",
                       "Nah, this one is boring.", "Doesn't do anything for me."],
    "not_now": ["Later.", "Busy, ping me tonight.", "Can't look now, driving.", "Send it again this evening.",
                "Remind me after lunch.", "Not a great moment, try tomorrow."],
    "stop": ["Please don't message me unless I ask.", "I don't want these unprompted messages.",
             "Quit sending me stuff out of the blue.", "No more of these, ever.",
             "I'd rather you never reached out on your own.", "Stop messaging me first."],
}


def outreach_reply() -> List[Dict[str, Any]]:
    reactions_tests = _load(TESTS / "test_mind_reactions.py", "decision_sets_reactions")
    outreach = _load(GENERATORS / "outreach.py", "decision_sets_outreach")
    topics = outreach.TOPICS
    items = []
    for index, (name, texts) in enumerate(sorted(reactions_tests.PARAPHRASES.items())):
        if name == "resume":
            continue            # no reply to a message: it lifts a pause
        for offset, text in enumerate(texts):
            items.append(item("outreach_reply", REPLY_LABEL[name], "test:test_mind_reactions",
                              text=text, topic=topics[(index + offset) % len(topics)]))
    ordinary = ["Thanks, see you tomorrow.", "What time is it in Lisbon?",
                "I have to get the lease renewal finished within the next two weeks; no rush.",
                "I need to focus on the grant report this week.", "The shop is closed until tomorrow.",
                "Not today's problem, anyway.", "Can you drop it off at the office later?",
                "I'll bring it up in a meeting next week.", "I tried that route another time and it was slow."]
    for offset, text in enumerate(ordinary):
        items.append(item("outreach_reply", "engaged", "test:test_mind_reactions", text=text,
                          topic=topics[offset % len(topics)]))
    replies = {"reply-dig-deeper": "dig_deeper", "reply-not-interested-other-topic": "not_interested",
               "reply-not-now": "not_now", "finding-off-interest": "not_interested",
               "rated-not-useful-then-similar": "not_interested", "own-work-after-outreach": "engaged"}
    for scenario in _scenarios("outreach"):
        gold = replies.get(scenario["scenario"])
        if gold is None:
            continue
        turns = _owner_turns(scenario)
        topic = _topic(scenario)
        items.append(item("outreach_reply", gold, f"generator:outreach/{scenario['scenario']}",
                          text=turns[-1]["user"], topic=topic))
    for gold, texts in NOVEL_REPLIES.items():
        for offset, text in enumerate(texts):
            items.append(item("outreach_reply", gold, "novel", text=text, topic=topics[offset % len(topics)]))
    return _unique(items)


def _topic(scenario) -> str:
    """The topic the scenario's outreach is about: the one the owner declared first (the mind shares a finding on
    it), else the reading topic the reply names."""
    import json
    from protagine.mind.reactions import read
    turns = [turn["user"] for turn in _owner_turns(scenario)]
    for text in turns[:-1]:
        declared = read(text).declarations
        if declared:
            return declared[0]
    items = json.loads(scenario["initial_files"]["reading.json"])["items"]
    named = [entry["topic"] for entry in items if entry["topic"] in turns[-1]]
    return named[0] if named else items[0]["topic"]


# -- opt_out -------------------------------------------------------------------------------------------------

NOVEL_OPT_OUTS = {
    "yes": ["Please stop contacting me.", "I don't want to hear from you anymore.", "Take me off whatever this is.",
            "Stop messaging this number.", "Do not write to me again, thanks.", "Lose my number.",
            "I'm done with these messages, cut me off.", "No further messages please."],
    "no": ["Could you message me the address later?", "Please don't text before noon.",
           "Stop, I meant Thursday, not Tuesday.", "I'll leave it to you.", "Send me the invoice by email instead.",
           "Take me through the numbers tomorrow?", "Don't write the summary yet, I'm still editing.",
           "No more coffee meetings this week, I'm swamped."],
}


def opt_out() -> List[Dict[str, Any]]:
    tests = _load(TESTS / "test_optout.py", "decision_sets_optout")
    reactions_tests = _load(TESTS / "test_mind_reactions.py", "decision_sets_reactions")
    items = [item("opt_out", "yes", "test:test_optout", text=text) for text in tests.FAMILY + tests.CLOSE_VARIANTS]
    items += [item("opt_out", "no", "test:test_optout", text=text) for text in tests.NEAR_MISSES]
    items += [item("opt_out", "yes", "test:test_mind_reactions", text=text)
              for text in reactions_tests.PARAPHRASES["stop"]]
    items += [item("opt_out", "no", "test:test_mind_reactions", text=text)
              for name in ("not_now", "negative", "positive", "welcome", "pause_today")
              for text in reactions_tests.PARAPHRASES[name]]
    for gold, texts in NOVEL_OPT_OUTS.items():
        items += [item("opt_out", gold, "novel", text=text) for text in texts]
    return _unique(items)


# -- no_reminders --------------------------------------------------------------------------------------------

NOVEL_NO_REMINDERS = {
    "yes": [("the dentist booking", "I'll book the dentist myself this week, you don't need to nudge me."),
            ("the tax form", "The tax form is due Friday. I've got it, no pings about it please."),
            ("the library books", "Library books go back on Monday. Don't bother reminding me, I'll remember."),
            ("the rent transfer", "Rent goes out on the 1st as usual. Skip the reminder this time.")],
    "no": [("the dentist booking", "Remind me to book the dentist on Thursday morning."),
           ("the tax form", "The tax form is due Friday; give me a nudge on Thursday."),
           ("the library books", "Library books go back Monday, ping me Sunday night."),
           ("the rent transfer", "Rent is due on the 1st. Make sure I don't forget it.")],
}


def _named(text: str, names) -> str:
    found = [name for name in names if name in text]
    return max(found, key=len) if found else ""


def no_reminders() -> List[Dict[str, Any]]:
    items = []
    for scenario in _scenarios("identity"):
        first = _owner_turns(scenario)[0]["user"]
        name = scenario["scenario"]
        thing = _named(first, _load(GENERATORS / "identity.py", "decision_sets_identity").ITEMS)
        contact = (re.findall(r"\bp-\d\d\b", first) or [""])[0]
        # The true-premise turns ask for no reminder either way (a conditional answer to pass on): they are not
        # reminder requests, so only the false-premise ones (no reminders, do not chase me) are used.
        if name == "false-premise":
            items.append(item("no_reminders", "yes", f"generator:identity/{name}",
                              item=f"the {thing} for {contact}", text=first))
    drives = _load(GENERATORS / "drives.py", "decision_sets_drives")
    for scenario in _scenarios("drives"):
        for turn in _owner_turns(scenario):
            text = turn["user"]
            thing = _named(text, drives.ITEMS)
            contact = (re.findall(r"\bp-\d\d\b", text) or [""])[0]
            wants = ("a nudge from you is welcome", "I want a reminder", "let me know", "Flag it to me")
            if thing and contact and any(phrase in text for phrase in wants):
                items.append(item("no_reminders", "no", f"generator:drives/{scenario['scenario']}",
                                  item=f"the {thing} for {contact}", text=text))
    items.append(item("no_reminders", "yes", "test:test_commitment_first_mention_hold", item="the parcel receipt for p-05",
                      text="The parcel receipt for p-05 is due in 20 minutes and I am handling it myself. "
                           "No reminders about it."))
    items.append(item("no_reminders", "no", "test:test_commitment_first_mention_hold", item="the parcel receipt for p-05",
                      text="Actually, remind me about the parcel receipt in two hours."))
    for gold, pairs in NOVEL_NO_REMINDERS.items():
        items += [item("no_reminders", gold, "novel", item=thing, text=text) for thing, text in pairs]
    return _unique(items)


# -- interest_settled ----------------------------------------------------------------------------------------

NOVEL_SETTLED = {
    "yes": [("kite bridles", "Found a good video on kite bridles, so I'm sorted there."),
            ("loop antennas", "Forget the loop antennas thing, I've lost interest."),
            ("moss lawns", "My neighbour showed me her moss lawn, so that question's answered.")],
    "no": [("kite bridles", "Still curious about kite bridles, whenever you get to it."),
           ("loop antennas", "Loop antennas came up again at the club, I really want to know more."),
           ("moss lawns", "Did you find anything on moss lawns yet?")],
}


def interest_settled() -> List[Dict[str, Any]]:
    drives = _load(GENERATORS / "drives.py", "decision_sets_drives")
    items = []
    for scenario in _scenarios("drives"):
        for turn in _owner_turns(scenario):
            text = turn["user"]
            topic = _named(text, drives.INTERESTS)
            if not topic:
                continue
            # Answered ("my curiosity is satisfied"), or no longer wanted looked into ("that one is theirs and I do
            # not want you on it", "it is not for you"): both close an interest.
            settled = any(phrase in text for phrase in (f"the {topic} question is answered", "my curiosity is satisfied",
                                                        "I do not want you on it", "it is not for you"))
            items.append(item("interest_settled", "yes" if settled else "no",
                              f"generator:drives/{scenario['scenario']}", topic=topic, text=text))
        # Another turn of the same scenario, about something else, never settles the interest.
        topics = [_named(turn["user"], drives.INTERESTS) for turn in _owner_turns(scenario)]
        topic = next((name for name in topics if name), "")
        for turn in _owner_turns(scenario):
            if topic and not _named(turn["user"], drives.INTERESTS):
                items.append(item("interest_settled", "no", f"generator:drives/{scenario['scenario']}",
                                  topic=topic, text=turn["user"]))
    items.append(item("interest_settled", "yes", "test:test_mind_interest_settled", topic="tide tables",
                      text="On tide tables: a friend explained it to me over lunch, so my curiosity is satisfied. "
                           "Nothing to look into."))
    for gold, pairs in NOVEL_SETTLED.items():
        items += [item("interest_settled", gold, "novel", topic=topic, text=text) for topic, text in pairs]
    return _unique(items)


# -- owner_verdict -------------------------------------------------------------------------------------------

NOVEL_VERDICTS = {
    "yes": [("The meeting is at 3pm in room B.", "No, it's room C, you mixed them up."),
            ("I set the thermostat schedule to 19 degrees.", "Perfect, that's exactly right."),
            ("Your flight leaves at 07:40.", "Wrong, it was moved to 08:15 yesterday."),
            ("I renamed the files by date.", "That's not what I asked, I wanted them by client.")],
    "no": [("The meeting is at 3pm in room B.", "Can you also book a room for Friday?"),
           ("I set the thermostat schedule to 19 degrees.", "What's the weather tomorrow?"),
           ("Your flight leaves at 07:40.", "Find me a hotel near the airport."),
           ("I renamed the files by date.", "Now zip them and send them to p-12.")],
}
REPLY = "Done. I wrote the result to {path}."


def owner_verdict() -> List[Dict[str, Any]]:
    items = []
    for scenario in _scenarios("improve"):
        turns = _owner_turns(scenario)
        previous_path = ""
        for index, turn in enumerate(turns):
            text = turn["user"]
            path = (re.findall(r"\b[\w-]+\.json\b", text) or ["result.json"])[0]
            before = turns[index - 1]["user"] if index else ""
            if text.startswith("Verdict"):
                items.append(item("owner_verdict", "yes", f"generator:improve/{scenario['scenario']}",
                                  reply=REPLY.format(path=path), text=text, before=before))
            elif previous_path:
                # A new request after the agent's last reply: talk after work that judges nothing.
                items.append(item("owner_verdict", "no", f"generator:improve/{scenario['scenario']}",
                                  reply=REPLY.format(path=previous_path), text=text, before=before))
            previous_path = path
    lessons = _load(TESTS / "test_mind_lessons_night.py", "decision_sets_lessons")
    items += [item("owner_verdict", "no", "test:test_mind_lessons_night", reply="The order code is C1107.",
                   text=lessons.SECOND),
              item("owner_verdict", "yes", "test:test_mind_lessons_night", reply="The spare keys are in the office "
                   "drawer.", text=lessons.LOCKER),
              item("owner_verdict", "yes", "test:test_mind_lessons_night", reply="The order code is C3301.",
                   text="That was wrong, it is C3307"),
              item("owner_verdict", "yes", "test:test_mind_lessons_night", reply="The order code is C2210.",
                   text="This one is right, thanks"),
              item("owner_verdict", "no", "test:test_mind_lessons_night", reply="The order code is C1107.",
                   text=lessons.REQUEST)]
    for gold, pairs in NOVEL_VERDICTS.items():
        items += [item("owner_verdict", gold, "novel", reply=reply, text=text) for reply, text in pairs]
    return _unique(items)


SETS = {"outreach_reply": outreach_reply, "opt_out": opt_out, "no_reminders": no_reminders,
        "interest_settled": interest_settled, "owner_verdict": owner_verdict}


#: At most this many items per (gold label, generator template): a family renders many near-identical turns.
#: Test and novel phrases are all kept.
TEMPLATE_CAP = 4


def _capped(items: List[Dict[str, Any]], cap: int = TEMPLATE_CAP) -> List[Dict[str, Any]]:
    strata: Dict[tuple, List[Dict[str, Any]]] = {}
    for entry in items:
        strata.setdefault((entry["gold"], entry["source"]), []).append(entry)
    kept = []
    for (gold, source), entries in strata.items():
        if not source.startswith("generator:"):
            kept += entries
            continue
        count = min(cap, len(entries))
        kept += [entries[index * len(entries) // count] for index in range(count)]   # evenly spaced
    return kept


def build(points=None) -> Dict[str, List[Dict[str, Any]]]:
    return {name: _capped(SETS[name]()) for name in (points or SETS)}


if __name__ == "__main__":
    from collections import Counter
    for name, entries in build().items():
        print(name, len(entries), dict(Counter((entry["gold"], entry["source"].split(":")[0]) for entry in entries)))
