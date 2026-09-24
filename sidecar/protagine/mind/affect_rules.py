"""The frozen stateless affect rules: the mechanism arm of the affect family (evals 6.4).

Each rule is a windowed count over the same ``AffectInputs`` snapshot the decaying state reads,
fixed before any result (the evals section 3 examples). With ``mind.faculties.affect_rules`` on,
every consumer reads its rule; otherwise only the consumers in ``RULE_CONSUMERS`` do. That set is
the gate's per-consumer decision (a rule that ties or beats the state replaces it for that
consumer), a code constant rather than a setting, and empty until the held-out gate has run. The
rules carry no tone: the tone line only ever comes from the state.
"""

from __future__ import annotations

from datetime import timedelta

from .affect import CONSUMERS, SWITCH_FAILURES, AffectInputs, AffectView, dismissals_of, failure_record, load_of

RULE_CONSUMERS: frozenset = frozenset()   # consumers the gate assigned to their rule; none at M6
SWITCH_WINDOW = timedelta(hours=24)       # the rule counts SWITCH_FAILURES (shared with the state) in this window
OVERLOAD_OBLIGATIONS = 3
SATIATION_DISMISSALS, SATIATION_WINDOW, SATIATION_BOOST = 2, timedelta(days=7), 0.5
PRIORITY_WORRY = 0.5                       # owed duty x 1.25 while anything owed is due soon and not started


def view(inputs: AffectInputs) -> AffectView:
    """Every consumer's rule over the snapshot; pure (no store, no clock beyond ``inputs.now``)."""
    # The switch and the failure record are the same count in the rule's window.
    frustrations = tuple(failure_record(inputs.events, since=inputs.now - SWITCH_WINDOW, prefix="rule:"))
    dismissals = dismissals_of(inputs, SATIATION_WINDOW)
    satiated = dismissals >= SATIATION_DISMISSALS
    return AffectView(
        route={name: "rules" for name in CONSUMERS}, owner_id=inputs.owner_id, frustrations=frustrations,
        tried=frustrations,
        overloaded=len(inputs.obligations) >= OVERLOAD_OBLIGATIONS or (inputs.cap > 0 and inputs.running >= inputs.cap),
        load=load_of(inputs), obligations=inputs.obligations,
        worry=PRIORITY_WORRY if inputs.due_soon else 0.0, curiosity=0.0, due_soon=inputs.due_soon,
        satiated=satiated, boost=SATIATION_BOOST if satiated else 0.0, dismissals=dismissals, line="")


__all__ = ["OVERLOAD_OBLIGATIONS", "PRIORITY_WORRY", "RULE_CONSUMERS", "SATIATION_BOOST", "SATIATION_DISMISSALS",
           "SATIATION_WINDOW", "SWITCH_FAILURES", "SWITCH_WINDOW", "view"]
