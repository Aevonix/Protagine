"""The decision-model measurement (``benchmarks/decisions``): its labelled sets and the arithmetic that decides
whether a decision point is enabled by default."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from protagine.decisions import MAX_STATE_CHARS, POINTS

HERE = Path(__file__).resolve().parents[2] / "benchmarks" / "decisions"


def _load(name):
    sys.path.insert(0, str(HERE))
    try:
        spec = importlib.util.spec_from_file_location(f"decisions_{name}", HERE / f"{name}.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(HERE))


measure = _load("measure")


@pytest.fixture(scope="module")
def built():
    return measure.sets.build()


def test_every_point_has_a_labelled_set_of_short_inputs(built):
    assert set(built) == set(POINTS)
    for point, items in built.items():
        spec = POINTS[point]
        labels = set(spec.labels) if spec.kind == "choice" else {"yes", "no"}
        assert {item["gold"] for item in items} == labels, point
        assert all(item["source"].split(":")[0] in {"generator", "test", "novel"} for item in items), point
        states = [spec.state.format(**item["fields"]) for item in items]
        assert sum(len(state) <= MAX_STATE_CHARS for state in states) >= 0.9 * len(states), point
        repo = [item for item in items if item["source"] != "novel"]
        assert len(repo) >= 2 * (len(items) - len(repo)), f"{point}: the repository's own words are the bulk"


def row(current, gold, raw, fired=False):
    return {"current": current, "gold": gold, "raw": raw, "fired": fired, "key": gold + current}


def test_the_model_fills_only_where_the_existing_path_said_nothing():
    reply = {"engaged": 0.1, "dig_deeper": 0.1, "not_interested": 0.7, "not_now": 0.05, "stop": 0.05}
    assert measure.composed("outreach_reply", row("engaged", "not_interested", reply), 1.0, 0.6) == "not_interested"
    assert measure.composed("outreach_reply", row("engaged", "engaged", reply), 1.0, 0.8) == "engaged"   # unsure
    assert measure.composed("outreach_reply", row("stop", "stop", reply, fired=True), 1.0, 0.1) == "stop"
    assert measure.composed("opt_out", row("no", "yes", {"yes": 0.9, "no": 0.1}), 1.0, 0.8) == "yes"
    assert measure.composed("opt_out", row("no", "no", {"yes": 0.1, "no": 0.9}), 1.0, 0.8) == "no"
    # A veto only withdraws a yes.
    assert measure.composed("owner_verdict", row("yes", "no", {"yes": 0.1, "no": 0.9}), 1.0, 0.2) == "no"
    assert measure.composed("owner_verdict", row("no", "yes", {"yes": 0.99, "no": 0.01}), 1.0, 0.2) == "no"


def test_a_model_that_only_hurts_is_fitted_to_never_act():
    rows = [row("no", "no", {"yes": 0.95, "no": 0.05}) for _ in range(5)] + [row("yes", "yes", {"yes": 0.9, "no": 0.1},
                                                                                fired=True)]
    assert measure.fit_threshold("opt_out", rows, 1.0) == measure.NEVER_HIGH
    helpful = [row("no", "yes", {"yes": 0.95, "no": 0.05}) for _ in range(5)] + [row("no", "no", {"yes": 0.3, "no": 0.7})]
    assert measure.fit_threshold("opt_out", helpful, 1.0) < measure.NEVER_HIGH


def test_calibration_error_is_zero_when_confidence_matches_accuracy():
    assert measure.ece([(0.75, True), (0.75, True), (0.75, True), (0.75, False)]) == pytest.approx(0.0)
    assert measure.ece([(0.95, False), (0.95, False)]) == pytest.approx(0.95)
    assert measure.ece([]) is None


def test_the_temperature_that_fits_best_is_found():
    overconfident = [row("no", "yes", {"yes": 0.99, "no": 0.01}) for _ in range(6)] + [
        row("no", "no", {"yes": 0.99, "no": 0.01}) for _ in range(4)]
    assert measure.fit_temperature("opt_out", overconfident) > 1.0


def _analysed(tmp_path, monkeypatch, entries, current, raw):
    """``analyse`` over ``entries``, the existing path's records as ``current`` writes them, and the model's."""
    import json
    monkeypatch.setattr(measure.sets, "build", lambda: {point: [e for e in entries if e["point"] == point]
                                                        for point in {e["point"] for e in entries}})
    (tmp_path / "current.json").write_text(json.dumps({measure.key(e): current(e) for e in entries}))
    (tmp_path / "answers.json").write_text(json.dumps({"label": "m", "items": {
        measure.key(e): {"raw": {"yes": raw(e), "no": 1 - raw(e)}, "server_ms": 5.0, "rtt_ms": 9.0, "tokens": 40}
        for e in entries}}))
    measure.analyse(SimpleNamespace(current=str(tmp_path / "current.json"),
                                    answers=[str(tmp_path / "answers.json")], out=str(tmp_path / "out.json")))
    return json.loads((tmp_path / "out.json").read_text())["checkpoints"]["m"]["points"]


def _entry(point, gold, index, **fields):
    return {"point": point, "gold": gold, "source": f"test:{index}", "fields": fields}


def test_a_confident_model_corrects_a_capture_miss_the_wiring_would_ask_about(tmp_path, monkeypatch):
    """The model-call points record what ``_model_call`` records (a label, an error, a time; no ``fired``): the
    model may act exactly where the sidecar would ask it, so ten capture misses it reads right are corrected."""
    entries = ([_entry("no_reminders", "yes", i, item="Pay the rent", text=f"I'll do it, no reminders ({i}).")
                for i in range(10)]
               + [_entry("no_reminders", "no", 10 + i, item="Pay the rent", text=f"Remind me at five ({i}).")
                  for i in range(10)]
               + [_entry("interest_settled", "yes", i, topic="tide tables",
                         text=f"Found a video on tide tables, so I'm sorted there ({i}).") for i in range(10)]
               + [_entry("interest_settled", "no", 10 + i, topic="tide tables",
                         text=f"The lease is signed, so I'm sorted there ({i}).") for i in range(10)])

    def current(entry):                 # the capture call missed every hold and every settlement
        return {"label": "no", "error": None, "ms": 900.0}

    def raw(entry):                     # the model reads each case right, and is sure of the unrelated settlement
        text = entry["fields"]["text"]
        return 0.99 if "no reminders" in text or "sorted" in text else 0.01

    points = _analysed(tmp_path, monkeypatch, entries, current, raw)
    held, settled = points["no_reminders"], points["interest_settled"]
    assert held["current"]["all"] == {"correct": 10, "n": 20}
    assert held["with_fallback_cv"]["all"] == {"correct": 20, "n": 20} and held["enable"] is True
    # A turn that does not name the topic is never asked (the sidecar's prefilter): the model's sure "yes" to the
    # signed lease is not an answer, so only the ten that name it are corrected.
    assert settled["with_fallback_cv"]["all"] == {"correct": 20, "n": 20} and settled["enable"] is True


def test_a_capture_point_is_never_asked_where_the_existing_path_already_acted():
    held = {"current": "yes", "gold": "no", "raw": {"yes": 0.01, "no": 0.99}, "fields": {"item": "x", "text": "y"}}
    assert measure.composed("no_reminders", held, 1.0, 0.5) == "yes"        # a guard only turns a reminder into a hold
    assert measure.composed("interest_settled", {**held, "fields": {"topic": "tide tables",
                                                                    "text": "tide tables, sorted"}}, 1.0, 0.5) == "yes"


def test_a_phrase_point_without_its_record_of_firing_is_refused():
    with pytest.raises(KeyError):
        measure.composed("opt_out", {"current": "no", "gold": "yes", "raw": {"yes": 0.99, "no": 0.01}}, 1.0, 0.5)


def test_the_defaults_are_the_recorded_measurement():
    """Each point's default is the typed-decisions checkpoint's fitted temperature and threshold, and it is enabled
    exactly where the recorded measurement enabled it."""
    import json
    [recorded] = sorted((HERE / "results").glob("*.json"))[-1:]
    points = json.loads(recorded.read_text())["checkpoints"]["typed-decisions"]["points"]
    assert set(points) == set(POINTS)
    for name, result in points.items():
        spec, fitted = POINTS[name], result["fitted"]
        assert spec.enabled is result["enable"], name
        assert spec.temperature == fitted["temperature"], name
        if not fitted["never"]:
            bound = spec.abstain[0] if fitted["composition"] == "veto" else spec.abstain[1]
            assert bound == fitted["threshold"], name
    assert {name for name, spec in POINTS.items() if spec.enabled} == {"owner_verdict"}
