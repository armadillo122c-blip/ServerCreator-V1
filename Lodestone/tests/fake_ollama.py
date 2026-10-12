"""A fake Ollama that speaks the real wire format, for the tests.

It serves /api/version, /api/tags, /api/show, /api/ps and a streaming
/api/chat that answers from a script: each queued turn is either a reply
(text, tool calls, or both) or a function of the request. Every request body
is kept, so a test can check exactly what was sent.
"""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LLAMA_TEMPLATE_WITH_TOOLS = "{{ if .Tools }}tools{{ end }}{{ .Prompt }}"


class FakeOllama:
    def __init__(self):
        self.models = {}          # name -> /api/show body
        self.turns = []           # queued replies for /api/chat
        self.requests = []        # every /api/chat body, in order
        self.loaded = []          # /api/ps entries
        self.version = "0.12.3"
        self.delay = 0.0          # seconds between streamed chunks
        self.hold = None          # threading.Event: block before answering until set
        self.disconnects = 0      # streams the client hung up on
        self.lock = threading.Lock()
        fake = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return json.loads(self.rfile.read(n).decode() or "{}") if n else {}

            def do_GET(self):
                if self.path == "/api/version":
                    return self._send(200, {"version": fake.version})
                if self.path == "/api/tags":
                    return self._send(200, {"models": [
                        {"name": n, "model": n, "size": 4_000_000_000, "modified_at": "2026-10-01T00:00:00Z",
                         "details": (s.get("details") or {})} for n, s in fake.models.items()]})
                if self.path == "/api/ps":
                    return self._send(200, {"models": fake.loaded})
                return self._send(404, {"error": "not found"})

            def do_POST(self):
                body = self._body()
                if self.path == "/api/show":
                    name = body.get("model") or body.get("name")
                    if name not in fake.models:
                        return self._send(404, {"error": "model '%s' not found" % name})
                    return self._send(200, fake.models[name])
                if self.path == "/api/chat":
                    with fake.lock:
                        fake.requests.append(body)
                    if body.get("model") not in fake.models:
                        return self._send(404, {"error": "model '%s' not found" % body.get("model")})
                    if not body.get("messages"):
                        # load or unload
                        return self._send(200, {"model": body["model"], "done": True,
                                                "done_reason": "unload" if body.get("keep_alive") == 0 else "load",
                                                "message": {"role": "assistant", "content": ""}})
                    with fake.lock:
                        turn = fake.turns.pop(0) if fake.turns else {"content": "(no scripted reply)"}
                    if callable(turn):
                        turn = turn(body)
                    if fake.hold is not None:
                        fake.hold.wait(30)
                    stream = body.get("stream", True)
                    content = turn.get("content", "")
                    calls = [{"function": {"name": n, "arguments": a}} for n, a in turn.get("tool_calls", [])]
                    if not stream:
                        return self._send(200, {"model": body["model"], "done": True, "done_reason": "stop",
                                                "message": {"role": "assistant", "content": content,
                                                            **({"tool_calls": calls} if calls else {})},
                                                "prompt_eval_count": 11, "eval_count": 7})
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.send_header("Transfer-Encoding", "chunked")
                    self.end_headers()

                    def chunk(obj):
                        data = (json.dumps(obj) + "\n").encode()
                        self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                        self.wfile.flush()

                    try:
                        if turn.get("thinking"):
                            chunk({"model": body["model"], "done": False,
                                   "message": {"role": "assistant", "content": "", "thinking": turn["thinking"]}})
                        pieces = [content[i:i + 7] for i in range(0, len(content), 7)] or []
                        for p in pieces:
                            if fake.delay:
                                time.sleep(fake.delay)
                            chunk({"model": body["model"], "done": False,
                                   "message": {"role": "assistant", "content": p}})
                        if calls:
                            chunk({"model": body["model"], "done": False,
                                   "message": {"role": "assistant", "content": "", "tool_calls": calls}})
                        if turn.get("error"):
                            chunk({"error": turn["error"]})
                        chunk({"model": body["model"], "done": True, "done_reason": "stop",
                               "message": {"role": "assistant", "content": ""},
                               "prompt_eval_count": 120, "eval_count": 30,
                               "load_duration": 5_000_000, "total_duration": 90_000_000})
                        self.wfile.write(b"0\r\n\r\n")
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        with fake.lock:
                            fake.disconnects += 1
                    return None
                return self._send(404, {"error": "not found"})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.base = "http://127.0.0.1:%d" % self.port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def add_model(self, name, tools=True, ctx=32768, reported=True, thinking=False):
        show = {"details": {"family": "qwen2", "parameter_size": "7.6B", "quantization_level": "Q4_K_M"},
                "model_info": {"general.architecture": "qwen2", "qwen2.context_length": ctx,
                               "qwen2.block_count": 28, "qwen2.attention.head_count": 28,
                               "qwen2.attention.head_count_kv": 4, "qwen2.embedding_length": 3584},
                "parameters": "temperature 0.7",
                "template": LLAMA_TEMPLATE_WITH_TOOLS if tools else "{{ .Prompt }}"}
        if reported:
            caps = ["completion"] + (["tools"] if tools else []) + (["thinking"] if thinking else [])
            show["capabilities"] = caps
        self.models[name] = show

    def say(self, content="", tool_calls=(), thinking="", error=""):
        self.turns.append({"content": content, "tool_calls": list(tool_calls),
                           "thinking": thinking, "error": error})

    def close(self):
        self.httpd.shutdown()
