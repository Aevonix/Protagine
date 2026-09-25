"""Every plugin tool call that cannot succeed this turn gets the one final answer (``client.final_answer``).

The memory provider's system note tells the model that a ``retry: false`` answer is final for the turn. A
refusal, an id or code nobody listed, an empty search, a guest's guard block or an unreachable sidecar
answered any other way sent the model to other spellings until Hermes' iteration cap ended the turn. This
walks every failure branch of every plugin tool and of the guard in a contact's session: a branch the model
cannot fix by correcting an argument answers finally; one it can fix names the valid form and stays an
ordinary error. The source check keeps a new failure branch from arriving as an ad-hoc error.
"""

import json
import re

from conftest import ROOT, probe

CODE = '''
from tools.registry import registry
from hermes_cli.plugins import get_plugin_manager as _manager
def session(name, sender, message="hi", platform="telegram"):
    invoke_hook("pre_llm_call", session_id=name, task_id="t", turn_id="turn", user_message=message,
                conversation_history=[], is_first_turn=True, model="m", platform=platform,
                parent_session_id="", sender_id=sender)
    return name
def call(tool, args, name):
    return json.loads(registry.dispatch(tool, args, task_id="t", session_id=name))
def check(tool, args, name):
    from hermes_cli.plugins import get_pre_tool_call_directive
    action, message = get_pre_tool_call_directive(tool, args, session_id=name, task_id="t", tool_call_id="c",
                                                  turn_id="turn", api_request_id="r")
    return {"action": action, "message": message}
'''

# (label, tool, args, session) for the branches only a later turn (or the owner) can change.
FINAL = [
    ("guest log", "protagine_self", {"operation": "log"}, "g"),
    ("guest why", "protagine_self", {"operation": "why", "id": "i-01"}, "g"),
    ("log given content", "protagine_self", {"operation": "log", "content": "note this"}, "o"),
    ("why given a reason", "protagine_self", {"operation": "why", "id": "i-01", "reason": "my stance"}, "o"),
    ("state given text", "protagine_self", {"operation": "state", "text": "record: fine with it"}, "o"),
    ("opinions given a stance", "protagine_self", {"operation": "opinions", "stance": "go with Ash"}, "o"),
    ("unknown operation", "protagine_self", {"operation": "opinion", "id": "x"}, "o"),
    ("no operation", "protagine_self", {"opinion": "Ash"}, "o"),
    ("why of an unknown id", "protagine_self", {"operation": "why", "id": "p-96-venue"}, "o"),
    ("why of an unknown opinion", "protagine_self", {"operation": "why", "id": "7"}, "o"),
    ("guest rate", "protagine_self", {"operation": "rate", "id": "i-01", "verdict": "useful"}, "g"),
    ("guest yes", "protagine_self", {"operation": "yes", "code": "K7F"}, "g"),
    ("guest withdraw", "protagine_self", {"operation": "withdraw", "id": "1", "reason": "x"}, "g"),
    ("yes without a typed code", "protagine_self", {"operation": "yes", "code": "CHECKIN98"}, "o-plain"),
    ("yes with an invented code", "protagine_self", {"operation": "yes", "code": "P08CHK"}, "o-plain"),
    ("no without a code", "protagine_self", {"operation": "no"}, "o-plain"),
    ("guest permission", "protagine_people", {"operation": "set_permission", "contact_id": "p-03",
                                              "permission": "auto"}, "g"),
    ("unknown person", "protagine_people", {"operation": "inspect", "contact_id": "Nobody Here"}, "o"),
    ("search without a participant", "protagine_memory_search", {"query": "x"}, "never-seen"),
    ("search with no hits", "protagine_memory_search", {"query": "who is p-71"}, "g"),
    ("guest forget", "protagine_memory_forget", {"source_ids": ["src-1"]}, "g"),
    ("guest reminder", "protagine_reminder", {"operation": "schedule", "source_id": "s", "source_version": "v",
                                              "claim_id": "c"}, "g"),
    ("unknown reminder job", "protagine_reminder", {"operation": "inspect", "job_id": "nope"}, "o"),
]
# (label, tool, args, session, words the error names) for the branches a corrected argument fixes.
CORRECTABLE = [
    ("since_hours", "protagine_self", {"operation": "log", "since_hours": "soon"}, "o", "number of hours"),
    ("why without an id", "protagine_self", {"operation": "why"}, "o", "id is required"),
    ("rate without a verdict", "protagine_self", {"operation": "rate", "id": "i-01"}, "o", "verdict"),
    ("withdraw without a number", "protagine_self", {"operation": "withdraw", "reason": "x"}, "o", "opinion number"),
    ("withdraw without a reason", "protagine_self", {"operation": "withdraw", "id": "1"}, "o", "reason is required"),
    ("yes with the typed code miscopied", "protagine_self", {"operation": "yes", "code": "K7"}, "o-code", "K7F"),
    ("people operation", "protagine_people", {"operation": "delete"}, "o", "one of"),
    ("people without a name", "protagine_people", {"operation": "inspect"}, "o", "contact_id"),
    ("guest listing", "protagine_people", {"operation": "who"}, "g", "name who you mean"),
    ("link handle", "protagine_people", {"operation": "propose_link", "contact_id": "p-03", "handle": "x"}, "o",
     "gateway:address"),
    ("merge without drop", "protagine_people", {"operation": "merge", "contact_id": "p-03"}, "o", "drop"),
    ("permission value", "protagine_people", {"operation": "set_permission", "contact_id": "p-03",
                                              "permission": "sometimes"}, "o", "never, ask or auto"),
    ("cadence minutes", "protagine_people", {"operation": "set_cadence", "contact_id": "p-03", "minutes": "x"}, "o",
     "whole number"),
    ("search without a query", "protagine_memory_search", {}, "o", "query is required"),
    ("forget without ids", "protagine_memory_forget", {}, "o", "source_ids"),
    ("reminder operation", "protagine_reminder", {"operation": "later"}, "o", "schedule, inspect or cancel"),
    ("reminder without a source", "protagine_reminder", {"operation": "schedule"}, "o", "required"),
]


def _final(answer) -> bool:
    return answer == {"unavailable": True, "retry": False, "reason": answer.get("reason")} and bool(answer["reason"])


def test_every_tool_failure_that_cannot_succeed_this_turn_answers_finally(home, sidecar):
    sidecar.mind_routes = True
    sidecar.mind.intentions["i-01"] = {"id": "i-01", "kind": "task", "status": "asked", "ask_code": "K7F",
                                       "title": "Email the vendor"}
    original = sidecar.dispatch

    def dispatch(method, path, query, body):
        if path == "/v1/host/memory/search" and body.get("person_id") == "p-02":
            return 200, {"content": "", "count": 0, "source_refs": []}   # a first contact: nothing retained
        return original(method, path, query, body)
    sidecar.dispatch = dispatch
    result = probe(CODE + '''
names = {"g": session("guest-1", "2002"), "o": session("owner-1", "1001", "what is on your list?"),
         "o-plain": session("owner-2", "1001", "they are fine with you messaging them directly"),
         "o-code": session("owner-3", "1001", "yes K7F"), "never-seen": "never-seen"}
FINAL, CORRECTABLE = %r, %r
emit(final={label: call(tool, args, names[who]) for label, tool, args, who in FINAL},
     correctable={label: call(tool, args, names[who]) for label, tool, args, who, _ in CORRECTABLE},
     guard={"search": check("session_search", {"query": "x"}, names["g"]),
            "cron": check("cronjob_manage", {"action": "create", "schedule": "in 1h", "prompt": "hi",
                                             "deliver": "telegram:2002"}, names["g"])})
''' % (FINAL, CORRECTABLE), home)
    for label, answer in result["final"].items():
        assert _final(answer), (label, answer)
    for label, _tool, _args, _who, words in CORRECTABLE:
        answer = result["correctable"][label]
        assert "retry" not in answer and words in answer.get("error", ""), (label, answer)
    for label, verdict in result["guard"].items():
        assert verdict["action"] == "block" and verdict["message"].endswith("(retry: false)"), (label, verdict)
        assert "BLOCKED" not in verdict["message"], (label, verdict)
    # The owner's typed code resolved nothing guessed: no decision reached the sidecar.
    assert sidecar.calls("/v1/mind/decide", "POST") == []
    assert "no ask is open" not in json.dumps(result["final"])


def test_no_ask_open_and_an_unreachable_sidecar_answer_finally(home, sidecar):
    sidecar.mind_routes = True
    result = probe(CODE + '''
o = session("owner-1", "1001", "yes K7F")
emit(no_ask=call("protagine_self", {"operation": "yes", "code": "K7F"}, o))
''', home)
    assert _final(result["no_ask"]) and "no ask is open" in result["no_ask"]["reason"]
    home.write_config(**{**home.config, "plugins": {**home.config["plugins"], "protagine": {
        "sidecar_url": "http://127.0.0.1:1", "key_file": str(home.key_file)}}})
    down = probe(CODE + '''
o = session("owner-1", "", "hi", platform="cli")
emit(log=call("protagine_self", {"operation": "log"}, o),
     why=call("protagine_self", {"operation": "why", "id": "i-01"}, o),
     people=call("protagine_people", {"operation": "who"}, o),
     search=call("protagine_memory_search", {"query": "x"}, o),
     forget=call("protagine_memory_forget", {"source_ids": ["src-1"]}, o),
     reminder=call("protagine_reminder", {"operation": "schedule", "source_id": "s", "source_version": "v",
                                          "claim_id": "c"}, o))
''', home)
    for label, answer in down.items():
        assert _final(answer), (label, answer)


def test_the_provider_answers_the_same_way():
    """The memory provider loads on its own and keeps its own copy of the answer; it is the same answer."""
    from protagine_hermes.client import final_answer
    from protagine_memory import provider

    assert provider._terminal("no open commitment") == final_answer("no open commitment")
    unknown = json.loads(provider.ProtagineMemoryProvider({"contact_id": "p-01"}).handle_tool_call(
        "protagine_nope", {}))
    assert _final(unknown), unknown


def test_no_failure_branch_is_an_ad_hoc_error():
    """Each ordinary error left in the tools is an argument the model can correct, and says how; a new branch
    must be classified (and walked above) before it can ship."""
    correctable = {"since_hours is a number of hours", "id is required", "id and verdict (",
                   "id is required (an opinion number from opinions)", "reason is required: the owner's own words",
                   "the owner's message answers ask ", "operation is one of ", "contact_id (a name, handle or id)",
                   "only the owner can list contacts; name who you mean", "handle is gateway:address",
                   "drop (folded into contact_id) is required", "permission is never, ask or auto",
                   "minutes is a whole number", "query is required", "source_ids is required",
                   "use schedule, inspect or cancel", "is required to schedule", "lead_seconds must be",
                   "the reminder time has already passed", "the deadline has no timezone"}
    for module in ("tools", "reminders"):
        source = (ROOT / f"plugins/hermes-plugin/{module}.py").read_text()
        for message in re.findall(r'(?:_error|ValueError)\(f?"([^"]*)"', source):
            assert any(known in message or message in known for known in correctable), (module, message)


def test_the_record_is_read_and_recording_is_automatic():
    """What the model reads about the record says it only reads: the pilots' model called ``log`` (and ``why``)
    with the owner's statement or its own stance to "record" it, read an empty log as a failed write and tried
    again. "Not in the log means it did not happen" is about the agent's own actions, never what a turn said."""
    from protagine_hermes.tools import SELF_SCHEMA

    text = SELF_SCHEMA["description"]
    assert "log (read-only" in text and "an action of yours not in it did not happen" in text
    assert "recorded after it, with no tool call" in text
    assert "not in the log means it did not happen" not in text


def test_no_hits_is_final_only_when_no_part_of_the_search_failed(home, sidecar):
    """Zero hits is the answer when the search ran whole: semantic recall ready, or not part of this install
    (lexical is then the whole search). When semantic recall failed (an embedding service down) only exact
    words were matched, so the answer says so and leaves the model free to search in other words: for the
    owner, a final "nothing retained" would deny a topic the memory holds."""
    statuses = {"lease agreement": "failed", "lease terms": "ready", "lease dates": "unavailable"}
    original = sidecar.dispatch

    def dispatch(method, path, query, body):
        if path == "/v1/host/memory/search":
            return 200, {"content": "", "count": 0, "source_refs": [], "watermark": 0, "annotation_checks": [],
                         "retrieval": {"semantic": statuses[body["query"]], "contact_facts": "ready"}}
        return original(method, path, query, body)
    sidecar.dispatch = dispatch
    result = probe(CODE + '''
o = session("owner-1", "1001", "what did I say about the lease last month?")
emit(**{query: call("protagine_memory_search", {"query": query}, o) for query in %r})
''' % list(statuses), home)
    degraded = result["lease agreement"]
    assert "retry" not in degraded and degraded["count"] == 0 and degraded["content"] == ""
    assert "only exact words" in degraded["note"] and "other words" in degraded["note"]
    assert _final(result["lease terms"]) and _final(result["lease dates"])
