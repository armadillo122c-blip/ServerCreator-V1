"""The Ollama client against a fake that speaks the real wire format:
capabilities, options, streaming, tool calls, errors and cancelling."""
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import ollama as ol
from fake_ollama import FakeOllama

fails = []


def check(n, c, e=""):
    print(("  ok   " if c else "  FAIL ") + n + (("  " + str(e)[:230]) if not c else ""))
    if not c:
        fails.append(n)


def section(t):
    print("\n-- " + t)


fake = FakeOllama()
fake.add_model("qwen2.5-coder:7b", tools=True, ctx=32768)
fake.add_model("phi:2.7b", tools=False, ctx=2048)
fake.add_model("old-template:1b", tools=True, reported=False)
fake.add_model("old-plain:1b", tools=False, reported=False)
c = ol.Ollama(fake.base)

# ---------------------------------------------------------------------------
section("what is installed")
check("version", c.version() == "0.12.3")
names = [t["name"] for t in c.tags()]
check("tags lists every model", set(names) == set(fake.models), names)
check("ps is empty when nothing is loaded", c.ps() == [])
fake.loaded = [{"name": "qwen2.5-coder:7b", "size": 6_000_000_000, "size_vram": 4_000_000_000,
                "expires_at": "2026-10-12T21:00:00Z", "context_length": 8192,
                "details": {"parameter_size": "7.6B", "quantization_level": "Q4_K_M"}}]
ps = c.ps()
check("ps reports what is in VRAM", ps and ps[0]["size_vram"] == 4_000_000_000 and ps[0]["context_length"] == 8192, ps)

section("capabilities")
d = c.capabilities("qwen2.5-coder:7b")
check("tools reported by Ollama are believed", d["tools"] is True and d["source"] == "reported", d)
check("context length comes from model_info", d["context_length"] == 32768, d)
d2 = c.capabilities("phi:2.7b")
check("a model without tools says so", d2["tools"] is False and d2["source"] == "reported", d2)
d3 = c.capabilities("old-template:1b")
check("older Ollama: tools inferred from a template that uses .Tools",
      d3["tools"] is True and d3["source"] == "template", d3)
d4 = c.capabilities("old-plain:1b")
check("older Ollama: no .Tools in the template means no tools", d4["tools"] is False, d4)
check("an empty show says nobody knows", ol.describe_model("x", {})["tools"] is None)
try:
    c.capabilities("nope:1b")
    check("a missing model is an error", False)
except ol.OllamaError as e:
    check("a missing model says to pull it", "pull" in str(e), e)

section("sizing the context")
kv = ol.kv_cache_bytes(d, 8192)
# 28 layers * 4 kv heads * (128 + 128) * 2 bytes * 8192 = 469,762,048
check("the KV cache estimate follows the model's own shape", kv == 28 * 4 * 256 * 2 * 8192, kv)
check("parallel requests multiply it", ol.kv_cache_bytes(d, 8192, parallel=2) == 2 * kv)
check("a model that does not say gives no estimate", ol.kv_cache_bytes({"kv": {}}, 8192) is None)
check("the recommendation never exceeds what the model was trained for",
      ol.recommend_ctx(d2, want=8192) == 2048, ol.recommend_ctx(d2))
check("plenty of memory: the coding default", ol.recommend_ctx(d, free_bytes=32 << 30) == 8192)
small = ol.recommend_ctx(d, free_bytes=600 << 20)
check("little memory: a smaller context", 2048 <= small < 8192, small)

section("options are only what was set")
o = ol.ModelOptions({"num_ctx": "8192", "temperature": 0.2, "num_thread": "", "num_gpu": "auto",
                     "keep_alive": "30m"})
body = o.apply({})
check("num_ctx and temperature are sent", body.get("options") == {"num_ctx": 8192, "temperature": 0.2}, body)
check("blank and auto are left to Ollama", "num_thread" not in body["options"] and "num_gpu" not in body["options"])
check("keep_alive is passed through", body.get("keep_alive") == "30m")
check("nothing set sends nothing", ol.ModelOptions({}).apply({}) == {})

section("streaming a reply")
fake.say("Hello from the fake model, streamed in pieces.")
deltas = []
r = c.chat("qwen2.5-coder:7b", [{"role": "user", "content": "hi"}], opts=o, on_delta=deltas.append)
check("the whole reply arrives", r["content"] == "Hello from the fake model, streamed in pieces.", r)
check("in more than one piece", len(deltas) > 3, deltas)
check("usage is read from the final chunk", r["usage"]["in"] == 120 and r["usage"]["out"] == 30, r["usage"])
sent = fake.requests[-1]
check("stream is on", sent.get("stream") is True)
check("the options went with it", sent.get("options", {}).get("num_ctx") == 8192, sent)

section("tool calls")
fake.say("", tool_calls=[("read_file", {"path": "plugin.yml"}), ("search", '{"query": "onEnable"}')])
tools = [{"type": "function", "function": {"name": "read_file", "description": "x",
                                           "parameters": {"type": "object", "properties": {}}}}]
r = c.chat("qwen2.5-coder:7b", [{"role": "user", "content": "look"}], tools=tools)
check("both calls come back", [t["name"] for t in r["tool_calls"]] == ["read_file", "search"], r)
check("object arguments stay a dict", r["tool_calls"][0]["arguments"] == {"path": "plugin.yml"})
check("string arguments are parsed into one", r["tool_calls"][1]["arguments"] == {"query": "onEnable"})
check("the tools were sent", fake.requests[-1].get("tools") == tools)
check("a broken argument string is kept, not dropped",
      ol.normalise_call({"function": {"name": "x", "arguments": "{oops"}})["arguments"] == {"_raw": "{oops"})

section("thinking")
fake.say("answer", thinking="let me think")
seen = []
r = c.chat("qwen2.5-coder:7b", [{"role": "user", "content": "q"}], think=True, on_thinking=seen.append)
check("thinking is collected separately", r["thinking"] == "let me think" and seen == ["let me think"], r)
check("think was sent", fake.requests[-1].get("think") is True)

section("errors")
fake.say("partial", error="model ran out of memory")
try:
    c.chat("qwen2.5-coder:7b", [{"role": "user", "content": "q"}])
    check("an error chunk raises", False)
except ol.OllamaError as e:
    check("an error chunk raises with Ollama's words", "out of memory" in str(e), e)
try:
    c.chat("nope:1b", [{"role": "user", "content": "q"}])
    check("an unknown model raises", False)
except ol.OllamaError as e:
    check("an unknown model says to pull it", "pull" in str(e), e)
dead = ol.Ollama("http://127.0.0.1:1")
try:
    dead.version()
    check("a dead server raises", False)
except ol.OllamaError as e:
    check("a dead server says how to start Ollama", "ollama serve" in str(e), e)
check("wait_ready gives up on a dead server", ol.wait_ready(dead, 0.6) is False)

section("cancelling mid-stream")
fake.delay = 0.05
fake.say("x" * 700)
tok = ol.CancelToken()
got = []
threading.Timer(0.3, tok.cancel, args=("Stopped by you.",)).start()
t0 = time.time()
try:
    c.chat("qwen2.5-coder:7b", [{"role": "user", "content": "long"}], on_delta=got.append, cancel=tok)
    check("cancel raises Cancelled", False)
except ol.Cancelled as e:
    check("cancel raises Cancelled with the reason", "Stopped by you" in str(e), e)
check("and it stops promptly", time.time() - t0 < 2.0, time.time() - t0)
check("having streamed only part", 0 < len("".join(got)) < 700, len("".join(got)))
fake.delay = 0

section("cancelling while the model is still reading the prompt")
fake.hold = threading.Event()
fake.say("never")
tok = ol.CancelToken()
threading.Timer(0.3, tok.cancel).start()
t0 = time.time()
try:
    c.chat("qwen2.5-coder:7b", [{"role": "user", "content": "slow"}], cancel=tok)
    check("cancel during a silent wait raises", False)
except ol.Cancelled:
    check("cancel during a silent wait raises", True)
check("without waiting for the first token", time.time() - t0 < 2.0, time.time() - t0)
fake.hold.set()
fake.hold = None
tok2 = ol.CancelToken()
tok2.cancel()
try:
    c.chat("qwen2.5-coder:7b", [{"role": "user", "content": "x"}], cancel=tok2)
    check("an already-cancelled token never sends", False)
except ol.Cancelled:
    check("an already-cancelled token never sends", True)
ran = []
tok3 = ol.CancelToken()
tok3.cancel()
tok3.on_cancel(lambda: ran.append(1))
check("a closer registered after the cancel runs at once", ran == [1])

section("loading and unloading")
c.unload("qwen2.5-coder:7b")
check("unload sends keep_alive 0 and no messages",
      fake.requests[-1] == {"model": "qwen2.5-coder:7b", "messages": [], "keep_alive": 0}, fake.requests[-1])
c.preload("qwen2.5-coder:7b", ol.ModelOptions({"num_ctx": 8192, "keep_alive": "30m"}))
check("preload uses the session's options, so the real request does not reload",
      fake.requests[-1].get("options") == {"num_ctx": 8192} and fake.requests[-1].get("keep_alive") == "30m",
      fake.requests[-1])

fake.close()
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
