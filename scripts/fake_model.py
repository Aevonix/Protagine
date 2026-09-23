"""A scripted OpenAI-compatible model server for the fresh-install recall probe (CI).

Serves ``/v1/models`` and ``/v1/chat/completions`` (streaming and not). Every
request body is appended to ``FAKE_MODEL_LOG`` (JSON lines) so the run can
check what reached the model. The reply is scripted: the assistant repeats
any recall token (``mem-<hex>``) it sees anywhere in the prompt, so a recall
that reached the model shows up in the answer; otherwise it acknowledges.
The sidecar's own extraction, observation and review prompts get the answers a
capable model would give for a "remember this token" message, so a memory forms
without any real model; every other JSON-shaped prompt gets an empty object.
"""
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.environ.get("FAKE_MODEL_LOG", "fake-model.log")
MODEL = os.environ.get("FAKE_MODEL_NAME", "scripted-model")
TOKEN = re.compile(r"mem-[0-9a-f]{8}")


def _text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(str(b.get("text") or "") for b in content if isinstance(b, dict))
    return "" if content is None else str(content)


def reply_for(body):
    messages = body.get("messages") or []
    prompt = "\n".join(_text(m.get("content")) for m in messages if isinstance(m, dict))
    system = "\n".join(_text(m.get("content")) for m in messages if m.get("role") == "system")
    last_user = next((_text(m.get("content")) for m in reversed(messages) if m.get("role") == "user"), "")
    found = sorted(set(TOKEN.findall(prompt)))
    # The sidecar's own extraction prompts: answer them the way a capable model would.
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
    if body.get("response_format") or ("Return only JSON" in system) or ("JSON" in system and "Return" in system):
        return "{}"
    asked = TOKEN.findall(last_user[:300])
    if asked and "remember" in last_user.lower():
        return "Noted. I will remember that: " + asked[0]
    if found:
        return "The token you asked me to remember is " + ", ".join(found[:4]) + "."
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
        return self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            body = {"raw": raw.decode("utf-8", "replace")}
        with open(LOG, "a", encoding="utf-8") as log:
            log.write(json.dumps({"path": self.path, "at": time.time(), "body": body}) + "\n")
        if not self.path.rstrip("/").endswith("/chat/completions"):
            return self._send(404, {"error": "not found"})
        text = reply_for(body)
        ident = "chatcmpl-" + hex(int(time.time() * 1000))[2:]
        if body.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            chunks = [
                {"id": ident, "object": "chat.completion.chunk", "created": int(time.time()), "model": MODEL,
                 "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]},
                {"id": ident, "object": "chat.completion.chunk", "created": int(time.time()), "model": MODEL,
                 "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
            ]
            for chunk in chunks:
                self.wfile.write(b"data: " + json.dumps(chunk).encode() + b"\n\n")
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return None
        return self._send(200, {
            "id": ident, "object": "chat.completion", "created": int(time.time()), "model": MODEL,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"fake model on http://127.0.0.1:{port}/v1 logging to {LOG}", flush=True)
    server.serve_forever()
