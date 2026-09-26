"""The owner's words about outreach, read deterministically (architecture 4.10).

Pure functions: no store, no model, no clock. ``read(text)`` returns what one owner turn says
about the mind reaching out, as classes and, where the words carry one, an object phrase:

| class | what the owner said | needs a link to an outreach |
|---|---|---|
| ``stop`` | no unprompted messages ("stop checking in", the contact opt-out phrases) | only a bare "stop" |
| ``resume`` | check-ins are welcome again | no |
| ``pause_today`` | quiet for the rest of the day | only "not today", "I need to focus", "hold it until tomorrow" |
| ``not_now`` | not at this moment ("not now", "in a meeting") | yes |
| ``negative`` | not wanted ("not interested", "not useful", "drop it"); with an object, about that topic | without an object |
| ``positive`` | more wanted ("dig deeper", "find out", "tell me more") | yes |
| ``welcome`` | it was worth it ("useful", "great find"), nothing more asked | yes |
| ``declaration`` | a topic the owner cares about ("keep me posted on X") | no |
| ``strain`` | a named thing stressing them or that they are behind on | no |
| ``relief`` | that thing is sorted | no |

What needs a link means something about outreach only as a reply to one: the owner types "stop" to
halt a turn and "not today" inside a request. A positive cue aimed at something the outreach did not
say ("find out when the last train leaves") and a negative about another topic are no reaction to
it (``Reading.reaction(about=...)``); a turn with a request of its own or a redo of the assistant's
work is not a reply at all (``elsewhere``).

A negation within three words before a cue voids it ("I'm not stressed about"), except in cues
that are negative themselves. An object phrase is at most six words, cut at punctuation or a
conjunction, without leading determiners or trailing fillers; a pronoun, a person, a contact id
or a known contact's name is never an object (a worry about a person is the people faculty's).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable, List, Optional, Pattern, Tuple

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
# A contact's bare STOP ends their messages; the owner's halts a running turn, so for outreach it is a
# stop only as a reply to one (``BARE_STOP_CUES``).
BARE_STOP_CUES: Tuple[Cue, ...] = (
    _cue(r"^\s*(?:stop|stop it|stop that|enough)[.!]*\s*$", negatable=False),
)
STOP_CUES: Tuple[Cue, ...] = tuple(Cue(pattern, negatable=False) for pattern in OPT_OUT_PATTERNS
                                   if not pattern.search("stop")) + (
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
    _cue(r"\bno (?:messages|pings|texts|check-ins|interruptions) (?:today|tonight|this afternoon|this evening)\b",
         negatable=False),
    # "I need to focus today" as the whole clause; "I need to focus on the report" is work talk.
    _cue(r"\bi (?:need|have|want) to (?:focus|concentrate|be left alone)"
         r"(?: today| tonight| this afternoon| for the rest of the day)" + _CLAUSE_END),
    _cue(r"\b(?:don't|do not) (?:disturb|bother|interrupt) me (?:today|tonight|this afternoon|this evening)\b",
         negatable=False),
)
# A pause that names no day, or refers to what was sent: a pause only as a reply to an outreach.
VAGUE_PAUSE_CUES: Tuple[Cue, ...] = (
    _cue(r"\bnot today(?!')\b", negatable=False),
    _cue(r"\bi (?:need|have|want) to (?:focus|concentrate|be left alone)" + _CLAUSE_END),
    _cue(r"\b(?:hold|save|keep) (?:it|that|them|anything|everything|messages)\b[\w\s']{0,40}\buntil tomorrow\b"),
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


_REACTION_CUES = (*STOP_CUES, *BARE_STOP_CUES, *RESUME_CUES, *PAUSE_TODAY_CUES, *VAGUE_PAUSE_CUES, *NOT_NOW_CUES,
                  *NEGATIVE_CUES, *POSITIVE_CUES, *WELCOME_CUES)

REFERRING = re.compile(r"\b(?:it|that|this|those|these|them|the (?:item|piece|link|one|message|thing)s?"
                       r"|what you (?:sent|found|shared)|you sent)\b", re.IGNORECASE)
SHORT_REPLY_WORDS = 12


def strip_prefix(text: str) -> str:
    return _PREFIX.sub("", str(text or ""))


# Someone else's words the owner passes on are never the owner's instruction: a quotation ("...", '...', and the
# typographic forms), a blockquote line ("> ..."), what a reporting verb whose speaker is not the owner introduces
# ("Alice said ...", "my boss wrote that ...", "they told me ..."), "according to X, ...", a chat line naming its
# speaker ("Alice: ..."), and the lines a frame ending in ":" introduces ("Alice said:" on its own line). The
# speaker of a reporting verb is the word before it once adverbs, auxiliaries and negations are set aside ("I
# just said", "I've already told you": the owner's own report of their own instruction). The owner saying they
# did NOT say something ("I never said ...") is no instruction either.
_TYPOGRAPHIC = str.maketrans({"’": "'", "‘": "'", "ʼ": "'", "′": "'", "`": "'",
                              "“": '"', "”": '"', "„": '"', "«": '"', "»": '"'})
_QUOTED = re.compile(r'"[^"\n]*"|(?<![\w\'])\'[^\'\n]+\'(?![\w\'])')
_REPORT_VERB = re.compile(r"\b(?:said|says|say|told|tells|tell|wrote|writes|write|texted|texts|messaged|emailed"
                          r"|asked|asks|ask|replied|replies|reply|answered|mentioned|added|commented|posted|tweeted"
                          r"|put\s+it)\b", re.IGNORECASE)
_ACCORDING = re.compile(r"\baccording to\b", re.IGNORECASE)
_SPEAKER_SKIP = frozenset("""
just already literally only also again really clearly even then earlier yesterday today before previously
specifically explicitly once twice repeatedly basically actually simply kindly politely firmly always ever so now
recently have has had 've 'd did do does was were is am be been will would could should can may might must
""".split())
_NEGATIONS = frozenset("never not didn't don't doesn't haven't hasn't hadn't wasn't weren't won't wouldn't no".split())
_OWN_SPEAKERS = frozenset("i we i've we've i'd we'd i'm me myself ourselves".split())
_CHAT_LABELS = frozenset("""
ps p.s also update note fyi btw edit reminder seriously ok okay anyway again important urgent re subject todo
question answer so and but please thanks today tonight tomorrow now then first second finally
""".split())
_CHAT_LINE = re.compile(r"^\s*(?P<who>[^\W\d][\w' .-]{0,38}?)\s*:\s*(?P<rest>.*)$")
_SEGMENT = re.compile(r"[^.?!;]*[.?!;]*")


def _speaker(before: str) -> tuple:
    """``(own, negated)`` for the words before a reporting verb in its sentence."""
    negated = False
    for word in reversed(re.findall(r"[\w']+", before.casefold())):
        if word in _NEGATIONS or word.endswith("n't"):
            negated = True
            continue
        if word in _SPEAKER_SKIP:
            continue
        return word in _OWN_SPEAKERS, negated
    return True, negated                       # "Said it already: ..." with no speaker named is the owner's


def _reported_from(segment: str) -> int:
    """Where someone else's words begin in one sentence (a reporting verb whose speaker is not the owner, or one
    the owner denies saying; "according to"), or -1."""
    match = _ACCORDING.search(segment)
    if match is not None:
        return match.start()
    for match in _REPORT_VERB.finditer(segment):
        own, negated = _speaker(segment[:match.start()])
        if not own or negated:
            return match.start()
    return -1


def _chat_speaker(line: str) -> Optional[tuple]:
    """``(own, rest)`` for a line naming its speaker ("Alice: ...", "Me: ..."), else None."""
    match = _CHAT_LINE.match(line)
    if match is None:
        return None
    who = match.group("who").strip().casefold()
    if who in _CHAT_LABELS or len(who.split()) > 4 or _REPORT_VERB.search(who):
        return None             # a label, a clause, or a reporting verb (read as one, with its speaker)
    return who in _OWN_SPEAKERS or who == "owner", match.group("rest")


def own_words(text: str, *, strict: bool = False) -> str:
    """The owner's own words in a turn, with every quotation and reported speech blanked out (``" "``), line by
    line. Where the extent of someone else's words is uncertain the two readings differ: ``strict`` (for words
    that would lift the owner's pause) blanks a reported sentence to the end of its line and every line a frame
    ending in ":" introduces, to the next blank line; the default (for a stop or a pause) blanks to the end of the
    sentence and the first line the frame introduces. An uncertain stop is taken, an uncertain resume is not."""
    text = _QUOTED.sub(" ", str(text or "").translate(_TYPOGRAPHIC))
    kept: List[str] = []
    framed = False            # the lines a frame introduced are someone else's
    seen = False              # a framed line was blanked already (the default reading blanks only the first)
    for line in text.split("\n"):
        body = line.strip()
        if framed:
            if not body:
                if seen:
                    framed = False
                kept.append("")
                continue
            if strict or not seen:
                seen = True
                kept.append(" ")
                continue
            framed = False
        if body.startswith(">"):
            kept.append(" ")
            continue
        chat = _chat_speaker(body)
        if chat is not None and not chat[0]:
            kept.append(" ")
            framed, seen = not chat[1].strip(), False
            continue
        out = []
        for segment in _SEGMENT.findall(line):
            at = _reported_from(segment)
            if at < 0:
                out.append(segment)
                continue
            out.append(segment[:at] + " ")
            if segment.rstrip().endswith(":") or segment[at:].rstrip().endswith(":"):
                framed, seen = True, False
            if strict:
                break          # the rest of the line may still be theirs
        line_out = "".join(out)
        if strict and body.endswith(":") and not framed:
            own_frame = _speaker(body[:-1]) == (True, False) and _REPORT_VERB.search(body)
            framed, seen = not own_frame, False
        kept.append(line_out)
    return "\n".join(kept)


def refers_back(text: str) -> bool:
    """Whether a turn reads as a reply to something just sent: short, or referring back to it. A position
    link (the owner's first turn after an outreach) needs this, so a long turn about something else that
    happens to hold a cue ("I need to find out when the train leaves") is not taken as a reaction."""
    body = strip_prefix(text)
    return len(re.findall(r"[\w'-]+", body)) <= SHORT_REPLY_WORDS or bool(REFERRING.search(body))


# A request of the turn's own ("can you book the dentist?") and a redo of the assistant's work ("try again
# with a shorter version"): the turn is talking to the assistant about something else.
_REQUEST = re.compile(r"^\s*(?:(?:and|also|oh|ok|okay|so)[\s,]+)?(?:(?:can|could|would|will|won't)\s+you\b|please\b|pls\b"
                      r"|i\s+(?:need|want|would like|'d like)\s+you\s+to\b)", re.IGNORECASE)
_REDO = re.compile(r"\b(?:try (?:it |that |this )?again|redo|re-do|rewrite|re-write|rephrase|start over|do (?:it|that) "
                   r"again|another (?:go|version|draft|attempt|try|pass)|(?:make|keep) it (?:shorter|longer|simpler)"
                   r"|(?:shorter|longer|simpler|different) version)\b", re.IGNORECASE)
_SENTENCES = re.compile(r"[.?!;\n]+")


def elsewhere(text: str, known: Iterable[str]) -> bool:
    """Whether a turn is talking to the assistant about something other than what was sent (``known``: the
    outreach's own words, ``content_terms``): a redo of the assistant's work, or a request of its own that
    names something the outreach did not say once its reaction cues are set aside."""
    body = strip_prefix(text)
    if _REDO.search(body):
        return True
    mine = set(known)
    for sentence in _SENTENCES.split(body):
        if not _REQUEST.match(sentence):
            continue
        stripped = sentence
        for cue in _REACTION_CUES:
            stripped = cue.pattern.sub(" ", stripped)
        if content_terms(stripped) - mine:
            return True
    return False


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
    if mentions_contact(value, contacts) or any(_person(word) for word in words):
        return None
    return value


def _person(word: str) -> bool:
    """A family or work-relation word, its possessive ("sister's", "kids'") or plural folded."""
    lowered = word.casefold().replace("\u2019", "'")
    if lowered.endswith("'s"):
        lowered = lowered[:-2]
    lowered = lowered.rstrip("'")
    return lowered in _PEOPLE or (lowered.endswith("s") and lowered[:-1] in _PEOPLE)


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


def _tail(text: str, end: int) -> str:
    """The rest of the clause after a cue, up to the next punctuation: what the cue is aimed at."""
    return _BOUNDARY.split(text[end:], maxsplit=1)[0].strip()


def _hits(text: str, cues: Iterable[Cue], contacts: Iterable[str]) -> List[Tuple[str, Optional[str], str]]:
    """``(matched phrase, object or None, the rest of its clause)`` for every cue that fires, in text order."""
    found = []
    contacts = tuple(contacts)
    for cue in cues:
        for match in cue.pattern.finditer(text):
            if cue.negatable and _negated(text, match.start()):
                continue
            target = None
            rest = _tail(text, match.end())
            if cue.takes_object:
                rest = _after(text, match.end())
                target = clean_object(rest, contacts)
            elif cue.object_before:
                target = clean_object(_before(text, match.start()), contacts)
            found.append((match.start(), match.group(0), target, rest))
    found.sort(key=lambda item: item[0])
    return [(phrase, target, rest) for _, phrase, target, rest in found]


def _find(text: str, cues: Iterable[Cue], contacts: Iterable[str]) -> List[Tuple[str, Optional[str]]]:
    """``(matched phrase, object or None)`` for every cue that fires, in text order."""
    return [(phrase, target) for phrase, target, _ in _hits(text, cues, contacts)]


# Words a reply uses about what was sent, whatever it was: never a sign it is about something else.
REPLY_WORDS = frozenset().union(*[set(re.findall(r"[\w'-]+", text)) for text in (
    "please thanks thank you yes yeah sure okay ok great good nice more further deeper bit little lot again also "
    "really now today later soon else item items piece pieces link links message messages thing things stuff one "
    "ones detail details source sources send sent share shared found find finding findings tell show get give "
    "look dig go going keep can could would will won't you your me my it that this those these them there what "
    "which who how when where why about into on in at of for with from the a an and or but so then just too",)])


def content_terms(text: str) -> set:
    """The words of ``text`` that name something, beyond what any reply says (``REPLY_WORDS``)."""
    from .lessons import terms
    return {word for word in terms(text) if word not in REPLY_WORDS}


@dataclass
class Reading:
    """What one owner turn says about outreach."""

    stop: bool = False
    stop_explicit: bool = False     # a stop that stands on its own ("stop checking in"), not a bare "stop"
    resume: bool = False
    pause_today: bool = False
    pause_explicit: bool = False    # a pause that names the day ("no messages today"), not "not today"
    not_now: bool = False
    negative: bool = False
    negative_objects: List[str] = field(default_factory=list)
    negative_bare: bool = False     # a negative that names nothing ("not useful", "not interested")
    positive: bool = False
    positive_tails: List[str] = field(default_factory=list)  # what each positive cue is aimed at ("" for none)
    welcome: bool = False
    declarations: List[str] = field(default_factory=list)
    strains: List[str] = field(default_factory=list)
    reliefs: List[str] = field(default_factory=list)
    relieved: bool = False     # a relief that names nothing ("all good now")

    def reaction(self, about: Optional[Callable[[str], bool]] = None) -> Optional[str]:
        """The one reaction a linked outreach takes from this turn, strongest first. With ``about`` (whether a
        phrase is about that outreach), a negative counts when it names nothing or names it, and a positive
        when it is aimed at nothing or at it: "not interested in the fern stuff, but dig deeper into X"
        is a positive on X."""
        for name in ("stop", "negative", "positive", "welcome", "not_now", "pause_today"):
            if not getattr(self, name):
                continue
            if about is not None and name == "negative" and not (
                    self.negative_bare or any(about(target) for target in self.negative_objects)):
                continue
            if about is not None and name == "positive" and not any(
                    not content_terms(tail) or about(tail) for tail in self.positive_tails):
                continue
            return name
        return None

    def needs_link(self) -> bool:
        """Whether what the turn says is about an outreach only a link can name."""
        return (self.positive or self.welcome or self.not_now or self.negative_bare
                or (self.stop and not self.stop_explicit) or (self.pause_today and not self.pause_explicit))

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
    # A stop, a resume or a pause is an instruction to the assistant: read from the owner's own words only.
    own = own_words(body)
    reading.stop_explicit = bool(_find(own, STOP_CUES, contacts))
    reading.stop = reading.stop_explicit or bool(_find(own, BARE_STOP_CUES, contacts))
    reading.resume = bool(_find(own_words(body, strict=True), RESUME_CUES, contacts))
    reading.pause_explicit = bool(_find(own, PAUSE_TODAY_CUES, contacts))
    reading.pause_today = reading.pause_explicit or bool(_find(own, VAGUE_PAUSE_CUES, contacts))
    reading.not_now = bool(_find(body, NOT_NOW_CUES, contacts))
    negatives = _hits(body, NEGATIVE_CUES, contacts)
    stop_sending = [target for phrase, target, _ in _hits(own, NEGATIVE_CUES, contacts)
                    if phrase.casefold().startswith(("stop sending", "quit sending"))
                    and (target is None or target.casefold() in _GENERIC)]
    if stop_sending:
        reading.stop = reading.stop_explicit = True
    negatives = [(phrase, target, rest) for phrase, target, rest in negatives
                 if not (phrase.casefold().startswith(("stop sending", "quit sending"))
                         and (target is None or target.casefold() in _GENERIC))]
    reading.negative = bool(negatives)
    reading.negative_objects = list(dict.fromkeys(target for _, target, _ in negatives if target))
    # Bare: no object in the turn (a "drop it" beside "not interested in X" is about X), and nothing refused as
    # one either (a person named is who it is about).
    reading.negative_bare = not reading.negative_objects and any(
        target is None and not content_terms(rest) for _, target, rest in negatives)
    positives = _hits(body, POSITIVE_CUES, contacts)
    reading.positive = bool(positives)
    reading.positive_tails = [rest for _, _, rest in positives]
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


__all__ = ["Cue", "MAX_OBJECT_WORDS", "REPLY_WORDS", "Reading", "classify_reaction", "clean_object", "content_terms",
           "declared_interest", "disinterest", "elsewhere", "mentions_contact", "own_words", "pause_today", "read", "refers_back",
           "relief", "resume", "stop", "strain", "strip_prefix"]
