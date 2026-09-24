"""``protagine mind``: status, the audit log, asks, verdicts, the level and the off switch.

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
from .tick import OFF_MARKER

COMMANDS = ("status", "log", "why", "asks", "yes", "no", "rate", "level", "reset", "off", "on", "tick", "stats",
            "concerns", "goals", "interest", "consolidate", "narrative")


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
    commands.add_parser("consolidate", help="Run the nightly consolidation now: the self-narrative delta, "
                                            "contradictions, dedupe, per-contact digests, episode summaries")
    commands.add_parser("narrative", help="The self-narrative as the prompt section renders it")


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
                marker.write_text(f"{args.reason} at {datetime.now(timezone.utc).isoformat()}\n", encoding="utf-8")
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
            value = sidecar.call("POST", "/v1/mind/tick", timeout=120)
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
        else:
            print(f"unknown mind command {command}", file=sys.stderr)
            return 2
    except SystemExit as exc:
        if exc.code and not isinstance(exc.code, int):
            print(exc.code, file=sys.stderr)
            return 1
        raise
    return 0


def _state_dir(sidecar: Sidecar) -> Path:
    from protagine import get_state_dir
    return get_state_dir()


__all__ = ["COMMANDS", "add_parser", "run"]
