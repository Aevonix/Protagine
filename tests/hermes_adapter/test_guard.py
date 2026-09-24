"""Guard rules through the stock ``pre_tool_call`` dispatch (architecture 7.5; evals 7.2 tests 3, 9, 12, 13).

Every check goes through ``hermes_cli.plugins.get_pre_tool_call_directive``,
the same function Hermes' tool dispatch calls, so the directive shape and the
hook timeout machinery are the stock ones.
"""

from conftest import OWNER, probe, worker_env

GUARD_CODE = '''
from hermes_cli.plugins import get_pre_tool_call_directive
def guest(session="guest-1", sender="2002"):
    invoke_hook("pre_llm_call", session_id=session, task_id="t", turn_id="turn", user_message="hi",
                conversation_history=[], is_first_turn=True, model="m", platform="telegram",
                parent_session_id="", sender_id=sender)
    return session
def owner(session="owner-1"):
    return guest(session, "1001")
def check(tool, args, session=""):
    action, message = get_pre_tool_call_directive(tool, args, session_id=session, task_id="t",
                                                  tool_call_id="c", turn_id="turn", api_request_id="r")
    return {"action": action, "message": message}
'''


def test_guest_session_search_is_blocked_and_owner_is_not(home, sidecar):
    result = probe(GUARD_CODE + '''
g, o = guest(), owner()
emit(guest=check("session_search", {"query": "x"}, g), owner=check("session_search", {"query": "x"}, o),
     guest_read=check("read_file", {"path": "/tmp/x"}, g), unknown=check("session_search", {"query": "x"}, "never-seen"))
''', home)
    assert result["guest"]["action"] == "block" and "session_search" in result["guest"]["message"]
    # The refusal is final for the turn, as the provider's retry: false answers are, and says what to use instead.
    assert "retry: false" in result["guest"]["message"] and "recalled context" in result["guest"]["message"]
    assert result["owner"]["action"] is None
    assert result["guest_read"]["action"] is None
    assert result["unknown"]["action"] is None


def test_guest_delivering_cron_needs_a_permitted_recipient(home, sidecar):
    """Evals test 3, non-owner half: delivery to a never contact is blocked."""
    result = probe(GUARD_CODE + '''
g = guest()
friend = guest("guest-2", "2003")
emit(never=check("cronjob_manage", {"action": "create", "schedule": "in 1h", "prompt": "hi", "deliver": "telegram:2002"}, g),
     own_chat_never=check("cronjob_manage", {"action": "create", "schedule": "in 1h", "prompt": "hi"}, g),
     own_chat_ok=check("cronjob_manage", {"action": "create", "schedule": "in 1h", "prompt": "hi"}, friend),
     to_owner=check("cronjob_manage", {"action": "create", "schedule": "in 1h", "prompt": "hi", "deliver": "telegram:1001"}, g),
     everyone=check("cronjob_manage", {"action": "update", "job_id": "j", "deliver": "all"}, g),
     unknown=check("cronjob_manage", {"action": "create", "schedule": "in 1h", "prompt": "hi", "deliver": "telegram:9999"}, g),
     local=check("cronjob_manage", {"action": "create", "schedule": "in 1h", "prompt": "hi", "deliver": "local"}, g),
     listing=check("cronjob_manage", {"action": "list"}, g),
     messaging=check("discord", {"action": "send", "channel_id": "1"}, g))
''', home)
    assert result["never"]["action"] == "block"
    assert result["own_chat_never"]["action"] == "block"
    assert result["own_chat_ok"]["action"] is None
    assert result["to_owner"]["action"] is None
    assert result["everyone"]["action"] == "block"
    assert result["unknown"]["action"] == "block"
    assert result["local"]["action"] is None
    assert result["listing"]["action"] is None
    assert result["messaging"]["action"] == "block"
    # The sidecar verdict sees who a delivering job reaches, resolved here: it applies may_contact and budgets.
    cron_calls = [c["json"] for c in sidecar.calls("/v1/mind/guard", "POST") if c["json"]["tool"] == "cronjob_manage"]
    assert [c["recipients"] for c in cron_calls] == [["p-03"], [OWNER], [], []]


def test_guard_fails_closed_when_the_sidecar_is_down_but_reads_still_run(home, sidecar):
    """Evals test 9: sidecar stopped."""
    home.write_config(**{**home.config, "plugins": {**home.config["plugins"], "protagine": {
        "sidecar_url": "http://127.0.0.1:1", "key_file": str(home.key_file)}}})
    result = probe(GUARD_CODE + '''
emit(write=check("write_file", {"path": str(%r) + "/a.txt", "content": "x"}),
     read=check("read_file", {"path": "/tmp/x"}),
     search=check("web_search", {"query": "x"}),
     create=check("kanban_create", {"title": "t", "assignee": "protagine-act"}))
''' % str(home.workspace), home, env=worker_env(home))
    assert result["write"]["action"] == "block"
    assert result["create"]["action"] == "block"
    assert result["read"]["action"] is None and result["search"]["action"] is None


def test_guard_fails_closed_when_forced_to_raise(home, sidecar):
    """Evals test 9: guard forced to raise."""
    result = probe(GUARD_CODE + '''
from protagine_hermes import guard as guard_module
def boom(self, *a, **k):
    raise RuntimeError("forced")
guard_module.Guard.decide = boom
emit(write=check("write_file", {"path": "x", "content": "x"}), read=check("read_file", {"path": "x"}))
''', home, env=worker_env(home))
    assert result["write"]["action"] == "block" and "guard error" in result["write"]["message"]
    assert result["read"]["action"] is None


def test_twenty_parallel_tool_calls_produce_no_spurious_blocks(home, sidecar):
    """Evals test 13: parallel tool calls in one turn. ``hook_callback_timeout: 0`` (what init writes) runs
    the guard inline; with a timeout Hermes skips a callback that is still running and fails closed."""
    result = probe(GUARD_CODE + '''
results = [None] * 20
def call(i):
    results[i] = check("write_file", {"path": str(%r) + f"/f{i}.txt", "content": "x"})
threads = [threading.Thread(target=call, args=(i,)) for i in range(20)]
for t in threads: t.start()
for t in threads: t.join()
emit(blocked=[r for r in results if r["action"] is not None])
''' % str(home.workspace), home, env=worker_env(home))
    assert result["blocked"] == []


def test_sidecar_verdict_is_honoured_when_mind_routes_exist(home, sidecar):
    """``POST /v1/mind/guard {tool, args, session}`` answers ``{allow, reason}``; 404 allows, silence blocks."""
    sidecar.mind_routes = True
    result = probe(GUARD_CODE + '''
emit(allow=check("write_file", {"path": str(%r) + "/a.txt", "content": "x"}, "worker-session"))
''' % str(home.workspace), home, env=worker_env(home))
    assert result["allow"]["action"] is None
    call, = sidecar.calls("/v1/mind/guard", "POST")
    assert call["json"]["run"] == "mind" and call["json"]["tool"] == "write_file"
    assert call["json"]["session"] == "worker-session" and call["json"]["task_id"] == "task-01"
    assert call["json"]["args"]["content"] == "x"
    sidecar.guard_verdict = {"allow": False, "reason": "not now"}
    result = probe(GUARD_CODE + '''
emit(block=check("write_file", {"path": str(%r) + "/a.txt", "content": "x"}),
     read=check("read_file", {"path": "x"}))
''' % str(home.workspace), home, env=worker_env(home))
    assert result["block"]["action"] == "block" and "not now" in result["block"]["message"]
    assert result["read"]["action"] is None
    sidecar.guard_verdict = {"allow": False, "reason": "owner?", "ask": True}
    result = probe(GUARD_CODE + '''
emit(ask=check("write_file", {"path": str(%r) + "/a.txt", "content": "x"}))
''' % str(home.workspace), home, env=worker_env(home))
    assert result["ask"]["action"] == "approve" and "owner?" in result["ask"]["message"]
    sidecar.guard_verdict = {"allow": True, "reason": ""}
    g = probe(GUARD_CODE + '''
g = guest()
emit(send=check("send_message", {"target": "telegram:2003", "message": "hi"}, g))
''', home)
    assert g["send"]["action"] is None
    guest_call = sidecar.calls("/v1/mind/guard", "POST")[-1]["json"]
    assert guest_call["run"] == "guest" and guest_call["owner"] is False and guest_call["contact_id"] == "p-02"
    assert guest_call["platform"] == "telegram" and guest_call["sender_id"] == "2002"


def test_cron_delivery_checks_cover_the_action_spelling_and_every_recipient(home, sidecar):
    """Stock ``cronjob()`` strips and lowercases the action, delivers failures to ``failure_deliver``
    and keeps a stored job's recipients when an update does not resend them."""
    result = probe(GUARD_CODE + '''
g = guest()
from datetime import datetime, timedelta, timezone
from cron.scheduler import create_job_with_scheduler_registration
later = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
never = create_job_with_scheduler_registration(prompt="hi", schedule=later, name="to never", deliver="telegram:2002", repeat=1)
friend = create_job_with_scheduler_registration(prompt="hi", schedule=later, name="to friend", deliver="telegram:2003", repeat=1)
failing = create_job_with_scheduler_registration(prompt="hi", schedule=later, name="fails to never", deliver="local",
                                                 failure_deliver="telegram:2002", repeat=1)
create = {"schedule": "in 1h", "prompt": "hi"}
emit(spaced=check("cronjob_manage", {"action": " CREATE ", **create, "deliver": "telegram:2002"}, g),
     failure=check("cronjob_manage", {"action": "create", **create, "deliver": "local", "failure_deliver": "telegram:2002"}, g),
     failure_list=check("cronjob_manage", {"action": "create", **create, "deliver": "local", "failure_deliver": ["telegram:2002"]}, g),
     failure_ok=check("cronjob_manage", {"action": "create", **create, "deliver": "local", "failure_deliver": "telegram:2003"}, g),
     stored=check("cronjob_manage", {"action": "update", "job_id": never["id"], "prompt": "new"}, g),
     stored_by_name=check("cronjob_manage", {"action": "update", "job_id": "to never", "prompt": "new"}, g),
     stored_failure=check("cronjob_manage", {"action": "update", "job_id": failing["id"], "deliver": "local"}, g),
     stored_ok=check("cronjob_manage", {"action": "update", "job_id": friend["id"], "prompt": "new"}, g),
     resent=check("cronjob_manage", {"action": "update", "job_id": never["id"], "deliver": "local", "failure_deliver": ""}, g),
     missing=check("cronjob_manage", {"action": "update", "job_id": "nope", "prompt": "new"}, g))
''', home)
    for name in ("spaced", "failure", "failure_list", "stored", "stored_by_name", "stored_failure"):
        assert result[name]["action"] == "block", name
    for name in ("failure_ok", "stored_ok", "resent", "missing"):
        assert result[name]["action"] is None, name


def test_a_mind_run_cannot_certify_its_own_commitment(home, sidecar):
    """The dispatched worker may dismiss or snooze a commitment with a reason, never mark it fulfilled:
    the body's outcome report closes the row. The owner's own session keeps the tool."""
    call = 'check("protagine_resolve_commitment", {"commitment_id": "c-01", "action": %r, "reason": "x", "new_due_at": "2030-01-01T00:00:00+00:00"}%s)'
    result = probe(GUARD_CODE + '''
emit(fulfilled=%s, spaced=%s, dismissed=%s, snoozed=%s)
''' % (call % ("fulfilled", ""), call % (" Fulfilled ", ""), call % ("dismissed", ""), call % ("snoozed", "")),
                   home, env=worker_env(home))
    assert result["fulfilled"]["action"] == "block" and "own commitment" in result["fulfilled"]["message"]
    assert result["spaced"]["action"] == "block"
    assert result["dismissed"]["action"] is None and result["snoozed"]["action"] is None
    owner = probe(GUARD_CODE + '''
o = owner()
emit(fulfilled=%s)
''' % (call % ("fulfilled", ", o")), home)
    assert owner["fulfilled"]["action"] is None


def test_effects_block_when_the_sidecar_goes_away_after_a_warm_cache(home, sidecar):
    """Evals test 9 with a warm cache: the first effect learnt that this sidecar has no mind routes;
    the sidecar then disappears and the next effect must still be refused, reads still run."""
    result = probe(GUARD_CODE + '''
first = check("write_file", {"path": str(%r) + "/a.txt", "content": "x"})
protagine_hermes._BODY.client.url = "http://127.0.0.1:1"
second = check("write_file", {"path": str(%r) + "/b.txt", "content": "x"})
emit(first=first, second=second, read=check("read_file", {"path": "/tmp/x"}))
''' % (str(home.workspace), str(home.workspace)), home, env=worker_env(home))
    assert result["first"]["action"] is None
    assert result["second"]["action"] == "block"
    assert result["read"]["action"] is None
