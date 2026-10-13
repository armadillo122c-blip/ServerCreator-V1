"""Talking to a local Ollama server, and knowing what each model can do.

Everything here follows Ollama's own API reference (docs/api.md in
github.com/ollama/ollama, mirrored at https://docs.ollama.com/api):

* ``GET  /api/version``  - is it running, and which release
* ``GET  /api/tags``     - the models that are installed
* ``POST /api/show``     - one model: ``capabilities``, ``model_info``, template
* ``GET  /api/ps``       - the models loaded right now, and how much is in VRAM
* ``POST /api/chat``     - a chat turn: streamed NDJSON, with ``tools``
* ``keep_alive: 0`` with no messages unloads a model

Three things shape the design:

**Not every model can call tools.** ``/api/show`` reports a ``capabilities``
list on current Ollama releases; on older ones the chat template is checked
for ``.Tools``, which is what Ollama itself keys tool support off. Either way
the answer can be overridden per model, because a template is only a promise.

**Cancelling has to be immediate.** A model that is still reading a long
prompt sends nothing for a while, so a cancel cannot wait for the next line.
The request runs on a plain ``http.client`` connection whose socket another
thread can shut, which unblocks the read and tells Ollama to stop.

**Changing options reloads the model.** Ollama loads a model for a given
``num_ctx``; a request with a different one unloads and reloads it, which on a
CPU costs tens of seconds. :class:`ModelOptions` builds the options once from
settings so every request a session makes asks for the same thing.

Standard library only.
"""

import http.client
import json
import re
import socket
import threading
import time
from urllib.parse import urlparse

DEFAULT_BASE = "http://localhost:11434"
UA = "Lodestone/1.0 (Server Creator AI)"


class OllamaError(RuntimeError):
    """Something to show the user, in a sentence."""


class Cancelled(Exception):
    """The request was cancelled from another thread."""


class CancelToken:
    """Shared between the thread doing the work and the one that may stop it.

    Anything blocking - an HTTP read, a child process - registers a closer, and
    :meth:`cancel` runs every closer that is registered at that moment.
    """

    def __init__(self):
        self._ev = threading.Event()
        self._lock = threading.Lock()
        self._closers = {}
        self._n = 0
        self.reason = ""

    @property
    def cancelled(self):
        return self._ev.is_set()

    def cancel(self, reason="Cancelled."):
        with self._lock:
            if self._ev.is_set():
                return
            self.reason = reason
            self._ev.set()
            closers = list(self._closers.values())
        for c in closers:
            try:
                c()
            except Exception:
                pass

    def check(self):
        if self._ev.is_set():
            raise Cancelled(self.reason or "Cancelled.")

    def wait(self, seconds):
        """Sleep, but wake as soon as this is cancelled. True if it was."""
        return self._ev.wait(seconds)

    def on_cancel(self, closer):
        """Register a closer; returns a function that unregisters it."""
        with self._lock:
            if self._ev.is_set():
                run_now = True
            else:
                run_now = False
                self._n += 1
                key = self._n
                self._closers[key] = closer
        if run_now:
            try:
                closer()
            except Exception:
                pass
            return lambda: None

        def remove():
            with self._lock:
                self._closers.pop(key, None)
        return remove


# ---------------------------------------------------------------------------
#  options that stay the same for a whole session
# ---------------------------------------------------------------------------

class ModelOptions:
    """The ``options`` and ``keep_alive`` sent with every request.

    Only what the user set is sent. Anything left blank stays Ollama's own
    default, so this never silently changes how a model runs.
    """

    # documented Modelfile parameters, plus num_thread / num_gpu which the
    # runner accepts but the current table no longer lists - advanced, opt-in
    FIELDS = (("num_ctx", int), ("temperature", float), ("top_p", float), ("top_k", int),
              ("num_predict", int), ("seed", int), ("repeat_penalty", float),
              ("num_thread", int), ("num_gpu", int))

    def __init__(self, cfg=None):
        cfg = cfg or {}
        self.options = {}
        for key, cast in self.FIELDS:
            v = cfg.get(key)
            if v in (None, "", "auto"):
                continue
            try:
                self.options[key] = cast(v)
            except (TypeError, ValueError):
                continue
        ka = cfg.get("keep_alive")
        self.keep_alive = None if ka in (None, "", "default") else ka

    def apply(self, body):
        if self.options:
            body["options"] = dict(self.options)
        if self.keep_alive is not None:
            body["keep_alive"] = self.keep_alive
        return body


# ---------------------------------------------------------------------------
#  the client
# ---------------------------------------------------------------------------

class Ollama:
    def __init__(self, base=None, timeout=12, read_timeout=900):
        self.base = (base or DEFAULT_BASE).rstrip("/")
        u = urlparse(self.base if "://" in self.base else "http://" + self.base)
        if u.scheme not in ("http", "https"):
            raise OllamaError("The Ollama address must start with http:// or https://")
        self.https = u.scheme == "https"
        self.host = u.hostname or "localhost"
        self.port = u.port or (443 if self.https else 80)
        self.prefix = (u.path or "").rstrip("/")
        self.timeout = timeout
        self.read_timeout = read_timeout
        self._show_cache = {}
        self._show_lock = threading.Lock()

    # -- plumbing ----------------------------------------------------------
    def _conn(self, timeout):
        cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
        return cls(self.host, self.port, timeout=timeout)

    def _request(self, method, path, body=None, timeout=None):
        conn = self._conn(timeout or self.timeout)
        try:
            data = json.dumps(body).encode() if body is not None else None
            headers = {"User-Agent": UA}
            if data is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, self.prefix + path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
        except (ConnectionRefusedError, socket.gaierror) as e:
            raise OllamaError("Ollama is not running at %s (%s). Start it with 'ollama serve' "
                              "or open the Ollama app." % (self.base, e))
        except socket.timeout:
            raise OllamaError("Ollama at %s did not answer in time." % self.base)
        except OSError as e:
            raise OllamaError("Could not reach Ollama at %s: %s" % (self.base, e))
        finally:
            conn.close()
        return self._decode(resp.status, raw, path)

    def _decode(self, status, raw, path):
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except (ValueError, UnicodeDecodeError):
            data = {}
        if status >= 400:
            msg = data.get("error") if isinstance(data, dict) else ""
            if status == 404 and "not found" in (msg or "").lower():
                raise OllamaError("%s - pull it first with 'ollama pull <name>'." % msg)
            raise OllamaError("Ollama answered %s on %s: %s" % (status, path, msg or "no detail"))
        return data

    # -- what is there -----------------------------------------------------
    def version(self):
        return (self._request("GET", "/api/version") or {}).get("version", "")

    def tags(self):
        out = []
        for m in (self._request("GET", "/api/tags") or {}).get("models", []) or []:
            d = m.get("details") or {}
            out.append({"name": m.get("name") or m.get("model") or "",
                        "size": int(m.get("size") or 0),
                        "modified_at": m.get("modified_at", ""),
                        "family": d.get("family", ""),
                        "parameter_size": d.get("parameter_size", ""),
                        "quantization": d.get("quantization_level", "")})
        return [m for m in out if m["name"]]

    def ps(self):
        out = []
        for m in (self._request("GET", "/api/ps") or {}).get("models", []) or []:
            d = m.get("details") or {}
            out.append({"name": m.get("name") or m.get("model") or "",
                        "size": int(m.get("size") or 0),
                        "size_vram": int(m.get("size_vram") or 0),
                        "expires_at": m.get("expires_at", ""),
                        "context_length": int(m.get("context_length") or 0),
                        "parameter_size": d.get("parameter_size", ""),
                        "quantization": d.get("quantization_level", "")})
        return out

    def show(self, model, fresh=False):
        with self._show_lock:
            if not fresh and model in self._show_cache:
                return self._show_cache[model]
        data = self._request("POST", "/api/show", {"model": model})
        with self._show_lock:
            self._show_cache[model] = data
        return data

    def capabilities(self, model, fresh=False):
        """What this model can do, from what Ollama reports about it."""
        return describe_model(model, self.show(model, fresh))

    # -- loading -----------------------------------------------------------
    def unload(self, model):
        """Ask Ollama to drop a model from memory now."""
        self._request("POST", "/api/chat", {"model": model, "messages": [], "keep_alive": 0},
                      timeout=60)

    def preload(self, model, opts=None):
        """Load a model with the options it will be used with, so the first real
        request does not pay for the load."""
        body = {"model": model, "messages": []}
        (opts or ModelOptions()).apply(body)
        self._request("POST", "/api/chat", body, timeout=self.read_timeout)

    # -- chatting ----------------------------------------------------------
    def chat(self, model, messages, tools=None, opts=None, fmt=None, think=None,
             on_delta=None, on_thinking=None, cancel=None):
        """One streamed chat turn.

        Returns ``{"content", "thinking", "tool_calls", "done_reason", "usage"}``.
        ``tool_calls`` is a list of ``{"name", "arguments"}`` with arguments
        always a dict. Raises :class:`Cancelled` if ``cancel`` fires.
        """
        body = {"model": model, "messages": messages, "stream": True}
        if tools:
            body["tools"] = tools
        if fmt is not None:
            body["format"] = fmt
        if think is not None:
            body["think"] = think
        (opts or ModelOptions()).apply(body)

        if cancel is not None:
            cancel.check()
        conn = self._conn(self.read_timeout)
        closed = {"v": False}

        def close():
            # Only shut the socket: that wakes the reading thread, which then
            # closes the connection itself. Closing it from here races
            # http.client's own reader and fails inside it.
            closed["v"] = True
            try:
                if conn.sock is not None:
                    conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        remove = cancel.on_cancel(close) if cancel is not None else (lambda: None)
        content, thinking, calls = [], [], []
        final = {}
        try:
            try:
                conn.request("POST", self.prefix + "/api/chat", body=json.dumps(body).encode(),
                             headers={"Content-Type": "application/json", "User-Agent": UA})
                resp = conn.getresponse()
            except (ConnectionRefusedError, socket.gaierror) as e:
                if closed["v"] and cancel is not None:
                    raise Cancelled(cancel.reason)
                raise OllamaError("Ollama is not running at %s (%s). Start it with 'ollama serve' "
                                  "or open the Ollama app." % (self.base, e))
            if resp.status >= 400:
                self._decode(resp.status, resp.read(), "/api/chat")
            while True:
                try:
                    line = resp.readline()
                except Exception:
                    if closed["v"] and cancel is not None:
                        raise Cancelled(cancel.reason)
                    raise
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                if chunk.get("error"):
                    raise OllamaError("Ollama: %s" % chunk["error"])
                msg = chunk.get("message") or {}
                if msg.get("thinking"):
                    thinking.append(msg["thinking"])
                    if on_thinking:
                        on_thinking(msg["thinking"])
                if msg.get("content"):
                    content.append(msg["content"])
                    if on_delta:
                        on_delta(msg["content"])
                for tc in msg.get("tool_calls") or []:
                    calls.append(normalise_call(tc))
                if chunk.get("done"):
                    final = chunk
                    break
        except socket.timeout:
            raise OllamaError("Ollama stopped answering for %d seconds. A large model on a "
                              "small machine can take that long - try a smaller model or a "
                              "smaller context." % self.read_timeout)
        except (OSError, http.client.HTTPException) as e:
            if closed["v"] and cancel is not None:
                raise Cancelled(cancel.reason)
            raise OllamaError("The connection to Ollama broke: %s" % e)
        finally:
            remove()
            try:
                conn.close()
            except Exception:
                pass
        if cancel is not None and cancel.cancelled:
            raise Cancelled(cancel.reason)
        return {"content": "".join(content), "thinking": "".join(thinking),
                "tool_calls": [c for c in calls if c["name"]],
                "done_reason": final.get("done_reason", ""),
                "usage": {"in": int(final.get("prompt_eval_count") or 0),
                          "out": int(final.get("eval_count") or 0),
                          "load_ms": int((final.get("load_duration") or 0) / 1e6),
                          "total_ms": int((final.get("total_duration") or 0) / 1e6)}}


def normalise_call(tc):
    """Ollama sends ``{"function": {"name", "arguments": {...}}}``. Some models
    put a JSON string in arguments instead; both come out as a dict."""
    fn = (tc or {}).get("function") or tc or {}
    args = fn.get("arguments")
    if isinstance(args, str):
        try:
            args = json.loads(args) if args.strip() else {}
        except ValueError:
            args = {"_raw": args}
    if not isinstance(args, dict):
        args = {}
    return {"name": str(fn.get("name") or ""), "arguments": args}


# ---------------------------------------------------------------------------
#  reading /api/show
# ---------------------------------------------------------------------------

def describe_model(name, show):
    """Capabilities, context size and the numbers needed to size the KV cache.

    ``tools`` is True or False when Ollama says so, and inferred from the chat
    template otherwise (``source`` says which). None means nobody knows.
    """
    show = show or {}
    caps = show.get("capabilities")
    info = show.get("model_info") or {}
    details = show.get("details") or {}
    arch = info.get("general.architecture") or details.get("family") or ""

    def mi(key):
        v = info.get("%s.%s" % (arch, key)) if arch else None
        return v if isinstance(v, (int, float)) else None

    if isinstance(caps, list):
        caps = [str(c) for c in caps]
        tools, source = "tools" in caps, "reported"
    else:
        tmpl = show.get("template") or ""
        if tmpl:
            tools, source = (".Tools" in tmpl), "template"
        else:
            tools, source = None, "unknown"
        caps = []
        if tools:
            caps.append("tools")

    params = show.get("parameters") or ""
    m = re.search(r"(?m)^\s*num_ctx\s+(\d+)", params)
    layers = mi("block_count")
    heads = mi("attention.head_count")
    kv_heads = mi("attention.head_count_kv") or heads
    emb = mi("embedding_length")
    key_len = mi("attention.key_length")
    val_len = mi("attention.value_length")
    head_dim = key_len or (int(emb // heads) if emb and heads else None)
    return {
        "name": name,
        "capabilities": caps,
        "tools": tools,
        "thinking": "thinking" in caps,
        "vision": "vision" in caps,
        "embedding_only": caps == ["embedding"],
        "source": source,
        "context_length": int(mi("context_length") or 0),
        "modelfile_num_ctx": int(m.group(1)) if m else 0,
        "family": details.get("family", "") or arch,
        "parameter_size": details.get("parameter_size", ""),
        "quantization": details.get("quantization_level", ""),
        "kv": {"layers": int(layers or 0), "kv_heads": int(kv_heads or 0),
               "head_dim": int(head_dim or 0), "value_dim": int(val_len or head_dim or 0)},
    }


def kv_cache_bytes(desc, num_ctx, bytes_per=2, parallel=1):
    """An estimate of the KV cache a context of ``num_ctx`` needs, at f16 by
    default. ``None`` when the model does not say enough to work it out.

    keys and values, per layer, per KV head, per position. It is the part of a
    model's memory that grows with the context, which is why the biggest
    context is not automatically the fastest one.
    """
    kv = (desc or {}).get("kv") or {}
    if not (kv.get("layers") and kv.get("kv_heads") and kv.get("head_dim")):
        return None
    per_tok = kv["layers"] * kv["kv_heads"] * (kv["head_dim"] + (kv.get("value_dim") or kv["head_dim"]))
    return int(per_tok * bytes_per * int(num_ctx) * max(1, int(parallel)))


def recommend_ctx(desc, free_bytes=None, want=8192):
    """A context size for a coding session: enough for tools and a few files,
    no more than the model was trained for, and - when the free memory is
    known - small enough that the cache fits in a third of it."""
    top = int((desc or {}).get("context_length") or 0) or want
    pick = min(want, top)
    if free_bytes:
        while pick > 2048:
            need = kv_cache_bytes(desc, pick)
            if need is None or need <= free_bytes / 3:
                break
            pick //= 2
    return max(2048, pick)


def fmt_bytes(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "%.1f TB" % n


def wait_ready(client, seconds=10):
    """True once Ollama answers /api/version, polling for up to ``seconds``."""
    end = time.time() + seconds
    while True:
        try:
            client.version()
            return True
        except OllamaError:
            if time.time() >= end:
                return False
            time.sleep(0.5)
