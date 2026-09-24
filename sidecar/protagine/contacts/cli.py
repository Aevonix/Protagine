"""``protagine people``: who people are, and the owner's say over them (architecture 4.7, 7.4, 7.10).

The CLI is the owner (it holds the instance key and sends ``by: cli``), so ``permit``,
``cadence`` and ``merge`` work here and in the owner's own chat session, nowhere else.
``<who>`` is a contact id, a phone number, an email, ``gateway:address`` or a unique name.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any
from urllib.parse import quote

from protagine.mind.cli import Sidecar, _emit

from .models import MAY_CONTACT

COMMANDS = ("who", "inspect", "permit", "cadence", "merge", "link", "proposals")
BASE = "/v1/mind/people"


def add_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("people", help="Contacts: who, inspect, permission, cadence, merge, link proposals")
    parser.add_argument("--json", action="store_true", help="Machine-readable output")
    commands = parser.add_subparsers(dest="people_command")
    who = commands.add_parser("who", help="Find people by name, handle or id (no query: the newest)")
    who.add_argument("query", nargs="?", default="")
    who.add_argument("--limit", type=int, default=20)
    inspect = commands.add_parser("inspect", help="One person: record, digest, handles, proposals, permission history")
    inspect.add_argument("who")
    permit = commands.add_parser("permit", help="Whether the agent may reach out: never, ask or auto")
    permit.add_argument("who")
    permit.add_argument("permission", choices=MAY_CONTACT)
    permit.add_argument("--reason", default="")
    cadence = commands.add_parser("cadence", help="Check-in cadence in minutes, or off")
    cadence.add_argument("who")
    cadence.add_argument("minutes", help="A whole number of minutes, or off")
    merge = commands.add_parser("merge", help="Fold <drop> into <keep>: handles, history and sources follow")
    merge.add_argument("keep")
    merge.add_argument("drop")
    link = commands.add_parser("link", help="Propose that a handle is this person (confirmed as an ask)")
    link.add_argument("who")
    link.add_argument("gateway")
    link.add_argument("address")
    commands.add_parser("proposals", help="Open link proposals")


def _minutes(value: str) -> int | None:
    if value.strip().lower() in {"off", "none"}:
        return None
    try:
        minutes = int(value)
    except ValueError:
        raise SystemExit(f"cadence is a whole number of minutes or off, not {value!r}") from None
    if minutes <= 0:
        raise SystemExit("cadence is a positive number of minutes or off")
    return minutes


def run(args: argparse.Namespace) -> int:
    command = getattr(args, "people_command", None) or "who"
    as_json = bool(getattr(args, "json", False))
    sidecar = Sidecar(getattr(args, "instance", None))
    try:
        value: Any
        if command == "who":
            value = sidecar.call("GET", BASE, params={"q": getattr(args, "query", ""), "limit": getattr(args, "limit", 20)})
            _emit(value["contacts"], as_json=as_json, text=value.get("text"))
        elif command == "inspect":
            value = sidecar.call("GET", f"{BASE}/{quote(args.who, safe='')}")
            _emit(value, as_json=as_json, text=value.get("text"))
        elif command == "permit":
            value = sidecar.call("POST", f"{BASE}/{quote(args.who, safe='')}/permission",
                                 json_body={"may_contact": args.permission, "by": "cli", "reason": args.reason})
            _emit(value, as_json=as_json, text=value.get("text"))
        elif command == "cadence":
            value = sidecar.call("POST", f"{BASE}/{quote(args.who, safe='')}/cadence",
                                 json_body={"minutes": _minutes(args.minutes), "by": "cli"})
            _emit(value, as_json=as_json, text=value.get("text"))
        elif command == "merge":
            value = sidecar.call("POST", f"{BASE}/merge", json_body={"keep": args.keep, "drop": args.drop, "by": "cli"},
                                 timeout=120)
            note = f" ({value['sources_pending']} source(s) left for the source worker)" \
                if value.get("sources_pending") else ""
            _emit(value, as_json=as_json, text=f"{value.get('text')}{note}")
        elif command == "link":
            value = sidecar.call("POST", f"{BASE}/link", json_body={
                "contact_id": args.who, "gateway": args.gateway, "address": args.address, "by": "cli"})
            _emit(value, as_json=as_json, text=value.get("text"))
        elif command == "proposals":
            value = sidecar.call("GET", f"{BASE}/proposals")
            _emit(value["proposals"], as_json=as_json, text=value.get("text"))
        else:
            print(f"unknown people command {command}", file=sys.stderr)
            return 2
    except SystemExit as exc:
        if exc.code and not isinstance(exc.code, int):
            print(exc.code, file=sys.stderr)
            return 1
        raise
    return 0


__all__ = ["COMMANDS", "add_parser", "run"]
