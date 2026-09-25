"""The owner's words about outreach, read deterministically (architecture 4.10).

Pure functions: no store, no model, no clock. ``read(text)`` returns what one owner turn says
about the mind reaching out, as classes and, where the words carry one, an object phrase:

| class | what the owner said | needs a link to an outreach |
|---|---|---|
| ``stop`` | no unprompted messages ("stop checking in", the contact opt-out phrases) | no |
| ``resume`` | check-ins are welcome again | no |
| ``pause_today`` | quiet for the rest of the day | no |
| ``not_now`` | not at this moment ("not now", "in a meeting") | yes |
| ``negative`` | not wanted ("not interested", "not useful", "drop it"); with an object, about that topic | without an object |
| ``positive`` | more wanted ("dig deeper", "find out", "tell me more") | yes |
| ``welcome`` | it was worth it ("useful", "great find"), nothing more asked | yes |
| ``declaration`` | a topic the owner cares about ("keep me posted on X") | no |
| ``strain`` | a named thing stressing them or that they are behind on | no |
| ``relief`` | that thing is sorted | no |

A negation within three words before a cue voids it ("I'm not stressed about"), except in cues
that are negative themselves. An object phrase is at most six words, cut at punctuation or a
conjunction, without leading determiners or trailing fillers; a pronoun, a person, a contact id
or a known contact's name is never an object (a worry about a person is the people faculty's).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Pattern, Tuple

from protagine.contacts.optout import OPT_OUT_PATTERNS

MAX_OBJECT_WORDS = 6
# A gateway's bracketed prefixes (a timestamp, a sender header) before the owner's own words.
_PREFIX = re.compile(r"^\s*(?:\[[^\[\]\n]{1,120}\]\s*)+")
_NEGATIONS = frozenset({"not", "no", "never", "hardly", "nor", "cannot", "without"})
_BOUNDARY = re.compile(r"[.;:,!?()\n\"]")
_CONJUNCTIONS = frozenset({"and", "but", "so", "though", "although", "because", "while", "since", "or", "which",
                           "whereas", "unless", "until", "if", "when", "as", "then", "whether", "how", "what",
                           "why", "where", "who"})
_DETERMINERS = frozenset({"the", "a", "an", "my", "our", "your", "this", "that", "these", "those", "some", "any",
                          "more", "all", "its", "their", "his", "her", "about", "on", "of", "with", "anything", "new"})
_FILLERS = ("after all", "at all", "as well", "any more", "anymore", "these days", "right now", "for now",
            "myself", "closely", "instead", "though", "too", "either", "please", "today", "lately", "news",
            "updates", "stuff", "items", "things", "pieces", "articles", "item", "piece", "you sent", "you sent me",
            "a lot", "again", "much", "really", "honestly", "also")
_PRONOUNS = frozenset({"it", "that", "this", "them", "him", "her", "me", "you", "us", "anything", "everything",
                       "something", "nothing", "things", "stuff", "either", "both", "one", "those", "these",
                       "which", "what", "any", "all", "whatever", "there", "here", "now", "later", "today"})
# A person is the people faculty's to worry about, never an outreach topic or a care object.
_PEOPLE = frozenset({"mother", "father", "mum", "mom", "dad", "brother", "sister", "son", "daughter", "wife",
                     "husband", "partner", "friend", "boss", "colleague", "flatmate", "roommate", "kid", "kids",
                     "baby", "grandma", "grandpa", "family", "neighbour", "neighbor", "manager", "team"})
_CONTACT_ID = re.compile(r"\bp-\d\d\b", re.IGNORECASE)
# Generic words after "stop sending me": a stop, not a topic.
_GENERIC = frozenset({"updates", "messages", "stuff", "things", "anything", "check-ins", "texts", "reminders",
                      "notes", "that", "these", "those", "this"})


@dataclass(frozen=True)
class Cue:
    pattern: Pattern[str]
    takes_object: bool = False
    negatable: bool = True       # a negation within three words before voids it
    object_before: bool = False  # the object is the phrase just before the cue ("X is stressing me out")


def _cue(expression: str, **flags: bool) -> Cue:
    return Cue(re.compile(expression, re.IGNORECASE), **flags)


_I = r"(?:i|i'm|i am|i've|i have|im)"
STOP_CUES: Tuple[Cue, ...] = tuple(Cue(pattern, negatable=False) for pattern in OPT_OUT_PATTERNS) + (
    _cue(r"\b(?:stop|quit|no more|enough)\s+(?:checking|check(?:ing)?-?\s*ins?)(?:\s+(?:in|up))?\b", negatable=False),
    _cue(r"\b(?:don't|do not|no need to|don't need to|do not need to|you needn't|never)\s+check\s+(?:in|up)\b",
         negatable=False),
    _cue(r"\bonly (?:message|text|write to|contact) me when i ask\b", negatable=False),
    _cue(r"\b(?:stop|quit) (?:messaging|texting|pinging|writing to|contacting) me(?:\s+unprompted)?\b",
         negatable=False),
    _cue(r"\bstop reaching out\b", negatable=False),
    _cue(r"\bno (?:more )?(?:unprompted|unsolicited) (?:messages|updates|check-ins)\b", negatable=False),
    _cue(r"\bi(?:'ll| will) (?:ask|come to you|reach out) when i (?:want|need)", negatable=False),
    _cue(r"\bi will come to you\b", negatable=False),
)
RESUME_CUES: Tuple[Cue, ...] = (
    _cue(r"\byou can (?:check in|message me|reach out|write to me)(?: with me)? again\b"),
    _cue(r"\bfeel free to (?:check in|message me|reach out)\b"),
    _cue(r"\b(?:start|resume|restart) (?:checking in|the check-?\s*ins)(?: again)?\b"),
    _cue(r"\bcheck-?\s*ins are (?:fine|ok|okay|welcome) again\b"),
)
_CLAUSE_END = r"(?=\s*(?:[.;,!]|please\b|thanks\b|$))"
PAUSE_TODAY_CUES: Tuple[Cue, ...] = (
    _cue(r"\bleave me alone (?:for )?(?:the rest of )?(?:today|tonight|this afternoon|this evening|the day)\b",
         negatable=False),
    _cue(r"\bnot today(?!')\b", negatable=False),
    _cue(r"\bno (?:messages|pings|texts|check-ins|interruptions) (?:today|tonight|this afternoon|this evening)\b",
         negatable=False),
    # "I need to focus" as the whole clause; "I need to focus on the report" is work talk.
    _cue(r"\bi (?:need|have|want) to (?:focus|concentrate|be left alone)"
         r"(?: today| tonight| this afternoon| for the rest of the day)?" + _CLAUSE_END),
    _cue(r"\b(?:hold|save|keep) (?:it|that|them|anything|everything|messages)\b[\w\s']{0,40}\buntil tomorrow\b"),
    _cue(r"\b(?:don't|do not) (?:disturb|bother|interrupt) me (?:today|tonight|this afternoon|this evening)\b",
         negatable=False),
)
NOT_NOW_CUES: Tuple[Cue, ...] = (
    _cue(r"\bnot now\b", negatable=False),
    _cue(r"\bmaybe later\b", negatable=False),
    _cue(r"\blater,? please\b", negatable=False),
    _cue(r"\b(?:busy|swamped|tied up) (?:right now|at the moment|just now)\b"),
    _cue(r"\b(?:i'm|i am|im|we're|we are|currently|still) in (?:a|the middle of a) meeting\b"),
    _cue(r"\b(?:i'm|i am|im) in the middle of\b"),
    _cue(r"\b(?:maybe |some )?another time" + _CLAUSE_END),
    _cue(r"\bcan(?:'|no)?t talk\b", negatable=False),
    _cue(r"\bwill have to wait\b"),
    _cue(r"\bnot a good time\b", negatable=False),
)
NEGATIVE_CUES: Tuple[Cue, ...] = (
    _cue(r"\bnot (?:really |at all )?interested(?: in)?\b", takes_object=True, negatable=False),
    _cue(r"\b(?:don't|do not|doesn't|didn't) (?:really )?care (?:much )?(?:about|for)\b", takes_object=True,
         negatable=False),
    _cue(r"\bcould(?:n't| not) care less(?: about)?\b", takes_object=True, negatable=False),
    _cue(r"\b(?:not|wasn't|isn't|was not|is not|not very|not really) (?:useful|helpful|relevant)\b", negatable=False),
    _cue(r"\bnot for me\b", negatable=False),
    _cue(r"\bno,? thanks\b", negatable=False),
    _cue(r"\bdrop (?:it|that|this)\b(?!\s+(?:off|in|by|at|into|over|down|round|around))", negatable=False),
    _cue(r"\bspare me(?: the)?\b", takes_object=True, negatable=False),
    _cue(r"\b(?:stop|quit) sending me\b", takes_object=True, negatable=False),
    _cue(r"\bwaste of (?:my )?time\b", negatable=False),
    _cue(r"\bnot (?:something|a topic) i (?:care|want to hear) about\b", negatable=False),
)
POSITIVE_CUES: Tuple[Cue, ...] = (
    _cue(r"\bdig (?:deeper|further|in|into)\b"),
    _cue(r"\blook (?:into|further into|more closely at|deeper into)\b"),
    _cue(r"\bfind out\b"),
    _cue(r"\btell me more\b"),
    _cue(r"\bmore (?:on|about) (?:it|that|this)\b"),
    _cue(r"\bgo deeper\b"),
    _cue(r"\bkeep going\b"),
    _cue(r"\bfollow up on\b"),
    _cue(r"\byes,? please\b"),
    _cue(r"\bresearch (?:it|that|this|more)\b"),
    _cue(r"\bget me (?:more|the details)\b"),
)
WELCOME_CUES: Tuple[Cue, ...] = (
    _cue(r"\b(?:that|this|it) (?:was|is) (?:really |very |so )?(?:useful|helpful|great|good|interesting|handy)\b"),
    _cue(r"\b(?:great|good|nice) find\b"),
    _cue(r"\b(?:very|really|super) (?:useful|helpful)\b"),
    _cue(r"\bthanks for (?:sending|sharing|the heads-up)\b"),
    _cue(r"\bi (?:love|loved|like|liked) (?:it|this|that)\b"),
)
DECLARATION_CUES: Tuple[Cue, ...] = (
    _cue(rf"\b{_I}\s+(?:really\s+|also\s+|genuinely\s+)?care\s+(?:a lot\s+|a great deal\s+|deeply\s+|so much\s+)?about\b",
         takes_object=True),
    # "I'm into the final stretch" is not a topic: "into" needs an intensifier.
    _cue(rf"\b{_I}\s+(?:really|very|quite|so|totally|also really)\s+into\b", takes_object=True),
    _cue(rf"\b{_I}\s+(?:really\s+|very\s+|also\s+|quite\s+)?(?:interested in|keen on|fascinated by)\b",
         takes_object=True),
    _cue(r"\bkeep me (?:posted|updated|informed|in the loop) (?:on|about)\b", takes_object=True),
    _cue(r"\bkeep an eye (?:out for|on)\b", takes_object=True),
    _cue(r"\b(?:i'd|i would) (?:love|like) to hear (?:more )?about\b", takes_object=True),
    # Following a topic, not a recipe: "closely" (or its kin) says so.
    _cue(rf"\b{_I}\s+(?:also\s+)?(?:closely|avidly|keenly)\s+follow\b", takes_object=True),
    _cue(rf"\b{_I}\s+(?:also\s+)?follow\b(?=[^.;:!?]{{1,60}}\b(?:closely|avidly|keenly|religiously)\b)",
         takes_object=True),
    _cue(r"\bi've been (?:getting )?into\b", takes_object=True),
)
STRAIN_CUES: Tuple[Cue, ...] = (
    _cue(r"\b(?:stressed|stressing)(?: out)? (?:about|over)\b", takes_object=True),
    _cue(r"\b(?:falling |fallen |way |really )?behind (?:on|with)\b", takes_object=True),
    _cue(r"\boverwhelmed (?:by|with)\b", takes_object=True),
    _cue(r"\bswamped with\b", takes_object=True),
    _cue(r"\bdrowning in\b", takes_object=True),
    _cue(r"\bcan(?:'|no)?t keep up with\b", takes_object=True, negatable=False),
    _cue(r"\bdreading\b", takes_object=True),
    _cue(r"\b(?:worried|anxious|panicking|freaking out) (?:about|over)\b", takes_object=True),
    _cue(r"\bstruggling (?:with|to finish)\b", takes_object=True),
    _cue(r"\blosing sleep over\b", takes_object=True),
    _cue(r"\b(?:is|are|has been|have been|keeps?) (?:really )?(?:stressing|worrying) me(?: out)?\b",
         object_before=True),
)
RELIEF_CUES: Tuple[Cue, ...] = (
    _cue(rf"\b{_I}\s+(?:just\s+|finally\s+)?(?:finished|sorted(?: out)?|submitted|handed in|wrapped up|dealt with)\b",
         takes_object=True),
    _cue(r"\b(?:is|are) (?:now |finally )?(?:done|finished|handled|sorted(?: out)?|submitted|in|under control)\b",
         object_before=True),
    _cue(r"\ball good now\b"),
    _cue(r"\bno longer (?:worried|stressed)\b", negatable=False),
    _cue(r"\bunder control now\b"),
)


REFERRING = re.compile(r"\b(?:it|that|this|those|these|them|the (?:item|piece|link|one|message|thing)s?"
                       r"|what you (?:sent|found|shared)|you sent)\b", re.IGNORECASE)
SHORT_REPLY_WORDS = 12


def strip_prefix(text: str) -> str:
    return _PREFIX.sub("", str(text or ""))


def refers_back(text: str) -> bool:
    """Whether a turn reads as a reply to something just sent: short, or referring back to it. A position
    link (the owner's first turn after an outreach) needs this, so a long turn about something else that
    happens to hold a cue ("I need to find out when the train leaves") is not taken as a reaction."""
    body = strip_prefix(text)
    return len(re.findall(r"[\w'-]+", body)) <= SHORT_REPLY_WORDS or bool(REFERRING.search(body))


def _words_before(text: str, start: int, count: int = 3) -> List[str]:
    head = _BOUNDARY.split(text[:start])[-1]
    return re.findall(r"[\w']+", head.casefold())[-count:]


def _negated(text: str, start: int) -> bool:
    return any(word in _NEGATIONS or word.endswith("n't") for word in _words_before(text, start))


def _trim(words: List[str]) -> List[str]:
    while words and words[0].casefold() in _DETERMINERS:
        words = words[1:]
    changed = True
    while words and changed:
        changed = False
        lowered = " ".join(words).casefold()
        for filler in _FILLERS:
            if lowered == filler or lowered.endswith(" " + filler):
                words = words[: len(words) - len(filler.split())]
                changed = True
                break
    return words


def clean_object(phrase: str, contacts: Iterable[str] = ()) -> Optional[str]:
    """The object phrase as a topic, or None when it names nothing (a pronoun) or a person."""
    words = re.findall(r"[\w'-]+", str(phrase or ""))
    words = _trim(words)[:MAX_OBJECT_WORDS]
    words = _trim(words)
    if not words:
        return None
    value = " ".join(words)
    lowered = value.casefold()
    if lowered in _PRONOUNS or all(word.casefold() in _PRONOUNS for word in words):
        return None
    if mentions_contact(value, contacts) or any(word.casefold().strip("'s") in _PEOPLE for word in words):
        return None
    return value


def _spaced(text: str) -> str:
    """The words of ``text``, casefolded, possessives folded, one space apart and padded, for whole-word
    containment ("Sam's move" names Sam)."""
    words = [word[:-2] if word.endswith("'s") else word
             for word in re.findall(r"[\w'-]+", str(text or "").casefold())]
    return " " + " ".join(words) + " "


def mentions_contact(text: str, contacts: Iterable[str] = ()) -> bool:
    """Whether ``text`` names a contact: a fixed-width contact id, or a known contact's id or name."""
    if _CONTACT_ID.search(str(text or "")):
        return True
    words = _spaced(text)
    return any(_spaced(name).strip() and _spaced(name) in words for name in contacts)


def _after(text: str, end: int) -> str:
    tail = _BOUNDARY.split(text[end:], maxsplit=1)[0]
    words = re.findall(r"[\w'-]+", tail)
    kept: List[str] = []
    for word in words:
        if word.casefold() in _CONJUNCTIONS:
            break
        kept.append(word)
        if len(kept) >= MAX_OBJECT_WORDS + 3:
            break
    return " ".join(kept)


def _before(text: str, start: int) -> str:
    head = _BOUNDARY.split(text[:start])[-1]
    words = re.findall(r"[\w'-]+", head)
    kept: List[str] = []
    for word in reversed(words):
        if word.casefold() in _CONJUNCTIONS or word.casefold() in {"i", "i'm", "honestly", "also", "now"}:
            break
        kept.insert(0, word)
        if len(kept) >= MAX_OBJECT_WORDS:
            break
    return " ".join(kept)


def _find(text: str, cues: Iterable[Cue], contacts: Iterable[str]) -> List[Tuple[str, Optional[str]]]:
    """``(matched phrase, object or None)`` for every cue that fires, in text order."""
    found = []
    contacts = tuple(contacts)
    for cue in cues:
        for match in cue.pattern.finditer(text):
            if cue.negatable and _negated(text, match.start()):
                continue
            target = None
            if cue.takes_object:
                target = clean_object(_after(text, match.end()), contacts)
            elif cue.object_before:
                target = clean_object(_before(text, match.start()), contacts)
            found.append((match.start(), match.group(0), target))
    found.sort(key=lambda item: item[0])
    return [(phrase, target) for _, phrase, target in found]


@dataclass
class Reading:
    """What one owner turn says about outreach."""

    stop: bool = False
    resume: bool = False
    pause_today: bool = False
    not_now: bool = False
    negative: bool = False
    negative_objects: List[str] = field(default_factory=list)
    positive: bool = False
    welcome: bool = False
    declarations: List[str] = field(default_factory=list)
    strains: List[str] = field(default_factory=list)
    reliefs: List[str] = field(default_factory=list)
    relieved: bool = False     # a relief that names nothing ("all good now")

    def reaction(self) -> Optional[str]:
        """The one reaction a linked outreach takes from this turn, strongest first."""
        for name in ("stop", "negative", "positive", "welcome", "not_now", "pause_today"):
            if getattr(self, name):
                return name
        return None

    def needs_link(self) -> bool:
        """Whether what the turn says is about an outreach only a link can name."""
        return (self.positive or self.welcome or self.not_now
                or (self.negative and not self.negative_objects))

    @property
    def classes(self) -> List[str]:
        names = [name for name in ("stop", "resume", "pause_today", "not_now", "negative", "positive", "welcome")
                 if getattr(self, name)]
        if self.declarations:
            names.append("declaration")
        if self.strains:
            names.append("strain")
        if self.reliefs or self.relieved:
            names.append("relief")
        return names


def read(text: str, *, contacts: Iterable[str] = ()) -> Reading:
    """Every class the owner's words carry, with object phrases where they name one."""
    body = strip_prefix(text)
    contacts = tuple(contacts)
    reading = Reading()
    if not body.strip():
        return reading
    reading.stop = bool(_find(body, STOP_CUES, contacts))
    reading.resume = bool(_find(body, RESUME_CUES, contacts))
    reading.pause_today = bool(_find(body, PAUSE_TODAY_CUES, contacts))
    reading.not_now = bool(_find(body, NOT_NOW_CUES, contacts))
    negatives = _find(body, NEGATIVE_CUES, contacts)
    stop_sending = [target for phrase, target in negatives
                    if phrase.casefold().startswith(("stop sending", "quit sending"))
                    and (target is None or target.casefold() in _GENERIC)]
    if stop_sending:
        reading.stop = True
    negatives = [(phrase, target) for phrase, target in negatives
                 if not (phrase.casefold().startswith(("stop sending", "quit sending"))
                         and (target is None or target.casefold() in _GENERIC))]
    reading.negative = bool(negatives)
    reading.negative_objects = list(dict.fromkeys(target for _, target in negatives if target))
    reading.positive = bool(_find(body, POSITIVE_CUES, contacts))
    reading.welcome = bool(_find(body, WELCOME_CUES, contacts)) and not reading.negative
    negated_topics = {target.casefold() for target in reading.negative_objects}
    reading.declarations = list(dict.fromkeys(
        target for _, target in _find(body, DECLARATION_CUES, contacts)
        if target and target.casefold() not in negated_topics))
    reading.strains = list(dict.fromkeys(target for _, target in _find(body, STRAIN_CUES, contacts) if target))
    reliefs = _find(body, RELIEF_CUES, contacts)
    reading.reliefs = list(dict.fromkeys(target for _, target in reliefs if target))
    reading.relieved = any(target is None and not phrase.casefold().startswith(("is ", "are "))
                           for phrase, target in reliefs)
    return reading


# The names the design uses, one per question.

def classify_reaction(text: str) -> Optional[str]:
    return read(text).reaction()


def declared_interest(text: str, *, contacts: Iterable[str] = ()) -> List[str]:
    return read(text, contacts=contacts).declarations


def disinterest(text: str, *, contacts: Iterable[str] = ()) -> List[str]:
    return read(text, contacts=contacts).negative_objects


def strain(text: str, *, contacts: Iterable[str] = ()) -> List[str]:
    return read(text, contacts=contacts).strains


def relief(text: str, *, contacts: Iterable[str] = ()) -> List[str]:
    return read(text, contacts=contacts).reliefs


def stop(text: str) -> bool:
    return read(text).stop


def pause_today(text: str) -> bool:
    return read(text).pause_today


def resume(text: str) -> bool:
    return read(text).resume


__all__ = ["Cue", "MAX_OBJECT_WORDS", "Reading", "classify_reaction", "clean_object", "declared_interest",
           "disinterest", "mentions_contact", "pause_today", "read", "refers_back", "relief", "resume", "stop",
           "strain", "strip_prefix"]
