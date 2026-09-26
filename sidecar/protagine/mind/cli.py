"""``protagine mind``: status, the audit log, asks, verdicts, the level, the off switch and the opinions.

Every mutation the owner makes goes through this CLI or the owner-checked
``protagine_self`` tool (architecture 7.10). The CLI talks to the running
sidecar with the instance key; ``off`` and ``level`` also work when the
sidecar is down, by writing the marker file and ``protagine.yaml`` directly.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from protagine.config import load_config, read_api_key, update_config

from .audit import render_log, render_stats
from .authority import CLASSES, LEVELS
from .outcomes import VERDICTS
from .tick import CONSOLIDATION_WAIT_S, OFF_MARKER
from protagine.util.temporal import now_utc

COMMANDS = ("status", "log", "why", "asks", "yes", "no", "rate", "level", "reset", "off", "on", "tick", "stats",
            "concerns", "goals", "interest", "outreach", "consolidate", "narrative", "opinions", "lessons")
OPINION_ACTIONS = ("list", "show", "withdraw", "reconsider")
LESSON_ACTIONS = ("list", "show", "retire")


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("mind", help="The mind: status, audit log, asks, verdicts, level, off switch")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    commands = parser.add_subparsers(dest="mind_command")
    commands.add_parser("status", help="Enabled, level, queues, breaker, budgets")
    log = commands.add_parser("log", help="The audit log, newest first")
    log.add_argument("--limit", type=int, default=20)
    log.add_argument("--status", default=None, help="Comma-separated statuses")
    log.add_argument("--kind", default=None, help="Comma-separated kinds")
    why = commands.add_parser("why", help="One intention: drive, evidence, decision, Hermes ref, outcome")
    why.add_argument("id")
    commands.add_parser("asks", help="Open asks with their codes")
    yes = commands.add_parser("yes", help="Approve an ask")
    yes.add_argument("code")
    no = commands.add_parser("no", help="Refuse an ask")
    no.add_argument("code")
    rate = commands.add_parser("rate", help="Give a verdict on an intention")
    rate.add_argument("id")
    rate.add_argument("verdict", choices=VERDICTS)
    level = commands.add_parser("level", help="Show or set the autonomy level")
    level.add_argument("autonomy", nargs="?", choices=LEVELS)
    reset = commands.add_parser("reset", help="Reset a tripped breaker")
    reset.add_argument("cls", choices=[cls for cls in CLASSES if cls != "floor"])
    off = commands.add_parser("off", help="No further effects (works with the model endpoint down)")
    off.add_argument("--reason", default="owner")
    commands.add_parser("on", help="Turn the mind back on")
    commands.add_parser("tick", help="Run one tick now")
    stats = commands.add_parser("stats", help="The in-vivo panel over the audit log")
    stats.add_argument("--days", type=int, default=7)
    concerns = commands.add_parser("concerns", help="What is on the mind: open concerns and the broadcast set")
    concerns.add_argument("--limit", type=int, default=24)
    commands.add_parser("goals", help="The agent-owned goals that are open")
    interest = commands.add_parser("interest", help="Seed an interest for the curiosity drive")
    interest.add_argument("topic")
    interest.add_argument("--why", default="")
    outreach = commands.add_parser("outreach", help="Owner outreach: status, off (a pause until turned on) or on")
    outreach.add_argument("state", nargs="?", choices=("status", "on", "off"), default="status")
    commands.add_parser("consolidate", help="Run the nightly consolidation now: the self-narrative delta, "
                                            "lessons, contradictions, per-contact digests, episode summaries")
    commands.add_parser("narrative", help="The self-narrative as the prompt section renders it")
    opinions = commands.add_parser("opinions", help="The agent's opinions: list, show <id>, withdraw or reconsider <id>")
    opinions.add_argument("action", nargs="?", default="list", choices=OPINION_ACTIONS)
    opinions.add_argument("id", nargs="?", type=int)
    opinions.add_argument("--query", default="", help="list: only the opinions relevant to this text")
    opinions.add_argument("--history", action="store_true", help="list: every revision, not only the current views")
    opinions.add_argument("--reason", default="", help="withdraw/reconsider: why (required)")
    lessons = commands.add_parser("lessons", help="What the mind learned: list, show <id>, retire <id>")
    lessons.add_argument("action", nargs="?", default="list", choices=LESSON_ACTIONS)
    lessons.add_argument("id", nargs="?")
    lessons.add_argument("--all", action="store_true", help="list: also superseded and retired lessons")
    lessons.add_argument("--reason", default="", help="retire: why (required)")


class Sidecar:
    def __init__(self, home: Optional[str]) -> None:
        self.cfg = load_config(home)
        self.key = read_api_key(self.cfg.home) or ""
        self.url = self.cfg.sidecar_url

    def call(self, method: str, path: str, *, json_body: Any = None, params: Dict[str, Any] | None = None,
             timeout: float = 30.0) -> Any:
        import httpx
        headers = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        try:
            with httpx.Client(timeout=timeout, trust_env=False) as client:
                response = client.request(method, self.url + path, headers=headers, json=json_body, params=params)
        except httpx.HTTPError as exc:
            raise SystemExit(f"sidecar unreachable at {self.url} ({type(exc).__name__})") from None
        if response.status_code == 404 and path.startswith("/v1/mind"):
            detail = response.json().get("detail") if response.headers.get("content-type", "").startswith("application/json") else None
            if isinstance(detail, dict):
                raise SystemExit(f"{detail.get('message') or detail.get('code')}")
            raise SystemExit("this sidecar does not serve the mind routes; upgrade and restart it")
        if not response.is_success:
            detail: Any = response.text
            try:
                detail = response.json().get("detail", detail)
            except ValueError:
                pass
            message = detail.get("message") or detail.get("code") if isinstance(detail, dict) else detail
            raise SystemExit(f"sidecar HTTP {response.status_code}: {message}")
        return response.json()


def _emit(value: Any, *, as_json: bool, text: Optional[str] = None) -> None:
    if as_json:
        print(json.dumps(value, indent=2, sort_keys=True))
    elif text is not None:
        print(text)
    else:
        print(json.dumps(value, indent=2, sort_keys=True))


def run(args: argparse.Namespace) -> int:
    command = getattr(args, "mind_command", None) or "status"
    home = getattr(args, "instance", None)
    as_json = bool(getattr(args, "json", False))
    sidecar = Sidecar(home)
    try:
        if command == "status":
            value = sidecar.call("GET", "/v1/mind/state")
            lines = [f"mind: {'on' if value.get('enabled') else 'off'}"
                     + (f" ({value.get('off_reason')})" if value.get("off_reason") else ""),
                     f"autonomy: {value.get('autonomy')}",
                     f"queued tasks: {value.get('queued')}, outbox: {value.get('outbox')}, "
                     f"dispatched: {value.get('dispatched')}, open asks: "
                     f"{len(value['asks']) if isinstance(value.get('asks'), list) else value.get('asks')}",
                     f"ticks: {value.get('ticks')}, last tick: {value.get('last_tick')}, "
                     f"last body pull: {value.get('last_pull')}"
                     + (" (body stale)" if value.get("body_stale") else ""),
                     "breaker: " + ", ".join(f"{b['cls']}={'tripped' if b['tripped'] else 'ok'}"
                                             for b in value.get("breaker", []))]
            if isinstance(value.get("affect"), dict):
                lines.append(_affect_line(value["affect"]))
            _emit(value, as_json=as_json, text="\n".join(lines))
        elif command == "log":
            value = sidecar.call("GET", "/v1/mind/log", params={k: v for k, v in {
                "limit": args.limit, "status": args.status, "kind": args.kind}.items() if v is not None})
            _emit(value["entries"], as_json=as_json, text=render_log(value["entries"]))
        elif command == "why":
            value = sidecar.call("GET", f"/v1/mind/why/{args.id}")
            _emit(value, as_json=as_json, text=value.get("sentence"))
        elif command == "asks":
            value = sidecar.call("GET", "/v1/mind/asks")
            _emit(value["asks"], as_json=as_json, text=value.get("text"))
        elif command in {"yes", "no"}:
            value = sidecar.call("POST", f"/v1/mind/asks/{args.code.upper()}/{command}", json_body={"by": "cli"})
            _emit(value, as_json=as_json, text=value.get("text"))
        elif command == "rate":
            value = sidecar.call("POST", "/v1/mind/rate", json_body={"id": args.id, "verdict": args.verdict})
            _emit(value, as_json=as_json, text=f"{value.get('title')}: verdict {value.get('verdict')}")
        elif command == "level":
            if not args.autonomy:
                value = sidecar.call("GET", "/v1/mind/state")
                _emit({"autonomy": value.get("autonomy")}, as_json=as_json, text=f"autonomy: {value.get('autonomy')}")
            else:
                update_config({"mind": {"autonomy": args.autonomy}}, home)
                try:
                    value = sidecar.call("POST", "/v1/mind/level", json_body={"autonomy": args.autonomy})
                except SystemExit as exc:
                    value = {"autonomy": args.autonomy, "note": f"written to protagine.yaml; sidecar not updated ({exc})"}
                _emit(value, as_json=as_json, text=f"autonomy: {value.get('autonomy')}"
                      + (f" ({value['note']})" if value.get("note") else ""))
        elif command == "reset":
            value = sidecar.call("POST", "/v1/mind/reset", json_body={"cls": args.cls})
            _emit(value, as_json=as_json, text=f"breaker {args.cls}: {'tripped' if value.get('tripped') else 'reset'}")
        elif command == "off":
            try:
                value = sidecar.call("POST", "/v1/mind/off", json_body={"reason": args.reason, "by": "cli"})
                note = f"{value.get('cancelled_messages', 0)} unsent message(s) cancelled"
            except SystemExit as exc:
                marker = _state_dir(sidecar) / OFF_MARKER
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(f"{args.reason} at {now_utc().isoformat()}\n", encoding="utf-8")
                value = {"enabled": False, "reason": args.reason, "note": f"sidecar unreachable ({exc}); marker written"}
                note = value["note"]
            _emit(value, as_json=as_json, text=f"mind off ({note}). Unstarted mind tasks are archived at the next "
                                               "plugin tick; running workers end at their runtime limit.")
        elif command == "on":
            update_config({"mind": {"enabled": True}}, home)
            try:
                value = sidecar.call("POST", "/v1/mind/on", json_body={"by": "cli"})
            except SystemExit as exc:
                (_state_dir(sidecar) / OFF_MARKER).unlink(missing_ok=True)
                value = {"enabled": True, "note": f"sidecar unreachable ({exc}); marker removed"}
            _emit(value, as_json=as_json, text="mind on" + (f" ({value['note']})" if value.get("note") else ""))
        elif command == "tick":
            value = sidecar.call("POST", "/v1/mind/tick", timeout=CONSOLIDATION_WAIT_S + 120)
            formed = ", ".join(f"{item['type']}={item['decision']}" for item in value.get("formed", [])) or "nothing"
            _emit(value, as_json=as_json, text=f"tick {value.get('tick')}: formed {formed}"
                  + (f"; skipped: {value['skipped']}" if value.get("skipped") else ""))
        elif command == "stats":
            value = sidecar.call("GET", "/v1/mind/stats")
            _emit(value, as_json=as_json, text=render_stats({k: v for k, v in value.items() if k != "text"}))
        elif command == "concerns":
            value = sidecar.call("GET", "/v1/mind/concerns", params={"limit": args.limit})
            drives = ", ".join(f"{name}={item['effective']:g} (level {item['level']:g})"
                               for name, item in (value.get("drives") or {}).items())
            _emit(value, as_json=as_json, text=f"drives: {drives}\n{value.get('text')}")
        elif command == "goals":
            value = sidecar.call("GET", "/v1/mind/goals")
            _emit(value, as_json=as_json, text=value.get("text"))
        elif command == "interest":
            value = sidecar.call("POST", "/v1/mind/interests", json_body={"topic": args.topic, "why": args.why, "by": "cli"})
            _emit(value, as_json=as_json, text=f"interest: {value.get('topic')} (weight {value.get('weight')})")
        elif command == "outreach":
            if args.state == "status":
                value = sidecar.call("GET", "/v1/mind/state").get("outreach") or {"enabled": False}
            else:
                value = sidecar.call("POST", "/v1/mind/outreach", json_body={"state": args.state})
            _emit(value, as_json=as_json, text=_outreach_line(value))
        elif command == "consolidate":
            value = sidecar.call("POST", "/v1/mind/consolidate", timeout=960)
            counts = ", ".join(f"{k}={v}" for k, v in sorted((value.get("counts") or {}).items())) or "nothing to consolidate"
            text = (f"consolidation {value.get('local_date')}: {value.get('calls', 0)} call(s), "
                    f"{value.get('tokens', 0)} tokens; {counts}")
            if value.get("skipped"):
                text = f"consolidation {value.get('local_date')}: skipped ({value['skipped']})"
            elif value.get("errors"):
                text += "; errors: " + ", ".join(str(item) for item in value["errors"])
            _emit(value, as_json=as_json, text=text)
        elif command == "narrative":
            value = sidecar.call("GET", "/v1/mind/narrative")
            text = value.get("text") or ("(self-narrative off)" if not value.get("enabled") else "(nothing recorded yet)")
            _emit(value, as_json=as_json, text=text)
        elif command == "opinions":
            return _opinions(sidecar, args, as_json=as_json)
        elif command == "lessons":
            return _lessons(sidecar, args, as_json=as_json)
        else:
            print(f"unknown mind command {command}", file=sys.stderr)
            return 2
    except SystemExit as exc:
        if exc.code and not isinstance(exc.code, int):
            print(exc.code, file=sys.stderr)
            return 1
        raise
    return 0


def _outreach_line(value: Dict[str, Any]) -> str:
    """``outreach: on; 1 of 3 today[; paused until <when>][; muted: <topics>]``, or off."""
    if not value.get("enabled"):
        return "outreach: off (mind.faculties.outreach)"
    parts = [f"outreach: on; {value.get('sent_24h', 0)} of {value.get('per_day', 0)} today"]
    paused = value.get("paused_until")
    if paused:
        parts.append("paused until you turn it on" if paused == "indefinite" else f"paused until {paused}")
    if value.get("muted"):
        parts.append("muted: " + ", ".join(value["muted"]))
    return "; ".join(parts)


def _affect_line(affect: Dict[str, Any]) -> str:
    """``affect: <tone or calm>; load <x>[, overloaded][, satiated]; switch: <topics or none> [<source>]``."""
    if not affect.get("enabled"):
        return "affect: off"
    load = affect.get("load") if isinstance(affect.get("load"), dict) else {}
    flags = "".join([", overloaded" if load.get("overloaded") else "", ", satiated" if affect.get("satiated") else ""])
    switch = ", ".join(str(topic) for topic in affect.get("switch") or []) or "none"
    return (f"affect: {affect.get('line') or 'calm'}; load {float(load.get('level') or 0.0):g}{flags}; "
            f"switch: {switch} [{affect.get('source')}]")


def _opinion_line(row: Dict[str, Any]) -> str:
    about = {"person": f" about {row.get('subject')}", "approach": f" (approach to {row.get('subject')})"}.get(
        row.get("subject_kind") or "topic", "")
    status = "" if row.get("status") in {None, "current"} else f" [{row.get('status')}]"
    return (f"[{row.get('id')}] {row.get('topic') or '(erased)'}{about}{status}: {row.get('stance') or ''}"
            + (f" (audience {row.get('audience')})" if row.get("audience") else ""))


def _opinions(sidecar: Sidecar, args: argparse.Namespace, *, as_json: bool) -> int:
    """``protagine mind opinions``: the owner's reads and controls, ``by=cli``."""
    action = args.action or "list"
    if action != "list" and args.id is None:
        print(f"usage: protagine mind opinions {action} <id>", file=sys.stderr)
        return 2
    if action == "list":
        value = sidecar.call("GET", "/v1/mind/opinions", params={
            "q": args.query, "history": "true" if args.history else "false", "by": "cli", "limit": 50})
        rows = value.get("opinions") or []
        text = ("opinions: off" if not value.get("enabled") else
                "\n".join(_opinion_line(row) for row in rows) or "no opinions recorded")
        _emit(value, as_json=as_json, text=text)
    elif action == "show":
        value = sidecar.call("GET", f"/v1/mind/opinions/{args.id}", params={"by": "cli"})
        row = value.get("opinion") or {}
        premises = [f"  rests on: {p.get('text')} ({p.get('ref')})" for p in row.get("premises") or []]
        decision = row.get("owner_decision") if isinstance(row.get("owner_decision"), dict) else {}
        decided = [f"  you decided: {decision.get('text')} (turn:{decision.get('turn_id')})"] if decision else []
        lines = [_opinion_line(row), f"  because: {row.get('reason') or ''}",
                 f"  would change if: {row.get('revise_if') or '(not stated)'}", *premises, *decided,
                 "  history: " + " <- ".join(str(item.get("id")) for item in value.get("history") or [])]
        _emit(value, as_json=as_json, text="\n".join(lines))
    else:
        if not args.reason.strip():
            print(f"protagine mind opinions {action} needs --reason", file=sys.stderr)
            return 2
        value = sidecar.call("POST", f"/v1/mind/opinions/{args.id}/{action}",
                             json_body={"reason": args.reason, "by": "cli"})
        _emit(value, as_json=as_json, text=f"opinion {args.id}: {value.get('status')} "
                                           f"(revision {value.get('revision_id')})")
    return 0


def _lesson_line(row: Dict[str, Any]) -> str:
    tally = row.get("tally") or {}
    return (f"{row.get('id')} [{row.get('status')}, {row.get('kind')}, {row.get('verified')}] {row.get('title')}: "
            f"when {row.get('when_to_use')} ({tally.get('wins', 0)} wins in {tally.get('uses', 0)} verified uses)")


def _lessons(sidecar: Sidecar, args: argparse.Namespace, *, as_json: bool) -> int:
    """``protagine mind lessons``: the owner's view of what the mind learned, and retirement, ``by=cli``."""
    action = args.action or "list"
    if action != "list" and not args.id:
        print(f"usage: protagine mind lessons {action} <id>", file=sys.stderr)
        return 2
    if action == "retire":
        if not args.reason.strip():
            print("protagine mind lessons retire needs --reason", file=sys.stderr)
            return 2
        value = sidecar.call("POST", f"/v1/mind/lessons/{args.id}/retire",
                             json_body={"reason": args.reason, "by": "cli"})
        _emit(value, as_json=as_json, text=f"lesson {args.id}: {value.get('status')} ({value.get('closed_reason')})")
        return 0
    params = {} if getattr(args, "all", False) or action == "show" else {"status": "active,candidate"}
    value = sidecar.call("GET", "/v1/mind/lessons", params=params)
    rows = value.get("lessons") or []
    if action == "list":
        skills = value.get("skills") if isinstance(value.get("skills"), dict) else {}
        loads = skills.get("loads") or {}
        lines = [_lesson_line(row) for row in rows]
        lines += [f"skill {name}: {int(loads.get(name, 0))} loads" for name in skills.get("owned") or []]
        text = "\n".join(lines) or (
            "no lessons" if value.get("enabled") else "lessons: off (the stored lessons are kept)")
        _emit(value, as_json=as_json, text=text)
        return 0
    row = next((item for item in rows if item.get("id") == args.id), None)
    if row is None:
        print(f"no lesson {args.id}", file=sys.stderr)
        return 1
    lines = [_lesson_line(row), f"  {row.get('content')}", f"  signature: {row.get('signature')}, origin "
             f"{row.get('origin')}", "  evidence: " + ", ".join(str(item) for item in row.get("evidence") or [])]
    if row.get("correction"):
        lines.append(f"  correction: {row['correction']}" + (f" ({row['retrieval_source']})"
                                                             if row.get("retrieval_source") else ""))
    if row.get("supersedes"):
        lines.append(f"  supersedes: {row['supersedes']}")
    if row.get("closed_reason"):
        lines.append(f"  {row.get('status')}: {row['closed_reason']}")
    _emit(row, as_json=as_json, text="\n".join(lines))
    return 0


def _state_dir(sidecar: Sidecar) -> Path:
    from protagine import get_state_dir
    return get_state_dir()


__all__ = ["COMMANDS", "add_parser", "run"]
