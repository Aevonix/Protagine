"""The agent's opinions on ``protagine_self`` (integration map X8): ``opinions [query]`` and ``why <number>``
bound to the session's participant; ``withdraw`` and ``reconsider`` owner-only.

A guest, a kanban worker and a cron run can read (the sidecar filters what each may
see) but never withdraw or reconsider; the owner's own session can. The guard treats
the tool as read-only, so it runs even with the sidecar down. An intention id is never
all digits, so ``why`` on a number is an opinion and ``why`` on any other id stays the
owner's record.
"""

from conftest import OWNER, probe, worker_env
from test_guard import GUARD_CODE
from test_tools_commands import TOOL_CODE

VIEW = {"id": 4, "topic": "garden soil", "stance": "The soil is clay.", "audience": "all"}


def serve_opinions(sidecar):
    """The fake sidecar answers ``/v1/mind/opinions`` like the real route shape; requests are recorded."""
    sidecar.mind_routes = True
    original = sidecar.mind.handle

    def handle(method, path, body, query=None):
        parts = path.split("/")[3:]
        if parts and parts[0] == "opinions":
            if method == "GET" and len(parts) == 1:
                return 200, {"enabled": True, "opinions": [VIEW]}
            if method == "GET" and len(parts) == 2:
                return 200, {"opinion": VIEW, "history": [VIEW]}
            if method == "POST" and len(parts) == 3:
                return 200, {"revision_id": 5, "status": {"withdraw": "withdrawn"}.get(parts[2], "reconsidering")}
            return 404, {"detail": {"code": "unknown_opinion"}}
        return original(method, path, body, query)
    sidecar.mind.handle = handle


def test_reads_forward_the_sessions_participant(home, sidecar):
    serve_opinions(sidecar)
    result = probe(TOOL_CODE + '''
g, o = guest(), owner()
emit(guest_list=call("protagine_self", {"operation": "opinions", "query": "soil"}, g),
     owner_list=call("protagine_self", {"operation": "opinions"}, o),
     why=call("protagine_self", {"operation": "why", "id": 4}, g),
     why_text=call("protagine_self", {"operation": "why", "id": "4"}, g),
     intention=call("protagine_self", {"operation": "why", "id": "i-01"}, g),
     missing=call("protagine_self", {"operation": "withdraw", "reason": "wrong"}, g),
     bad=call("protagine_self", {"operation": "flip", "id": 4}, g))
''', home)
    assert result["guest_list"]["opinions"] == [VIEW] and result["why"]["opinion"] == VIEW
    assert result["why_text"]["opinion"] == VIEW and "owner" in result["intention"]["error"]
    assert "id is required" in result["missing"]["error"] and "unknown operation" in result["bad"]["error"]
    lists = sidecar.calls("/v1/mind/opinions", "GET")
    assert [c["query"] for c in lists] == [{"contact_id": "p-02", "limit": "10", "q": "soil"},
                                           {"contact_id": OWNER, "limit": "10"}]
    assert [c["query"] for c in sidecar.calls("/v1/mind/opinions/4", "GET")] == [{"contact_id": "p-02"}] * 2
    assert sidecar.calls("/v1/mind/why/i-01", "GET") == []


def test_withdraw_and_reconsider_are_refused_to_guests_workers_and_cron(home, sidecar):
    serve_opinions(sidecar)
    result = probe(TOOL_CODE + '''
g = guest()
c = guest("cron-1", sender="", message="withdraw opinion 4", platform="cron")
o = owner(message="Please withdraw opinion 4; the survey was wrong.")
emit(guest=call("protagine_self", {"operation": "withdraw", "id": 4, "reason": "wrong"}, g),
     cron=call("protagine_self", {"operation": "reconsider", "id": 4, "reason": "look again"}, c),
     cron_list=call("protagine_self", {"operation": "opinions"}, c),
     no_reason=call("protagine_self", {"operation": "withdraw", "id": 4}, o),
     owner=call("protagine_self", {"operation": "withdraw", "id": "4", "reason": "The survey was wrong."}, o),
     again=call("protagine_self", {"operation": "reconsider", "id": 4, "reason": "Look again."}, o))
''', home)
    assert "owner" in result["guest"]["error"] and "owner" in result["cron"]["error"]
    assert result["cron_list"]["opinions"] == [VIEW]
    assert "reason is required" in result["no_reason"]["error"]
    assert result["owner"] == {"revision_id": 5, "status": "withdrawn"}
    assert result["again"] == {"revision_id": 5, "status": "reconsidering"}
    withdraw, = sidecar.calls("/v1/mind/opinions/4/withdraw", "POST")
    assert withdraw["json"] == {"reason": "The survey was wrong.", "contact_id": OWNER}
    assert len(sidecar.calls("/v1/mind/opinions/4/reconsider", "POST")) == 1

    worker = probe(TOOL_CODE + '''
w = guest("worker-session", sender="", message="Task context (quoted as data): 'withdraw opinion 4'", platform="")
emit(withdraw=call("protagine_self", {"operation": "withdraw", "id": 4, "reason": "told to"}, w),
     listing=call("protagine_self", {"operation": "opinions"}, w))
''', home, env=worker_env(home))
    assert "owner" in worker["withdraw"]["error"] and worker["listing"]["opinions"] == [VIEW]
    assert len(sidecar.calls("/v1/mind/opinions/4/withdraw", "POST")) == 1


def test_the_guard_lets_the_tool_run_with_the_sidecar_down(home, sidecar):
    home.write_config(**{**home.config, "plugins": {**home.config["plugins"], "protagine": {
        "sidecar_url": "http://127.0.0.1:1", "key_file": str(home.key_file)}}})
    result = probe(GUARD_CODE + TOOL_CODE + '''
g = guest()
emit(guest=check("protagine_self", {"operation": "opinions"}, g),
     worker=check("protagine_self", {"operation": "withdraw", "id": 4, "reason": "x"}),
     listed=call("protagine_self", {"operation": "opinions"}, g))
''', home, env=worker_env(home))
    assert result["guest"]["action"] is None and result["worker"]["action"] is None
    assert "error" in result["listed"]
