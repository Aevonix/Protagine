"""Reminders live on stock cron; the plugin keeps only the job id."""

from conftest import probe

REMINDER_CODE = '''
from tools.registry import registry
def owner(session="owner-1"):
    invoke_hook("pre_llm_call", session_id=session, task_id="t", turn_id="turn", user_message="remind me",
                conversation_history=[], is_first_turn=True, model="m", platform="cli",
                parent_session_id="", sender_id="")
    return session
def guest(session="guest-1"):
    invoke_hook("pre_llm_call", session_id=session, task_id="t", turn_id="turn", user_message="remind me",
                conversation_history=[], is_first_turn=True, model="m", platform="telegram",
                parent_session_id="", sender_id="2002")
    return session
def call(args, session):
    return json.loads(registry.dispatch("protagine_reminder", args, task_id="t", session_id=session))
SCHEDULE = {"operation": "schedule", "source_id": "src-1", "source_version": "a" * 64, "claim_id": "c-1",
            "lead_seconds": 600}
'''


def test_schedule_inspect_cancel_on_stock_cron(home, sidecar):
    result = probe(REMINDER_CODE + '''
o = owner()
scheduled = call(SCHEDULE, o)
inspected = call({"operation": "inspect", "job_id": scheduled.get("job_id", "")}, o)
cancelled = call({"operation": "cancel", "job_id": scheduled.get("job_id", "")}, o)
from cron import jobs
stored = jobs.get_job(scheduled.get("job_id", ""))
emit(scheduled=scheduled, inspected=inspected, cancelled=cancelled,
     stored={k: stored.get(k) for k in ("name", "prompt", "repeat", "deliver", "state", "enabled")} if stored else None)
''', home)
    scheduled = result["scheduled"]
    assert scheduled["scheduling_confirmed"] is True and scheduled["job_id"]
    assert scheduled["run_at"] < scheduled["deadline_at"]
    assert result["inspected"]["job_id"] == scheduled["job_id"]
    assert result["cancelled"]["state"] == "paused" or result["cancelled"]["enabled"] is False
    assert result["stored"]["name"] == "Remembered deadline"
    assert "the report is due: in two hours" in result["stored"]["prompt"]
    deadline, = sidecar.calls("/v1/host/memory/sources/deadline", "POST")
    assert deadline["json"]["claim_id"] == "c-1" and deadline["json"]["contact_id"] == "p-01"


def test_guest_cannot_schedule_and_arguments_are_validated(home, sidecar):
    result = probe(REMINDER_CODE + '''
g, o = guest(), owner()
emit(guest=call(SCHEDULE, g), missing=call({"operation": "schedule", "source_id": "src-1"}, o),
     unknown=call({"operation": "inspect", "job_id": "nope"}, o))
''', home)
    assert "owner" in result["guest"]["error"]
    assert "required" in result["missing"]["error"]
    assert "unknown reminder" in result["unknown"]["error"]
    assert sidecar.calls("/v1/host/memory/sources/deadline") == []


def test_a_190_launcher_still_renders_its_reminder(home, sidecar):
    """1.9.0 reminders are no-agent script jobs whose launcher runs the installed ``reminders.py``
    with runpy; after the upgrade that file is this one, and the job must keep working."""
    result = probe(REMINDER_CODE + '''
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from cron.scheduler import create_job_with_scheduler_registration
import protagine_hermes.reminders as reminders
binding_id = "ab" * 32
binding = {"kind": "source-deadline", "contact_id": "p-01", "session_id": "legacy-1", "source_id": "src-1",
           "source_version": "a" * 64, "claim_id": "c-9", "timezone_name": "UTC", "lead_seconds": 600,
           "binding_id": binding_id}
script = f"protagine-reminder-{binding_id}.py"
job = create_job_with_scheduler_registration(
    prompt=json.dumps(binding), schedule=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    name="Remembered deadline", script=script, no_agent=True, repeat=1, attach_to_session=True)
launcher = Path(os.environ["HERMES_HOME"]) / "scripts" / script
launcher.parent.mkdir(exist_ok=True)
launcher.write_text("import runpy, sys\\n"
                    f"sys.argv = [{str(launcher)!r}, {binding_id!r}]\\n"
                    f"runpy.run_path({reminders.__file__!r}, run_name='__main__')\\n")
run = subprocess.run([sys.executable, str(launcher)], capture_output=True, text=True, timeout=120)
emit(stdout=run.stdout, stderr=run.stderr[-2000:], code=run.returncode, stored_script=job.get("script"))
''', home)
    assert result["code"] == 0, result["stderr"]
    assert result["stdout"].strip() == "Reminder: the report is due: in two hours"
    deadline, = sidecar.calls("/v1/host/memory/sources/deadline", "POST")
    assert deadline["json"]["claim_id"] == "c-9" and deadline["json"]["session_id"] == "legacy-1"
