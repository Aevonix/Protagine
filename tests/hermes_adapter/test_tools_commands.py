"""``/mind`` and the tools: read-only in chat, mutations only for the owner (architecture 7.10)."""

import json

from conftest import API_KEY, OWNER, probe

TOOL_CODE = '''
from tools.registry import registry
def guest(session="guest-1", sender="2002", message="hi"):
    invoke_hook("pre_llm_call", session_id=session, task_id="t", turn_id="turn", user_message=message,
                conversation_history=[], is_first_turn=True, model="m", platform="telegram",
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
    assert result["level"].startswith("usage:") and result["yes"].startswith("usage:")
    assert "not serve the mind routes" in result["log"]
    assert result["empty"] == result["status"]


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


def test_self_tool_status_and_owner_only_approval(home, sidecar):
    sidecar.mind_routes = True
    result = probe(TOOL_CODE + '''
g = guest(message="yes K7F")
o_without = owner("owner-1", message="please do it")
o_with = owner("owner-2", message="yes K7F, go ahead")
emit(status=call("protagine_self", {"operation": "status"}, o_with),
     guest_yes=call("protagine_self", {"operation": "yes", "code": "K7F"}, g),
     owner_without_code=call("protagine_self", {"operation": "yes", "code": "K7F"}, o_without),
     owner_with_code=call("protagine_self", {"operation": "yes", "code": "K7F"}, o_with),
     log=call("protagine_self", {"operation": "log"}, g))
''', home)
    assert result["status"]["enabled"] is True and result["status"]["sidecar_reachable"] is True
    assert "owner" in result["guest_yes"]["error"]
    assert "code" in result["owner_without_code"]["error"]
    assert result["owner_with_code"] == {"ok": True, "code": "K7F", "answer": "yes"}
    assert result["log"] == {"text": "log entries"}
    approvals = sidecar.calls("/v1/mind/asks/K7F/yes", "POST")
    assert len(approvals) == 1 and approvals[0]["authorization"] == f"Bearer {API_KEY}"


def test_people_tool_mutation_is_owner_only(home, sidecar):
    sidecar.mind_routes = True
    result = probe(TOOL_CODE + '''
g, o = guest(), owner()
emit(listing=call("protagine_people", {"operation": "list"}, g),
     show=call("protagine_people", {"operation": "show", "contact_id": "p-03"}, g),
     guest_set=call("protagine_people", {"operation": "set_permission", "contact_id": "p-03", "permission": "auto"}, g),
     owner_set=call("protagine_people", {"operation": "set_permission", "contact_id": "p-03", "permission": "auto"}, o))
''', home)
    assert {item["contact_id"] for item in result["listing"]} >= {OWNER, "p-02", "p-03"}
    assert result["show"][0]["contact_id"] == "p-03"
    assert "owner" in result["guest_set"]["error"]
    assert result["owner_set"] == {"ok": True, "may_contact": "auto"}


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
