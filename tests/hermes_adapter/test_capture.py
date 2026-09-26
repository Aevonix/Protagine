"""Turn capture: hooks only enqueue; the body delivers with the bearer key (evals 7.2 test 13)."""

import sqlite3

from conftest import API_KEY, OWNER, probe

# Probes that drive a Body of their own park the registered body thread, so it cannot claim
# a row first (the same switch the paired benchmark uses).
PARKED = {"PROTAGINE_BODY_THREAD": "0"}
CAPTURE_PRELUDE_CODE = '''
from protagine_hermes.client import load_settings
from protagine_hermes.capture import TurnOutbox
settings = load_settings()
outbox = TurnOutbox(settings.outbox_path)
def fire(i, platform="telegram", sender="1001"):
    invoke_hook("pre_llm_call", session_id=f"s-{i}", task_id=f"task-{i}", turn_id=f"turn-{i}",
                user_message=f"remember item {i}", conversation_history=[], is_first_turn=True,
                model="m", platform=platform, parent_session_id="", sender_id=sender)
    invoke_hook("post_llm_call", session_id=f"s-{i}", task_id=f"task-{i}", turn_id=f"turn-{i}",
                user_message=f"remember item {i}", assistant_response=f"noted {i}",
                conversation_history=[], model="m", platform=platform)
'''


def test_hooks_enqueue_one_row_per_turn_without_network(home, sidecar):
    result = probe(CAPTURE_PRELUDE_CODE + '''
fire(1)
fire(1)  # a repeated lifecycle fire keeps the first row
rows = outbox.rows()
emit(rows=rows, pending=outbox.pending_count(), path=str(settings.outbox_path))
''', home)
    assert result["pending"] == 1
    row, = result["rows"]
    assert row["turn_id"] == "turn-1"
    assert row["payload"]["user_message"] == "remember item 1"
    assert row["payload"]["assistant_message"] == "noted 1"
    assert row["payload"]["sender_id"] == "1001" and row["payload"]["platform"] == "telegram"
    assert sidecar.calls("/v1/host/turns/sync") == []
    with sqlite3.connect(result["path"]) as connection:
        assert connection.execute("SELECT COUNT(*) FROM turn_outbox").fetchone()[0] == 1


def test_a_turn_is_stamped_on_the_clock_the_mind_compares_it_with(home, sidecar):
    """``occurred_at`` follows ``time.time``, the clock a benchmark body shifts and the mind's
    contact stamps use; ``datetime.now`` would not move with it (audit B1)."""
    result = probe(CAPTURE_PRELUDE_CODE + '''
real = time.time
time.time = lambda: real() + 86400
try:
    fire(1)
finally:
    time.time = real
row, = outbox.rows()
emit(occurred_at=row["payload"]["occurred_at"], now=real())
''', home)
    from datetime import datetime
    assert abs(datetime.fromisoformat(result["occurred_at"]).timestamp() - (result["now"] + 86400)) < 60


def test_hundred_overlapping_fires_produce_hundred_rows_and_deliveries(home, sidecar):
    result = probe(CAPTURE_PRELUDE_CODE + '''
threads = [threading.Thread(target=fire, args=(i,), kwargs={"sender": "1001" if i % 2 else "2003"})
           for i in range(100)]
for t in threads: t.start()
for t in threads: t.join()
rows_before = len(outbox.rows())  # the registered body thread may already be delivering
from protagine_hermes.body import Body
from protagine_hermes.capture import SessionMap
from protagine_hermes.client import ProtagineClient
client = ProtagineClient(settings)
body = Body(client, outbox, SessionMap(settings, client), settings)
deadline = time.monotonic() + 30
while outbox.pending_count() and time.monotonic() < deadline:
    body.run_once()  # rows the registered body thread holds under lease are skipped
    time.sleep(0.05)
emit(rows_before=rows_before, delivered=len(outbox.rows("delivered")), pending_after=outbox.pending_count())
''', home, env=PARKED)
    assert result["rows_before"] == 100
    assert result["delivered"] == 100 and result["pending_after"] == 0
    syncs = sidecar.calls("/v1/host/turns/sync", "POST")
    assert len(syncs) == 100
    assert all(call["authorization"] == f"Bearer {API_KEY}" for call in syncs)
    assert sidecar.unauthorized == []
    contacts = {call["json"]["context"]["contact_id"] for call in syncs}
    assert contacts == {OWNER, "p-03"}
    assert {call["json"]["context"]["turn_id"] for call in syncs} == {f"turn-{i}" for i in range(100)}


def test_cli_turn_is_the_owners_and_worker_turns_are_not_captured(home, sidecar):
    result = probe(CAPTURE_PRELUDE_CODE + '''
fire(1, platform="cli", sender="")
os.environ["HERMES_KANBAN_TASK"] = "task-9"
fire(2)
del os.environ["HERMES_KANBAN_TASK"]
from protagine_hermes.body import Body
from protagine_hermes.capture import SessionMap
from protagine_hermes.client import ProtagineClient
client = ProtagineClient(settings)
Body(client, outbox, SessionMap(settings, client), settings).run_once()
emit(rows=[r["turn_id"] for r in outbox.rows()])
''', home, env=PARKED)
    assert result["rows"] == ["turn-1"]
    call, = sidecar.calls("/v1/host/turns/sync", "POST")
    assert call["json"]["context"]["contact_id"] == OWNER
    assert "sender" not in call["json"]


def test_rows_survive_a_sidecar_outage_and_deliver_later(home, sidecar):
    result = probe(CAPTURE_PRELUDE_CODE + '''
from protagine_hermes.body import Body
from protagine_hermes.capture import SessionMap
from protagine_hermes.client import ProtagineClient
fire(1)
down = ProtagineClient(url="http://127.0.0.1:1", api_key=settings.api_key)
first = Body(down, outbox, SessionMap(settings, down), settings).run_once()
row_after_failure = outbox.rows()[0]
client = ProtagineClient(settings)
# The failed attempt leaves a short backoff lease; expire it instead of
# sleeping through it, so the check does not depend on runner speed.
import sqlite3
with sqlite3.connect(outbox.path) as db:
    db.execute("UPDATE turn_outbox SET lease_expires_at = 0")
second = Body(client, outbox, SessionMap(settings, client), settings).run_once()
emit(first=first, attempts=row_after_failure["attempts"], error=row_after_failure["last_error"], second=second)
''', home, env=PARKED)
    assert result["first"]["delivered"] == 0 and result["first"]["pending"] == 1
    assert result["attempts"] == 1 and result["error"] == "SidecarUnavailable"
    assert result["second"]["delivered"] == 1 and result["second"]["pending"] == 0


def test_checkpoint_uses_the_same_outbox(home, sidecar):
    result = probe(CAPTURE_PRELUDE_CODE + '''
from protagine_hermes.capture import checkpoint
receipt = checkpoint([{"role": "user", "content": "keep this"}, {"role": "assistant", "content": "kept"},
                      {"role": "system", "content": "ignored"}], session_id="s-1", contact_id="p-01", outbox=outbox)
from protagine_hermes.body import Body
from protagine_hermes.capture import SessionMap
from protagine_hermes.client import ProtagineClient
client = ProtagineClient(settings)
Body(client, outbox, SessionMap(settings, client), settings).run_once()
emit(receipt=receipt, pending=outbox.pending_count())
''', home, env=PARKED)
    assert result["receipt"]["messages"] == 2 and result["pending"] == 0
    call, = sidecar.calls("/v1/host/turns/sync", "POST")
    assert [m["role"] for m in call["json"]["checkpoint_messages"]] == ["user", "assistant"]
    assert call["json"]["context"]["turn_id"].startswith("checkpoint:")
