"""The outbox: messages the body sends verbatim, ask notices and the daily digest.

Architecture 6.2 and 6.3. A message intention that authority approved waits
here as ``approved``. The plugin marks it ``sending`` before it sends and
``sent`` after; a message still ``sending`` when the sidecar starts is
``uncertain`` and is never resent. Ask notices are batched to one per 4 h;
the digest goes out once a day.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from protagine.initiatives.models import StoredInitiative

from .audit import NOTICE_TYPES, _clip, ask_line, outgoing_text
from protagine.util.temporal import now_utc

logger = logging.getLogger(__name__)

ASK_NOTICE_INTERVAL = timedelta(hours=4)
# A message the body claimed but never settled (it stopped between ``sending``
# and ``sent`` before its own ledger saw the send) is uncertain after this long.
STALE_SENDING = timedelta(minutes=10)


def _context(row: StoredInitiative) -> Dict[str, Any]:
    return row.context if isinstance(row.context, dict) else {}


def message_payload(row: StoredInitiative, *, owner_id: str | None) -> Dict[str, Any]:
    """What the body needs to send one message verbatim."""
    context = _context(row)
    recipient = row.entity_id or owner_id
    return {
        "id": row.id,
        "kind": "notice" if row.type in NOTICE_TYPES or (recipient and recipient == owner_id) else "message",
        "type": row.type,
        "dedup_key": f"mind:{row.id}",
        "recipient": recipient,
        "recipient_is_owner": bool(owner_id) and recipient == owner_id,
        "recipient_handles": list(context.get("recipient_handles") or []),
        "text": outgoing_text(row),
        "title": row.description,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }


class Outbox:
    def __init__(self, store: Any, *, owner_id: str | None, clock=None) -> None:
        self.store = store
        self.owner_id = owner_id
        self.clock = clock or (lambda: now_utc())
        self.on_sent = None   # callable(row) set by the tick: a sent message may settle what it was for

    # -- the queue --------------------------------------------------------------

    def ready(self, *, enabled: bool = True, quiet: bool = False) -> List[Dict[str, Any]]:
        """Messages the body may send now; nothing while the mind is off.

        During quiet hours only the digest goes out (it is scheduled outside
        them anyway); everything else waits for the next tick. A message
        unsent past its window is the mind's to expire (``Mind._expire_messages``,
        before every pull and tick), through the same settle path as a task.
        """
        if not enabled:
            return []
        now = self.clock()
        for row in self.store.intentions(status=["sending"], kind=["message"], limit=200):
            claimed = next((item.timestamp for item in self.store.get_history(row.id, limit=50)
                            if item.action == "sending"), None)
            if claimed is not None and claimed.tzinfo is None:
                claimed = claimed.replace(tzinfo=timezone.utc)
            if claimed is None or now - claimed >= STALE_SENDING:
                self.store.transition(row.id, "uncertain", action="uncertain", outcome="uncertain", verified="none",
                                      details={"reason": "no sent report after sending"}, at=now)
        rows = self.store.intentions(status=["approved"], kind=["message"], limit=200)
        ready = []
        for row in sorted(rows, key=lambda item: item.created_at):
            if quiet and row.type != "digest":
                continue
            ready.append(message_payload(row, owner_id=self.owner_id))
        return ready

    def sending(self, intention_id: str, *, target: str | None = None) -> Optional[StoredInitiative]:
        """The body's claim before it sends: only a ready (``approved``) message, and only for a
        target the body resolved. A claim that names no target is not a claim: the body found no
        handle to send to, so the message stays ready (noted once as ``unroutable``) and is offered
        again at the next pull with the recipient's handles read afresh. Only a delivery the body
        attempted can end ``failed``."""
        row = self.store.get(intention_id)
        if row is None or row.kind != "message" or row.status not in {"approved", "sending"}:
            return None
        if row.status == "sending":
            return row
        now = self.clock()
        if not str(target or "").strip():
            # Noted once per row, not once per pull: a message that gains a target moves on to
            # ``sending`` and never comes back here.
            if not any(item.action == "unroutable" for item in self.store.get_history(intention_id, limit=100)):
                self.store.transition(intention_id, "approved", action="unroutable", at=now,
                                      details={"reason": "the body has no handle to send this to; it stays ready"})
            return None
        return self.store.transition(intention_id, "sending", action="sending", hermes_kind="message",
                                     details={"target": target}, at=now)

    def sent(self, intention_id: str, *, result: str = "sent", hermes_ref: str | None = None,
             summary: str = "", error: str | None = None) -> Optional[StoredInitiative]:
        """``sent`` | ``failed`` | ``uncertain`` from the body; anything else is uncertain."""
        row = self.store.get(intention_id)
        if row is None or row.kind != "message":
            return None
        if row.status in {"sent", "uncertain", "failed"}:
            return row
        now = self.clock()
        result = str(result or "uncertain").lower()
        note = summary or error or None
        if result in {"sent", "ok", "done", "delivered"}:
            updated = self.store.transition(intention_id, "sent", action="sent", outcome="done", verified="none",
                                            hermes_kind="message", hermes_ref=hermes_ref or row.hermes_ref,
                                            result=note, completed_at=now, at=now)
            if callable(self.on_sent) and updated is not None:
                try:
                    self.on_sent(updated)
                except Exception as error:
                    logger.warning("sent hook failed for %s (%s)", intention_id, type(error).__name__)
            return updated
        if result in {"failed", "error"}:
            return self.store.transition(intention_id, "failed", action="send_failed", outcome="failed",
                                         verified="hermes_failure", hermes_kind="message",
                                         hermes_ref=hermes_ref or row.hermes_ref, result=note,
                                         failed_at=now, failed_reason=note or "send failed", at=now)
        return self.store.transition(intention_id, "uncertain", action="uncertain", outcome="uncertain",
                                     verified="none", hermes_kind="message", hermes_ref=hermes_ref or row.hermes_ref,
                                     result=note, details={"reason": error} if error else None, at=now)

    def recover(self) -> int:
        """At startup: a message left in ``sending`` is uncertain, never resent."""
        count = 0
        for row in self.store.intentions(status=["sending"], kind=["message"], limit=500):
            self.store.transition(row.id, "uncertain", action="uncertain", outcome="uncertain", verified="none",
                                  details={"reason": "sidecar restarted while sending"}, at=self.clock())
            count += 1
        return count

    def cancel_unsent(self, reason: str) -> int:
        """The off switch: unsent entries are cancelled at once."""
        count = 0
        for row in self.store.intentions(status=["approved"], kind=["message"], limit=500):
            self.store.transition(row.id, "cancelled", action="cancelled", outcome="cancelled",
                                  cancelled_at=self.clock(), cancelled_reason=reason, at=self.clock())
            count += 1
        return count

    # -- owner notices ----------------------------------------------------------------

    def _owner_message(self, *, type: str, title: str, text: str, dedup_key: str | None,
                       expires_in: timedelta = timedelta(days=2)) -> Optional[StoredInitiative]:
        if not self.owner_id:
            logger.warning("no owner contact configured; %s notice not queued", type)
            return None
        now = self.clock()
        row, outcome = self.store.create_intention(
            kind="message", type=type, title=title, drive="upkeep", cls="owner", decision="act",
            decision_reason="owner notice", status="approved", dedup_key=dedup_key, recipient=self.owner_id,
            context={"text": text, "concern": title}, expires_at=now + expires_in, hermes_kind="message",
            created_at=now)
        if outcome == "created":
            self.store.transition(row.id, "approved", action="queued", at=now)
        return row if outcome == "created" else None

    def ask_notice_due(self, now: datetime | None = None) -> bool:
        now = now or self.clock()
        last = self.store.last_transition_at("queued", type="ask_notice")
        return last is None or now - last >= ASK_NOTICE_INTERVAL

    def notify_asks(self, asks: List[StoredInitiative], *, force: bool = False) -> Optional[StoredInitiative]:
        """One notice listing every open ask with its code, at most every 4 h.

        An ask decided with ``notice: false`` (an ordinary suggest-level ask)
        waits for the daily digest; only floor asks are noticed at once.
        """
        asks = [row for row in asks if row.status == "asked" and row.ask_code
                and (force or _context(row).get("notice") is not False)]
        if not asks or (not force and not self.ask_notice_due()):
            return None
        lines = ["I need your say on these before I act:"]
        for row in asks:
            lines.append(f"- {ask_line(row, reason=True)}")
        lines.append("Reply 'yes <code>' or 'no <code>'. Silence lets them expire.")
        stamp = self.clock().strftime("%Y%m%d%H%M")
        return self._owner_message(type="ask_notice", title=f"{len(asks)} open ask(s)", text="\n".join(lines),
                                   dedup_key=f"ask_notice:{stamp}")

    def notice(self, *, type: str, title: str, text: str, dedup_key: str | None) -> Optional[StoredInitiative]:
        return self._owner_message(type=type, title=title, text=text, dedup_key=dedup_key)

    # -- the daily digest ------------------------------------------------------------------

    def digest_sent_today(self, local_date: str) -> bool:
        return self.store.get_by_dedup_key(f"digest:{local_date}") is not None

    def build_digest(self, *, since: datetime, level: str, breaker_states: List[Dict[str, Any]] | None = None,
                     suggestions: List[StoredInitiative] | None = None, goals: List[str] | None = None,
                     opt_outs: List[str] | None = None, found: List[str] | None = None,
                     offers: List[str] | None = None, paused: str | None = None) -> str:
        """The day's digest. Owner outreach (architecture 4.10) adds what the mind found that bears on the
        owner's interests but did not interrupt them for (``found``), the offers of help that went unsent or
        were put off (``offers``) and, while check-ins are paused, how to resume them (``paused``)."""
        rows = self.store.intentions(since=since, limit=500)
        rows = [row for row in rows if row.type not in NOTICE_TYPES]
        acted = [row for row in rows if row.decision == "act" and row.kind in {"task", "message", "goal"}]
        asks = [row for row in self.store.intentions(status=["asked"], limit=200)]
        uncertain = [row for row in rows if row.status == "uncertain"]
        failed = [row for row in rows if row.outcome == "failed"]
        lines = ["Daily digest"]
        if acted:
            lines.append(f"Acted ({len(acted)}):")
            for row in acted[:12]:
                lines.append(f"- {_clip(row.description, 120)}: {row.outcome or row.status}"
                             + (f" ({row.hermes_ref})" if row.hermes_ref else ""))
        if failed:
            lines.append(f"Failed ({len(failed)}):")
            for row in failed[:6]:
                lines.append(f"- {_clip(row.description, 120)}: {_clip(row.failed_reason, 100)}")
        if asks:
            heading = "Waiting for you" if level != "suggest" else "Suggested (reply 'yes <code>' to do one)"
            lines.append(f"{heading} ({len(asks)}):")
            for row in asks[:12]:
                lines.append(f"- {ask_line(row, limit=120)}")
        for row in suggestions or []:
            lines.append(f"- suggestion: {_clip(row.description, 120)}")
        if found:
            lines.append(f"Found for you ({len(found)}):")
            lines += [f"- {_clip(item, 200)}" for item in found[:6]]
        if offers:
            lines.append(f"Offers ({len(offers)}):")
            lines += [f"- {_clip(item, 160)}" for item in offers[:6]]
        if paused:
            lines.append(paused)
        if goals:
            lines.append(f"Goals I am pursuing ({len(goals)}):")
            lines += [f"- {_clip(item, 140)}" for item in goals[:4]]
        if opt_outs:
            lines.append(f"Opted out ({len(opt_outs)}), no longer messaged:")
            lines += [f"- {_clip(item, 140)}" for item in opt_outs[:12]]
        if uncertain:
            lines.append(f"Delivery uncertain ({len(uncertain)}), not resent:")
            for row in uncertain[:6]:
                lines.append(f"- {_clip(row.description, 120)}")
        for state in breaker_states or []:
            if state.get("tripped"):
                lines.append(f"Breaker: {state['cls']} demoted to ask after {state['failures']} failures, until {state['until']}")
        if len(lines) == 1:
            lines.append("Nothing to report.")
        return "\n".join(lines)

    def queue_digest(self, *, local_date: str, text: str) -> Optional[StoredInitiative]:
        return self._owner_message(type="digest", title=f"Daily digest {local_date}", text=text,
                                   dedup_key=f"digest:{local_date}", expires_in=timedelta(hours=20))


__all__ = ["ASK_NOTICE_INTERVAL", "NOTICE_TYPES", "STALE_SENDING", "Outbox", "message_payload"]
