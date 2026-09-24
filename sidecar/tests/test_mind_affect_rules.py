"""The frozen stateless affect rules (evals 6.4): a pure function of the snapshot the state reads."""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta, timezone

from protagine.mind import affect_rules
from protagine.mind.affect import CONSUMERS, AffectEvent, AffectInputs, Obligation

NOW = datetime(2026, 9, 24, 9, 30, tzinfo=timezone.utc)


def event(kind, topic="quarterly figures", *, hours=1.0, ref=None, approach="the archive export", **extra):
    at = NOW - timedelta(hours=hours)
    return AffectEvent(kind=kind, ref=ref or f"outcome:{kind}-{topic}-{hours}", at=at, topic=topic,
                       approach=approach if kind == "failed" else "", **extra)


def inputs(*events, obligations=(), due_soon=(), running=0, cap=2, asks=0):
    return AffectInputs(now=NOW, owner_id="p-01", events=tuple(sorted(events, key=lambda e: (e.at, e.ref))),
                        obligations=tuple(obligations), due_soon=tuple(due_soon), running=running, cap=cap, asks=asks)


def obligation(ident, hours=2.0, started=False):
    return Obligation(id=ident, description=f"item {ident}", due_at=NOW + timedelta(hours=hours), started=started)


def test_two_failures_on_a_topic_within_a_day_switch_it():
    view = affect_rules.view(inputs(event("failed", hours=5), event("failed", "quarterly figure", hours=1)))
    [frustration] = view.frustrations
    assert frustration.level is None and frustration.failures == 2
    assert frustration.approaches == ("the archive export",)
    assert frustration.note() == ("Prior attempts at quarterly figures failed 2 times using the archive export; "
                                  "choose a different approach or ask one question.")
    assert view.failing("the stale quarterly figures") is frustration


def test_one_failure_old_failures_and_recovered_topics_do_not_switch():
    assert affect_rules.view(inputs(event("failed", hours=1))).frustrations == ()
    old = inputs(event("failed", hours=70), event("failed", hours=72))
    assert affect_rules.view(old).frustrations == (), "failures older than a day never switch"
    straddle = inputs(event("failed", hours=30), event("failed", hours=2))
    assert affect_rules.view(straddle).frustrations == (), "only failures inside the 24 h window count"
    recovered = inputs(event("failed", hours=6), event("failed", hours=5), event("succeeded", hours=3))
    assert affect_rules.view(recovered).frustrations == (), "failures before the last success do not count"
    for success in ("verified", "useful", "resolved"):
        assert affect_rules.view(inputs(event("failed", hours=6), event("failed", hours=5),
                                        event(success, hours=4))).frustrations == ()
    again = inputs(event("failed", hours=9), event("succeeded", hours=8), event("failed", hours=3),
                   event("failed", hours=2))
    assert [f.failures for f in affect_rules.view(again).frustrations] == [2]


def test_failures_and_successes_on_other_topics_do_not_spread():
    view = affect_rules.view(inputs(event("failed", hours=4), event("failed", hours=3),
                                    event("failed", "budget draft", hours=2),
                                    event("succeeded", "budget draft", hours=1)))
    assert [f.topic for f in view.frustrations] == ["quarterly figures"]
    mixed = affect_rules.view(inputs(event("failed", hours=4), event("failed", hours=3),
                                     event("failed", "budget draft", hours=2),
                                     event("failed", "budget draft", hours=1)))
    assert sorted(f.topic for f in mixed.frustrations) == ["budget draft", "quarterly figures"]
    assert mixed.failing("budget figures") is None


def test_overload_is_three_obligations_or_a_full_worker_pool():
    near = [obligation(str(i)) for i in range(3)]
    assert affect_rules.view(inputs(obligations=near)).overloaded is True
    assert affect_rules.view(inputs(obligations=near[:2])).overloaded is False
    assert affect_rules.view(inputs(running=2, cap=2)).overloaded is True
    assert affect_rules.view(inputs(running=1, cap=2)).overloaded is False
    assert affect_rules.view(inputs(obligations=near)).obligations == tuple(near)


def test_worry_is_one_half_while_anything_is_due_soon_and_curiosity_is_never_set():
    soon = obligation("c-17")
    view = affect_rules.view(inputs(obligations=[soon], due_soon=[soon]))
    assert view.worry == affect_rules.PRIORITY_WORRY == 0.5 and view.due_soon == (soon,)
    assert view.notes()[0].startswith("Due soon and not started: item c-17 (due 11:30 UTC)")
    calm = affect_rules.view(inputs(obligations=[soon]))
    assert calm.worry == 0.0 and calm.due_soon == () and calm.curiosity == 0.0
    duty = type("C", (), {"drive": "duty", "kind": "task", "priority": 0.7, "dedup_base": None, "recipient": None})()
    assert view.score_factor(duty) == 1.25 and calm.score_factor(duty) == 1.0


def test_two_dismissals_in_a_week_hold_optional_nudges():
    two = inputs(event("dismissed", hours=100), event("dismissed", hours=2, ref="outcome:second"))
    view = affect_rules.view(two)
    assert view.satiated is True and view.boost == affect_rules.SATIATION_BOOST == 0.5 and view.dismissals == 2
    assert "Holding back optional nudges: 2 were waved off recently." in view.notes()
    assert affect_rules.view(inputs(event("dismissed", hours=2))).satiated is False
    stale = inputs(event("dismissed", hours=24 * 8), event("dismissed", hours=24 * 9, ref="outcome:older"))
    assert affect_rules.view(stale).satiated is False


def test_the_rules_are_pure_and_route_every_consumer():
    snapshot = inputs(event("failed", hours=3), event("failed", hours=2), event("dismissed", hours=1),
                      event("dismissed", hours=1, ref="outcome:d2"), obligations=[obligation("a")],
                      due_soon=[obligation("a")])
    assert affect_rules.view(snapshot) == affect_rules.view(snapshot)
    assert list(inspect.signature(affect_rules.view).parameters) == ["inputs"]
    view = affect_rules.view(snapshot)
    assert dict(view.route) == {name: "rules" for name in CONSUMERS} and view.line == ""
    assert affect_rules.RULE_CONSUMERS == frozenset(), "the gate decides the wiring; none at M6"
