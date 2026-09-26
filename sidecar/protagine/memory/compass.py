"""Per-turn context selection and superseded values.

Selection ("retrieve wide, let a judge choose, inject little"): every lane of
the per-turn context (recall, pending commitments, recorded views, the
relationship and landscape lines, observed work, ...) offers its items as
candidates; the reranker the recall path already uses scores each against the
current message, and the best ones are injected within one character budget.
Owner-critical items are pinned and never compete: the current time, open reply
waits, the owner's standing instructions and corrections, commitments that are
overdue or due soon, and the corrections to earlier context. When the reranker
is unconfigured, slow, failing or returns an unusable answer, the sections are
returned as assembled (the behaviour without selection), and the fallback is
logged.

Superseded values: a line that asserts a value the current record has
superseded (a changed or corrected claim, a rescheduled commitment) is annotated
inline with the current value and date; nothing is deleted. A line is a
record's only by identity: it carries the commitment's or the claim's own id; a
source message other records share, shared words or an equal value elsewhere
never make it one, and a line without a record id is never annotated. Earlier turns of a
conversation are replayed by the host as they were, so values served earlier
that have since been superseded are listed once more, as corrections, in the
new turn's context.
"""
from __future__ import annotations

import asyncio
import bisect
import itertools
import json
import logging
import math
import os
import re
import threading
import time
import unicodedata
from collections import OrderedDict
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)

SELECTION_ENV = "PROTAGINE_CONTEXT_SELECTION"
BUDGET_ENV = "PROTAGINE_CONTEXT_SELECTION_CHARS"
TIMEOUT_ENV = "PROTAGINE_CONTEXT_SELECTION_TIMEOUT_MS"
CANDIDATES_ENV = "PROTAGINE_CONTEXT_SELECTION_CANDIDATES"
DUE_SOON_ENV = "PROTAGINE_CONTEXT_DUE_SOON_HOURS"
MIN_SCORE_ENV = "PROTAGINE_CONTEXT_SELECTION_MIN_SCORE"
DEFAULT_ENABLED = True
DEFAULT_BUDGET = 3000
DEFAULT_TIMEOUT_MS = 2000
DEFAULT_CANDIDATES = 48
DEFAULT_DUE_SOON_HOURS = 24.0
DEFAULT_MIN_SCORE = 0.0
DOCUMENT_CHARS = 600  # what the judge reads of one candidate; the injected text is never cut

# Owner-critical sections: kept whole, outside the budget.
PINNED_SECTIONS = frozenset({
    "temporal-context",             # Current Time
    "protagine-waiting",            # Expected replies: the open asks
    "protagine-owner-preferences",  # How they want me to communicate: standing instructions
    "protagine-self-perspective",   # Owner priority corrections
    "protagine-corrections",        # Corrections to earlier context
})
COMMITMENTS = "protagine-commitments"
CORRECTIONS = "protagine-corrections"
MARKER = "[superseded"

_ITEM_START = re.compile(r"^(?:[-•*] |\{)")
_PASSAGE = re.compile(r'^- \{"evidence_ref": "(q\d+)"')
_REFERENCE = re.compile(r'"evidence_ref": "(q\d+)"')
_DUE = re.compile(r"\(due: ([0-9][0-9T:+\-.Z ]{9,40}?)[,)]")
_SKIP_KEY = re.compile(r"(?:^|_)(?:id|ids|sha256|hash|uri|version|anchor|anchors|ref|refs|at|seconds|watermark)$"
                       r"|^source|^evidence_ref$|^(?:kind|state|role|operation|validity_basis|representation|"
                       r"applicability|epistemic_state|claim_status|content_format|conversation_context|"
                       r"confidence|excerpt_truncated|precision)$")
_VALUE_KEYS = frozenset({"value", "quote", "content", "text", "description", "label"})  # words, whatever they look like
_IDENTIFIER = re.compile(r"^(?:[0-9a-f]{16,}|[\w.:-]*\d{4}-\d\d-\d\dT[\w.:+-]*|turn:\S+|claim:\S+)$", re.I)


@dataclass(frozen=True)
class SelectionSettings:
    enabled: bool
    budget: int
    timeout_s: float
    candidates: int
    due_soon: timedelta
    min_score: float = DEFAULT_MIN_SCORE  # a judged item scoring below it is not injected, budget or not


def _number(name, default, low, high):
    try:
        value = float(os.environ.get(name, "").strip() or default)
    except ValueError:
        return default
    return min(max(value, low), high) if math.isfinite(value) else default


def selection_settings() -> SelectionSettings:
    flag = os.environ.get(SELECTION_ENV, "").strip().lower()
    enabled = DEFAULT_ENABLED if not flag else flag in ("1", "on", "true", "yes")
    return SelectionSettings(
        enabled=enabled,
        budget=int(_number(BUDGET_ENV, DEFAULT_BUDGET, 0, 100_000)),
        timeout_s=_number(TIMEOUT_ENV, DEFAULT_TIMEOUT_MS, 1, 60_000) / 1000.0,
        candidates=int(_number(CANDIDATES_ENV, DEFAULT_CANDIDATES, 1, 512)),
        due_soon=timedelta(hours=_number(DUE_SOON_ENV, DEFAULT_DUE_SOON_HOURS, 0, 24 * 365)),
        min_score=_number(MIN_SCORE_ENV, DEFAULT_MIN_SCORE, float("-inf"), float("inf")))


def _replace(section, **fields):
    if hasattr(section, "model_copy"):
        return section.model_copy(update=fields)
    clone = type(section).__new__(type(section))
    clone.__dict__.update(section.__dict__, **fields)
    return clone


# -- Items ---------------------------------------------------------------------------------------

@dataclass
class _Item:
    section: int
    index: int
    text: str
    pinned: bool = False
    provides: str = ""
    requires: tuple = ()


@dataclass
class _Parts:
    header: list = field(default_factory=list)
    items: list = field(default_factory=list)  # lists of lines
    footer: list = field(default_factory=list)


def split_items(body: str) -> _Parts:
    """A section body as header lines, items and footer lines. An item starts with a bullet or a JSON
    record; indented lines continue it. A body without items is one item."""
    parts, trailing = _Parts(), []
    for line in str(body or "").split("\n"):
        if _ITEM_START.match(line):
            if trailing and parts.items:
                parts.items[-1].extend(trailing)
            trailing = []
            parts.items.append([line])
        elif not parts.items:
            parts.header.append(line)
        elif line[:1].isspace():
            parts.items[-1].extend(trailing + [line])
            trailing = []
        else:
            trailing.append(line)
    parts.footer = trailing
    if not parts.items:
        return _Parts(items=[parts.header] if any(line.strip() for line in parts.header) else [])
    return parts


def _due_soon(text: str, now: datetime, horizon: timedelta) -> bool:
    if "[OVERDUE]" in text:
        return True
    match = _DUE.search(text)
    if not match:
        return False
    try:
        due = datetime.fromisoformat(match.group(1).strip().replace("Z", "+00:00"))
    except ValueError:
        return False
    if due.tzinfo is None:
        due = due.replace(tzinfo=timezone.utc)
    return due <= now + horizon


def _strings(value, key=""):
    if isinstance(value, str):
        if key and _SKIP_KEY.search(key):
            return
        text = value.strip()
        if text and (key in _VALUE_KEYS or not _IDENTIFIER.match(text)):
            yield text
    elif isinstance(value, dict):
        for name, item in value.items():
            yield from _strings(item, str(name))
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item, key)


_LITERAL = re.compile(r'"(?:[^"\\]|\\.)*"')


def _unescaped(text: str) -> str:
    """``text`` with each JSON string literal's escapes read as the characters they stand for (Caf\\u00e9 is Café)."""
    def read(match):
        literal = match.group()
        if "\\" not in literal:
            return literal
        try:
            return '"' + json.loads(literal) + '"'
        except ValueError:
            return literal
    return _LITERAL.sub(read, text)


def _readable(text: str, *, fallback: bool = True) -> str:
    """What the judge reads: the words of an item, without identifiers, hashes and timestamps. Without
    ``fallback`` a record with no words reads as nothing (never as its identifiers)."""
    body = re.sub(r"^[-•*] ", "", text.strip())
    if not body.startswith("{"):
        return _unescaped(body)
    decoder, words = json.JSONDecoder(), []
    position = 0
    while position < len(body):
        while position < len(body) and body[position].isspace():
            position += 1
        if position >= len(body):
            break
        try:
            value, position = decoder.raw_decode(body, position)
        except ValueError:
            words.append(_unescaped(body[position:]))
            break
        words.extend(_strings(value))
    return " ".join(words) or (_unescaped(body) if fallback else "")


def _document(item: _Item, title: str, passages: dict[str, str]) -> str:
    text = _readable(item.text)
    quotes = [passages[ref] for ref in item.requires if ref in passages]
    return (f"{title}: " + " ".join([text, *quotes]))[:DOCUMENT_CHARS]


# -- Selection -----------------------------------------------------------------------------------

_warned_at: float | None = None


def _warn(reason: str) -> None:
    global _warned_at
    now = time.monotonic()
    if _warned_at is None or now - _warned_at >= 300:
        _warned_at = now
        logger.warning("context selection unavailable (%s); the assembled context is used", reason)
    else:
        logger.debug("context selection unavailable (%s); the assembled context is used", reason)


# One tokenizer for the message, the candidates and the values matched in lines: Unicode compatibility forms and
# composed accents folded, curly apostrophes and hyphens read as their plain forms, case folded; a token is a run
# of letters and digits joined by apostrophes, hyphens or dots ("on-call", "mira's", "3.5").
_FOLD = str.maketrans({"\u2019": "'", "\u2018": "'", "\u02bc": "'", "\uff07": "'", "\u2010": "-", "\u2011": "-"})
_TOKEN = re.compile(r"[^\W_]+(?:['.-][^\W_]+)*")
_JOINERS = re.compile(r"['.-]")


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).translate(_FOLD).casefold()


def tokens(text: str) -> list[str]:
    return _TOKEN.findall(_fold(text))


def _terms(text: str) -> set[str]:
    """A text's terms: its tokens and the words of its joined tokens ("on-call" is also "on" and "call")."""
    terms = set()
    for token in tokens(text):
        terms.add(token)
        if not token.isalnum():
            terms.update(part for part in _JOINERS.split(token) if len(part) > 1)
    return terms


def _lexical_ranks(query: str, documents: list[str]) -> list[tuple[float, float]]:
    """A cheap first ranking of every candidate against the message, ``(exact, stem)`` per document: the message's
    terms each document shares exactly, then those it shares by a five-letter stem (inflections), rarer terms
    weighing more. A cap on the judge's candidates keeps exact matches first and ties go to lexical matches,
    never to lane priority alone."""
    from protagine.memory.recall import _STOP
    terms = {term for term in _terms(query) if term not in _STOP}
    stems = {term[:5] for term in terms}
    written = [_terms(document) for document in documents]
    exact = [terms & words for words in written]
    stemmed = [stems & {word[:5] for word in words} for words in written]

    def weights(shared):
        frequency = {}
        for found in shared:
            for term in found:
                frequency[term] = frequency.get(term, 0) + 1
        return [sum(math.log1p(len(documents) / frequency[term]) for term in found) for found in shared]
    return list(zip(weights(exact), weights(stemmed)))


def _scores(results, count: int) -> list[float]:
    scores: dict[int, float] = {}
    for row in results or []:
        index = row.get("index") if isinstance(row, dict) else getattr(row, "index", None)
        score = row.get("score", row.get("relevance_score")) if isinstance(row, dict) else getattr(row, "score", None)
        if not isinstance(index, int) or isinstance(index, bool) or index in scores or not 0 <= index < count:
            raise ValueError("reranker returned an invalid or duplicate index")
        score = float(score)
        if not math.isfinite(score):
            raise ValueError("reranker returned an invalid score")
        scores[index] = score
    if len(scores) != count:
        raise ValueError("reranker returned incomplete scores")
    return [scores[i] for i in range(count)]


def _body(parts: _Parts, keep: Iterable[int]) -> str:
    keep = set(keep)
    lines = [line for i, item in enumerate(parts.items) if i in keep for line in item]
    return "\n".join(parts.header + lines + parts.footer)


def _cited(citations, body: str):
    if not citations:
        return citations
    kept = [ref for ref in citations if not isinstance(ref, dict) or not ref.get("source_id")
            or str(ref["source_id"]) in body]
    return kept or None


async def select_context(sections: list, query: str, rerank_fn, *, settings: SelectionSettings | None = None,
                         now: datetime | None = None) -> tuple[list, dict]:
    """``(sections, report)``: the pinned items and the best-scoring others within the budget.

    The report says what happened: ``selected`` (with the characters of the selectable items before and after),
    ``within_budget`` (nothing to cut; no reranker call), or ``fallback`` with its reason (the sections come back
    as assembled)."""
    settings = settings or selection_settings()
    if now is None:
        from protagine.util.temporal import now_utc
        now = now_utc()
    split, items = [], []
    for s_index, section in enumerate(sections):
        if section.id in PINNED_SECTIONS:
            split.append(None)
            continue
        parts = split_items(section.body)
        split.append(parts)
        for i_index, lines in enumerate(parts.items):
            text = "\n".join(lines)
            passage = _PASSAGE.match(text)
            items.append(_Item(s_index, i_index, text,
                               pinned=section.id == COMMITMENTS and _due_soon(text, now, settings.due_soon),
                               provides=passage.group(1) if passage else "",
                               requires=() if passage else tuple(dict.fromkeys(_REFERENCE.findall(text)))))
    providers = {item.provides: item for item in items if item.provides}
    needed = {ref for item in items for ref in item.requires}
    # A passage shared by assertion cards follows the cards that cite it; it is not judged alone.
    candidates = [item for item in items if not item.pinned and not (item.provides and item.provides in needed)]
    header_cost = {s: len("\n".join(parts.header + parts.footer)) + 1
                   for s, parts in enumerate(split) if parts is not None}
    opened_by_pin = {item.section for item in items if item.pinned}

    def cost_of(item, opened, included):
        cost, sections_opened = 0, set(opened)
        for part in (item, *(providers[ref] for ref in item.requires if ref in providers)):
            if id(part) in included:
                continue
            cost += len(part.text) + 1 + (0 if part.section in sections_opened else header_cost[part.section])
            sections_opened.add(part.section)
        return cost

    unpinned = [item for item in items if not item.pinned]
    before = sum(len(item.text) + 1 for item in unpinned) + sum(
        header_cost[s] for s in {item.section for item in unpinned} - opened_by_pin)
    report = {"status": "within_budget", "candidates": len(candidates), "chars_before": before, "chars_after": before,
              "budget": settings.budget}
    if not candidates or before <= settings.budget or not str(query or "").strip():
        return sections, report
    if rerank_fn is None:  # a configuration, not a failure: no warning
        logger.debug("context selection skipped: no reranker configured")
        return sections, {**report, "status": "fallback", "reason": "no_reranker"}
    priority = {s: -(sections[s].priority or 0) for s in range(len(sections))}
    passages = {ref: _readable(provider.text) for ref, provider in providers.items()}
    written = {id(item): _document(item, sections[item.section].title or sections[item.section].id, passages)
               for item in candidates}
    # Every candidate is ranked against the message before the cap; lane priority only breaks ties.
    lexical = (dict(zip(written, _lexical_ranks(str(query), list(written.values()))))
               if len(candidates) > settings.candidates else {})
    judged = sorted(candidates, key=lambda item: (*(-rank for rank in lexical.get(id(item), (0.0, 0.0))),
                                                  priority[item.section], item.section, item.index))
    judged, unjudged = judged[:settings.candidates], judged[settings.candidates:]
    documents = [written[id(item)] for item in judged]
    started = time.monotonic()
    try:
        results = await asyncio.wait_for(rerank_fn(str(query)[:4000], documents, top_k=len(documents)),
                                         timeout=settings.timeout_s)
        scores = _scores(results, len(documents))
    except asyncio.TimeoutError:
        _warn("reranker timed out after %.0f ms" % (settings.timeout_s * 1000))
        return sections, {**report, "status": "fallback", "reason": "timeout"}
    except Exception as exc:
        _warn(f"{type(exc).__name__}: {exc}")
        return sections, {**report, "status": "fallback", "reason": type(exc).__name__}
    order = sorted(range(len(judged)), key=lambda i: (-scores[i], priority[judged[i].section],
                                                      judged[i].section, judged[i].index))
    opened, included, used = set(opened_by_pin), set(), 0
    for i in order:
        if scores[i] < settings.min_score:
            break
        item = judged[i]
        cost = cost_of(item, opened, included)
        if used + cost > settings.budget:
            continue
        used += cost
        for chosen in (item, *(providers[ref] for ref in item.requires if ref in providers)):
            included.add(id(chosen))
            opened.add(chosen.section)
    keep: dict[int, set] = {}
    for item in items:
        if item.pinned or id(item) in included:
            keep.setdefault(item.section, set()).add(item.index)
    selected = []
    for s_index, section in enumerate(sections):
        parts = split[s_index]
        if parts is None:
            selected.append(section)
        elif s_index in keep:
            body = _body(parts, keep[s_index])
            selected.append(section if body == section.body else
                            _replace(section, body=body, citations=_cited(section.citations, body)))
    return selected, {**report, "status": "selected", "chars_after": used, "judged": len(judged),
                      "unjudged": len(unjudged), "elapsed_ms": round((time.monotonic() - started) * 1000, 1)}


# -- Superseded values ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Superseded:
    """A value the current record no longer holds: ``old`` was replaced by ``current`` on ``since``.

    ``record`` is the record's own id as the context renders it: a commitment's ``id=<id>``, a claim's
    ``"claim_id"``. Only a line (or, in a line of several records, the part) carrying that id states this record's
    value; a source message other records share, or equal words, never does."""
    old: str
    current: str
    since: str = ""
    subject: str = ""
    kind: str = "changed"  # changed | corrected | rescheduled
    record: str = ""

    def note(self) -> str:
        date = f" since {self.since[:10]}" if self.since else ""
        value = json.dumps(self.current, ensure_ascii=False)
        head = f"{MARKER} id={self.record}:"
        if self.kind == "rescheduled":
            return f"{head} rescheduled to {value}{date}]" if self.current else f"{head} due time removed{date}]"
        verb = "corrected to" if self.kind == "corrected" else "now"
        return f"{head} {verb} {value}{date}]"


# A note names its record and states that record's value as current; a later record can supersede that value.
_NOTE = re.compile(r'\s*\[superseded id=(\S+?): (?:(?:now|corrected to|rescheduled to) ("(?:[^"\\]|\\.)*")'
                   r'|due time removed)(?: since [^\]]*)?\]')
_RECORD_FIELDS = ("claim_id", "commitment_id")  # a JSON record's own id; never prior_claim_id or a source
_TEXT_ID = re.compile(r"(?<![\w.:=-])id=([^\s;,)\]\"]+)")


def _notes(line: str) -> tuple[str, list[tuple[str, str]]]:
    """``line`` without its notes, and each note's (record id, value stated as current)."""
    values = []

    def take(match):
        literal = match.group(2)
        try:
            values.append((match.group(1), json.loads(literal) if literal else ""))
        except ValueError:
            values.append((match.group(1), literal[1:-1]))
        return ""
    return _NOTE.sub(take, line), values


@dataclass
class _Element:
    """The part of a line that is one record's: its id, its value field when it has one, and its words."""
    record: str
    value: str | None = None
    words: list = field(default_factory=list)


def _own_id(value) -> str:
    return next((value[name] for name in _RECORD_FIELDS if isinstance(value.get(name), str) and value[name]), "") \
        if isinstance(value, dict) else ""


def _walk(value, owner, elements, loose, key=""):
    if isinstance(value, dict):
        rid = _own_id(value)
        if rid:
            stated = value.get("value")
            owner = _Element(rid, str(stated) if isinstance(stated, (str, int, float))
                             and not isinstance(stated, bool) else None)
            elements.append(owner)
        for name, item in value.items():
            if name not in _RECORD_FIELDS and not (rid and name == "value"):  # its value field is read as such
                _walk(item, owner, elements, loose, str(name))
    elif isinstance(value, list):
        for item in value:
            _walk(item, owner, elements, loose, key)
    elif isinstance(value, str):
        # Only metadata fields are skipped, by their key: a quotation or value is a value whatever it looks like
        # (an ISO timestamp quoted as the answer is the answer).
        text = value.strip()
        if text and not (key and _SKIP_KEY.search(key)):
            (owner.words if owner is not None else loose).append(text)


def _elements(text: str) -> list[_Element]:
    """The records a line (without its notes) states. A text line is the record of its one ``id=``; a JSON line
    holds one part per object carrying a record id, and its loose quotation belongs to the record of its first
    object. A line with no record id, or a text line naming several, states no record."""
    body = re.sub(r"^[-•*] ", "", text.strip())
    if not body.startswith("{"):
        ids = set(_TEXT_ID.findall(body))
        return [_Element(ids.pop(), None, [_unescaped(body)])] if len(ids) == 1 else []
    decoder, elements, loose, position, first = json.JSONDecoder(), [], [], 0, None
    while position < len(body):
        while position < len(body) and body[position].isspace():
            position += 1
        if position >= len(body):
            break
        try:
            value, position = decoder.raw_decode(body, position)
        except ValueError:
            loose.append(_unescaped(body[position:]))
            break
        count = len(elements)
        _walk(value, None, elements, loose if first is not None else [])
        if first is None:  # the element the line's first object is (its own children follow it)
            first = elements[count] if _own_id(value) else False
    if first:
        first.words.extend(loose)
    return elements


def _normal(value: str) -> str:
    text = " ".join(_fold(value).split()).strip(" \t.,;:!?\"'")
    return re.sub(r"^(?:the|a|an)\s+", "", text)


def _value_tokens(value: str) -> tuple:
    words = tokens(value)
    return tuple(words[1:] if len(words) > 1 and words[0] in ("the", "a", "an") else words)


def shows(words: list[str], value: str) -> bool:
    """``words`` (a line's tokens) hold ``value``'s tokens as one run, by the same tokenizer as selection."""
    wanted = _value_tokens(value)
    if not wanted:
        return False
    width = len(wanted)
    return any(tuple(words[i:i + width]) == wanted for i in range(len(words) - width + 1)
               if words[i] == wanted[0])


def _same(value: str, other: str) -> bool:
    return _normal(value) == _normal(other)


def stated(line: str, record: Superseded) -> tuple[str | None, str]:
    """What ``line`` last told the model about ``record``, by the record's history rather than by the first value
    found: ``("current", value)``, ``("stale", value)`` (a value the record no longer holds) or ``(None, "")`` when
    the line does not state the record. The line's latest word about its record decides: a note naming the record
    (notes are added after the text they correct), then the record's own value field or due time, then its words."""
    if not record.record or record.record not in line:
        return None, ""
    text, notes = _notes(line)
    notes = [value for rid, value in notes if rid == record.record]
    if notes:
        return ("current" if _same(notes[-1], record.current) else "stale"), notes[-1]
    own = [part for part in _elements(text) if part.record == record.record]
    if not own:
        return None, ""
    values = [part.value for part in own if part.value is not None]
    if record.kind == "rescheduled":
        due = _DUE.search(text)  # a commitment line's own due field
        if due is not None:
            values.append(due.group(1).strip())
    if values:
        wrong = [value for value in values if not _same(value, record.current)]
        return ("stale", wrong[-1]) if wrong else ("current", values[-1])
    # Only words: matched in the record's own words, never in identifiers and timestamps (identity already makes
    # them the record's, so a value of any length ("42") counts, and a "42" inside 09:42:00 does not).
    strings = [tokens(word) for part in own for word in part.words]  # a value never spans two strings
    if any(shows(words, record.current) for words in strings):
        return "current", record.current
    if any(shows(words, record.old) for words in strings):
        return "stale", record.old
    return None, ""


def line_state(line: str, record: Superseded) -> str | None:
    """``"current"``, ``"stale"`` or ``None``: see ``stated``."""
    return stated(line, record)[0]


def asserts_superseded(line: str, record: Superseded) -> bool:
    """``line`` is ``record``'s and shows a value the record has replaced, with no note of the current one."""
    return line_state(line, record) == "stale"


def annotate_superseded(sections: list, records: list[Superseded]) -> list:
    """Every line that states a superseded record's replaced value gains a note naming the record with its current
    value and date, one per record (a line with one note still counts as stale for every record left unmarked);
    nothing is removed. A line that carries no record's own id is never annotated."""
    if not records:
        return sections
    result = []
    for section in sections:
        lines, changed = str(section.body or "").split("\n"), False
        for i, line in enumerate(lines):
            notes = []
            for record in records:
                if asserts_superseded(line, record) and record.note() not in notes:
                    notes.append(record.note())
            if notes:
                lines[i], changed = line + " " + " ".join(notes), True
        result.append(_replace(section, body="\n".join(lines)) if changed else section)
    return result


def dead_value_lines(text: str, records: list[Superseded]) -> int:
    """Lines of ``text`` that show a superseded value as current (the dead-value metric)."""
    return sum(1 for line in str(text or "").split("\n") if any(asserts_superseded(line, r) for r in records))


CHAIN_LIMIT = 4096  # claims read to resolve the chains of one assemble; beyond it a chain asserts no current value


def claim_supersessions(ledger, *, contact_id: str, session_id: str, limit: int = 64) -> list[Superseded]:
    """Changed and corrected claims visible in this scope, each with the value its latest successor holds."""
    if ledger is None or not contact_id:
        return []
    from protagine.beliefs.source_projection import SourceClaimProjection
    projection = SourceClaimProjection(ledger)
    with closing(ledger._connect()) as conn:
        rows = conn.execute(
            """SELECT c.id, coalesce(c.retracted_by, c.superseded_by) AS successor,
                      CASE WHEN c.retracted_by IS NOT NULL THEN 'corrected' ELSE 'changed' END AS kind
               FROM source_claims c JOIN turn_sources s ON s.turn_id=c.turn_id
               WHERE (c.superseded_by IS NOT NULL OR c.retracted_by IS NOT NULL)
                 AND s.contact_id=? AND (s.scope='person' OR s.session_id=?)
               ORDER BY s.ingested_at DESC LIMIT ?""", (contact_id, session_id, limit)).fetchall()
        if not rows:
            return []
        successor = {row["id"]: row["successor"] for row in rows}
        kinds = {row["id"]: row["kind"] for row in rows}
        held = set()  # claims read and found superseded by nothing: the values held now
        frontier = {value for value in successor.values() if value not in successor}
        while frontier and len(successor) + len(held) <= CHAIN_LIMIT:
            # A successor superseded in turn: follow every chain to its end, however long.
            found = conn.execute("SELECT id, coalesce(retracted_by, superseded_by) FROM source_claims WHERE id IN ("
                                 + ",".join("?" for _ in frontier) + ")", sorted(frontier)).fetchall()
            frontier = set()
            for claim_id, following in found:
                if not following:
                    held.add(claim_id)
                elif claim_id not in successor:
                    successor[claim_id] = following
                    if following not in successor and following not in held:
                        frontier.add(following)
        if frontier:
            logger.warning("superseded claim chains longer than %d claims are not resolved", CHAIN_LIMIT)
        wanted = sorted(set(successor) | held)
        visible = {row["id"]: row for row in projection._rows(conn, contact_id, session_id, ids=wanted,
                                                              limit=len(wanted) + 1)}
    records = []
    for claim_id in kinds:
        old = visible.get(claim_id)
        if old is None:
            continue
        current, seen = successor.get(claim_id), {claim_id}
        while current in successor and current not in seen:
            seen.add(current)
            current = successor[current]
        # Only a claim read and found superseded by nothing holds the value now: a cycle or an unread chain end
        # asserts no current value. An erased or unattributed successor revives nothing and asserts nothing.
        latest = visible.get(current) if current in held else None
        if latest is None:
            continue
        old_value, new_value = str(old.get("value") or ""), str(latest.get("value") or "")
        # A chain back to the old value (A -> B -> A) is still a record: a line that was told B is corrected.
        if not old_value:
            continue
        records.append(Superseded(old=old_value, current=new_value,
                                  since=str(latest.get("valid_from") or latest.get("observed_at") or "")[:10],
                                  subject=str(old.get("subject") or ""), kind=kinds[claim_id], record=claim_id))
    return records


def commitment_reschedules(rows: Iterable[dict[str, Any]]) -> list[Superseded]:
    """Listed commitments whose deadline moved: the old deadline is superseded by the listed one."""
    records = []
    for row in rows or []:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        moved = metadata.get("reschedule") if isinstance(metadata.get("reschedule"), dict) else {}
        old = str(moved.get("from") or "").strip()
        if old and old != str(row.get("due_at") or ""):
            records.append(Superseded(old=old, current=str(row.get("due_at") or ""), kind="rescheduled",
                                      since=str(row.get("updated_at") or "")[:10],
                                      subject=str(row.get("description") or ""), record=str(row.get("id") or "")))
    return records


# -- Earlier turns in the window ------------------------------------------------------------------

def _positions(text: str, needle: str):
    position = text.find(needle)
    while position >= 0:
        yield position
        position = text.find(needle, position + 1)


def correction_line(record: Superseded, shown: str | None = None) -> str:
    """One correction of the value ``shown`` earlier (the record's old value by default), carrying the record's own
    identity (never a source other records share) so a later turn can tell it was delivered or is stale."""
    subject = f" ({record.subject})" if record.subject else ""
    value = record.old if shown is None else shown
    return f"- id={record.record}; {json.dumps(value, ensure_ascii=False)}{subject}: {record.note()}"


class ServedWindow:
    """What this sidecar served to each conversation, so a value superseded after it was served is corrected in
    a later turn (the host replays earlier turns' context as it was). In memory, bounded, per process."""

    def __init__(self, *, conversations: int = 128, chars: int = 200_000, lines: int = 8):
        self._served: OrderedDict[tuple, str] = OrderedDict()
        self._lock = threading.Lock()
        self._conversations, self._chars, self._lines = conversations, chars, lines

    def corrections(self, key: tuple, records: list[Superseded]) -> str:
        with self._lock:
            served = self._served.get(key, "")
        if not served or not records:
            return ""
        lines, owed = served.split("\n"), []
        starts = list(itertools.accumulate((len(line) + 1 for line in lines[:-1]), initial=0))
        for order, record in enumerate(records):
            if not record.record:
                continue
            stale = shown = -1
            value = record.old
            # Delivery is accounted per record: only the lines carrying the record's own id (never a source other
            # records share) can be its lines, and a substring scan finds them.
            carrying = sorted({bisect.bisect_right(starts, position) - 1 for position in _positions(served, record.record)})
            for index in carrying:
                state, said = stated(lines[index], record)
                if state == "stale":
                    stale, value = index, said
                elif state == "current":
                    shown = index
            # Owed while the last time this record's line was served it showed a replaced value: a correction or
            # a line with the current value served after it has been delivered, and is not repeated.
            if stale > shown:
                owed.append((-stale, order, record, value))
        # The most recently served stale values first; the ones past the bound are owed to the next turn.
        notes = [correction_line(record, value) for _, _, record, value
                 in sorted(owed, key=lambda row: row[:2])[:self._lines]]
        return ("Earlier context in this conversation showed values the record has since superseded:\n"
                + "\n".join(notes)) if notes else ""

    def remember(self, key: tuple, text: str) -> None:
        if not text:
            return
        with self._lock:
            served = (self._served.pop(key, "") + "\n" + text)[-self._chars:]
            while len(self._served) >= self._conversations:
                self._served.popitem(last=False)
            self._served[key] = served

    def clear(self) -> None:
        with self._lock:
            self._served.clear()


SERVED = ServedWindow()
