"""The development assistant, code tools and performance numbers through the
engine's real HTTP routes - the ones the desktop app uses - against a fake
Ollama and a fake server process that answers like Paper and Skript do."""
import importlib.util
import json
import os
import subprocess
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
_TESTHOME = os.environ.get("LODESTONE_TEST_HOME")
if not _TESTHOME:
    _TESTHOME = tempfile.mkdtemp(prefix="lodestone_testhome_")
    os.environ["LODESTONE_TEST_HOME"] = _TESTHOME
os.environ["HOME"] = _TESTHOME
os.environ["USERPROFILE"] = _TESTHOME
os.environ["HOMEDRIVE"] = os.path.splitdrive(_TESTHOME)[0] or "C:"
os.environ["HOMEPATH"] = os.path.splitdrive(_TESTHOME)[1]

spec = importlib.util.spec_from_file_location("lodestone", "lodestone.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.PANEL.save = lambda *a, **k: True

from fake_ollama import FakeOllama
from http.server import ThreadingHTTPServer

httpd = ThreadingHTTPServer(("127.0.0.1", 0), m.Handler)
httpd.daemon_threads = True
PORT = httpd.server_address[1]
threading.Thread(target=httpd.serve_forever, daemon=True).start()

fails = []


def check(n, c, e=""):
    print(("  ok   " if c else "  FAIL ") + n + (("  " + str(e)[:300]) if not c else ""))
    if not c:
        fails.append(n)


def section(t):
    print("\n-- " + t)


def call(method, path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (PORT, path), data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


def get(path):
    return call("GET", path)


def post(path, body):
    return call("POST", path, body)


fake = FakeOllama()
fake.add_model("qwen2.5-coder:7b", tools=True, ctx=32768)

# a server folder: Paper with Skript
SRV = tempfile.mkdtemp(prefix="srv_", dir=_TESTHOME)
os.makedirs(os.path.join(SRV, "plugins", "Skript", "scripts"))
open(os.path.join(SRV, "server.properties"), "w").write("server-port=25565\npvp=maybe\n")
open(os.path.join(SRV, "plugins", "Skript", "scripts", "shop.sk"), "w").write(
    "command /shop:\n    trigger:\n        sendd \"hi\"\n")
import zipfile
with zipfile.ZipFile(os.path.join(SRV, "plugins", "Skript.jar"), "w") as z:
    z.writestr("plugin.yml", "name: Skript\nversion: 2.9.5\nmain: ch.njol.skript.Skript\n")
srv = m.PANEL.upsert({"kind": "local", "name": "Survival", "local": {"cwd": SRV, "server_type": "paper",
                                                                     "mc_version": "1.21.4", "max_ram": "4G"}})
SID = srv["id"]

section("state and settings")
s, j = get("/api/agent/state")
check("the assistant is available", s == 200 and j["available"], j)
check("defaults are there", j["settings"]["num_ctx"] == 8192 and j["settings"]["perf_mode"] is False, j["settings"])
check("the local server is a workspace", any(w["id"] == "srv-" + SID and w["kind"] == "server" for w in j["workspaces"]),
      j["workspaces"])
s, j = post("/api/agent/settings", {"base_url": fake.base, "model": "qwen2.5-coder:7b", "num_ctx": "16384"})
check("settings save", s == 200 and j["settings"]["num_ctx"] == 16384, j)
s, j = post("/api/agent/settings", {"num_ctx": 10})
check("a context below 512 is refused", s == 400 and "between" in j.get("error", ""), j)
s, j = post("/api/agent/settings", {"base_url": "ftp://x"})
check("a non-http address is refused", s == 400, j)
post("/api/agent/settings", {"base_url": fake.base})
s, j = get("/api/agent/ollama?fresh=1")
check("Ollama is seen with its models", s == 200 and j["running"] and j["models"][0]["name"] == "qwen2.5-coder:7b"
      and j["models"][0]["mode"] == "native", j)
s, j = get("/api/agent/recommend?model=qwen2.5-coder:7b")
check("a context recommendation with the KV cache table", s == 200 and j["recommended"] >= 2048 and j["table"]
      and j["table"][0]["kv"] != "unknown", j)

section("a project workspace")
PROJ = tempfile.mkdtemp(prefix="proj_", dir=_TESTHOME)
os.makedirs(os.path.join(PROJ, "src", "main", "java", "com", "x"))
os.makedirs(os.path.join(PROJ, "src", "main", "resources"))
open(os.path.join(PROJ, "build.gradle"), "w").write("plugins { id 'java' }\ndependencies { implementation 'io.papermc.paper:paper-api:1.21.4-R0.1-SNAPSHOT' }\n")
open(os.path.join(PROJ, "src/main/resources/plugin.yml"), "w").write("name: X\nversion: 1\nmain: com.x.Main\n")
open(os.path.join(PROJ, "src/main/java/com/x/Main.java"), "w").write(
    "package com.x;\npublic class Main {\n  @EventHandler public void j(E e) { Thread.sleep(5); }\n}\n")
s, j = post("/api/agent/workspace/add", {"root": PROJ, "name": "X plugin"})
check("a folder becomes a workspace", s == 200 and j["workspace"]["kind"] == "project", j)
WS = j["workspace"]["id"]
s, j = post("/api/agent/workspace/add", {"root": os.path.join(PROJ, "nope")})
check("a folder that does not exist is refused", s == 400, j)
s, j = post("/api/agent/workspace/remove", {"id": "srv-" + SID})
check("a server's workspace cannot be removed by itself", s == 400, j)

section("the code editor's files")
s, j = get("/api/dev/files?ws=%s&path=src/main/resources" % WS)
check("listing", s == 200 and [i["name"] for i in j["items"]] == ["plugin.yml"], j)
s, j = get("/api/dev/files?ws=%s&path=../" % WS)
check("listing outside is refused", s == 400 and "outside" in j.get("error", ""), j)
s, j = get("/api/dev/file?ws=%s&path=src/main/resources/plugin.yml" % WS)
check("reading", s == 200 and j["content"].startswith("name: X") and j["format"] == "yaml", j)
mt = j["mtime"]
s, j = post("/api/dev/file/save", {"ws": WS, "path": "src/main/resources/plugin.yml",
                                   "content": "name: X\nversion: 1\nmain: com.x.Main\napi-version: '1.21'\n", "mtime": mt})
check("saving returns a check of what was saved", s == 200 and j["check"]["ok"] and j["change"], j)
s, j2 = post("/api/dev/file/save", {"ws": WS, "path": "src/main/resources/plugin.yml", "content": "x", "mtime": mt})
check("saving over a newer file is a conflict", s == 409, j2)
s, j3 = post("/api/agent/revert", {"ws": WS, "change": j["change"]})
check("an editor save can be put back", s == 200 and "api-version" not in open(
    os.path.join(PROJ, "src/main/resources/plugin.yml")).read(), j3)

section("checks")
s, j = post("/api/dev/check", {"name": "config.yml", "text": "a:\n\tb: 1\n"})
check("pasted YAML is checked", s == 200 and not j["check"]["ok"], j)
s, j = post("/api/dev/check", {"ws": "srv-" + SID, "path": "server.properties"})
check("server.properties is checked with Minecraft's types", s == 200 and any(
    "true or false" in p["message"] for p in j["check"]["problems"]), j)
s, j = get("/api/dev/java?ws=" + WS)
check("the Java project is read", s == 200 and j["project"]["kind"] == "gradle", j.get("project", {}).get("kind"))
check("with its problems", any("packed into your jar" in p["message"] for p in j["project"]["problems"]), j["project"]["problems"])
check("and source heuristics", any(p["file"].endswith("Main.java") for p in j["lint"]), j["lint"])
s, j = get("/api/dev/skript?ws=srv-" + SID)
check("Skript's environment is read", s == 200 and j["environment"]["skript"]["version"] == "2.9.5"
      and j["environment"]["scripts"][0]["name"] == "shop.sk", j.get("environment"))
s, j = post("/api/dev/trace", {"text": "java.lang.NoSuchMethodError: 'void a.b()'\n\tat com.x.Main.j(Main.java:3)\n",
                               "ws": WS})
check("a pasted trace is explained with the project's frame", s == 200 and j["traces"][0]["own_frames"]
      and j["traces"][0]["hints"], j)
s, j = get("/api/dev/find?ws=%s&q=main" % WS)
check("finding files by symbol", s == 200 and any(i["path"].endswith("Main.java") for i in j["items"]), j)

section("a build as a job")
gw = os.path.join(PROJ, "gradlew")
open(gw, "w").write("#!/bin/sh\necho compiling\necho \"$PWD/src/main/java/com/x/Main.java:3: error: cannot find symbol\"\n"
                    "echo 'BUILD FAILED'\nexit 1\n")
os.chmod(gw, 0o755)
s, j = post("/api/dev/build", {"ws": WS, "task": "build"})
check("a build starts as a job", s == 200 and j["job"]["kind"] == "build", j)
jid = j["job"]["id"]
for _ in range(40):
    s, jj = get("/api/jobs?id=" + jid)
    if jj["job"]["state"] != "running":
        break
    time.sleep(0.2)
job = jj["job"]
check("it fails with the compiler's error parsed", job["state"] == "failed" and
      job["result"]["build"]["errors"][0]["line"] == 3, job)
check("its log has the output", any("BUILD FAILED" in ln for ln in job["log"]), job["log"])
s, j = get("/api/jobs")
check("the jobs list still serialises", s == 200 and j["items"], j)

section("an assistant run over HTTP")
fake.say("", tool_calls=[("edit_file", {"path": "src/main/resources/plugin.yml", "old_text": "version: 1",
                                        "new_text": "version: 2"})])
fake.say("Bumped the version in `src/main/resources/plugin.yml:2`.")
s, j = post("/api/agent/run", {"ws": WS, "message": "bump the version"})
check("a run starts", s == 200 and j["run"]["state"] in ("queued", "running", "thinking"), j)
RID = j["run"]["id"]
seq, seen = 0, []
deadline = time.time() + 20
while time.time() < deadline:
    s, j = get("/api/agent/events?run=%s&since=%d&wait=5" % (RID, seq))
    for ev in j["events"]:
        seq = ev["seq"]
        seen.append(ev)
        if ev["type"] == "approval":
            post("/api/agent/approve", {"run": RID, "call_id": ev["call_id"], "decision": "approve"})
    if seen and seen[-1]["type"] == "done":
        break
types = [e["type"] for e in seen]
check("events arrive over long polling, approval included", "approval" in types and types[-1] == "done", types)
check("the file changed", "version: 2" in open(os.path.join(PROJ, "src/main/resources/plugin.yml")).read())
s, j = get("/api/agent/history?ws=" + WS)
check("the history has the exchange", s == 200 and j["items"][-1]["content"].startswith("Bumped"), j)
s, j = get("/api/agent/changes?ws=%s&run=%s" % (WS, RID))
check("the run's change is listed", s == 200 and j["items"] and j["items"][0]["path"].endswith("plugin.yml"), j)
s, j = post("/api/agent/memory/add", {"ws": WS, "text": "Version is bumped by hand"})
check("a note can be added by hand", s == 200 and j["items"][-1]["source"] == "you", j)
s, j = post("/api/agent/run", {"ws": WS, "message": ""})
check("an empty message is refused", s == 400, j)

section("performance numbers")
# a fake server process that answers like Paper and Skript
FAKE_SRV = os.path.join(_TESTHOME, "fake_server.py")
open(FAKE_SRV, "w").write(r'''import sys
print("[12:00:00 INFO]: Done (2.1s)! For help, type \"help\"", flush=True)
for line in sys.stdin:
    c = line.strip()
    if c == "tps":
        print("[12:00:01 INFO]: TPS from last 1m, 5m, 15m: 19.9, 20.0, 20.0", flush=True)
    elif c == "mspt":
        print("[12:00:01 INFO]: Server tick times (avg/min/max) from last 5s, 10s, 1m:", flush=True)
        print("[12:00:01 INFO]: \u25f4 8.1/2.0/31.0, 8.0/2.0/31.0, 9.5/1.8/48.2", flush=True)
    elif c.startswith("sk reload"):
        print("[12:00:02 INFO]: [Skript] Reloading shop.sk...", flush=True)
        print("[12:00:02 ERROR]: [Skript] Line 3: (shop.sk)", flush=True)
        print("    Can't understand this condition/effect: sendd \"hi\"", flush=True)
        print("    Line: sendd \"hi\"", flush=True)
        print("[12:00:02 INFO]: [Skript] Encountered 1 error while reloading shop.sk! (7ms)", flush=True)
    elif c == "stop":
        break
''')
rt = m.PANEL.runtime(SID)
rt.proc = subprocess.Popen([sys.executable, FAKE_SRV], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, bufsize=1, encoding="utf-8")
rt.started_at = time.time()
threading.Thread(target=m.PANEL._reader, args=(SID,), daemon=True).start()
time.sleep(0.5)
s, j = get("/api/perf?sid=" + SID)
check("the page answers", s == 200 and j["server"]["running"], j.get("server"))
check("the allocation comes from the server's settings", j["server"]["allocated_mb"] == 4096, j["server"])
if j["supported"]:
    check("the process is measured", j["server"]["process"] and j["server"]["process"]["rss"] > 0, j["server"])
check("which commands report TPS is said", j["server"]["tps_commands"] == ["tps", "mspt"], j["server"])
check("no TPS is shown before one is measured", "tps" not in j["server"], j["server"])
check("the host is measured", j["host"]["cores"] >= 1)
s, j = post("/api/perf/measure", {"sid": SID})
check("measuring reads Paper's own tps and mspt", s == 200 and j["tps"]["tps"]["1m"] == 19.9
      and j["tps"]["mspt"]["1m"]["avg"] == 9.5, j)
s, j = get("/api/perf?sid=" + SID)
check("and it is kept with its age", j["server"]["tps"]["tps"]["5m"] == 20.0 and j["server"]["tps"]["age"] >= 0, j["server"])
s, j = post("/api/dev/skript/reload", {"ws": "srv-" + SID, "script": "shop.sk"})
check("a Skript reload reads Skript's verdict", s == 200 and j["reload"]["status"] == "errors"
      and j["reload"]["entries"][0]["line"] == 3, j)
rt.proc.stdin.write("stop\n")
rt.proc.stdin.flush()
rt.proc.wait(5)
time.sleep(0.3)
s, j = get("/api/perf?sid=" + SID)
check("a stopped server says why there are no process numbers", j["server"]["process"] is None
      and j["server"]["process_why"], j["server"])
s, j = post("/api/perf/measure", {"sid": SID})
check("measuring a stopped server says so", s == 400 and "not running" in j.get("error", ""), j)

section("Development Performance Mode")
s, j = post("/api/agent/settings", {"perf_mode": True})
check("it switches on", s == 200 and j["perf_mode"] is True)
check("the engine sees it", m.PANEL.perf_mode())
ag = m.PANEL.dev_agent()
check("the assistant keeps the model loaded while coding", ag.options().keep_alive == "30m")
check("and background indexing pauses", ag.paused())
post("/api/agent/settings", {"perf_mode": False})
check("and off again", not m.PANEL.perf_mode() and ag.options().keep_alive is None)

section("safety of the routes")
h = m.Handler.__new__(m.Handler)
h.client_address = ("192.168.1.50", 50123)
check("a request from the LAN is not this computer", h._local_only() is False)
h.client_address = ("127.0.0.1", 50123)
check("a request from this computer is", h._local_only() is True)
sent = []
h.client_address = ("10.0.0.7", 1)
h._err = lambda msg, code=400: sent.append((code, msg))
h._dev_post("/api/agent/run", {"ws": WS, "message": "rm everything"})
check("the assistant refuses LAN requests", sent and sent[0][0] == 403, sent)
post("/api/agent/settings", {"base_url": "http://10.255.255.1:11434"})
m.PANEL._ollama_seen = {"at": 0.0, "state": None}
t0 = time.time()
for _ in range(3):
    s, j = get("/api/perf?sid=" + SID)
check("the performance page never waits on an Ollama that does not answer", time.time() - t0 < 3 and s == 200,
      time.time() - t0)
check("it says it is still checking", j["ollama"]["running"] in (None, False), j["ollama"])
post("/api/agent/settings", {"base_url": fake.base})

fake.close()
httpd.shutdown()
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
