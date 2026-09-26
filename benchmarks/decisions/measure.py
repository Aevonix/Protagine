"""Measure a decision model against each decision point's existing path (``protagine.decisions``).

Three steps, each writing JSON, so the slow one runs once:

    python benchmarks/decisions/measure.py current --model-url URL --model NAME --out current.json
    python benchmarks/decisions/measure.py decide --url URL --out answers.json [--label NAME]
    python benchmarks/decisions/measure.py analyse --current current.json --answers answers.json [...] --out summary.json

``current`` runs the existing path of every point: the phrase tables for ``outreach_reply`` and ``opt_out``,
and for the others the production model call on an OpenAI-compatible endpoint (the capture prompt for
``no_reminders`` and ``interest_settled``, the night's lesson prompt for ``owner_verdict``), recording each
answer and its wall time. ``decide`` asks the decision model every item's question, one at a time, and records
the raw probabilities, the server's time and the round trip. ``analyse`` compares, per point: the existing path,
the model alone (zero-shot and temperature-scaled), and the model with the fallback as ``protagine.decisions``
wires it, with the temperature and the abstain band fitted by five-fold cross-validation (the reported accuracy
is the held-out folds'); and it gives the defaults fitted on the whole set. A point is enabled by default only
when the model with the fallback is at least as accurate as the existing path, overall and on the repository's
own items (tests and generators) alone.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import random
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sets  # noqa: E402

from protagine.decisions import MAX_STATE_CHARS, POINTS, answer_probabilities, calibrate, calibrate_yes  # noqa: E402

TURN_TIME = "2026-09-24T12:00:00+00:00"
FOLDS = 5
TEMPERATURES = (0.25, 0.33, 0.5, 0.67, 0.8, 1.0, 1.25, 1.5, 2.0, 3.0, 4.0, 6.0)
NEVER_HIGH, NEVER_LOW = 1.01, -0.01
HIGHS = (0.2, 0.3, 0.4, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.93, 0.95, 0.97, 0.99, NEVER_HIGH)
LOWS = (0.5, 0.45, 0.4, 0.35, 0.3, 0.25, 0.2, 0.15, 0.1, 0.07, 0.05, 0.03, 0.01, NEVER_LOW)
# How the decision composes with the existing path at each point (as wired in the sidecar):
#   fill: the existing answer when it has one, else the model's when it is sure (outreach_reply, opt_out,
#         interest_settled); guard: the model may only turn a reminder into a hold (no_reminders);
#   veto: the model may only withdraw a reported verdict (owner_verdict).
COMPOSITION = {"outreach_reply": "fill", "opt_out": "fill", "no_reminders": "fill", "interest_settled": "fill",
               "owner_verdict": "veto"}
MODEL_POINTS = ("no_reminders", "interest_settled", "owner_verdict")


def key(entry: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps([entry["point"], entry["fields"]], sort_keys=True).encode()).hexdigest()[:16]


# -- the existing path --------------------------------------------------------------------------------------

def rules_answer(entry: Dict[str, Any]) -> Dict[str, Any]:
    """The phrase tables' answer; ``fired`` is whether they said anything (else the point's default stands)."""
    started = time.perf_counter()
    fields = entry["fields"]
    if entry["point"] == "opt_out":
        from protagine.contacts.optout import detects_opt_out
        fired = detects_opt_out(fields["text"]) is not None
        label = "yes" if fired else "no"
    else:
        from protagine.mind import outreach as outreach_functions
        from protagine.mind import reactions
        topic = fields["topic"]
        known = reactions.content_terms(f"You said you care about {topic}, so I looked into {topic}.")
        about = lambda text: outreach_functions.similar(topic, text) or not (reactions.content_terms(text) - known)
        cls = reactions.read(reactions.strip_prefix(fields["text"])).reaction(about=about)
        fired = cls is not None
        label = sets.REPLY_LABEL[cls]
    return {"label": label, "fired": fired, "ms": (time.perf_counter() - started) * 1000}


def _capture_prompt(entry: Dict[str, Any]) -> tuple:
    from protagine.commitments import extract
    fields = entry["fields"]
    interests = [fields["topic"]] if entry["point"] == "interest_settled" else []
    prompt = extract.build_prompt(user_message=fields["text"], assistant_message="Understood.", conversation_text="",
                                  existing=[], rejections=[], turn_time=TURN_TIME, timezone_name="UTC",
                                  speaker="the owner", interests=interests)
    return extract.SYSTEM, prompt, extract.RESPONSE_SCHEMA


def _lesson_prompt(entry: Dict[str, Any]) -> tuple:
    from protagine.mind import lessons
    fields = entry["fields"]
    before = fields.get("before") or "Please take care of the next item on my list."
    text = "\n".join([
        "The owner's sessions (owner messages are labelled; the agent's replies follow them):",
        "Session s-1:",
        f"t1 [2026-09-24T11:50] owner: {lessons._clean(before, lessons.MESSAGE_CHARS)}",
        f"    agent: {fields['reply']}",
        f"t2 [2026-09-24T12:00] owner: {lessons._clean(fields['text'], lessons.MESSAGE_CHARS)}",
        "    agent: Noted."])
    return lessons.LESSON_SYSTEM, text, lessons.LESSON_SCHEMA


def model_label(entry: Dict[str, Any], content: str) -> str:
    """What the existing model call decided, read as the sidecar reads it."""
    point, fields = entry["point"], entry["fields"]
    if point == "owner_verdict":
        from protagine.mind.lessons import quoted
        answer = json.loads(content)
        reported = [verdict for verdict in answer.get("verdicts") or []
                    if isinstance(verdict, dict) and verdict.get("turn") == "t2"
                    and verdict.get("work_was") in {"right", "wrong"} and quoted(verdict.get("quote"), fields["text"])]
        return "yes" if reported else "no"
    from protagine.commitments.extract import parse_items
    items = parse_items(content)
    if point == "interest_settled":
        return "yes" if any(item.get("action") in {"complete", "cancel"} and item.get("target") in (1, "1")
                            for item in items) else "no"
    # no_reminders: "yes" (held) unless a new item would bring a reminder or a heads-up.
    reminded = any(item.get("action") == "create" and (item.get("due_at") or (
        isinstance(item.get("metadata"), dict) and (item["metadata"].get("heads_up_at")
                                                    or item["metadata"].get("lead_minutes"))))
                   for item in items)
    return "no" if reminded else "yes"


async def _model_call(client, url: str, model: str, entry: Dict[str, Any], gate: asyncio.Semaphore) -> Dict[str, Any]:
    system, user, schema = (_lesson_prompt if entry["point"] == "owner_verdict" else _capture_prompt)(entry)
    body = {"model": model, "max_tokens": 6000,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema", "json_schema": {**schema, "strict": True}}}
    async with gate:
        started = time.perf_counter()
        try:
            response = await client.post(f"{url.rstrip('/')}/chat/completions", json=body, timeout=900)
            response.raise_for_status()
            content = response.json()["choices"][0]["message"].get("content") or ""
            label, error = model_label(entry, content), None
        except Exception as exc:        # a failed call is the existing path failing: recorded, never retried
            label, error = None, type(exc).__name__
        return {"label": label, "error": error, "ms": (time.perf_counter() - started) * 1000}


async def current(args) -> None:
    import httpx
    built = sets.build()
    out = Path(args.out)
    results = json.loads(out.read_text()) if out.exists() else {}
    gate = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient() as client:
        pending = []
        for point, entries in built.items():
            for entry in entries:
                if key(entry) in results and results[key(entry)].get("label") is not None:
                    continue
                if point in MODEL_POINTS:
                    pending.append((entry, asyncio.ensure_future(_model_call(client, args.model_url, args.model,
                                                                             entry, gate))))
                else:
                    results[key(entry)] = rules_answer(entry)
        for index, (entry, task) in enumerate(pending, start=1):
            results[key(entry)] = await task
            if index % 5 == 0 or index == len(pending):
                out.write_text(json.dumps(results, indent=1, sort_keys=True))
                print(f"current: {index}/{len(pending)} model calls", flush=True)
    out.write_text(json.dumps(results, indent=1, sort_keys=True))


# -- the decision model -------------------------------------------------------------------------------------

def decide(args) -> None:
    import httpx
    built = sets.build()
    answers: Dict[str, Any] = {"label": args.label, "at": datetime.now(timezone.utc).isoformat(), "items": {}}
    with httpx.Client(timeout=10) as client:
        health = client.get(f"{args.url.rstrip('/')}/health").json()
        answers["backend"] = health.get("backend")
        for point, entries in built.items():
            spec = POINTS[point]
            for entry in entries:
                state = spec.state.format(**{k: " ".join(str(v or "").split()) for k, v in entry["fields"].items()})
                if len(state) > MAX_STATE_CHARS:
                    answers["items"][key(entry)] = {"skipped": "long"}
                    continue
                payload = {"state": state, "questions": {"decision": spec.question()}}
                for attempt in range(5):
                    started = time.perf_counter()
                    response = client.post(f"{args.url.rstrip('/')}/v1/decide", json=payload)
                    rtt = (time.perf_counter() - started) * 1000
                    if response.status_code != 429:
                        break
                    time.sleep(0.2)
                if response.status_code != 200:
                    answers["items"][key(entry)] = {"error": response.status_code}
                    continue
                body = response.json()
                try:                # read as the sidecar reads it: an answer it would refuse is no answer here
                    raw = answer_probabilities(spec, body)
                except ValueError as error:
                    answers["items"][key(entry)] = {"error": str(error)}
                    continue
                answers["items"][key(entry)] = {"raw": raw, "server_ms": body["elapsed_ms"], "rtt_ms": rtt,
                                                "tokens": body["usage"]["input_tokens"]}
    Path(args.out).write_text(json.dumps(answers, indent=1, sort_keys=True))
    print(f"decide: {sum(len(v) for v in built.values())} items -> {args.out}")


# -- analysis -----------------------------------------------------------------------------------------------

def tempered(point: str, raw: Dict[str, float], temperature: float) -> Dict[str, float]:
    if POINTS[point].kind == "choice":
        return calibrate(raw, temperature)
    p_yes = calibrate_yes(raw["yes"], temperature)
    return {"yes": p_yes, "no": 1 - p_yes}


def nll(point: str, rows, temperature: float) -> float:
    return -sum(math.log(max(tempered(point, row["raw"], temperature)[row["gold"]], 1e-9)) for row in rows) / len(rows)


def fit_temperature(point: str, rows) -> float:
    usable = [row for row in rows if row.get("raw")]
    return min(TEMPERATURES, key=lambda t: (nll(point, usable, t), abs(math.log(t)))) if usable else 1.0


def composed(point: str, row: Dict[str, Any], temperature: float, threshold: float) -> str:
    """The point's answer with the model and the fallback, as the sidecar wires it."""
    existing = row["current"]
    if not row.get("raw"):
        return existing
    probabilities = tempered(point, row["raw"], temperature)
    if COMPOSITION[point] == "veto":
        return "no" if existing == "yes" and probabilities["yes"] <= threshold else existing
    if row["fired"]:
        return existing
    if POINTS[point].kind == "choice":
        label = max(probabilities, key=probabilities.get)
        return label if probabilities[label] >= threshold else existing
    return "yes" if probabilities["yes"] >= threshold else existing


def fit_threshold(point: str, rows, temperature: float) -> float:
    grid = LOWS if COMPOSITION[point] == "veto" else HIGHS
    # The most accurate; among equals, the one that lets the model act least (the grid runs from bold to shy).
    return max(reversed(grid), key=lambda t: sum(composed(point, row, temperature, t) == row["gold"] for row in rows))


def ece(pairs, bins: int = 10) -> Optional[float]:
    """Expected calibration error of (confidence, correct) pairs over equal-width bins."""
    if not pairs:
        return None
    total = 0.0
    for index in range(bins):
        low, high = index / bins, (index + 1) / bins
        chunk = [(c, ok) for c, ok in pairs if (low < c <= high) or (index == 0 and c == 0)]
        if chunk:
            total += len(chunk) * abs(sum(c for c, _ in chunk) / len(chunk) - sum(ok for _, ok in chunk) / len(chunk))
    return total / len(pairs)


def top(point: str, raw, temperature: float):
    probabilities = tempered(point, raw, temperature)
    label = max(probabilities, key=probabilities.get)
    return label, probabilities[label]


def folds(rows, count: int = FOLDS):
    ordered = sorted(rows, key=lambda row: (row["gold"], row["key"]))
    random.Random(7).shuffle(ordered)
    ordered.sort(key=lambda row: row["gold"])
    return [ordered[index::count] for index in range(count)]


def percentile(values, q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))], 2)


def accuracy(rows, label_of) -> Dict[str, Any]:
    def share(subset):
        return {"correct": sum(label_of(row) == row["gold"] for row in subset), "n": len(subset)}
    repo = [row for row in rows if row["kind"] != "novel"]
    novel = [row for row in rows if row["kind"] == "novel"]
    return {"all": share(rows), "repo": share(repo), "novel": share(novel)}


def analyse_point(point: str, rows) -> Dict[str, Any]:
    answered = [row for row in rows if row.get("raw")]
    result: Dict[str, Any] = {"n": len(rows), "answered": len(answered),
                              "current": accuracy(rows, lambda row: row["current"]),
                              "current_ms": {"p50": percentile([row["current_ms"] for row in rows], 0.5),
                                             "p95": percentile([row["current_ms"] for row in rows], 0.95)},
                              "current_failed": sum(1 for row in rows if row.get("current_error"))}
    result["model_alone_zero_shot"] = accuracy(rows, lambda row: top(point, row["raw"], 1.0)[0] if row.get("raw")
                                               else row["current"])
    result["ece_zero_shot"] = ece([(top(point, row["raw"], 1.0)[1], top(point, row["raw"], 1.0)[0] == row["gold"])
                                   for row in answered])
    held_out, cv_pairs, cv_rows = [], [], []
    for index, fold in enumerate(folds(rows)):
        train = [row for other, part in enumerate(folds(rows)) if other != index for row in part]
        temperature = fit_temperature(point, train)
        threshold = fit_threshold(point, train, temperature)
        for row in fold:
            label = composed(point, row, temperature, threshold)
            held_out.append({**row, "composed": label})
            if row.get("raw"):
                cv_pairs.append((top(point, row["raw"], temperature)[1], top(point, row["raw"], temperature)[0]
                                 == row["gold"]))
                cv_rows.append({**row, "alone": top(point, row["raw"], temperature)[0]})
    result["model_alone_calibrated_cv"] = accuracy(cv_rows, lambda row: row["alone"])
    result["ece_calibrated_cv"] = ece(cv_pairs)
    result["with_fallback_cv"] = accuracy(held_out, lambda row: row["composed"])
    result["changed_cv"] = {
        "helped": sum(1 for row in held_out if row["composed"] != row["current"] and row["composed"] == row["gold"]),
        "hurt": sum(1 for row in held_out if row["composed"] != row["current"] and row["current"] == row["gold"])}
    temperature = fit_temperature(point, rows)
    threshold = fit_threshold(point, rows, temperature)
    result["fitted"] = {"temperature": temperature, "threshold": threshold, "composition": COMPOSITION[point],
                        "never": threshold in (NEVER_HIGH, NEVER_LOW)}
    fallback, now = result["with_fallback_cv"], result["current"]
    result["enable"] = (not result["fitted"]["never"]
                        and fallback["all"]["correct"] >= now["all"]["correct"]
                        and fallback["repo"]["correct"] >= now["repo"]["correct"])
    result["latency_ms"] = {
        "server_p50": percentile([row["server_ms"] for row in answered], 0.5),
        "server_p95": percentile([row["server_ms"] for row in answered], 0.95),
        "round_trip_p50": percentile([row["rtt_ms"] for row in answered], 0.5),
        "round_trip_p95": percentile([row["rtt_ms"] for row in answered], 0.95),
        "input_tokens_max": max((row["tokens"] for row in answered), default=None)}
    result["errors"] = [{"text": row["fields"].get("text"), "gold": row["gold"], "current": row["current"],
                         "composed": row["composed"]} for row in held_out if row["composed"] != row["gold"]][:20]
    return result


def analyse(args) -> None:
    built = sets.build()
    existing = json.loads(Path(args.current).read_text())
    summary: Dict[str, Any] = {"at": datetime.now(timezone.utc).isoformat(), "folds": FOLDS, "checkpoints": {}}
    for path in args.answers:
        answers = json.loads(Path(path).read_text())
        points = {}
        for point, entries in built.items():
            rows = []
            for entry in entries:
                now, said = existing.get(key(entry)), answers["items"].get(key(entry), {})
                if now is None or now.get("label") is None:
                    continue            # an existing-path call that failed is left out (counted below)
                rows.append({"key": key(entry), "fields": entry["fields"], "gold": entry["gold"],
                             "kind": entry["source"].split(":")[0], "current": now["label"],
                             "fired": now.get("fired", True), "current_ms": now["ms"],
                             **({k: said[k] for k in ("raw", "server_ms", "rtt_ms", "tokens")} if "raw" in said
                                else {})})
            result = analyse_point(point, rows)
            result["current_missing"] = len(entries) - len(rows)
            points[point] = result
        summary["checkpoints"][answers["label"]] = {"backend": answers.get("backend"), "points": points}
    Path(args.out).write_text(json.dumps(summary, indent=1, sort_keys=True))
    for label, block in summary["checkpoints"].items():
        print(f"== {label}")
        for point, result in block["points"].items():
            def pct(share):
                return f"{share['correct']}/{share['n']}"

            def num(value):
                return "-" if value is None else f"{value:.3f}"
            print(f"{point:17s} current {pct(result['current']['all'])} (repo {pct(result['current']['repo'])}) | "
                  f"alone0 {pct(result['model_alone_zero_shot']['all'])} ece0 {num(result['ece_zero_shot'])} | "
                  f"aloneT {pct(result['model_alone_calibrated_cv']['all'])} eceT {num(result['ece_calibrated_cv'])} | "
                  f"fallback {pct(result['with_fallback_cv']['all'])} (repo {pct(result['with_fallback_cv']['repo'])}"
                  f", novel {pct(result['with_fallback_cv']['novel'])}) helped {result['changed_cv']['helped']} "
                  f"hurt {result['changed_cv']['hurt']} | T {result['fitted']['temperature']} "
                  f"t {result['fitted']['threshold']} | enable {result['enable']} | "
                  f"rtt p50 {result['latency_ms']['round_trip_p50']} server p50 {result['latency_ms']['server_p50']}"
                  f" | current p50 {result['current_ms']['p50']} ms")


def main(argv: Optional[List[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="step", required=True)
    step = sub.add_parser("current")
    step.add_argument("--model-url", required=True)
    step.add_argument("--model", required=True)
    step.add_argument("--concurrency", type=int, default=2)
    step.add_argument("--out", required=True)
    step = sub.add_parser("decide")
    step.add_argument("--url", required=True)
    step.add_argument("--label", default="decision-model")
    step.add_argument("--out", required=True)
    step = sub.add_parser("analyse")
    step.add_argument("--current", required=True)
    step.add_argument("--answers", nargs="+", required=True)
    step.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    if args.step == "current":
        asyncio.run(current(args))
    elif args.step == "decide":
        decide(args)
    else:
        analyse(args)


if __name__ == "__main__":
    main()
