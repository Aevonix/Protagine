"""``/mind`` and the tools: read-only in chat, mutations only for the owner (architecture 7.7, 7.10; evals test 7)."""

import json

from conftest import API_KEY, CANARY, OWNER, probe, worker_env

TOOL_CODE = '''
from tools.registry import registry
def guest(session="guest-1", sender="2002", message="hi", platform="telegram"):
    invoke_hook("pre_llm_call", session_id=session, task_id="t", turn_id="turn", user_message=message,
                conversation_history=[], is_first_turn=True, model="m", platform=platform,
                parent_session_id="", sender_id=sender)
    return session
def owner(session="owner-1", message="hi"):
    return guest(session, "1001", message)
def call(tool, args, session):
    return json.loads(registry.dispatch(tool, args, task_id="t", session_id=session))
def command(args=""):
    from hermes_cli.plugins import resolve_plugin_command_result
    return resolve_plugin_command_result(manager._plugin_commands["mind"]["handler"](args))
'''


def test_mind_command_is_read_only_plus_off(home, sidecar):
    result = probe(TOOL_CODE + '''
emit(status=command("status"), level=command("level trusted"), yes=command("yes K7F"),
     log=command("log"), empty=command(""))
''', home)
    assert "sidecar:" in result["status"] and "reachable" in result["status"]
    assert "mind: on" in result["status"] and "autonomy standard" in result["status"]
    assert "body:" in result["status"]
    assert result["level"].startswith("usage:") and result["yes"].startswith("usage:")
    assert "not serve the mind routes" in result["log"]
    assert result["empty"] == result["status"]


def test_mind_command_reads_the_mind_routes(home, sidecar):
    sidecar.mind_routes = True
    sidecar.mind.intentions["i-01"] = {"id": "i-01", "kind": "task", "status": "asked", "ask_code": "K7F",
                                       "title": "Email the vendor", "drive": "duty", "decision": "ask"}
    sidecar.mind.intentions["i-02"] = {"id": "i-02", "kind": "message", "status": "done", "title": "Nudge"}
    result = probe(TOOL_CODE + '''
emit(status=command("status"), log=command("log"), asks=command("asks"), why=command("why i-01"),
     unknown=command("why nope"), bare_why=command("why"))
''', home)
    assert "enabled: True" in result["status"] and "asks: K7F" in result["status"]
    assert "id=i-01" in result["log"] and "id=i-02" in result["log"] and "status=asked" in result["log"]
    assert "code=K7F" in result["asks"] and "Email the vendor" in result["asks"]
    assert "drive: duty" in result["why"] and "decision: ask" in result["why"]
    assert result["unknown"] == "Not found." and result["bare_why"].startswith("usage:")


def test_mind_off_archives_unstarted_mind_tasks(home, sidecar):
    """Evals test 1, the part that needs no running worker: the ready mind task is archived."""
    result = probe(TOOL_CODE + '''
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
conn = connect()
mind_task = kb.create_task(conn, title="mind task", assignee="protagine-act", idempotency_key="mind:abc")
owner_task = kb.create_task(conn, title="owner task", assignee="default", idempotency_key="owner:1")
conn.close()
text = command("off")
conn = connect()
states = {t.idempotency_key: t.status for t in kb.list_tasks(conn, include_archived=True)}
conn.close()
emit(text=text, states=states)
''', home)
    assert result["text"].startswith("Mind off.")
    assert "archived 1 unstarted mind task" in result["text"]
    assert result["states"]["mind:abc"] == "archived"
    assert result["states"]["owner:1"] != "archived"


def test_mind_off_in_chat_reaches_the_sidecar_without_a_model(home, sidecar):
    sidecar.mind_routes = True
    sidecar.mind.outbox["m-01"] = {"id": "m-01", "kind": "notice", "text": "late", "target": "telegram:1001"}
    result = probe(TOOL_CODE + 'emit(text=command("off"))', home)
    assert "sidecar: off" in result["text"]
    assert sidecar.mind.enabled is False and sidecar.mind.outbox["m-01"]["state"] == "cancelled"
    off, = sidecar.calls("/v1/mind/off", "POST")
    assert off["authorization"] == f"Bearer {API_KEY}"


def test_self_tool_state_log_and_why(home, sidecar):
    sidecar.mind_routes = True
    sidecar.mind.intentions["i-01"] = {"id": "i-01", "kind": "task", "status": "dispatched", "drive": "duty",
                                       "title": "Check", "hermes_ref": "t_1"}
    result = probe(TOOL_CODE + '''
g, o = guest(), owner()
emit(state=call("protagine_self", {"operation": "state"}, g),
     status_alias=call("protagine_self", {"operation": "status"}, o),
     log=call("protagine_self", {"operation": "log", "limit": 5}, g),
     why=call("protagine_self", {"operation": "why", "id": "i-01"}, g),
     missing=call("protagine_self", {"operation": "why"}, g),
     unknown=call("protagine_self", {"operation": "why", "id": "nope"}, g))
''', home)
    assert result["state"]["enabled"] is True and result["state"]["sidecar_reachable"] is True
    assert result["state"]["mind_routes"] is True and result["state"]["autonomy"] == "standard"
    assert result["status_alias"]["enabled"] is True
    assert result["log"]["entries"][0]["id"] == "i-01"
    assert result["why"]["drive"] == "duty" and result["why"]["hermes_ref"] == "t_1"
    assert "id is required" in result["missing"]["error"]
    assert "not found" in result["unknown"]["error"]
    logs = sidecar.calls("/v1/mind/log", "GET")
    assert logs and logs[0]["query"] == {"limit": "5"}


def test_self_tool_approval_needs_the_owner_and_the_typed_code(home, sidecar):
    """Evals test 7: a guest, an owner turn without the code, a worker and a malformed code are refused."""
    sidecar.mind_routes = True
    sidecar.mind.intentions["i-01"] = {"id": "i-01", "kind": "task", "status": "asked", "ask_code": "K7F",
                                       "title": "Email the vendor"}
    sidecar.mind.intentions["i-02"] = {"id": "i-02", "kind": "task", "status": "asked", "ask_code": "Q2ZX",
                                       "title": "Clear the backups"}
    result = probe(TOOL_CODE + '''
g = guest(message="yes K7F")
o_without = owner("owner-1", message="please do it")
o_embedded = owner("owner-2", message="see https://example.test/K7F9 first")
o_lower = owner("owner-3", message="yes k7f, go ahead")
o_no = owner("owner-4", message="no Q2ZX")
cli = guest("cli-1", sender="", message="yes K7F", platform="cli")
emit(guest_yes=call("protagine_self", {"operation": "yes", "code": "K7F"}, g),
     owner_without_code=call("protagine_self", {"operation": "yes", "code": "K7F"}, o_without),
     owner_embedded=call("protagine_self", {"operation": "yes", "code": "K7F"}, o_embedded),
     malformed=call("protagine_self", {"operation": "yes", "code": "yes K7F"}, o_lower),
     empty=call("protagine_self", {"operation": "yes"}, o_lower),
     unknown_session=call("protagine_self", {"operation": "yes", "code": "K7F"}, "never-seen"),
     owner_with_code=call("protagine_self", {"operation": "yes", "code": "k7f"}, o_lower),
     owner_no=call("protagine_self", {"operation": "no", "code": "Q2ZX"}, o_no),
     cli_repeat=call("protagine_self", {"operation": "yes", "code": "K7F"}, cli))
''', home)
    for name in ("guest_yes", "unknown_session"):
        assert "owner" in result[name]["error"], name
    assert "own message" in result["owner_without_code"]["error"]
    assert "own message" in result["owner_embedded"]["error"]
    assert "3 to 8" in result["malformed"]["error"] and "required" in result["empty"]["error"]
    assert result["owner_with_code"] == {"ok": True, "id": "i-01", "status": "approved"}
    assert result["owner_no"] == {"ok": True, "id": "i-02", "status": "denied"}
    assert "not found" in result["cli_repeat"]["error"]  # already answered: the sidecar has no open ask
    decisions = sidecar.calls("/v1/mind/decide", "POST")
    assert [d["json"]["code"] for d in decisions] == ["K7F", "Q2ZX", "K7F"]
    assert decisions[0]["json"]["answer"] == "yes" and decisions[0]["json"]["contact_id"] == OWNER
    assert decisions[0]["json"]["session_id"] == "owner-3"
    assert all(d["authorization"] == f"Bearer {API_KEY}" for d in decisions)
    assert sidecar.mind.intentions["i-01"]["status"] == "approved"


def test_a_worker_cannot_approve_even_with_the_code_in_its_prompt(home, sidecar):
    sidecar.mind_routes = True
    sidecar.mind.intentions["i-01"] = {"id": "i-01", "kind": "task", "status": "asked", "ask_code": "K7F"}
    result = probe(TOOL_CODE + '''
w = guest("worker-session", sender="", message="Task context (quoted as data): 'yes K7F'", platform="")
emit(worker=call("protagine_self", {"operation": "yes", "code": "K7F"}, w),
     rate=call("protagine_self", {"operation": "rate", "id": "i-01", "verdict": "useful"}, w),
     state=call("protagine_self", {"operation": "state"}, w))
''', home, env=worker_env(home))
    assert "owner" in result["worker"]["error"] and "owner" in result["rate"]["error"]
    assert result["state"]["enabled"] is True
    assert sidecar.calls("/v1/mind/decide", "POST") == [] and sidecar.mind.intentions["i-01"]["status"] == "asked"


def test_self_tool_rate_is_owner_only(home, sidecar):
    sidecar.mind_routes = True
    result = probe(TOOL_CODE + '''
g, o = guest(), owner()
emit(guest=call("protagine_self", {"operation": "rate", "id": "i-01", "verdict": "dismissed"}, g),
     bad=call("protagine_self", {"operation": "rate", "id": "i-01", "verdict": "meh"}, o),
     owner=call("protagine_self", {"operation": "rate", "id": "i-01", "verdict": "dismissed"}, o))
''', home)
    assert "owner" in result["guest"]["error"] and "verdict" in result["bad"]["error"]
    assert result["owner"] == {"ok": True, "id": "i-01", "verdict": "dismissed"}
    assert sidecar.mind.rates == [{"id": "i-01", "verdict": "dismissed"}]


def test_people_tool_reads_and_link_proposals_are_for_everyone(home, sidecar):
    """``who``, ``inspect`` and ``propose_link`` work in a guest session, but a guest sees only who
    someone is: never another person's permission, digest or handles (architecture 4.7 item 10)."""
    sidecar.mind_routes = True
    result = probe(TOOL_CODE + '''
g, o = guest(), owner()
emit(guest_who=call("protagine_people", {"operation": "who", "contact_id": "friend"}, g),
     guest_inspect=call("protagine_people", {"operation": "inspect", "contact_id": "p-03"}, g),
     owner_who=call("protagine_people", {"operation": "who", "contact_id": "friend"}, o),
     owner_inspect=call("protagine_people", {"operation": "inspect", "contact_id": "Friend"}, o),
     unknown=call("protagine_people", {"operation": "inspect", "contact_id": "nobody"}, o),
     link=call("protagine_people", {"operation": "propose_link", "contact_id": "p-03",
                                    "handle": "email:friend@example.test"}, g),
     bare_link=call("protagine_people", {"operation": "propose_link", "contact_id": "p-03"}, g),
     bad=call("protagine_people", {"operation": "list"}, o))
''', home)
    assert result["guest_who"] == [{"contact_id": "p-03", "display_name": "Friend", "trust_tier": "REGULAR"}]
    assert result["guest_inspect"] == {"contact_id": "p-03", "display_name": "Friend", "trust_tier": "REGULAR"}
    assert CANARY not in json.dumps([result["guest_who"], result["guest_inspect"], result["link"]])
    assert result["owner_who"][0]["may_contact"] == "ask" and "interaction_allowed" not in result["owner_who"][0]
    assert result["owner_inspect"]["digest"].startswith("Friend: known since spring")
    assert set(result["owner_inspect"]) == {"contact_id", "display_name", "trust_tier", "may_contact",
                                            "cadence_minutes", "digest"}
    assert "no single contact" in result["unknown"]["error"]
    assert result["link"]["status"] == "pending" and result["link"]["candidate_id"]
    assert "gateway:address" in result["bare_link"]["error"] and "one of" in result["bad"]["error"]
    reads = [c for c in sidecar.requests if c["path"].startswith("/v1/mind/people") and c["method"] == "GET"]
    guest_reads = [c for c in reads if c["query"].get("contact_id") == "p-02"]
    assert len(guest_reads) == 2  # the guest's reads name the guest as the viewer; the owner's name nobody
    assert all("contact_id" not in c["query"] for c in reads if c not in guest_reads)
    link, = sidecar.calls("/v1/mind/people/link", "POST")
    assert link["json"]["by"] == "p-02" and link["json"]["address"] == "friend@example.test"


def test_people_tool_mutations_are_owner_only(home, sidecar):
    """set_permission, set_cadence and merge: refused in a guest session without a sidecar call;
    the owner's go out with the owner as ``contact_id`` so the sidecar can check again."""
    sidecar.mind_routes = True
    result = probe(TOOL_CODE + '''
g, o = guest(), owner()
ops = {"permission": {"operation": "set_permission", "contact_id": "p-03", "permission": "auto"},
       "cadence": {"operation": "set_cadence", "contact_id": "p-03", "minutes": 90},
       "merge": {"operation": "merge", "contact_id": "p-03", "drop": "p-02"}}
emit(guest={name: call("protagine_people", args, g) for name, args in ops.items()},
     owner={name: call("protagine_people", args, o) for name, args in ops.items()},
     clear=call("protagine_people", {"operation": "set_cadence", "contact_id": "p-03", "minutes": 0}, o),
     bad=call("protagine_people", {"operation": "set_permission", "contact_id": "p-03", "permission": "maybe"}, o))
''', home)
    for name, reply in result["guest"].items():
        assert "only the owner" in reply["error"], name
    assert result["owner"]["permission"] == {"ok": True, "may_contact": "auto"}
    assert result["owner"]["cadence"] == {"ok": True, "cadence_minutes": 90}
    assert result["owner"]["merge"]["ok"] is True and result["owner"]["merge"]["dropped"] == "p-02"
    assert result["clear"] == {"ok": True, "cadence_minutes": None}
    assert "never, ask or auto" in result["bad"]["error"]
    posts = [c for c in sidecar.requests if c["path"].startswith("/v1/mind/people") and c["method"] == "POST"]
    assert [c["path"] for c in posts] == ["/v1/mind/people/p-03/permission", "/v1/mind/people/p-03/cadence",
                                          "/v1/mind/people/merge", "/v1/mind/people/p-03/cadence"]
    assert all(c["json"]["contact_id"] == OWNER for c in posts)
    assert posts[1]["json"]["minutes"] == 90 and posts[3]["json"]["minutes"] is None


def test_the_sidecar_checks_the_owner_again(home, sidecar):
    """An owner session whose sender the sidecar does not know as the owner is refused there too."""
    sidecar.mind_routes = True
    sidecar.people_owner = "p-99"
    result = probe(TOOL_CODE + '''
o = owner()
emit(permission=call("protagine_people", {"operation": "set_permission", "contact_id": "p-03", "permission": "auto"}, o))
''', home)
    assert "only the owner" in result["permission"]["error"]
    assert sidecar.contacts[("telegram", "2003")]["may_contact"] == "ask"


def test_people_mutations_are_refused_in_a_worker(home, sidecar):
    sidecar.mind_routes = True
    result = probe(TOOL_CODE + '''
o = owner()
emit(permission=call("protagine_people", {"operation": "set_permission", "contact_id": "p-03", "permission": "auto"}, o),
     who=call("protagine_people", {"operation": "who", "contact_id": "friend"}, o))
''', home, env=worker_env(home))
    assert "only the owner" in result["permission"]["error"]
    assert result["who"] == [{"contact_id": "p-03", "display_name": "Friend", "trust_tier": "REGULAR"}]
    assert not [c for c in sidecar.requests if c["method"] == "POST" and c["path"].startswith("/v1/mind/people")]


def test_memory_tools_bind_to_the_sessions_contact(home, sidecar):
    result = probe(TOOL_CODE + '''
g, o = guest(), owner()
emit(guest_search=call("protagine_memory_search", {"query": "plans"}, g),
     owner_search=call("protagine_memory_search", {"query": "plans"}, o),
     guest_forget=call("protagine_memory_forget", {"source_ids": ["src-1"]}, g),
     owner_forget=call("protagine_memory_forget", {"source_ids": ["src-1", "src-1"]}, o))
''', home)
    assert result["guest_search"]["content"] == "excerpt for p-02"
    assert result["owner_search"]["content"] == f"excerpt for {OWNER}"
    assert "owner" in result["guest_forget"]["error"]
    assert result["owner_forget"]["source_erased"] is True
    forget, = sidecar.calls("/v1/host/memory/sources/forget", "POST")
    assert forget["json"] == {"contact_id": OWNER, "source_ids": ["src-1"]}
    searches = sidecar.calls("/v1/host/memory/search", "POST")
    assert {c["json"]["person_id"] for c in searches} == {"p-02", OWNER}
    assert all(c["json"]["session_id"] in {"guest-1", "owner-1"} for c in searches)


def test_memory_search_without_a_participant_answers_once(home, sidecar):
    """A channel turn with no sender binding cannot be searched: one terminal answer, no sidecar call,
    the same shape the memory provider's lane refusals use."""
    result = probe(TOOL_CODE + '''
u = guest("unbound-1", sender="", platform="telegram")
emit(search=call("protagine_memory_search", {"query": "plans"}, u))
''', home)
    assert result["search"] == {"unavailable": True, "retry": False, "reason": result["search"]["reason"]}
    assert "no resolved participant" in result["search"]["reason"]
    assert sidecar.calls("/v1/host/memory/search", "POST") == []


def test_prompt_section_renders_within_bounds(home, sidecar):
    result = probe('''
from hermes_cli.plugins import render_system_prompt_sections
sections = render_system_prompt_sections({"session_id": "s", "platform": "cli"})
emit(sections=[(s.id, s.content) for s in sections if s.id == "protagine"])
''', home)
    (section_id, content), = result["sections"]
    assert section_id == "protagine"
    assert "Your owner is Owner." in content and len(content) <= 4000
    assert json.dumps(content)  # plain text


def test_a_cron_run_cannot_answer_an_ask_or_mutate(home, sidecar):
    """Stock cron runs its agents with ``platform="cron"`` and no sender: a stored prompt is not the
    owner typing, so yes/no, rate, set_permission and forget are refused there while reads still work."""
    sidecar.mind_routes = True
    sidecar.mind.intentions["i-01"] = {"id": "i-01", "kind": "task", "status": "asked", "ask_code": "K7F"}
    result = probe(TOOL_CODE + '''
c = guest("cron-1", sender="", message="yes K7F", platform="cron")
emit(yes=call("protagine_self", {"operation": "yes", "code": "K7F"}, c),
     rate=call("protagine_self", {"operation": "rate", "id": "i-01", "verdict": "useful"}, c),
     permission=call("protagine_people", {"operation": "set_permission", "contact_id": "p-03", "permission": "auto"}, c),
     cadence=call("protagine_people", {"operation": "set_cadence", "contact_id": "p-03", "minutes": 60}, c),
     merge=call("protagine_people", {"operation": "merge", "contact_id": "p-03", "drop": "p-02"}, c),
     forget=call("protagine_memory_forget", {"source_ids": ["src-1"]}, c),
     state=call("protagine_self", {"operation": "state"}, c),
     search=call("protagine_memory_search", {"query": "report"}, c))
''', home)
    for name in ("yes", "rate", "permission", "cadence", "merge", "forget"):
        assert "owner" in result[name]["error"], name
    assert result["state"]["enabled"] is True and result["search"]["count"] == 1
    assert sidecar.calls("/v1/mind/decide", "POST") == [] and sidecar.mind.intentions["i-01"]["status"] == "asked"
