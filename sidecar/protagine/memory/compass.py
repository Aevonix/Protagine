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
record's only by identity: it carries the commitment's id, or the claim's id or
the source that stated it; shared words or an equal value elsewhere never make
it one. Earlier turns of a
conversation are replayed by the host as they were, so values served earlier
that have since been superseded are listed once more, as corrections, in the
new turn's context.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import math
import os
import re
import threading
import time
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
MARKER = "[superseded:"

_ITEM_START = re.compile(r"^(?:[-•*] |\{)")
_PASSAGE = re.compile(r'^- \{"evidence_ref": "(q\d+)"')
_REFERENCE = re.compile(r'"evidence_ref": "(q\d+)"')
_DUE = re.compile(r"\(due: ([0-9][0-9T:+\-.Z ]{9,40}?)[,)]")
_SKIP_KEY = re.compile(r"(?:^|_)(?:id|ids|sha256|hash|uri|version|anchor|anchors|ref|refs|at|seconds|watermark)$"
                       r"|^source|^evidence_ref$|^(?:kind|state|role|operation|validity_basis|representation|"
                       r"applicability|epistemic_state|claim_status|content_format|conversation_context|"
                       r"confidence|excerpt_truncated|precision)$")
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
        if text and not _IDENTIFIER.match(text):
            yield text
    elif isinstance(value, dict):
        for name, item in value.items():
            yield from _strings(item, str(name))
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item, key)


def _readable(text: str) -> str:
    """What the judge reads: the words of an item, without identifiers, hashes and timestamps."""
    body = re.sub(r"^[-•*] ", "", text.strip())
    if not body.startswith("{"):
        return body
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
            words.append(body[position:])
            break
        words.extend(_strings(value))
    return " ".join(words) or body


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
    judged = sorted(candidates, key=lambda item: (priority[item.section], item.section, item.index))
    judged, unjudged = judged[:settings.candidates], judged[settings.candidates:]
    passages = {ref: _readable(provider.text) for ref, provider in providers.items()}
    documents = [_document(item, sections[item.section].title or sections[item.section].id, passages)
                 for item in judged]
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

    ``keys`` are the record's identity as the context renders it (a commitment's ``id=<id>``; a claim's id and the
    ``turn:<id>`` of the source that stated it). Only a line carrying one of them states this record's value."""
    old: str
    current: str
    since: str = ""
    subject: str = ""
    kind: str = "changed"  # changed | corrected | rescheduled
    keys: tuple = ()

    def note(self) -> str:
        date = f" since {self.since[:10]}" if self.since else ""
        value = json.dumps(self.current, ensure_ascii=False)
        if self.kind == "rescheduled":
            return f"{MARKER} rescheduled to {value}{date}]" if self.current else f"{MARKER} due time removed{date}]"
        verb = "corrected to" if self.kind == "corrected" else "now"
        return f"{MARKER} {verb} {value}{date}]"


# A note states a value as current; a later record can supersede that value in turn.
_NOTE = re.compile(r'\s*\[superseded: (?:(?:now|corrected to|rescheduled to) ("(?:[^"\\]|\\.)*")|due time removed)'
                   r'(?: since [^\]]*)?\]')


def _notes(line: str) -> tuple[str, list[str]]:
    """``line`` without its notes, and the values its notes state as current."""
    values = []

    def take(match):
        literal = match.group(1)
        try:
            values.append(json.loads(literal) if literal else "")
        except ValueError:
            values.append(literal[1:-1])
        return ""
    return _NOTE.sub(take, line), values


def _normal(value: str) -> str:
    text = " ".join(str(value or "").split()).strip(" \t.,;:!?\"'").casefold()
    return re.sub(r"^(?:the|a|an)\s+", "", text)


def _pattern(value: str):
    text = _normal(value)
    if len(text) < 3:
        return None
    return re.compile(r"(?<!\w)" + r"\s+".join(re.escape(word) for word in text.split()) + r"(?!\w)", re.I)


@functools.lru_cache(maxsize=4096)
def _identity(keys: tuple):
    """A line carries one of ``keys`` as a whole token (``id=c-1`` is not ``id=c-10``)."""
    keys = [key for key in keys if key]
    if not keys:
        return None
    return re.compile(r"(?<![\w.:=-])(?:" + "|".join(re.escape(key) for key in keys) + r")(?![\w-]|[.:]\w)")


def _carries(line: str, record: Superseded) -> bool:
    pattern = _identity(tuple(record.keys))
    return pattern is not None and bool(pattern.search(line))


def _same(value: str, other: str) -> bool:
    return _normal(value) == _normal(other)


def line_state(line: str, record: Superseded) -> str | None:
    """``"current"`` when ``line`` is ``record``'s (it carries the record's identity) and shows the current value, in
    its words or in a note; ``"stale"`` when it is the record's and shows a value the record replaced, in its words
    or as a note's current value; otherwise ``None``."""
    if not _carries(line, record):
        return None
    text, notes = _notes(line)
    current = _pattern(record.current)
    if any(_same(value, record.current) for value in notes) or (current is not None and current.search(text)):
        return "current"
    old = _pattern(record.old)
    shown = (old is not None and bool(old.search(text))) or any(_same(value, record.old) for value in notes)
    if not shown and record.kind == "rescheduled":
        due = _DUE.search(text)  # a commitment line's own due field: any due but the current one is replaced
        shown = due is not None and not _same(due.group(1), record.current)
    return "stale" if shown else None


def asserts_superseded(line: str, record: Superseded) -> bool:
    """``line`` is ``record``'s and shows a value the record has replaced, with no note of the current one."""
    return line_state(line, record) == "stale"


def annotate_superseded(sections: list, records: list[Superseded], *, per_line: int = 2) -> list:
    """Every line that asserts a superseded value gains the current value and date; nothing is removed."""
    if not records:
        return sections
    result = []
    for section in sections:
        lines, changed = str(section.body or "").split("\n"), False
        for i, line in enumerate(lines):
            notes = []
            for record in records:
                if len(notes) >= per_line:
                    break
                if asserts_superseded(line, record) and record.note() not in notes:
                    notes.append(record.note())
            if notes:
                lines[i], changed = line + " " + " ".join(notes), True
        result.append(_replace(section, body="\n".join(lines)) if changed else section)
    return result


def dead_value_lines(text: str, records: list[Superseded]) -> int:
    """Lines of ``text`` that show a superseded value as current (the dead-value metric)."""
    return sum(1 for line in str(text or "").split("\n") if any(asserts_superseded(line, r) for r in records))


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
        frontier = {value for value in successor.values() if value not in successor}
        for _ in range(8):  # a successor superseded in turn: follow the chain to the value held now
            if not frontier:
                break
            found = conn.execute("SELECT id, coalesce(retracted_by, superseded_by) FROM source_claims WHERE id IN ("
                                 + ",".join("?" for _ in frontier) + ")", sorted(frontier)).fetchall()
            frontier = set()
            for claim_id, following in found:
                if following:
                    successor[claim_id] = following
                    if following not in successor:
                        frontier.add(following)
        wanted = sorted(set(successor) | set(successor.values()))
        visible = {row["id"]: row for row in projection._rows(conn, contact_id, session_id, ids=wanted,
                                                              limit=len(wanted) + 1)}
    records = []
    for claim_id in kinds:
        old = visible.get(claim_id)
        if old is None:
            continue
        current, seen = successor.get(claim_id), {claim_id}
        while current in successor and current not in seen and len(seen) < 16:
            seen.add(current)
            current = successor[current]
        latest = visible.get(current)
        if latest is None:
            continue  # an erased or unattributed successor revives nothing and asserts nothing
        old_value, new_value = str(old.get("value") or ""), str(latest.get("value") or "")
        if not old_value or _normal(old_value) == _normal(new_value):
            continue
        records.append(Superseded(old=old_value, current=new_value,
                                  since=str(latest.get("valid_from") or latest.get("observed_at") or "")[:10],
                                  subject=str(old.get("subject") or ""), kind=kinds[claim_id],
                                  keys=tuple(key for key in ("turn:" + str(old.get("turn_id") or ""), claim_id)
                                             if key != "turn:")))
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
                                      subject=str(row.get("description") or ""),
                                      keys=(f"id={row['id']}",) if row.get("id") else ()))
    return records


# -- Earlier turns in the window ------------------------------------------------------------------

def correction_line(record: Superseded) -> str:
    """One correction, carrying the record's identity so a later turn can tell it was delivered or is stale."""
    subject = f" ({record.subject})" if record.subject else ""
    return f"- {record.keys[0]}; {json.dumps(record.old, ensure_ascii=False)}{subject}: {record.note()}"


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
        lines, notes = served.split("\n"), []
        for record in records:
            if any(asserts_superseded(line, record) for line in lines):
                notes.append(correction_line(record))
            if len(notes) >= self._lines:
                break
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
