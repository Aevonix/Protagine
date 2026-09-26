"""The fast decision layer: a decision model's typed answer, or None so the caller keeps its existing path."""

from __future__ import annotations

import asyncio
import json
import math

import httpx
import pytest

from protagine import decisions
from protagine.decisions import POINTS, Decider, calibrate, from_environment

URL = "http://decide.test"


def choice_answer(probabilities, *, question="q"):
    top = max(probabilities, key=probabilities.get)
    return {"protocol": "local-decision.v1", "backend": {"name": "fake"}, "elapsed_ms": 3.0,
            "usage": {"input_tokens": 40, "output_tokens": 0}, "input_truncated": False,
            "answers": {question: {"type": "choice", "choice": top, "probabilities": probabilities,
                                   "confidence": 0.5, "action": {"act_probability": 1.0}}}}


def yes_no_answer(p_yes, *, question="q"):
    return {"protocol": "local-decision.v1", "backend": {"name": "fake"}, "elapsed_ms": 3.0,
            "usage": {"input_tokens": 40, "output_tokens": 0}, "input_truncated": False,
            "answers": {question: {"type": "noul", "noul": p_yes, "confidence": 0.5,
                                   "action": {"act_probability": 1.0}}}}


class Server:
    """A fake decision endpoint: ``answer(request_json)`` is the JSON body, or an ``httpx.Response``."""

    def __init__(self, answer):
        self.answer, self.requests = answer, []

    def transport(self):
        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            self.requests.append((request.url.path, body))
            result = self.answer(body)
            if isinstance(result, httpx.Response):
                return result
            [name] = body["questions"]                       # the answer is to the question asked
            result["answers"] = {name: answer for answer in result["answers"].values()}
            return httpx.Response(200, json=result)
        return httpx.MockTransport(handler)


def decider(server, **points):
    return Decider(URL, timeout_s=0.25, points={name: {"enabled": True, **config} for name, config in points.items()},
                   transport=server.transport())


REPLY = {"engaged": 0.05, "dig_deeper": 0.8, "not_interested": 0.05, "not_now": 0.05, "stop": 0.05}


async def test_an_enabled_point_asks_one_typed_question_and_returns_the_answer():
    server = Server(lambda body: choice_answer(REPLY))
    decision = await decider(server, outreach_reply={"temperature": 1.0, "abstain": [0.0, 0.5]}).decide(
        "outreach_reply", text="Who ran it?", topic="tidal energy")
    assert decision is not None and decision.label == "dig_deeper" and decision.probability == pytest.approx(0.8)
    [(path, body)] = server.requests
    assert path == "/v1/decide" and set(body) == {"state", "questions"}
    [(name, question)] = body["questions"].items()
    assert question["type"] == "choice" and set(question["criteria"]) == set(POINTS["outreach_reply"].labels)
    assert "tidal energy" in body["state"] and "Who ran it?" in body["state"]


async def test_a_disabled_point_or_no_endpoint_makes_no_call():
    server = Server(lambda body: choice_answer(REPLY))
    off = Decider(URL, points={"outreach_reply": {"enabled": False}}, transport=server.transport())
    assert not off.enabled("outreach_reply")
    assert await off.decide("outreach_reply", text="Who ran it?", topic="tidal energy") is None
    nowhere = Decider("", points={"outreach_reply": {"enabled": True}}, transport=server.transport())
    assert not nowhere.enabled("outreach_reply")
    assert await nowhere.decide("outreach_reply", text="Who ran it?", topic="tidal energy") is None
    assert server.requests == []


async def test_an_answer_inside_the_abstain_band_is_no_answer():
    server = Server(lambda body: choice_answer(REPLY))
    assert await decider(server, outreach_reply={"temperature": 1.0, "abstain": [0.0, 0.9]}).decide(
        "outreach_reply", text="Who ran it?", topic="tidal energy") is None
    yes = Server(lambda body: yes_no_answer(0.7))
    band = decider(yes, opt_out={"temperature": 1.0, "abstain": [0.2, 0.8]})
    assert await band.decide("opt_out", text="whatever") is None                  # 0.7 is inside (0.2, 0.8)
    assert band.stats["opt_out"]["abstained"] == 1
    sure = await decider(Server(lambda body: yes_no_answer(0.95)), opt_out={"temperature": 1.0, "abstain": [0.2, 0.8]}).decide(
        "opt_out", text="whatever")
    assert sure.label == "yes" and sure.probability == pytest.approx(0.95)
    no = await decider(Server(lambda body: yes_no_answer(0.1)), opt_out={"temperature": 1.0, "abstain": [0.2, 0.8]}).decide(
        "opt_out", text="whatever")
    assert no.label == "no" and no.probability == pytest.approx(0.9)


def test_temperature_scaling_is_calibration_and_keeps_the_ranking():
    flat = calibrate({"a": 0.8, "b": 0.2}, 2.0)
    assert flat["a"] == pytest.approx(math.sqrt(0.8) / (math.sqrt(0.8) + math.sqrt(0.2)))
    assert calibrate({"a": 0.8, "b": 0.2}, 1.0) == pytest.approx({"a": 0.8, "b": 0.2})
    sharp = calibrate({"a": 0.6, "b": 0.3, "c": 0.1}, 0.5)
    assert sharp["a"] > 0.6 and sharp["a"] > sharp["b"] > sharp["c"] and sum(sharp.values()) == pytest.approx(1)
    assert calibrate({"a": 1.0, "b": 0.0}, 3.0)["a"] == pytest.approx(1.0)


async def test_the_points_temperature_is_applied_before_the_band():
    server = Server(lambda body: yes_no_answer(0.9))
    cooled = await decider(server, opt_out={"temperature": 4.0, "abstain": [0.2, 0.8]}).decide("opt_out", text="x")
    assert cooled is None           # 0.9 at temperature 4 is about 0.63: inside the band
    warm = await decider(server, opt_out={"temperature": 0.5, "abstain": [0.2, 0.8]}).decide("opt_out", text="x")
    assert warm.label == "yes" and warm.probability == pytest.approx(0.81 / 0.82)


@pytest.mark.parametrize("response", [
    httpx.Response(429, json={"detail": "inference busy"}),
    httpx.Response(503, json={"detail": "inference failed"}),
    httpx.Response(422, json={"detail": {"code": "input_would_truncate"}}),
    httpx.Response(200, text="not json"),
    httpx.Response(200, json={"protocol": "other", "answers": {}}),
    httpx.Response(200, json=choice_answer({"engaged": 0.9, "unknown_label": 0.1})),
    httpx.Response(200, json=choice_answer({**REPLY, "stop": 1.7})),
    httpx.Response(200, json=yes_no_answer(0.9)),          # a yes/no answer to a choice question
])
async def test_a_failed_or_malformed_answer_is_no_answer(response):
    server = Server(lambda body: response)
    ask = decider(server, outreach_reply={"abstain": [0.0, 0.0]})
    assert await ask.decide("outreach_reply", text="Who ran it?", topic="tidal energy") is None
    assert ask.stats["outreach_reply"]["failed"] == 1


def _enveloped(body, **changes):
    body = {**body, **changes}
    return {key: value for key, value in body.items() if value is not _DROP}


_DROP = object()


@pytest.mark.parametrize("changes", [
    {"input_truncated": True},                  # the model read a cut input: its answer is about other words
    {"input_truncated": _DROP},                 # the contract says it was not cut; an answer that does not say so
    {"input_truncated": None},
    {"input_truncated": 0},
    {"input_truncated": "false"},
])
async def test_an_answer_that_does_not_say_its_input_was_read_whole_is_no_answer(changes):
    server = Server(lambda body: _enveloped(yes_no_answer(0.01), **changes))
    ask = decider(server, owner_verdict={})
    assert await ask.decide("owner_verdict", reply="The order code is C4411.", text="That was wrong.") is None
    assert ask.stats["owner_verdict"]["failed"] == 1 and "answered" not in ask.stats["owner_verdict"]


async def test_an_answer_to_other_questions_than_the_one_asked_is_no_answer():
    extra = yes_no_answer(0.01, question=decisions._QUESTION)
    extra["answers"]["another"] = dict(extra["answers"][decisions._QUESTION])
    ask = decider(Server(lambda body: httpx.Response(200, json=extra)), owner_verdict={})
    assert await ask.decide("owner_verdict", reply="The order code is C4411.", text="That was wrong.") is None
    assert ask.stats["owner_verdict"]["failed"] == 1


async def test_a_slow_endpoint_is_no_answer_within_the_timeout():
    async def slow(request):
        await asyncio.sleep(2)
        return httpx.Response(200, json=choice_answer(REPLY))

    class Slow(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return await slow(request)

    ask = Decider(URL, timeout_s=0.05, points={"outreach_reply": {"enabled": True, "abstain": [0.0, 0.0]}},
                  transport=Slow())
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await ask.decide("outreach_reply", text="Who ran it?", topic="tidal energy") is None
    assert loop.time() - started < 1.0


class Hanging(httpx.AsyncBaseTransport):
    """An endpoint that accepts the request and never answers."""

    async def handle_async_request(self, request):
        await asyncio.Event().wait()


@pytest.mark.parametrize("raw", ["inf", "Infinity", "-inf", "nan", "NaN", "0", "-5", "0.5", "5001", "60000",
                                 "1e308", "abc", ""])
async def test_an_unusable_time_limit_from_the_environment_keeps_the_default_one(raw):
    """A time limit outside 1 to 5000 ms (what protagine.yaml accepts), or not a finite number, is ignored: the
    call still ends at the default limit, so a hanging endpoint never holds the caller."""
    ask = from_environment({"PROTAGINE_DECISIONS_URL": URL, "PROTAGINE_DECISIONS_TIMEOUT_MS": raw,
                            "PROTAGINE_DECISIONS_POINTS": json.dumps({"opt_out": {"enabled": True}})})
    assert ask.timeout_s == decisions.DEFAULT_TIMEOUT_S
    ask.transport = Hanging()
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await asyncio.wait_for(ask.decide("opt_out", text="leave it"), 2.0) is None
    assert loop.time() - started < decisions.DEFAULT_TIMEOUT_S + 0.5


@pytest.mark.parametrize("seconds", [math.inf, -math.inf, math.nan, 0.0, -1.0, 5.001, 600.0, "soon", None, True])
async def test_the_client_refuses_an_unusable_time_limit(seconds):
    ask = Decider(URL, timeout_s=seconds, points={"opt_out": {"enabled": True}}, transport=Hanging())
    assert ask.timeout_s == decisions.DEFAULT_TIMEOUT_S
    assert await asyncio.wait_for(ask.decide("opt_out", text="leave it"), 2.0) is None


def test_a_usable_time_limit_is_kept():
    assert from_environment({"PROTAGINE_DECISIONS_TIMEOUT_MS": "1"}).timeout_s == pytest.approx(0.001)
    assert from_environment({"PROTAGINE_DECISIONS_TIMEOUT_MS": "5000"}).timeout_s == pytest.approx(5.0)
    assert Decider(URL, timeout_s=0.05).timeout_s == pytest.approx(0.05)


async def test_an_unreachable_endpoint_is_no_answer():
    def refuse(request):
        raise httpx.ConnectError("refused", request=request)
    ask = Decider(URL, points={"opt_out": {"enabled": True}}, transport=httpx.MockTransport(refuse))
    assert await ask.decide("opt_out", text="leave it") is None


async def test_an_input_too_long_for_the_model_is_never_sent():
    server = Server(lambda body: yes_no_answer(0.99))
    ask = decider(server, opt_out={"abstain": [0.2, 0.8]})
    assert await ask.decide("opt_out", text="word " * 400) is None
    assert server.requests == [] and ask.stats["opt_out"]["skipped"] == 1


async def test_an_unknown_point_is_refused():
    with pytest.raises(KeyError):
        await decider(Server(lambda body: yes_no_answer(0.9))).decide("anything", text="x")


def test_every_point_is_a_short_typed_question():
    for name, point in POINTS.items():
        assert point.kind in {"choice", "yes_no"}, name
        assert 0 < point.temperature and 0 <= point.abstain[0] <= point.abstain[1] <= 1, name
        assert point.kind != "choice" or 2 <= len(point.labels) <= 8, name
        assert len(point.instructions) <= 200, name


def test_the_environment_configures_the_decider():
    environ = {"PROTAGINE_DECISIONS_URL": URL, "PROTAGINE_DECISIONS_TIMEOUT_MS": "400",
               "PROTAGINE_DECISIONS_POINTS": json.dumps({"opt_out": {"enabled": True, "temperature": 2.0,
                                                                     "abstain": [0.1, 0.95]}})}
    configured = from_environment(environ)
    assert configured.url == URL and configured.timeout_s == pytest.approx(0.4)
    assert configured.enabled("opt_out") and configured.setting("opt_out").temperature == 2.0
    assert configured.setting("opt_out").abstain == (0.1, 0.95)
    assert configured.enabled("outreach_reply") is POINTS["outreach_reply"].enabled
    assert not from_environment({}).enabled("opt_out")
    # A malformed override is logged and ignored: the points keep their defaults.
    broken = from_environment({"PROTAGINE_DECISIONS_URL": URL, "PROTAGINE_DECISIONS_POINTS": "{not json"})
    assert broken.enabled("opt_out") is POINTS["opt_out"].enabled


def test_the_shared_decider_follows_the_environment(monkeypatch):
    monkeypatch.delenv("PROTAGINE_DECISIONS_URL", raising=False)
    assert decisions.shared().url == ""
    monkeypatch.setenv("PROTAGINE_DECISIONS_URL", URL)
    assert decisions.shared().url == URL and decisions.shared() is decisions.shared()


def test_a_malformed_override_or_url_never_breaks_the_caller():
    environ = {"PROTAGINE_DECISIONS_URL": URL, "PROTAGINE_DECISIONS_POINTS": json.dumps({
        "opt_out": {"enabled": True, "temperature": "hot"}, "no_reminders": {"enabled": True, "abstain": [0.9, 0.1]},
        "interest_settled": {"enabled": True, "temperature": -1}, "owner_verdict": {"abstain": 3}})}
    configured = from_environment(environ)
    for name in ("opt_out", "no_reminders", "interest_settled", "owner_verdict"):
        assert configured.setting(name) == POINTS[name], name


async def test_an_unusable_url_is_no_answer():
    ask = Decider("not a url at all", points={"opt_out": {"enabled": True}})
    assert await ask.decide("opt_out", text="leave it") is None
    assert ask.stats["opt_out"]["failed"] == 1
