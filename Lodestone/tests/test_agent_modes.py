"""How a message is worked on: Research (read-only and the web, a report),
Agent (no approvals for that task - except what only the user may agree to),
a model chosen per message, pictures for models that can see them - and the
Code page's terminal through the engine."""
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
_TESTHOME = tempfile.mkdtemp(prefix="lodestone_modes_")
for k in ("HOME", "USERPROFILE", "LODESTONE_TEST_HOME"):
    os.environ[k] = _TESTHOME

import agent as ag
from fake_ollama import FakeOllama

fails = []


def check(n, c, e=""):
    print(("  ok   " if c else "  FAIL ") + n + (("  " + str(e)[:300]) if not c else ""))
    if not c:
        fails.append(n)


def section(t):
    print("\n-- " + t)


fake = FakeOllama()
fake.add_model("coder:7b", tools=True, ctx=32768)
fake.add_model("eyes:8b", tools=True, ctx=32768, vision=True)
SETTINGS = {"base_url": fake.base, "model": "coder:7b", "num_ctx": 8192, "approval_mode": "ask",
            "shell_enabled": True, "web_enabled": True}
A = ag.Agent(os.path.join(_TESTHOME, "data"), lambda: SETTINGS, save=SETTINGS.update)
WSDIR = tempfile.mkdtemp(dir=_TESTHOME)
open(os.path.join(WSDIR, "notes.md"), "w").write("old\n")
ws = A.add_workspace(WSDIR, "w")


def run_until(run, approve=None, timeout=30):
    seen, seq, end = [], 0, time.time() + timeout
    while time.time() < end:
        for ev in run.since(seq, wait=1.0):
            seq = ev["seq"]
            seen.append(ev)
            if ev["type"] == "approval" and approve:
                A.approve(run.id, ev["call_id"], *approve(ev))
            if ev["type"] == "done":
                return seen
    return seen


def results(evs, name):
    return [e for e in evs if e["type"] == "tool_result" and e["name"] == name]


section("Research mode")
fake.say("", tool_calls=[("read_file", {"path": "notes.md"}), ("write_file", {"path": "notes.md", "content": "new\n"})])
fake.say("## Answer\nThe notes say old. Source: notes.md:1")
run = A.start(ws, "what do the notes say?", mode="research")
evs = run_until(run)
tools = {t["function"]["name"] for t in fake.requests[-1]["tools"]}
check("only reading tools and the web are offered", "read_file" in tools and "fetch_url" in tools and "web_search" in tools
      and not tools & {"write_file", "edit_file", "run_shell", "run_command", "run_build", "server_command",
                       "start_server", "git_commit", "save_skill"}, sorted(tools))
check("a write it tries anyway is refused, and nothing changes", not results(evs, "write_file")[0]["ok"]
      and open(os.path.join(WSDIR, "notes.md")).read() == "old\n")
check("the system prompt asks for a report with sources", "Research mode" in fake.requests[-1]["messages"][0]["content"]
      and "where it came from" in fake.requests[-1]["messages"][0]["content"])
check("the chat is told which mode", any(e["type"] == "mode" and e["mode"] == "research" for e in evs))

section("Agent mode")
fake.say("", tool_calls=[("write_file", {"path": "notes.md", "content": "new\n"}), ("run_shell", {"command": "echo built"})])
fake.say("Done: changed and checked.")
run = A.start(ws, "update the notes and check", mode="agent")
asked = []
evs = run_until(run, approve=lambda ev: (asked.append(ev), ("reject",))[1])
check("nothing waits for approval in this task", not asked and open(os.path.join(WSDIR, "notes.md")).read() == "new\n"
      and "built" in results(evs, "run_shell")[0]["output"], asked)
check("the model is told to carry it through", "Agent mode" in fake.requests[-1]["messages"][0]["content"])
check("the setting itself is untouched", SETTINGS["approval_mode"] == "ask")
fake.say("", tool_calls=[("write_file", {"path": "notes.md", "content": "again\n"})])
fake.say("ok")
run = A.start(ws, "normal again")
asked = []
evs = run_until(run, approve=lambda ev: (asked.append(ev), ("reject",))[1])
check("the next normal message asks again", len(asked) == 1 and open(os.path.join(WSDIR, "notes.md")).read() == "new\n")


class Run:
    mode = "agent"
    always = set()


r = Run()
r.pending, r.cancel = {}, ag.ol.CancelToken()
r.emit = lambda *a, **k: None
r.set_state = lambda *a, **k: None
got = []


def answer():
    time.sleep(0.3)
    for cid, p in list(r.pending.items()):
        p["decision"] = "approve"
        p["ev"].set()
        got.append(cid)


threading.Thread(target=answer, daemon=True).start()
d, _n = A._wait_approval(r, "c1", "server", "accept the EULA", force=True)
check("what only the user may agree to still asks in Agent mode", got == ["c1"] and d == "approve")
try:
    A.start(ws, "x", mode="yolo")
    check("an unknown mode is refused", False)
except ag.AgentError:
    check("an unknown mode is refused", True)

section("a model per message, and pictures")
fake.say("I see a red block.")
run = A.start(ws, "what is in this picture?", attachments=[{"name": "shot.png", "image": "aGVsbG8="}], model="eyes:8b")
evs = run_until(run)
req = fake.requests[-1]
check("the message goes to the model chosen for it", req["model"] == "eyes:8b")
check("a model that sees gets the picture", req["messages"][-1].get("images") == ["aGVsbG8="], req["messages"][-1])
fake.say("I cannot see it.")
run = A.start(ws, "and this one?", attachments=[{"name": "shot.png", "image": "aGVsbG8="}])
evs = run_until(run)
req = fake.requests[-1]
check("the default model otherwise", req["model"] == "coder:7b")
check("a model that cannot see gets no picture, and is told so", "images" not in req["messages"][-1]
      and "cannot see images" in req["messages"][-1]["content"], req["messages"][-1])
check("and so is the user", any(e["type"] == "note" and "cannot see images" in e["text"] for e in evs))

section("the Code page's terminal")
spec = importlib.util.spec_from_file_location("lodestone", "lodestone.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.PANEL.save = lambda *a, **k: True
from http.server import ThreadingHTTPServer
httpd = ThreadingHTTPServer(("127.0.0.1", 0), m.Handler)
httpd.daemon_threads = True
threading.Thread(target=httpd.serve_forever, daemon=True).start()


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (httpd.server_address[1], path), data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def follow(jid, timeout=20):
    end, job = time.time() + timeout, {}
    while time.time() < end:
        s, j = call("GET", "/api/jobs?id=" + jid)
        job = j["job"]
        if job["state"] != "running":
            return job
        time.sleep(0.2)
    return job


TDIR = tempfile.mkdtemp(dir=_TESTHOME)
os.makedirs(os.path.join(TDIR, "sub"))
s, j = call("POST", "/api/agent/workspace/add", {"root": TDIR, "name": "t"})
TW = j["workspace"]["id"]
s, j = call("POST", "/api/dev/terminal", {"ws": TW, "command": "echo hello; pwd"})
check("a command starts as a job", s == 200 and j["job"]["id"] and j["job"]["shell"], j)
job = follow(j["job"]["id"])
check("its output streams back, from the workspace folder", "hello" in job["log"] and
      os.path.realpath(TDIR) in [os.path.realpath(x) for x in job["log"]], job["log"])
s, j = call("POST", "/api/dev/terminal", {"ws": TW, "command": "pwd", "cwd": "sub"})
job = follow(j["job"]["id"])
check("cd into a folder inside it", any(x.endswith("/sub") for x in job["log"]), job["log"])
s, j = call("POST", "/api/dev/terminal", {"ws": TW, "command": "pwd", "cwd": "../.."})
check("but not out of the workspace", s == 400 and "outside" in j.get("error", ""), j)
s, j = call("POST", "/api/dev/terminal", {"ws": TW, "command": "exit 4"})
job = follow(j["job"]["id"])
check("a failing command reports its exit code", job["state"] == "failed" and job["result"]["exit"] == 4, job)
s, j = call("POST", "/api/dev/terminal", {"ws": TW, "command": "sleep 30"})
jid = j["job"]["id"]
time.sleep(0.8)
s, c = call("POST", "/api/dev/build/cancel", {"ws": TW, "job": jid})
job = follow(jid, 10)
check("Stop ends it", c.get("ok") and job["state"] == "failed" and job["result"]["killed"], job)
s, j = call("POST", "/api/dev/terminal", {"ws": TW, "command": "  "})
check("an empty command is refused", s == 400)

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
