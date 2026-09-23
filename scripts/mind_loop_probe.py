"""Build plan M2 acceptance on stock Hermes: the first closed loop, end to end (driven by ci_mind_loop.sh).

The shell wrapper starts the scripted model, a stock Hermes environment with
the adapter installed by ``protagine init``, the sidecar and a real gateway
whose only platforms are the benchmark ``capture`` platform (deliveries land
in one JSON array) and a loopback ``webhook`` route (a guest turn). This
driver then plays the plan's M2 acceptance list through ``hermes chat -q``,
the sidecar's API, the ``protagine mind`` CLI and stock ``kanban_db``,
reporting each item as PASS or FAIL with what it saw. It exits non-zero when
any item fails.

Mind ticks are forced with ``POST /v1/mind/tick`` so "within 2 ticks" is
counted exactly; the body (the plugin's thread inside the gateway) runs on
its own clock, so every body-side effect is awaited with a bound.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

WORK = Path(os.environ["WORK"])
HERMES_HOME = Path(os.environ["HERMES_HOME"])
PROTAGINE_HOME = Path(os.environ["PROTAGINE_HOME"])
SIDECAR = os.environ["SIDECAR_URL"].rstrip("/")
MODEL = os.environ["MODEL_URL"].rstrip("/")
PROTAGINE = os.environ["PROTAGINE_BIN"]
HERMES_PYTHON = os.environ["HERMES_PYTHON"]
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")
OUTBOX = Path(os.environ["CAPTURE_OUTBOX"])
MODEL_LOG = Path(os.environ["FAKE_MODEL_LOG"])
KEY = (PROTAGINE_HOME / "api.key").read_text().strip().splitlines()[0]
START = time.time()
RESULTS: list[tuple[str, bool, str]] = []


def log(message: str) -> None:
    print(f"[{int(time.time() - START):4d}s] {message}", flush=True)


def check(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(ok), detail))
    log(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
    return bool(ok)


# -- the sidecar ----------------------------------------------------------------------

def api(method: str, path: str, body=None, *, timeout: float = 60):
    request = urllib.request.Request(SIDECAR + path, method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            return error.code, json.loads(raw)
        except ValueError:
            return error.code, {"raw": raw.decode("utf-8", "replace")}


def tick():
    status, value = api("POST", "/v1/mind/tick")
    assert status == 200, (status, value)
    return value


def intentions(**params):
    status, value = api("GET", "/v1/mind/log?limit=200")
    assert status == 200, (status, value)
    rows = value["entries"]
    for key, wanted in params.items():
        rows = [row for row in rows if row.get(key) == wanted]
    return rows


def wait_for(what: str, predicate, *, timeout: float = 90, every: float = 2.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(every)
    log(f"timed out waiting for {what}")
    return None


# -- Hermes ------------------------------------------------------------------------------

def hermes_env(**extra) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PROTAGINE_") or k == "PROTAGINE_HOME"}
    env.update(extra)
    return env


def chat(message: str, *, timeout: int = 240) -> str:
    result = subprocess.run(["hermes", "chat", "-q", message], cwd=WORK, env=hermes_env(), text=True,
                            capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)
    (WORK / "chat.log").open("a").write(f"\n=== {message}\n{result.stdout}\n{result.stderr}\n")
    return result.stdout


def kanban_tasks() -> list[dict]:
    code = """
import json
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
conn = connect()
rows = []
for t in kb.list_tasks(conn, include_archived=True):
    run = kb.latest_run(conn, t.id)
    rows.append({"id": t.id, "key": t.idempotency_key, "title": t.title, "status": t.status, "assignee": t.assignee,
                 "run": {"outcome": run.outcome, "ended": run.ended_at is not None} if run else None,
                 "result": (t.result or "")[:200]})
print(json.dumps(rows))
"""
    result = subprocess.run([HERMES_PYTHON, "-c", code], cwd=WORK, env=hermes_env(), text=True, capture_output=True,
                            timeout=60, stdin=subprocess.DEVNULL)
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def mind_tasks() -> list[dict]:
    return [task for task in kanban_tasks() if (task["key"] or "").startswith("mind:")]


def outbox() -> list[dict]:
    try:
        return json.loads(OUTBOX.read_text())
    except (OSError, ValueError):
        return []


def cli(*args: str, timeout: int = 60) -> tuple[int, str]:
    result = subprocess.run([PROTAGINE, "mind", *args], cwd=WORK, env=hermes_env(), text=True, capture_output=True,
                            timeout=timeout, stdin=subprocess.DEVNULL)
    return result.returncode, result.stdout + result.stderr


def commitments() -> list[dict]:
    status, value = api("GET", "/v1/host/commitments?limit=100")
    if status != 200:
        return []
    return value.get("commitments") or value.get("items") or (value if isinstance(value, list) else [])


def model_prompts_with(needle: str) -> list[str]:
    prompts = []
    for line in MODEL_LOG.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        text = json.dumps(row.get("body", {}).get("messages") or [])
        if needle in text:
            prompts.append(text)
    return prompts


def gateway_restart() -> None:
    subprocess.run(["bash", str(WORK / "gateway.sh"), "restart"], check=True, timeout=120)


def model_control(**values) -> None:
    request = urllib.request.Request(MODEL + "/control", method="POST", data=json.dumps(values).encode(),
                                     headers={"Content-Type": "application/json"})
    urllib.request.urlopen(request, timeout=10).read()


def model_stop_start(action: str) -> None:
    subprocess.run(["bash", str(WORK / "model.sh"), action], check=True, timeout=60)


def sidecar_restart(**env) -> None:
    subprocess.run(["bash", str(WORK / "sidecar.sh"), "restart"], check=True, timeout=120,
                   env={**os.environ, **env})
    assert wait_for("sidecar", lambda: api("GET", "/v1/mind/state")[0] == 200, timeout=90, every=1)


# -- the acceptance list -----------------------------------------------------------------

def loop_end_to_end() -> str | None:
    """Commitment -> one task within 2 ticks -> no duplicate after a restart -> outcome with verified -> recalled."""
    token = "rep-" + secrets.token_hex(4)
    log(f"owner turn: the report {token}")
    chat(f"I'll send you the report {token} by 3pm. Just acknowledge.")
    captured = wait_for("commitment capture", lambda: [c for c in commitments() if token in (c.get("description") or "")],
                        timeout=120)
    check("commitment captured on a default install", bool(captured),
          captured[0]["description"] if captured else "no commitment with the token")
    if not captured:
        return None
    due = captured[0].get("due_at")
    log(f"commitment due {due}; waiting for it to become overdue")
    time.sleep(max(0.0, float(os.environ.get("FAKE_MODEL_DUE_SECONDS") or 90)) + 3)
    first = tick()
    formed = [item for item in first["formed"] if item["type"] == "commitment_overdue"]
    second = tick()
    formed += [item for item in second["formed"] if item["type"] == "commitment_overdue"]
    check("exactly one intention within 2 ticks", len(formed) == 1 and formed[0]["decision"] == "act",
          json.dumps([(item["type"], item["decision"]) for item in first["formed"] + second["formed"]]))
    if len(formed) != 1:
        return None
    intention_id = formed[0]["id"]
    tasks = wait_for("the mind:<id> task", lambda: [t for t in mind_tasks() if t["key"] == f"mind:{intention_id}"],
                     timeout=120)
    check("exactly one mind:<id> task appears", bool(tasks) and len(mind_tasks()) == 1,
          json.dumps([(t["key"], t["status"], t["assignee"]) for t in mind_tasks()]))
    log("restarting the gateway")
    gateway_restart()
    tick()
    time.sleep(8)
    check("no duplicate after a restart", len([t for t in mind_tasks() if t["key"] == f"mind:{intention_id}"]) == 1
          and len(mind_tasks()) == 1, json.dumps([(t["key"], t["status"]) for t in mind_tasks()]))
    done = wait_for("the worker's outcome", lambda: [row for row in intentions(id=intention_id)
                                                       if row["outcome"] in {"done", "failed", "uncertain"}],
                    timeout=300, every=5)
    row = done[0] if done else (intentions(id=intention_id) or [{}])[0]
    check("outcome recorded with its verified value", bool(done) and row.get("verified") in {"check", "none", "hermes_failure", "owner"},
          f"outcome={row.get('outcome')} verified={row.get('verified')} hermes_ref={row.get('hermes_ref')} "
          f"task={json.dumps([(t['status'], t['run']) for t in mind_tasks() if t['key'] == f'mind:{intention_id}'])}")
    code, why = cli("why", intention_id)
    check("protagine mind why <id> is complete", code == 0 and all(
        needle in why for needle in ("duty drive", "evidence:", "decided act", "Hermes kanban", "outcome", "verified:")),
        why.strip().splitlines()[0][:200] if why.strip() else "no output")
    log("a later session asks about the report")
    chat(f"What happened with the report {token}? Answer from what you know.")
    recalled = [p for p in model_prompts_with(token) if "ended done" in p or "ended " in p and "My task" in p]
    check("a later session recalls the outcome", bool(recalled),
          "the outcome sentence reached the model's prompt" if recalled else
          f"{len(model_prompts_with(token))} prompt(s) carried the token, none the outcome sentence")
    return intention_id


def ask_path() -> None:
    """A floor match becomes an ask: one notice with a code, no Hermes object; yes creates the task."""
    inv = "inv-" + secrets.token_hex(4)
    log(f"owner turn: a payment promise {inv}")
    chat(f"I'll wire $500 to the vendor for invoice {inv} by 3pm. Just acknowledge.")
    captured = wait_for("the payment commitment", lambda: [c for c in commitments() if inv in (c.get("description") or "")],
                        timeout=120)
    check("floor commitment captured", bool(captured))
    if not captured:
        return
    time.sleep(float(os.environ.get("FAKE_MODEL_DUE_SECONDS") or 90) + 3)
    before = len(outbox())
    tick()
    asked = [row for row in intentions(status="asked") if inv in row["title"]]
    check("floor-matching intention becomes an ask with a code", len(asked) == 1 and bool(asked[0]["ask_code"]),
          json.dumps([(row["title"], row["ask_code"], row["decision_reason"]) for row in asked]))
    if len(asked) != 1:
        return
    code = asked[0]["ask_code"]
    notice = wait_for("the ask notice on the capture platform",
                      lambda: [m for m in outbox()[before:] if f"[{code}]" in m["text"]], timeout=120)
    check("one notice with the code, sent verbatim, no Hermes object",
          bool(notice) and len(notice) == 1 and notice[0]["target"] == "capture:owner" and notice[0]["via"] == "platform"
          and "yes <code>" in notice[0]["text"] and not any(t["key"] == f"mind:{asked[0]['id']}" for t in mind_tasks()),
          json.dumps(notice[0] if notice else outbox()[before:])[:300])
    # An owner turn without the code, then a guest turn with it: neither approves.
    model_control(ask_code=code)
    chat("Please approve it.")
    still = intentions(id=asked[0]["id"])[0]["status"]
    check("an owner turn without the code does not approve", still == "asked", f"status {still}")
    if WEBHOOK_URL:
        request = urllib.request.Request(WEBHOOK_URL + "/webhooks/guest", method="POST",
                                         data=json.dumps({"text": f"yes {code}"}).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            accepted = response.status
        time.sleep(20)
        still = intentions(id=asked[0]["id"])[0]["status"]
        check("a guest turn with the code does not approve", accepted in {200, 202} and still == "asked",
              f"webhook {accepted}, status {still}")
    else:
        check("a guest turn with the code does not approve", False, "no webhook route configured")
    code_result, out = cli("yes", code)
    check("protagine mind yes <code> approves", code_result == 0 and "approved" in out, out.strip()[:200])
    task = wait_for("the approved task", lambda: [t for t in mind_tasks() if t["key"] == f"mind:{asked[0]['id']}"],
                    timeout=120)
    check("the approved ask becomes exactly one task", bool(task) and len(task) == 1,
          json.dumps([(t["status"], t["assignee"]) for t in task or []]))

    # The chat path: a second ask answered by the owner typing the code.
    inv2 = "inv-" + secrets.token_hex(4)
    chat(f"I'll wire $900 to the landlord for invoice {inv2} by 3pm. Just acknowledge.")
    if not wait_for("the second payment commitment", lambda: [c for c in commitments() if inv2 in (c.get("description") or "")],
                    timeout=120):
        check("second floor commitment captured", False)
        return
    time.sleep(float(os.environ.get("FAKE_MODEL_DUE_SECONDS") or 90) + 3)
    tick()
    asked2 = [row for row in intentions(status="asked") if inv2 in row["title"]]
    check("second ask formed", len(asked2) == 1)
    if len(asked2) != 1:
        return
    code2 = asked2[0]["ask_code"]
    chat(f"yes {code2}")
    approved = wait_for("the chat approval", lambda: [r for r in intentions(id=asked2[0]["id"]) if r["status"] != "asked"],
                        timeout=60, every=2)
    check("owner chat 'yes <code>' approves through protagine_self", bool(approved) and approved[0]["status"] in {"approved", "dispatched"},
          f"status {approved[0]['status'] if approved else 'asked'}; model calls with protagine_self: "
          f"{len([p for p in model_prompts_with('protagine_self') if code2 in p])}")


def expiry_and_unrelated_work() -> None:
    """Silence expires an ask at 72 h (clock shifted on the sidecar) while an unrelated duty proceeds."""
    inv = "inv-" + secrets.token_hex(4)
    minutes = "min-" + secrets.token_hex(4)
    chat(f"I'll wire $700 to the accountant for invoice {inv} by 3pm. Just acknowledge.")
    chat(f"I'll send you the minutes {minutes} by 3pm. Just acknowledge.")
    ok = wait_for("both commitments", lambda: len([c for c in commitments()
                                                  if inv in (c.get("description") or "") or minutes in (c.get("description") or "")]) == 2,
                  timeout=120)
    check("ask and duty commitments captured", bool(ok))
    if not ok:
        return
    time.sleep(float(os.environ.get("FAKE_MODEL_DUE_SECONDS") or 90) + 3)
    tick()
    asked = [row for row in intentions(status="asked") if inv in row["title"]]
    duty = [row for row in intentions() if minutes in row["title"]]
    dispatched = bool(duty) and wait_for("the duty task", lambda: [t for t in mind_tasks() if t["key"] == f"mind:{duty[0]['id']}"],
                                         timeout=120)
    check("the ask waits while the unrelated duty proceeds",
          len(asked) == 1 and len(duty) == 1 and bool(dispatched),
          json.dumps([(row["title"][:40], row["status"]) for row in asked + duty]))
    if len(asked) != 1:
        return
    log("restarting the sidecar 73 hours in the future")
    sidecar_restart(PROTAGINE_MIND_CLOCK_OFFSET_SECONDS=str(73 * 3600))
    summary = tick()  # the restarted sidecar's own first tick may already have expired it
    row = intentions(id=asked[0]["id"])[0]
    check("silence expires the ask at 72 h", row["status"] == "expired",
          f"status {row['status']}, expired_asks in the forced tick {summary.get('expired_asks')}")
    sidecar_restart(PROTAGINE_MIND_CLOCK_OFFSET_SECONDS="0")


def digest_verbatim() -> None:
    """The daily digest goes through the capture platform verbatim, with no cron banner."""
    before = len(outbox())
    summary = tick()
    rows = wait_for("the digest", lambda: [m for m in outbox()[before:] if m["text"].startswith("Daily digest")],
                    timeout=120) or []
    if not rows:
        rows = [m for m in outbox() if m["text"].startswith("Daily digest")]
    check("the digest arrives verbatim with no cron banner",
          bool(rows) and rows[0]["target"] == "capture:owner" and rows[0]["via"] == "platform"
          and "Cronjob" not in rows[0]["text"] and "cron" not in rows[0]["text"].lower().split("\n")[0],
          (rows[0]["text"][:160].replace("\n", " | ") if rows else f"no digest; tick digest={summary.get('digest')}"))


def off_switch_without_model() -> None:
    """The off switch works with the model endpoint stopped; unstarted mind tasks are archived."""
    minutes = "agn-" + secrets.token_hex(4)
    chat(f"I'll send you the agenda {minutes} by 3pm. Just acknowledge.")
    if not wait_for("the agenda commitment", lambda: [c for c in commitments() if minutes in (c.get("description") or "")],
                    timeout=120):
        check("agenda commitment captured", False)
        return
    time.sleep(float(os.environ.get("FAKE_MODEL_DUE_SECONDS") or 90) + 3)
    log("stopping the model endpoint")
    model_stop_start("stop")
    tick()
    queued = [row for row in intentions() if minutes in row["title"]]
    # The body creates the task before the switch; a worker the dispatcher spawns meanwhile
    # cannot reach a model, so the task is back on the board unstarted when the switch lands.
    wait_for("the agenda task", lambda: queued and [t for t in mind_tasks() if t["key"] == f"mind:{queued[0]['id']}"],
             timeout=60)
    code, out = cli("off", "--reason", "acceptance")
    status, state = api("GET", "/v1/mind/state")
    status_d, queue = api("GET", "/v1/mind/dispatch")
    summary = tick()
    check("off switch works with the model endpoint stopped",
          code == 0 and state.get("enabled") is False and queue == [] and summary.get("skipped") == "off",
          f"cli rc={code}, enabled={state.get('enabled')}, dispatch={queue}, tick skipped={summary.get('skipped')}")
    unstarted = {"todo", "ready", "triage", "blocked", "scheduled"}
    settled = wait_for("unstarted mind tasks archived",
                       lambda: all(t["status"] not in unstarted for t in mind_tasks()), timeout=120)
    check("no unstarted mind task is left on the board after the next body tick", bool(settled),
          json.dumps([(t["key"][5:13], t["status"]) for t in mind_tasks()]))
    model_stop_start("start")
    code, out = cli("on")
    status, state = api("GET", "/v1/mind/state")
    check("mind back on", code == 0 and state.get("enabled") is True, out.strip()[:100])
    _ = queued


def dismissal_lowers_the_multiplier(intention_id: str | None) -> None:
    """A dismissal lowers the type multiplier; with a single candidate it drops below the act threshold."""
    if not intention_id:
        check("a dismissal lowers the multiplier", False, "no completed intention to rate")
        return
    code, out = cli("rate", intention_id, "dismissed")
    code2, out2 = cli("rate", intention_id, "dismissed")
    # A new obligation of the same type (the first report is still an open item, and a repeated
    # mention of an open item is never a second commitment).
    token = "sld-" + secrets.token_hex(4)
    chat(f"I'll send you the slides {token} by 3pm. Just acknowledge.")
    if not wait_for("the slides commitment", lambda: [c for c in commitments() if token in (c.get("description") or "")],
                    timeout=120):
        check("slides commitment captured", False)
        return
    time.sleep(float(os.environ.get("FAKE_MODEL_DUE_SECONDS") or 90) + 3)
    summary = tick()
    formed = [item for item in summary["formed"] if item["type"] == "commitment_overdue"]
    check("a dismissal drops the single candidate below the act threshold",
          code == 0 and formed == [] and summary.get("below_threshold", 0) >= 1,
          f"rate rc={code}/{code2}, formed={[(i['type'], i['decision']) for i in summary['formed']]}, "
          f"below_threshold={summary.get('below_threshold')}")


def main() -> int:
    assert api("GET", "/v1/mind/state")[0] == 200, "the sidecar does not serve the mind routes"
    intention_id = loop_end_to_end()
    ask_path()
    expiry_and_unrelated_work()
    digest_verbatim()
    off_switch_without_model()
    dismissal_lowers_the_multiplier(intention_id)
    failed = [name for name, ok, _ in RESULTS if not ok]
    log(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} acceptance items passed")
    (WORK / "acceptance.json").write_text(json.dumps([{"item": n, "pass": ok, "detail": d} for n, ok, d in RESULTS], indent=1))
    return 1 if failed else 0


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(2))
    raise SystemExit(main())
