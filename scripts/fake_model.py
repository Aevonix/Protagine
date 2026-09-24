"""A scripted OpenAI-compatible model server for the CI probes against stock Hermes.

Serves ``/v1/models`` and ``/v1/chat/completions`` (streaming and not). Every
request body is appended to ``FAKE_MODEL_LOG`` (JSON lines) so a run can check
what reached the model. The replies are scripted:

- the assistant repeats any probe token (``mem-<hex>``, ``rep-<hex>``, ...) it
  sees anywhere in the prompt, so a recall that reached the model shows up in
  the answer; otherwise it acknowledges
- an owner request "<do something> by 3pm" is answered with the assistant's
  own promise ("I'll <do it> by 3pm."), so the promise the sidecar audits is the
  assistant's; an owner's own "I'll ..." is acknowledged
- the sidecar's own extraction, observation, judgment and review prompts get
  the answers a capable model would give for a "remember this token" message,
  so a memory forms without any real model; every other JSON-shaped prompt gets
  an empty object
- the ``commitment_extract`` router task (docs/MIND.md, current contract:
  action, target, listed_due, counterpart, obligor) gets one ``create`` for the
  audited turn's promise, read from the assistant's reply first (``obligor``
  ``assistant``, owed to the ``owner``) and otherwise from the person's own words
  (``obligor`` ``owner``); never for an earlier turn shown as context and never
  for an item already listed as open. It is due ``FAKE_MODEL_DUE_SECONDS`` after
  the turn (default 90 s), so the mind loop can be watched in minutes; anything
  else gets ``[]``
- a kanban worker turn ("work kanban task ...") calls ``kanban_complete`` with
  a summary, then says it is done
- "yes <CODE>" / "no <CODE>" calls ``protagine_self`` with that code; the
  owner's "please approve it" (no code) calls it with the code last posted to
  ``POST /control {"ask_code": ...}``, so the plugin's typed-code rule is what
  refuses it, not the model

Tool calls are only emitted when the request offers that tool.
"""
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.environ.get("FAKE_MODEL_LOG", "fake-model.log")
MODEL = os.environ.get("FAKE_MODEL_NAME", "scripted-model")
DUE_SECONDS = float(os.environ.get("FAKE_MODEL_DUE_SECONDS") or 90)
TOKEN = re.compile(r"\b(?:mem|rep|inv|min|agn|sld)-[0-9a-f]{8}\b")
# The owner's own words start the user message; Hermes appends recalled context after them.
ASK = re.compile(r"^\s*(yes|no)\s+([A-Za-z0-9]{3,8})\b", re.IGNORECASE)
# A promise in one side's words ("I'll send you the report rep-1234 by 3pm") and an owner request the
# scripted assistant answers with a promise of its own ("Send me the report rep-1234 by 3pm.").
PROMISE = re.compile(r"\bI(?:'ll| will)\s+(.+?)(?:\s+by\s+3\s*pm|[.!]|$)", re.IGNORECASE)
REQUEST = re.compile(r"^\s*(?P<what>[^.!?\n]+?)\s+by\s+3\s*pm\b", re.IGNORECASE)
# The commitment_extract prompt (P/commitments/extract.py: build_prompt): the audited turn and the
# numbered open items; earlier turns come before it as "Recent conversation" and are not audited.
TURN = re.compile(r"This turn, verbatim:\n  They said: (?P<said>.*?)\n  Assistant replied: (?P<replied>.*?)"
                  r"(?:\n\s*Already-recorded OPEN items|\n\s*Recently CLOSED items|\s*\Z)", re.DOTALL)
LISTED = re.compile(r"^\[(\d+)\] (.+?) \((?:due \S+|no due)\)$", re.MULTILINE)
CONTROL = {"ask_code": ""}
_LOCK = threading.Lock()


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(b.get("text") or "") for b in content if isinstance(b, dict))
    return "" if content is None else str(content)


def _tool_names(body):
    names = []
    for tool in body.get("tools") or []:
        function = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(function, dict) and function.get("name"):
            names.append(function["name"])
    return names


def _tool_call(name, arguments):
    return {"tool_calls": [{"id": "call-" + hex(int(time.time() * 1000))[2:], "type": "function",
                            "function": {"name": name, "arguments": json.dumps(arguments)}}]}


def _words(text):
    return re.sub(r"[^a-z0-9$]+", " ", text.lower()).split()


def _phrase(text, swaps):
    """A promised phrase as an item description: the pronouns turned to the third person, capitalized."""
    words = [swaps.get(word.lower(), word) for word in text.strip().split()]
    description = " ".join(words).rstrip(".!")
    return description[:1].upper() + description[1:]


def _commitment_reply(body, prompt):
    """The ``commitment_extract`` answer for one audited turn, in the current contract.

    The promise is read from the assistant's reply first (the assistant took the
    work on: obligor ``assistant``, owed to the ``owner``), else from the
    person's own words (obligor ``owner``). Earlier turns shown as context are
    not audited, and a promise already listed as an open item is not recorded
    again (mentioning an open item is not an item).
    """
    turn = TURN.search(prompt)
    said, replied = (turn.group("said"), turn.group("replied")) if turn else (prompt, "")
    listed = [_words(wording) for _, wording in LISTED.findall(prompt)]
    items = []
    for text, obligor, counterpart, swaps in ((replied, "assistant", "owner", {"you": "them", "your": "their"}),
                                             (said, "owner", None, {})):
        promise = PROMISE.search(text)
        if promise is None:
            continue
        description = _phrase(promise.group(1), swaps) or "Do what was promised"
        if _words(description) not in listed:
            due = (datetime.now(timezone.utc) + timedelta(seconds=DUE_SECONDS)).replace(microsecond=0)
            items.append({"action": "create", "target": None, "description": description, "due_at": due.isoformat(),
                          "priority": 70, "source_type": "cognition", "metadata": None, "listed_due": None,
                          "counterpart": counterpart, "obligor": obligor})
        break
    return json.dumps({"items": items} if body.get("response_format") else items)


def reply_for(body):
    """The scripted answer: a string, or a dict with ``tool_calls``."""
    messages = body.get("messages") or []
    prompt = "\n".join(_text(m.get("content")) for m in messages if isinstance(m, dict))
    system = "\n".join(_text(m.get("content")) for m in messages if m.get("role") == "system")
    last_user = next((_text(m.get("content")) for m in reversed(messages) if m.get("role") == "user"), "")
    last_role = messages[-1].get("role") if messages and isinstance(messages[-1], dict) else ""
    tools = _tool_names(body)
    found = sorted(set(TOKEN.findall(prompt)))
    # The sidecar's own prompts: answer them the way a capable model would.
    if "You audit ONE finished assistant turn" in system:
        return _commitment_reply(body, last_user)
    if 'exactly one key, "claims"' in system:
        in_message = TOKEN.findall(last_user[:400])
        if in_message and "remember" in last_user.lower():
            return json.dumps({"claims": [{
                "representation": "assertion", "memory_kind": "personal_context",
                "subject": "this token", "predicate": "token_value", "value": in_message[0],
                "evidence": "Please remember this token for me: " + in_message[0] + ".",
                "recall_reason": "The user may ask for this token again later.",
                "operation": "assert", "prior_claim_id": None,
                "valid_from_text": None, "valid_to_text": None, "event_at_text": None}]})
        return json.dumps({"claims": []})
    if "observations and incident_decisions" in system:
        return json.dumps({"observations": [], "incident_decisions": []})
    if "Return one JSON object keyed by each supplied index" in system:
        try:
            proposals = json.loads(last_user).get("proposals") or []
        except ValueError:
            proposals = []
        return json.dumps({str(p.get("index", i)): {"keep": True, "reason": "The source asks to remember this exact value."}
                           for i, p in enumerate(proposals)})
    if "Use exactly one of these shapes" in system and '{"action":"abstain"}' in system:
        return '{"action":"abstain"}'  # the self-model judgment: nothing here is a durable opinion
    schema = (body.get("response_format") or {}).get("json_schema") or {}
    if schema.get("name") == "session_title":  # Hermes names the session from the opening message
        return json.dumps({"title": " ".join(last_user.split()[:5]).rstrip(".,:;!?") or "New chat"})
    if body.get("response_format") or ("Return only JSON" in system) or ("JSON" in system and "Return" in system):
        return "{}"
    # Hermes turns: a tool result comes back as a tool message; answer it in words.
    if last_role == "tool":
        return "Done."
    if "kanban_complete" in tools and last_user.lower().startswith("work kanban task"):
        return _tool_call("kanban_complete", {"summary": "Done as asked. Evidence: this run's log. It worked."})
    own_words = re.split(r"<memory-context|\n\n<", last_user, maxsplit=1)[0]
    if "protagine_self" in tools:
        ask = ASK.search(own_words)
        if ask:
            return _tool_call("protagine_self", {"operation": ask.group(1).lower(), "code": ask.group(2).upper()})
        if "please approve it" in own_words.lower() and CONTROL["ask_code"]:
            return _tool_call("protagine_self", {"operation": "yes", "code": CONTROL["ask_code"]})
    if PROMISE.search(own_words):
        return "Noted, I have that down."           # the owner's own promise: acknowledged, not repeated
    request = REQUEST.match(own_words)
    if request:                                     # an owner request: the assistant's own promise
        words = request.group("what").split()
        words[0] = words[0].lower()
        return "I'll " + " ".join({"me": "you", "my": "your"}.get(word.lower(), word) for word in words) + " by 3pm."
    asked = TOKEN.findall(last_user[:300])
    if asked and "remember" in last_user.lower():
        return "Noted. I will remember that: " + asked[0]
    if found:
        return "On record: " + ", ".join(found[:4]) + "."
    return "I do not have that on record."


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send(self, code, payload, content_type="application/json"):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/models"):
            return self._send(200, {"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "fake"}]})
        if self.path.rstrip("/").endswith("/control"):
            return self._send(200, dict(CONTROL))
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {"raw": raw.decode("utf-8", "replace")}
        if self.path.rstrip("/").endswith("/control"):
            with _LOCK:
                CONTROL.update({k: str(v) for k, v in body.items() if k in CONTROL})
            return self._send(200, dict(CONTROL))
        with _LOCK, open(LOG, "a", encoding="utf-8") as log:
            log.write(json.dumps({"path": self.path, "at": time.time(), "body": body}) + "\n")
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._send(404, {"error": "not found"})
        reply = reply_for(body)
        ident = "chatcmpl-" + hex(int(time.time() * 1000))[2:]
        text = reply if isinstance(reply, str) else None
        calls = reply["tool_calls"] if isinstance(reply, dict) else None
        finish = "tool_calls" if calls else "stop"
        message = {"role": "assistant", "content": text}
        if calls:
            message["tool_calls"] = calls
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            delta = {"role": "assistant", "content": text or ""}
            if calls:
                delta["tool_calls"] = [{"index": i, **call} for i, call in enumerate(calls)]
            chunks = [
                {"id": ident, "object": "chat.completion.chunk", "created": int(time.time()), "model": MODEL,
                 "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {"id": ident, "object": "chat.completion.chunk", "created": int(time.time()), "model": MODEL,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": finish}], "usage": usage},
            ]
            for chunk in chunks:
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return None
        return self._send(200, {
            "id": ident, "object": "chat.completion", "created": int(time.time()), "model": MODEL,
            "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage,
        })


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"fake model on http://127.0.0.1:{port}/v1 logging to {LOG}", flush=True)
    server.serve_forever()
