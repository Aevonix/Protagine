"""The body half of the M2 loop on stock Hermes (architecture 6.2, 7.9; evals 7.2 tests 1 and 10).

Every probe runs the registered adapter in a fresh interpreter against the
fake sidecar's ``/v1/mind`` contract and drives ``Body.run_once`` by hand.
Kanban is the real stock ``hermes_cli.kanban_db`` under ``HERMES_HOME``;
messages go through stock ``tools.send_message_tool`` unless a probe swaps
in a recorder.
"""

import sqlite3

import yaml

from conftest import API_KEY, MIND_PRELUDE, OWNER, probe


def intention(id, **fields):
    return {"id": id, "kind": "task", "status": "approved", "title": f"Check on {id}",
            "body": f"Report what you did for {id}.", "created_at": "2026-01-01T00:00:00+00:00", **fields}


def test_dispatch_creates_one_task_per_intention_across_restarts(home, sidecar):
    """Evals test 10, tasks: a lost bound ack plus a restart creates no second task."""
    sidecar.mind_routes = True
    sidecar.mind.intentions["i-01"] = intention("i-01", max_runtime_seconds=120, max_retries=2, priority=3,
                                                goal_mode=True, goal_max_turns=4)
    sidecar.mind.intentions["i-02"] = intention("i-02", kind="message", text="not a task")
    sidecar.mind.lose_bound = True  # the ack is accepted but the sidecar keeps offering the intention
    first = probe("emit(tick=tick(), tasks=tasks())", home, prelude=MIND_PRELUDE)
    assert first["tick"]["mind"] is True and first["tick"]["dispatched"] == 1
    task, = first["tasks"].values()
    assert task["assignee"] == "protagine-act" and task["status"] == "ready"
    assert task["title"] == "Check on i-01" and task["body"] == "Report what you did for i-01."
    assert task["max_runtime_seconds"] == 120 and task["max_retries"] == 2 and task["goal_mode"] is True
    assert task["created_by"] == "protagine" and task["workspace_kind"] == "scratch"
    assert list(first["tasks"]) == ["mind:i-01"]
    bound, = sidecar.mind.bound
    assert bound["id"] == "i-01" and bound["hermes_ref"] == task["id"] and bound["hermes_kind"] == "kanban"

    second = probe("emit(tick=tick(), tasks=tasks())", home, prelude=MIND_PRELUDE)  # a restart
    assert second["tick"]["dispatched"] == 1  # offered again, bound again, to the same task
    assert list(second["tasks"]) == ["mind:i-01"] and second["tasks"]["mind:i-01"]["id"] == task["id"]
    assert [b["hermes_ref"] for b in sidecar.mind.bound] == [task["id"], task["id"]]

    sidecar.mind.lose_bound = False
    third = probe("emit(tick=tick(), tasks=tasks())", home, prelude=MIND_PRELUDE)
    assert list(third["tasks"]) == ["mind:i-01"]
    assert sidecar.mind.intentions["i-01"]["status"] == "dispatched"
    assert sidecar.mind.intentions["i-01"]["hermes_ref"] == task["id"]
    fourth = probe("emit(tick=tick(), tasks=tasks())", home, prelude=MIND_PRELUDE)
    assert fourth["tick"]["dispatched"] == 0 and len(fourth["tasks"]) == 1
    assert all(call["authorization"] == f"Bearer {API_KEY}" for call in sidecar.calls("/v1/mind/dispatch"))
    assert sidecar.unauthorized == []


def test_outbox_messages_are_sent_verbatim_exactly_once(home, sidecar):
    sidecar.mind_routes = True
    text = "Reminder: the report you promised is due at 3pm.\n\n[[verbatim]] markers stay."
    sidecar.mind.outbox["m-01"] = {"id": "m-01", "kind": "notice", "text": text, "target": "telegram:1001"}
    sidecar.mind.outbox["m-02"] = {"id": "m-02", "kind": "message", "text": "hello p-03",
                                   "recipient": {"platform": "telegram", "chat_id": "2003", "contact_id": "p-03"}}
    sidecar.mind.outbox["m-03"] = {"id": "m-03", "kind": "notice", "text": "to the owner by handle"}
    sidecar.mind.relist_sending = True  # a sloppy sidecar that offers a message again mid-send
    result = probe('''
_smt.send_message_tool = fake_send
first = tick()
second = tick()
emit(first=first, second=second, sends=SENDS)
''', home, prelude=MIND_PRELUDE)
    assert result["first"]["sent"] == 3 and result["second"]["sent"] == 0
    assert result["sends"] == [
        {"action": "send", "target": "telegram:1001", "message": text},
        {"action": "send", "target": "telegram:2003", "message": "hello p-03"},
        {"action": "send", "target": "telegram:1001", "message": "to the owner by handle"},
    ]
    assert [s["id"] for s in sidecar.mind.sending] == ["m-01", "m-02", "m-03"]
    assert {s["id"]: s["result"] for s in sidecar.mind.sent} == {"m-01": "sent", "m-02": "sent", "m-03": "sent"}
    assert all(s["hermes_ref"].startswith("m-") for s in sidecar.mind.sent)
    assert {m["state"] for m in sidecar.mind.outbox.values()} == {"sent"}


def test_a_send_interrupted_between_sending_and_sent_is_uncertain_and_never_resent(home, sidecar):
    """Evals test 10, messages: a crash after ``sending`` produces one uncertain report and no resend."""
    sidecar.mind_routes = True
    sidecar.mind.outbox["m-01"] = {"id": "m-01", "kind": "notice", "text": "only once", "target": "telegram:1001"}
    sidecar.mind.relist_sending = True
    crashed = probe('''
def crash(args, **kw):
    emit(crashed=True, args=args)
    os._exit(0)
_smt.send_message_tool = crash
tick()
emit(crashed=False)
''', home, prelude=MIND_PRELUDE)
    assert crashed["crashed"] is True and crashed["args"]["message"] == "only once"
    assert [s["id"] for s in sidecar.mind.sending] == ["m-01"] and sidecar.mind.sent == []
    assert sidecar.mind.outbox["m-01"]["state"] == "sending"

    restarted = probe('''
_smt.send_message_tool = fake_send
first = tick()
sidecar_state = None
second = tick()
emit(first=first, second=second, sends=SENDS, ledger=body.ledger.send("m-01"))
''', home, prelude=MIND_PRELUDE)
    assert restarted["sends"] == []
    assert restarted["ledger"]["state"] == "uncertain" and restarted["ledger"]["reported"] == 1
    sent, = sidecar.mind.sent
    assert sent["id"] == "m-01" and sent["result"] == "uncertain" and "stopped" in sent["error"]
    assert sidecar.mind.outbox["m-01"]["state"] == "uncertain"
    assert len(sidecar.mind.sending) == 1  # never claimed a second time


def test_a_refused_send_is_reported_failed_through_the_stock_tool(home, sidecar):
    """No platform is configured in this home: stock ``send_message_tool`` refuses before sending."""
    sidecar.mind_routes = True
    sidecar.mind.outbox["m-01"] = {"id": "m-01", "kind": "notice", "text": "hi", "target": "telegram:1001"}
    sidecar.mind.outbox["m-02"] = {"id": "m-02", "kind": "message", "text": "no handle",
                                   "recipient": {"contact_id": "p-09"}}
    result = probe("emit(first=tick(), second=tick(), ledger=[body.ledger.send(i) for i in ('m-01', 'm-02')])",
                   home, prelude=MIND_PRELUDE)
    assert result["first"]["sent"] == 0 and result["second"]["sent"] == 0
    reported = {s["id"]: s for s in sidecar.mind.sent}
    assert reported["m-01"]["result"] == "failed" and reported["m-01"]["error"]
    assert reported["m-02"]["result"] == "failed" and "recipient" in reported["m-02"]["error"]
    assert [s["id"] for s in sidecar.mind.sending] == ["m-01", "m-02"]
    assert {row["state"] for row in result["ledger"]} == {"failed"}


def test_reconciliation_posts_each_outcome_once(home, sidecar):
    sidecar.mind_routes = True
    for i in ("i-01", "i-02", "i-03"):
        sidecar.mind.intentions[i] = intention(i)
    result = probe('''
tick()
ids = {k: v["id"] for k, v in tasks().items()}
conn = connect()
kb.claim_task(conn, ids["mind:i-01"], claimer="worker-a")
kb.complete_task(conn, ids["mind:i-01"], summary="Sent the report. Evidence: the message id. It worked.")
kb.claim_task(conn, ids["mind:i-02"], claimer="worker-b")
kb.block_task(conn, ids["mind:i-02"], reason="need the owner's password", kind="needs_input")
conn.close()
after = tick()
again = tick()
emit(after=after, again=again, tasks=tasks())
''', home, prelude=MIND_PRELUDE)
    assert result["after"]["reconciled"] == 2 and result["again"]["reconciled"] == 0
    outcomes = {o["id"]: o for o in sidecar.mind.outcomes}
    assert set(outcomes) == {"i-01", "i-02"}
    done = outcomes["i-01"]
    assert done["status"] == "done" and done["outcome"] == "done" and done["final"] is True
    assert done["summary"].startswith("Sent the report") and done["verified"] is None
    assert done["hermes_ref"] == result["tasks"]["mind:i-01"]["id"] and done["run"]["outcome"] == "completed"
    blocked = outcomes["i-02"]
    assert blocked["status"] == "blocked" and blocked["outcome"] == "blocked" and blocked["block_kind"] == "needs_input"
    assert sidecar.mind.intentions["i-01"]["status"] == "done"
    assert result["tasks"]["mind:i-03"]["status"] == "ready"  # untouched, still dispatched


def test_tasks_without_a_dispatched_intention_are_archived(home, sidecar):
    """Architecture 7.7: a hand-made ``mind:*`` task, or one for an unapproved ask, owns nothing."""
    sidecar.mind_routes = True
    sidecar.mind.intentions["i-01"] = intention("i-01")
    sidecar.mind.intentions["i-02"] = intention("i-02", status="asked", ask_code="K7F")
    result = probe('''
tick()
conn = connect()
kb.create_task(conn, title="board-made", assignee="protagine-act", idempotency_key="mind:ghost")
kb.create_task(conn, title="ask made by hand", assignee="protagine-act", idempotency_key="mind:i-02")
kb.create_task(conn, title="owner task", assignee="default", idempotency_key="owner:1")
conn.close()
before = tasks()
after = tick()
emit(before=before, after=after, tasks=tasks())
''', home, prelude=MIND_PRELUDE)
    assert result["before"]["mind:ghost"]["status"] == "ready" and result["before"]["mind:i-02"]["status"] == "ready"
    assert result["after"]["archived"] == 2
    assert result["tasks"]["mind:ghost"]["status"] == "archived"
    assert result["tasks"]["mind:i-02"]["status"] == "archived"
    assert result["tasks"]["mind:i-01"]["status"] == "ready"
    assert result["tasks"]["owner:1"]["status"] == "ready"
    assert sidecar.mind.intentions["i-02"]["status"] == "asked"  # the ask itself is untouched
    assert [o["id"] for o in sidecar.mind.outcomes] == []


def test_off_switch_archives_unstarted_mind_tasks_and_sends_nothing(home, sidecar):
    """Evals test 1 without a worker: off in the sidecar, at the next tick nothing new happens."""
    sidecar.mind_routes = True
    sidecar.mind.intentions["i-01"] = intention("i-01")
    sidecar.mind.intentions["i-02"] = intention("i-02")
    sidecar.mind.outbox["m-01"] = {"id": "m-01", "kind": "notice", "text": "late", "target": "telegram:1001"}
    result = probe('''
_smt.send_message_tool = fake_send
tick()
conn = connect()
kb.claim_task(conn, tasks()["mind:i-02"]["id"], claimer="worker-b")  # running: ends on its own
kb.create_task(conn, title="owner task", assignee="default", idempotency_key="owner:1")
conn.close()
sends_before = list(SENDS)
protagine_hermes._BODY.client.post("/v1/mind/off")
off = tick()
emit(sends_before=sends_before, off=off, sends=SENDS, tasks=tasks())
''', home, prelude=MIND_PRELUDE)
    assert len(result["sends_before"]) == 1 and len(result["sends"]) == 1
    assert result["off"]["enabled"] is False and result["off"]["archived"] == 1
    assert result["tasks"]["mind:i-01"]["status"] == "archived"
    assert result["tasks"]["mind:i-02"]["status"] == "running"
    assert result["tasks"]["owner:1"]["status"] == "ready"
    cancelled = [o for o in sidecar.mind.outcomes if o["id"] == "i-01"]
    assert cancelled and cancelled[-1]["outcome"] == "cancelled" and cancelled[-1]["status"] == "archived"


def test_off_in_protagine_yaml_stops_dispatch_without_the_sidecar_saying_so(home, sidecar):
    sidecar.mind_routes = True
    sidecar.mind.intentions["i-01"] = intention("i-01")
    home.write_mind(enabled=False)
    result = probe("emit(tick=tick(), tasks=tasks())", home, prelude=MIND_PRELUDE)
    assert result["tick"]["enabled"] is False and result["tick"]["dispatched"] == 0 and result["tasks"] == {}
    assert sidecar.calls("/v1/mind/dispatch") == []


def test_observations_carry_stale_owner_tasks_goals_mind_tasks_and_the_heartbeat(home, sidecar):
    sidecar.mind_routes = True
    sidecar.mind.intentions["i-01"] = intention("i-01")
    result = probe('''
conn = connect()
stale = kb.create_task(conn, title="review the budget", assignee="default", idempotency_key="owner:stale")
fresh = kb.create_task(conn, title="fresh", assignee="default", idempotency_key="owner:fresh")
goal = kb.create_task(conn, title="finish the site", assignee="default", goal_mode=True, goal_max_turns=9)
blocked = kb.create_task(conn, title="stuck", assignee="default")
kb.claim_task(conn, blocked, claimer="w")
kb.block_task(conn, blocked, reason="needs a browser", kind="capability")  # dependency waits in todo, not blocked
conn.execute("UPDATE tasks SET created_at = created_at - 4 * 86400 WHERE id = ?", (stale,))
conn.close()
first = tick()
second = tick()
emit(first=first, second=second, stale=stale, goal=goal, blocked=blocked, heartbeat=body.heartbeat())
''', home, prelude=MIND_PRELUDE)
    assert result["first"]["observed"] is True and result["second"]["observed"] is False  # unchanged board
    observation = sidecar.mind.observations[-1]
    assert [t["id"] for t in observation["stale_tasks"]] == [result["stale"]]
    assert observation["stale_tasks"][0]["idle_s"] >= 4 * 86400 - 60 and observation["stale_tasks"][0]["stale_after_s"] == 72 * 3600
    assert [g["id"] for g in observation["goals"]] == [result["goal"]] and observation["goals"][0]["goal_max_turns"] == 9
    assert [b["id"] for b in observation["blocked_tasks"]] == [result["blocked"]]
    assert observation["blocked_tasks"][0]["block_kind"] == "capability"
    mine, = observation["mind_tasks"]
    assert mine["intention_id"] == "i-01" and mine["status"] == "ready"
    assert observation["counts"]["ready"] >= 3 and observation["body"]["mind_ticks"] == 1
    assert result["heartbeat"]["ticks"] == 2 and result["heartbeat"]["last_pull_at"] and not result["heartbeat"]["stale"]
    assert sidecar.mind.last_pull_at is not None


def test_workers_never_run_the_mind_tick(home, sidecar):
    sidecar.mind_routes = True
    sidecar.mind.intentions["i-01"] = intention("i-01")
    from conftest import worker_env
    result = probe("emit(tick=tick(), tasks=tasks())", home, env=worker_env(home), prelude=MIND_PRELUDE)
    assert result["tick"]["mind"] is False and result["tasks"] == {}
    assert sidecar.calls("/v1/mind/dispatch") == []


def test_body_ledger_is_private_and_durable(home, sidecar):
    sidecar.mind_routes = True
    sidecar.mind.outbox["m-01"] = {"id": "m-01", "kind": "notice", "text": "x", "target": "telegram:1001"}
    result = probe('''
_smt.send_message_tool = fake_send
tick()
emit(path=str(body.ledger.path), mode=oct(os.stat(body.ledger.path).st_mode & 0o777))
''', home, prelude=MIND_PRELUDE)
    assert result["path"].startswith(str(home.hermes)) and result["mode"] == "0o600"
    with sqlite3.connect(result["path"]) as connection:
        rows = connection.execute("SELECT id, state, reported FROM mind_sends").fetchall()
    assert rows == [("m-01", "sent", 1)]


def test_only_the_dispatcher_owner_runs_the_mind(home, sidecar):
    """A CLI session or a gateway without the dispatcher lock drains turns only; the process stock's
    dispatch tick reaches is the one writer, and ``tick()`` drives it on request."""
    sidecar.mind_routes = True
    sidecar.mind.intentions["i-01"] = intention("i-01")
    result = probe('''
before = body.run_once()
flushed = protagine_hermes.flush()
body.on_dispatch_tick(board="default", outcome="idle")
after = body.run_once()
emit(before=before, flushed=flushed, after=after, tasks=tasks(), heartbeat=body.heartbeat())
''', home, prelude=MIND_PRELUDE)
    assert result["before"]["mind"] is False and result["before"]["dispatcher"] is False
    assert result["flushed"]["mind"] is False
    assert result["after"]["mind"] is True and result["after"]["dispatched"] == 1 and result["after"]["dispatcher"] is True
    assert list(result["tasks"]) == ["mind:i-01"] and result["heartbeat"]["dispatcher"] is True
    assert len(sidecar.calls("/v1/mind/dispatch")) == 1
    driven = probe("emit(tick=protagine_hermes.tick())", home, prelude=MIND_PRELUDE)
    assert driven["tick"]["mind"] is True and driven["tick"]["dispatcher"] is False


def test_hermes_pause_holds_dispatch_and_sends_but_keeps_the_books(home, sidecar):
    """Stock ``hermes pause`` (the ESTOP sentinel) stops new work: no task is created and no message
    sent while it is engaged; reconciliation and observations still run."""
    sidecar.mind_routes = True
    sidecar.mind.intentions["i-01"] = intention("i-01")
    sidecar.mind.outbox["m-01"] = {"id": "m-01", "kind": "notice", "text": "later", "target": "telegram:1001"}
    result = probe('''
_smt.send_message_tool = fake_send
from agent.estop import engage, disengage
engage("maintenance")
held = tick()
pulls_while_held = None
disengage()
resumed = tick()
emit(held=held, resumed=resumed, sends=SENDS, tasks=tasks())
''', home, prelude=MIND_PRELUDE)
    assert result["held"]["mind"] is True and result["held"]["paused"] is True
    assert result["held"]["dispatched"] == 0 and result["held"]["sent"] == 0 and result["held"]["observed"] is True
    assert result["resumed"]["paused"] is False and result["resumed"]["dispatched"] == 1 and result["resumed"]["sent"] == 1
    assert len(result["sends"]) == 1 and list(result["tasks"]) == ["mind:i-01"]
    assert sidecar.mind.pulls == 1 and [s["id"] for s in sidecar.mind.sending] == ["m-01"]


def test_an_archived_task_settles_its_intention_instead_of_being_recreated(home, sidecar):
    """A task created here but archived before its ack landed (the body died in between) is bound and
    settled as cancelled; stock's key lookup alone, which skips archived tasks, would create a second."""
    sidecar.mind_routes = True
    sidecar.mind.intentions["i-01"] = intention("i-01")
    sidecar.mind.lose_bound = True
    first = probe('''
tick()
conn = connect()
kb.archive_task(conn, tasks()["mind:i-01"]["id"])
count = len(kb.list_tasks(conn, include_archived=True))
conn.close()
emit(tasks=tasks(), count=count)
''', home, prelude=MIND_PRELUDE)
    old_id = first["tasks"]["mind:i-01"]["id"]
    assert first["tasks"]["mind:i-01"]["status"] == "archived" and first["count"] == 1
    sidecar.mind.lose_bound = False
    second = probe('''
after = tick()
conn = connect()
count = len(kb.list_tasks(conn, include_archived=True))
conn.close()
emit(tick=after, tasks=tasks(), count=count)
''', home, prelude=MIND_PRELUDE)
    assert second["count"] == 1 and second["tasks"]["mind:i-01"]["id"] == old_id
    assert second["tick"]["dispatched"] == 1
    assert sidecar.mind.bound[-1]["hermes_ref"] == old_id and sidecar.mind.bound[-1]["status"] == "archived"
    outcome, = sidecar.mind.outcomes
    assert outcome["id"] == "i-01" and outcome["outcome"] == "cancelled" and outcome["final"] is True
    assert outcome["hermes_ref"] == old_id and sidecar.mind.intentions["i-01"]["status"] == "cancelled"
    third = probe("emit(tick=tick(), tasks=tasks())", home, prelude=MIND_PRELUDE)
    assert third["tick"]["dispatched"] == 0 and len(sidecar.mind.outcomes) == 1


def test_exhausted_hermes_failures_settle_the_intention_as_failed(home, sidecar):
    """Stock parks a task whose retries ran out as blocked with its failed run: that is a final failure
    (the slot and the breaker see it); a retry in flight is progress; an operator's block stays blocked."""
    sidecar.mind_routes = True
    sidecar.mind.intentions["i-01"] = intention("i-01", max_retries=1)
    sidecar.mind.intentions["i-02"] = intention("i-02", max_retries=3)
    sidecar.mind.intentions["i-03"] = intention("i-03")
    result = probe('''
from hermes_cli import kanban_db_dispatch as kbd
tick()
ids = {k: v["id"] for k, v in tasks().items()}
conn = connect()
for key in ("mind:i-01", "mind:i-02"):
    kb.claim_task(conn, ids[key], claimer="worker")
    kbd._record_task_failure(conn, ids[key], "spawn failed", outcome="spawn_failed", release_claim=True, end_run=True)
kb.claim_task(conn, ids["mind:i-03"], claimer="worker")
kb.block_task(conn, ids["mind:i-03"], reason="need a key", kind="needs_input")
conn.close()
after = tick()
emit(after=after, tasks=tasks())
''', home, prelude=MIND_PRELUDE)
    assert result["after"]["reconciled"] == 3
    outcomes = {o["id"]: o for o in sidecar.mind.outcomes}
    exhausted = outcomes["i-01"]
    assert result["tasks"]["mind:i-01"]["status"] == "blocked"
    assert exhausted["outcome"] == "failed" and exhausted["final"] is True and exhausted["consecutive_failures"] == 1
    assert exhausted["verified"] == "hermes_failure" and exhausted["run"]["outcome"] == "gave_up"  # stock's trip
    assert sidecar.mind.intentions["i-01"]["status"] == "failed"
    retrying = outcomes["i-02"]
    assert result["tasks"]["mind:i-02"]["status"] == "ready"
    assert retrying["outcome"] == "failed" and retrying["final"] is False
    assert sidecar.mind.intentions["i-02"]["status"] == "dispatched"
    blocked = outcomes["i-03"]
    assert blocked["outcome"] == "blocked" and blocked["block_kind"] == "needs_input" and blocked["final"] is True


def test_handles_send_to_the_dm_the_gateway_has_had_with_that_sender(home, sidecar):
    """A handle names a sender; on Discord that is a user id, not a channel. The target is the DM
    session stock recorded with that user, and without one the handle is used as it is."""
    sidecar.mind_routes = True
    (home.instance / "identity.yaml").write_text(yaml.safe_dump({
        "owner": {"name": "Owner", "contact_id": OWNER, "handles": {"discord": ["4242"]}}}))
    result = probe('''
from hermes_state_registry import acquire, release_or_close
notice = {"id": "m-01", "kind": "notice", "text": "hi", "recipient_is_owner": True}
contact = {"id": "m-02", "kind": "message", "text": "hi", "recipient": "p-03",
           "recipient_handles": [{"gateway": "discord", "address": "5151", "is_primary": True}]}
bare = body.message_target(notice)
db = acquire()
db.create_session("s-group", "discord", user_id="4242", chat_id="7007", chat_type="group", session_key="discord:g:7007")
db.create_session("s-dm", "discord", user_id="4242", chat_id="9009", chat_type="dm", session_key="discord:dm:9009")
db.create_session("s-dm-3", "discord", user_id="5151", chat_id="9010", chat_type="dm", session_key="discord:dm:9010")
release_or_close(db)
emit(bare=bare, owner=body.message_target(notice), contact=body.message_target(contact),
     explicit=body.message_target({"id": "m-03", "text": "x", "target": "discord:1234"}))
''', home, prelude=MIND_PRELUDE)
    assert result["bare"] == "discord:4242"
    assert result["owner"] == "discord:9009" and result["contact"] == "discord:9010"
    assert result["explicit"] == "discord:1234"
