"""A fast decision layer: short typed decisions from a non-generative decision model ("System One").

A decision model reads a short text and answers one typed question about it in a single forward pass: a
``choice`` among labels with a probability for each, or a ``yes_no`` (the probability of yes). It generates no
text, so there is nothing to parse beyond the probabilities, and an answer takes tens of milliseconds.

Protagine asks one only at a decision point that already has an answer of its own (a phrase table, or a model
call), and only where that point is enabled (``decisions.points.<name>.enabled``). Whatever goes wrong gives
``None`` and the caller keeps its existing path: no endpoint configured, the point disabled, an input longer
than the model reads (its context is 512 tokens; a longer input is never cut, it is not sent), a timeout (250 ms
by default), a busy, failing or unreachable endpoint, an answer that is not the typed answer asked for, or an
answer whose calibrated probability falls inside the point's abstain band. The model's answer is a reading of
words, never an authority: it can only do what the point's existing path could have done with the same words.

Calibration is temperature scaling: a probability p becomes p^(1/T), renormalised (for yes/no, the log-odds are
divided by T). ``abstain: [lo, hi]``: a yes/no answer counts as yes at or above ``hi``, as no at or below ``lo``,
and is no answer in between; a choice counts when its top calibrated probability is at least ``hi`` (``lo`` is
not used). The defaults below come from the measurements in docs/DECISIONS.md, and a point is enabled by default
only where its decision with the fallback was at least as accurate as the existing path alone.

The HTTP contract (``local-decision.v1``): ``POST <url>/v1/decide`` with ``{"state", "questions": {name:
{"type": "choice", "instructions", "criteria": {label: description}} | {"type": "noul", "instructions",
"criteria": {"false", "true"}}}}``; the answer carries ``answers[name]`` with ``probabilities`` (choice) or
``noul`` (the probability of yes).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field, replace
from typing import Any, Dict, Mapping, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

PROTOCOL = "local-decision.v1"
DEFAULT_TIMEOUT_S = 0.25
#: The longest state sent: about 300 tokens of English, inside the 512-token context with the question and
#: its options. A longer input keeps the existing path; it is never cut to fit.
MAX_STATE_CHARS = 1200
_QUESTION = "decision"
_EPSILON = 1e-6


@dataclass(frozen=True)
class Point:
    """One decision point: its typed question, how its state is written, and its measured defaults."""

    name: str
    kind: str                         # "choice" | "yes_no"
    instructions: str
    labels: Mapping[str, str]         # choice: label -> what it means; yes_no: {"false": ..., "true": ...}
    state: str                        # a format string over the fields the caller passes
    enabled: bool = False
    temperature: float = 1.0
    abstain: Tuple[float, float] = (0.1, 0.9)

    def question(self) -> Dict[str, Any]:
        if self.kind == "choice":
            return {"type": "choice", "instructions": self.instructions, "criteria": dict(self.labels)}
        return {"type": "noul", "instructions": self.instructions, "criteria": dict(self.labels)}


# The defaults are the typed-decisions checkpoint's, measured in docs/DECISIONS.md: the temperature that fitted
# best, and the abstain threshold that did (a point whose best threshold was never to act keeps (0.1, 0.9)).
# Only owner_verdict, a veto, was at least as accurate as its existing path with the fallback.
POINTS: Dict[str, Point] = {point.name: point for point in (
    Point(
        "outreach_reply", "choice",
        "How does the owner answer the assistant's unprompted message?",
        {"engaged": "welcomes or acknowledges it and asks for nothing more",
         "dig_deeper": "asks for more on it: details, a follow-up or research",
         "not_interested": "does not want this topic, or found it useless",
         "not_now": "busy right now; it can wait until later",
         "stop": "wants no more unprompted messages at all"},
        'The assistant messaged the owner unprompted about {topic}. The owner answered: "{text}"',
        temperature=0.33, abstain=(0.0, 0.55)),
    Point(
        "opt_out", "yes_no",
        "Does the sender ask to receive no more messages from the assistant at all?",
        {"false": "anything else, including when, where or how to send something",
         "true": "they want no more messages at all"},
        'Message to the assistant: "{text}"',
        temperature=0.33, abstain=(0.15, 0.85)),
    Point(
        "no_reminders", "yes_no",
        "Does the person ask not to be reminded about this item?",
        {"false": "reminders are wanted, or not mentioned", "true": "they want no reminders about it"},
        'Item: {item}\nThe person said: "{text}"',
        temperature=0.25),
    Point(
        "interest_settled", "yes_no",
        "Does the owner say their question about this topic is answered or no longer wanted?",
        {"false": "still open, or not about this topic", "true": "answered, satisfied or dropped"},
        'Topic: {topic}\nThe owner said: "{text}"',
        temperature=0.25),
    Point(
        "owner_verdict", "yes_no",
        "Does the owner judge or correct the assistant's earlier reply, rather than ask for something new?",
        {"false": "a new request or other talk", "true": "a verdict or a correction on that reply"},
        'The assistant replied: "{reply}"\nThe owner then said: "{text}"',
        enabled=True, temperature=0.25, abstain=(0.03, 0.97)),
)}


@dataclass(frozen=True)
class Decision:
    """A typed answer: its label (a choice label, or "yes"/"no") and its calibrated probability."""

    point: str
    label: str
    probability: float
    probabilities: Mapping[str, float]
    elapsed_ms: float


def calibrate(probabilities: Mapping[str, float], temperature: float) -> Dict[str, float]:
    """Temperature scaling over a distribution: p^(1/T), renormalised. T above 1 softens, below 1 sharpens."""
    if temperature == 1.0:
        return dict(probabilities)
    weights = {label: float(p) ** (1.0 / temperature) for label, p in probabilities.items()}
    total = sum(weights.values())
    if not total > 0:
        return dict(probabilities)
    return {label: weight / total for label, weight in weights.items()}


def calibrate_yes(p_yes: float, temperature: float) -> float:
    """Temperature scaling of one probability of yes: its log-odds divided by T."""
    if temperature == 1.0:
        return float(p_yes)
    p = min(max(float(p_yes), _EPSILON), 1 - _EPSILON)
    return 1.0 / (1.0 + math.exp(-math.log(p / (1 - p)) / temperature))


def _probability(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) and 0.0 <= value <= 1.0 else None


class Decider:
    """The decision client: ``await decide(point, **fields)`` is a ``Decision`` or None (see the module)."""

    def __init__(self, url: str = "", *, timeout_s: float = DEFAULT_TIMEOUT_S,
                 points: Optional[Mapping[str, Mapping[str, Any]]] = None,
                 transport: Optional[httpx.AsyncBaseTransport] = None) -> None:
        self.url = str(url or "").rstrip("/")
        self.timeout_s = float(timeout_s)
        self.transport = transport
        self.points: Dict[str, Point] = dict(POINTS)
        for name, override in (points or {}).items():
            changes = _override(override) if name in self.points else None
            if changes is None:
                logger.warning("decision point override ignored: %s", name)
                continue
            self.points[name] = replace(self.points[name], **changes)
        self.stats: Dict[str, Counter] = defaultdict(Counter)

    def setting(self, point: str) -> Point:
        return self.points[point]

    def enabled(self, point: str) -> bool:
        return bool(self.url) and self.points[point].enabled

    async def decide(self, point: str, **fields: Any) -> Optional[Decision]:
        spec = self.points[point]
        if not self.enabled(point):
            return None
        state = spec.state.format(**{key: " ".join(str(value or "").split()) for key, value in fields.items()})
        if len(state) > MAX_STATE_CHARS:
            self.stats[point]["skipped"] += 1
            return None
        started = time.monotonic()
        try:
            body = await asyncio.wait_for(self._post({"state": state, "questions": {_QUESTION: spec.question()}}),
                                          self.timeout_s)
            decision = self._read(spec, body, (time.monotonic() - started) * 1000)
        except asyncio.CancelledError:
            raise
        except Exception as error:      # whatever went wrong, the caller keeps its existing path
            self.stats[point]["failed"] += 1
            logger.debug("decision %s: no answer (%s)", point, type(error).__name__)
            return None
        if decision is None:
            self.stats[point]["abstained"] += 1
            return None
        self.stats[point]["answered"] += 1
        return decision

    async def _post(self, payload: Dict[str, Any]) -> Any:
        async with httpx.AsyncClient(transport=self.transport, timeout=self.timeout_s) as client:
            response = await client.post(f"{self.url}/v1/decide", json=payload)
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _read(spec: Point, body: Any, elapsed_ms: float) -> Optional[Decision]:
        """The calibrated decision, or None inside the abstain band; ValueError for anything malformed."""
        if not isinstance(body, dict) or body.get("protocol") != PROTOCOL:
            raise ValueError("not a decision answer")
        answer = (body.get("answers") or {}).get(_QUESTION)
        if not isinstance(answer, dict):
            raise ValueError("the question is not answered")
        low, high = spec.abstain
        if spec.kind == "choice":
            raw = answer.get("probabilities")
            if answer.get("type") != "choice" or not isinstance(raw, dict) or set(raw) != set(spec.labels):
                raise ValueError("not a choice over the point's labels")
            values = {label: _probability(value) for label, value in raw.items()}
            if any(value is None for value in values.values()):
                raise ValueError("a probability is out of range")
            probabilities = calibrate(values, spec.temperature)
            label = max(probabilities, key=probabilities.get)
            if probabilities[label] < high:
                return None
            return Decision(spec.name, label, probabilities[label], probabilities, elapsed_ms)
        p_yes = _probability(answer.get("noul"))
        if answer.get("type") != "noul" or p_yes is None:
            raise ValueError("not a yes/no answer")
        p_yes = calibrate_yes(p_yes, spec.temperature)
        probabilities = {"yes": p_yes, "no": 1.0 - p_yes}
        if p_yes >= high:
            return Decision(spec.name, "yes", p_yes, probabilities, elapsed_ms)
        if p_yes <= low:
            return Decision(spec.name, "no", 1.0 - p_yes, probabilities, elapsed_ms)
        return None


def _override(override: Any) -> Optional[Dict[str, Any]]:
    """A point's ``{enabled, temperature, abstain}`` override checked, or None when any of it is unusable (the
    point then keeps its defaults whole: ``protagine.yaml`` refuses such a section, the environment may not)."""
    if not isinstance(override, Mapping) or set(override) - {"enabled", "temperature", "abstain"}:
        return None
    changes: Dict[str, Any] = {}
    try:
        if "enabled" in override:
            if not isinstance(override["enabled"], bool):
                return None
            changes["enabled"] = override["enabled"]
        if "temperature" in override:
            temperature = float(override["temperature"])
            if not (math.isfinite(temperature) and temperature > 0):
                return None
            changes["temperature"] = temperature
        if "abstain" in override:
            low, high = (float(value) for value in override["abstain"])
            if not 0.0 <= low <= high <= 1.0:
                return None
            changes["abstain"] = (low, high)
    except (TypeError, ValueError):
        return None
    return changes


def from_environment(environ: Optional[Mapping[str, str]] = None) -> Decider:
    """A decider from ``PROTAGINE_DECISIONS_URL``, ``_TIMEOUT_MS`` and ``_POINTS`` (JSON overrides per point),
    which ``protagine.yaml``'s ``decisions`` section exports. No URL: every point is off."""
    environ = os.environ if environ is None else environ
    url = str(environ.get("PROTAGINE_DECISIONS_URL") or "").strip()
    timeout_s = DEFAULT_TIMEOUT_S
    raw_timeout = str(environ.get("PROTAGINE_DECISIONS_TIMEOUT_MS") or "").strip()
    if raw_timeout:
        try:
            timeout_s = max(float(raw_timeout), 1.0) / 1000.0
        except ValueError:
            logger.warning("PROTAGINE_DECISIONS_TIMEOUT_MS is not a number; using %d ms", DEFAULT_TIMEOUT_S * 1000)
    points: Dict[str, Any] = {}
    raw_points = str(environ.get("PROTAGINE_DECISIONS_POINTS") or "").strip()
    if raw_points:
        try:
            loaded = json.loads(raw_points)
            if not isinstance(loaded, dict):
                raise ValueError("not a mapping")
            points = loaded
        except ValueError:
            logger.warning("PROTAGINE_DECISIONS_POINTS is not a JSON mapping; the points keep their defaults")
    return Decider(url, timeout_s=timeout_s, points=points)


_SHARED: Dict[Tuple[str, str, str], Decider] = {}


def shared() -> Decider:
    """The process's decider, rebuilt when its environment changes."""
    key = tuple(os.environ.get(name, "") for name in (
        "PROTAGINE_DECISIONS_URL", "PROTAGINE_DECISIONS_TIMEOUT_MS", "PROTAGINE_DECISIONS_POINTS"))
    if key not in _SHARED:
        _SHARED.clear()
        _SHARED[key] = from_environment()
    return _SHARED[key]


__all__ = ["DEFAULT_TIMEOUT_S", "MAX_STATE_CHARS", "POINTS", "Decider", "Decision", "Point", "calibrate",
           "calibrate_yes", "from_environment", "shared"]
