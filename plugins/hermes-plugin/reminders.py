"""Source-bound reminders on stock cron; the plugin keeps only the cron job id.

``schedule`` resolves the recalled deadline through the sidecar once, then
creates a one-shot cron job whose prompt carries the reminder text. Stock
cron owns scheduling, delivery and history.

1.9.0 scheduled its reminders as no-agent script jobs whose launcher runs this
file with ``runpy`` (``main`` below), so the imports are absolute.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from typing import Any

from protagine_hermes.capture import SessionMap
from protagine_hermes.client import ProtagineClient, SidecarUnavailable

logger = logging.getLogger(__name__)

ENDPOINT = "/v1/host/memory/sources/deadline"
JOB_NAME = "Remembered deadline"
SCHEMA = {
    "name": "protagine_reminder",
    "description": "Remind the owner of a recalled deadline: schedule by its exact source_id, source_version and "
                   "claim_id, lead_seconds ahead; inspect or cancel by job_id. Unrelated schedules use "
                   "cronjob_manage.",
    "parameters": {"type": "object", "properties": {
        "operation": {"type": "string", "enum": ["schedule", "inspect", "cancel"]},
        "source_id": {"type": "string"}, "source_version": {"type": "string"}, "claim_id": {"type": "string"},
        "job_id": {"type": "string"}, "lead_seconds": {"type": "integer"}},
        "required": ["operation"]}}


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("the deadline has no timezone")
    return parsed.astimezone(timezone.utc)


def _view(job: dict[str, Any]) -> dict[str, Any]:
    return {"job_id": job.get("id"), **{key: job.get(key) for key in (
        "name", "state", "enabled", "next_run_at", "last_status", "deliver")}}


def _text(current: dict[str, Any]) -> str:
    return f"Reminder: {current.get('subject', '')} {current.get('predicate', '')}: {current.get('value', '')}".strip()


class Reminders:
    def __init__(self, client: ProtagineClient, sessions: SessionMap):
        self.client, self.sessions = client, sessions

    def handle(self, args: Any = None, *, session_id: str = "", **_: Any) -> str:
        args = args if isinstance(args, dict) else {}
        try:
            return json.dumps(self._handle(args, session_id))
        except SidecarUnavailable:
            return json.dumps({"error": "the sidecar is unreachable", "scheduling_confirmed": False})
        except Exception as error:
            message = str(error) if isinstance(error, ValueError) else type(error).__name__
            return json.dumps({"error": message, "scheduling_confirmed": False})

    def _handle(self, args: dict[str, Any], session_id: str) -> dict[str, Any]:
        from cron import jobs
        if not self.sessions.is_owner(session_id):
            raise ValueError("a current owner conversation is required")
        operation = args.get("operation")
        if operation in {"inspect", "cancel"}:
            job = jobs.get_job(str(args.get("job_id") or ""))
            if not job or job.get("name") != JOB_NAME:
                raise ValueError("unknown reminder job")
            if operation == "cancel":
                job = jobs.pause_job(job["id"], reason="Cancelled by owner") or job
            return _view(job)
        if operation != "schedule":
            raise ValueError("use schedule, inspect or cancel")
        for key in ("source_id", "source_version", "claim_id"):
            if not str(args.get(key) or "").strip():
                raise ValueError(f"{key} is required to schedule")
        lead = args.get("lead_seconds", 0)
        if type(lead) is not int or not 0 <= lead <= 2592000:
            raise ValueError("lead_seconds must be a nonnegative integer, at most 30 days")
        from hermes_time import get_timezone_name
        contact = self.sessions.contact_id(session_id)
        response = self.client.post(ENDPOINT, timeout=3, json={
            "contact_id": contact, "session_id": session_id, "source_id": args["source_id"],
            "source_version": args["source_version"], "claim_id": args["claim_id"],
            "timezone_name": get_timezone_name() or None})
        response.raise_for_status()
        current = response.json()
        if current.get("status") != "current":
            return {"error": "this source has no current precise deadline", "deadline": current}
        run_at = _instant(current["deadline_at"]) - timedelta(seconds=lead)
        if run_at <= datetime.now(timezone.utc):
            raise ValueError("the reminder time has already passed; choose a smaller lead")
        from cron.scheduler import create_job_with_scheduler_registration
        from tools.cronjob_job_args import _origin_from_env
        job = create_job_with_scheduler_registration(
            prompt=f"Deliver this reminder to the owner word for word, nothing else: {_text(current)}",
            schedule=run_at.isoformat(), name=JOB_NAME, repeat=1, origin=_origin_from_env(),
            attach_to_session=True)
        return {**_view(job), "deadline_at": current["deadline_at"], "run_at": run_at.isoformat(),
                "scheduling_confirmed": True}


def main(binding_id: str) -> None:
    """The 1.9.0 launcher entry: ``<hermes_home>/scripts/protagine-reminder-<id>.py`` runs this
    file with the binding id in ``argv``.

    A 1.9.0 reminder is a stock no-agent script job: its prompt holds the binding (contact,
    session, source, claim, time zone) and stock cron delivers whatever the script prints.
    Resolving the deadline through the sidecar once and printing the reminder keeps every
    job scheduled before the upgrade working; an unresolved deadline prints nothing.
    """
    from cron import jobs
    from protagine_hermes.client import load_settings
    script = f"protagine-reminder-{binding_id}.py"
    job = next((job for job in jobs.list_jobs(include_disabled=True) if job.get("script") == script), None)
    if job is None:
        raise SystemExit(f"no reminder job runs {script}")
    binding = json.loads(job.get("prompt") or "{}")
    response = ProtagineClient(load_settings()).post(ENDPOINT, timeout=3, json={key: binding.get(key) for key in (
        "contact_id", "session_id", "source_id", "source_version", "claim_id", "timezone_name")})
    response.raise_for_status()
    current = response.json()
    if current.get("status") == "current":
        print(_text(current))


if __name__ == "__main__":
    import sys
    main(sys.argv[1])


__all__ = ["JOB_NAME", "Reminders", "SCHEMA", "main"]
