"""How far the assistant reaches, when the user widens it: the whole computer
(and the system folders it still may not write), the shell, approval modes,
every server (start, stop, restart, console, commands) from any workspace,
and the web - search through a provider and reading pages - against local
fakes of Ollama, a server and a web site."""
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import agent as ag
import websearch
from fake_ollama import FakeOllama

fails = []


def check(n, c, e=""):
    print(("  ok   " if c else "  FAIL ") + n + (("  " + str(e)[:300]) if not c else ""))
    if not c:
        fails.append(n)


def section(t):
    print("\n-- " + t)


TMP = tempfile.mkdtemp(prefix="agent_reach_")
fake = FakeOllama()
fake.add_model("coder:7b", tools=True, ctx=32768)
fake.add_model("plain:3b", tools=False, ctx=8192)
SETTINGS = {"base_url": fake.base, "model": "coder:7b", "num_ctx": 8192, "max_parallel": 1}


def save(changes):
    SETTINGS.update(changes)


def reset(**kw):
    for k in ("access", "shell_enabled", "approval_mode", "auto_approve_edits", "web_enabled", "web_provider",
              "ollama_api_key", "ollama_web_base", "searxng_url", "trusted_commands", "model"):
        SETTINGS.pop(k, None)
    SETTINGS["model"] = "coder:7b"
    SETTINGS.update(kw)


def run_until(agent, run, approve=None, timeout=30):
    seen, seq = [], 0
    end = time.time() + timeout
    while time.time() < end:
        for ev in run.since(seq, wait=1.0):
            seq = ev["seq"]
            seen.append(ev)
            if ev["type"] == "approval" and approve is not None:
                d = approve(ev)
                if d:
                    agent.approve(run.id, ev["call_id"], d[0], d[1] if len(d) > 1 else "")
            if ev["type"] == "done":
                return seen
    return seen


def results(evs, name=None):
    return [e for e in evs if e["type"] == "tool_result" and (name is None or e["name"] == name)]


def approvals(evs):
    return [e for e in evs if e["type"] == "approval"]


def offered(i=-1):
    return {t["function"]["name"] for t in (fake.requests[i].get("tools") or [])}


def ws_dir():
    d = tempfile.mkdtemp(dir=TMP)
    open(os.path.join(d, "notes.txt"), "w").write("inside\n")
    return d


# ---------------------------------------------------------------------------
section("the whole-computer sandbox")
proj = ws_dir()
outside = tempfile.mkdtemp(dir=TMP)
open(os.path.join(outside, "elsewhere.txt"), "w").write("from outside\n")
inner = ag.Sandbox(proj)
wide = ag.Sandbox(proj, outside=True)
try:
    inner.resolve(os.path.join(outside, "elsewhere.txt"))
    check("the workspace sandbox still refuses outside paths", False)
except ag.AgentError as e:
    check("the workspace sandbox still refuses outside paths", "settings" in str(e), e)
check("whole-computer access reads anywhere", wide.resolve(os.path.join(outside, "elsewhere.txt")).is_file())
check("~ is the home folder", str(wide.resolve("~")) == str(ag.Path(os.path.expanduser("~")).resolve()))
check("outside paths are shown in full", wide.rel(os.path.join(outside, "elsewhere.txt")).startswith(outside))
check("inside paths stay relative", wide.rel(os.path.join(proj, "notes.txt")) == "notes.txt")
for bad in ("/etc/lodestone-test", "/usr/bin/lodestone-test", "/boot/x"):
    try:
        wide.writable(wide.resolve(bad))
        check("%s may not be written" % bad, False)
    except ag.AgentError as e:
        check("%s may not be written" % bad, "system folder" in str(e), e)
check("a normal folder outside the workspace may be written", wide.writable(wide.resolve(outside)) is not None)
try:
    inner.writable(ag.Path(outside))
    check("the workspace sandbox will not write outside", False)
except ag.AgentError:
    check("the workspace sandbox will not write outside", True)

# ---------------------------------------------------------------------------
section("what each setting offers")
names = lambda caps: {d[0] for d in ag.tool_defs(caps)}
check("by default: no shell, no web, no server tools", not names({}) & {"run_shell", "web_search", "fetch_url",
                                                                         "start_server", "skript_reload"})
check("an old bool caller still works", {"skript_reload", "server_command"} <= {
    t["function"]["name"] for t in ag.tool_schemas(True)})
check("servers anywhere means server control without Skript tools",
      {"start_server", "stop_server", "restart_server", "list_servers"} <= names({"servers": True})
      and "skript_reload" not in names({"servers": True}))
check("the shell and the web each have a switch", "run_shell" in names({"shell": True}) and
      {"web_search", "fetch_url"} <= names({"web": True}))

A = ag.Agent(os.path.join(TMP, "data"), lambda: SETTINGS, save=save)
reset()
ws = A.add_workspace(proj, "proj")
fake.say("", tool_calls=[("run_shell", {"command": "echo hi"})])
fake.say("I cannot use the shell.")
run = A.start(ws, "run something")
evs = run_until(A, run)
check("a turned-off tool is not offered", "run_shell" not in offered())
r = results(evs, "run_shell")
check("and a call to it anyway is refused, not run", r and not r[0]["ok"] and "turned off" in r[0]["summary"], r)
check("the system prompt says files stay in the workspace",
      "only inside the workspace folder" in fake.requests[-1]["messages"][0]["content"])

# ---------------------------------------------------------------------------
section("whole-computer access in a run")
reset(access="computer")
target = os.path.join(outside, "made-by-assistant.txt")
fake.say("", tool_calls=[("read_file", {"path": os.path.join(outside, "elsewhere.txt")}),
                         ("write_file", {"path": target, "content": "hello\n"}),
                         ("write_file", {"path": "/etc/lodestone-test.conf", "content": "x"})])
fake.say("Done.")
run = A.start(ws, "write a file next to the other one")
asked = []
evs = run_until(A, run, approve=lambda ev: (asked.append(ev), ("approve",))[1])
rd = results(evs, "read_file")
check("a file outside the workspace is read", rd and rd[0]["ok"] and "from outside" in rd[0]["output"], rd)
check("a file outside the workspace is written after approval",
      os.path.isfile(target) and open(target).read() == "hello\n" and asked and asked[0]["kind"] == "write")
wr = results(evs, "write_file")
check("a system folder is refused before anyone is asked",
      len(asked) == 1 and not wr[1]["ok"] and "system folder" in wr[1]["summary"], (asked, wr))
check("the system prompt says it may reach the computer",
      "any file on this computer" in fake.requests[-1]["messages"][0]["content"])
ch = [e for e in evs if e["type"] == "change"][0]
A.revert(ws, ch["id"])
check("and the change can be put back", not os.path.exists(target))

pc = {"id": "pc", "name": "This computer", "root": TMP, "kind": "computer", "server_id": ""}
os.makedirs(os.path.join(TMP, "deep", "er"), exist_ok=True)
open(os.path.join(TMP, "deep", "er", "Paper-Server-1.21.jar"), "w").write("x")
fake.say("", tool_calls=[("find_files", {"query": "paper server"})])
fake.say("Found it.")
run = A.start(pc, "where is my paper jar")
evs = run_until(A, run)
ff = results(evs, "find_files")
check("the computer workspace finds files by name", ff and "Paper-Server-1.21.jar" in ff[0]["output"], ff)
check("and is not indexed like a project", not os.path.exists(os.path.join(TMP, "data", "pc", "index.json")))

# ---------------------------------------------------------------------------
section("the shell")
reset(shell_enabled=True)
fake.say("", tool_calls=[("run_shell", {"command": "echo hello from the shell; echo oops 1>&2; exit 3"})])
fake.say("It printed hello.")
run = A.start(ws, "run a command")
asked = []
evs = run_until(A, run, approve=lambda ev: (asked.append(ev), ("approve",))[1])
check("the shell tool is offered when turned on", "run_shell" in offered())
sh = results(evs, "run_shell")
check("it asks first, showing the exact command", asked and asked[0]["kind"] == "shell" and
      asked[0]["command"].startswith("echo hello"), asked)
check("output and errors and the exit code come back", sh and "hello from the shell" in sh[0]["output"]
      and "oops" in sh[0]["output"] and sh[0]["summary"] == "exit 3", sh)
check("output streams while it runs", any(e["type"] == "output" for e in evs))

fake.say("", tool_calls=[("run_shell", {"command": "pwd", "cwd": outside})])
fake.say("ok")
run = A.start(ws, "where")
evs = run_until(A, run, approve=lambda ev: ("approve",))
sh = results(evs, "run_shell")
check("cwd outside the workspace needs whole-computer access", sh and not sh[0]["ok"], sh)

fake.say("", tool_calls=[("run_shell", {"command": "echo trusted"})])
fake.say("ok")
run = A.start(ws, "trust it")
run_until(A, run, approve=lambda ev: ("always",))
check("'always' remembers that exact command", "echo trusted" in SETTINGS.get("trusted_commands", []))
fake.say("", tool_calls=[("run_shell", {"command": "echo trusted"})])
fake.say("ok")
run = A.start(ws, "again")
asked = []
evs = run_until(A, run, approve=lambda ev: (asked.append(ev), ("reject",))[1])
check("and runs it without asking next time", not asked and "trusted" in results(evs, "run_shell")[0]["output"])

fake.say("", tool_calls=[("run_shell", {"command": "sleep 30", "timeout": 5})])
fake.say("It was too slow.")
run = A.start(ws, "slow")
t0 = time.time()
evs = run_until(A, run, approve=lambda ev: ("approve",))
sh = results(evs, "run_shell")
check("a command past its timeout is stopped and says so", sh and "timed out" in sh[0]["summary"]
      and time.time() - t0 < 20, sh)

fake.say("", tool_calls=[("run_shell", {"command": "sleep 30"})])
fake.say("never")
run = A.start(ws, "cancel me")
seq, t0 = 0, time.time()
while time.time() - t0 < 10:
    evs = run.since(seq, wait=0.5)
    if evs:
        seq = evs[-1]["seq"]
    if any(e["type"] == "approval" for e in evs):
        A.approve(run.id, [e for e in evs if e["type"] == "approval"][0]["call_id"], "approve")
    if any(e["type"] == "output" or (e["type"] == "status" and "running sleep" in e.get("detail", ""))
           for e in evs):
        break
time.sleep(0.5)
A.cancel(run.id)
evs = run_until(A, run)
check("cancelling stops a running shell command", run.state == "cancelled" and time.time() - t0 < 15, run.state)
with fake.lock:
    fake.turns.clear()          # the reply the cancelled run never got to

# ---------------------------------------------------------------------------
section("approval modes")
reset(approval_mode="edits", shell_enabled=True)
fake.say("", tool_calls=[("write_file", {"path": "a.txt", "content": "a\n"}),
                         ("run_shell", {"command": "echo b"})])
fake.say("ok")
run = A.start(ws, "edits mode")
asked = []
evs = run_until(A, run, approve=lambda ev: (asked.append(ev), ("approve",))[1])
check("'edits': file changes go ahead", os.path.isfile(os.path.join(proj, "a.txt")))
check("'edits': commands still ask", [a["kind"] for a in asked] == ["shell"], asked)

reset(approval_mode="all", shell_enabled=True)
fake.say("", tool_calls=[("write_file", {"path": "b.txt", "content": "b\n"}),
                         ("run_shell", {"command": "echo c"})])
fake.say("ok")
run = A.start(ws, "all mode")
asked = []
evs = run_until(A, run, approve=lambda ev: (asked.append(ev), ("reject",))[1])
check("'all': nothing asks", not asked and os.path.isfile(os.path.join(proj, "b.txt"))
      and "c" in results(evs, "run_shell")[0]["output"], asked)
check("'all': each auto-approval is still in the log", sum(1 for e in evs if e["type"] == "approval_done"
                                                          and e["decision"] == "auto") == 2)
check("'all': the model is told to be careful", "nothing waits for approval" in
      fake.requests[-1]["messages"][0]["content"])
reset(auto_approve_edits=True)
check("the old auto-approve switch reads as 'edits'", A.cfg()["approval_mode"] == "edits")

# ---------------------------------------------------------------------------
section("every server, from any workspace")


class Server:
    """A fake server: start prints its boot lines then 'Done', stop exits."""

    def __init__(self, sid, name, boots=True, local=True):
        self.sid, self.name, self.boots, self.local = sid, name, boots, local
        self.lines, self.seq, self.cmds, self.running, self.ready = [], 0, [], False, False
        self.lock = threading.Lock()
        self.calls = []

    def add(self, t):
        with self.lock:
            self.seq += 1
            self.lines.append((self.seq, t))

    def info(self):
        return {"name": self.name, "id": self.sid, "software": "paper", "mc": "1.21.4", "running": self.running,
                "ready": self.ready, "can_command": self.running, "can_start": True, "local": self.local,
                "players": [], "uptime": ""}

    def console(self, n):
        return [t for _s, t in self.lines[-n:]]

    def last_seq(self):
        return self.seq

    def since(self, seq):
        with self.lock:
            return [(s, t) for s, t in self.lines if s > seq]

    def command(self, cmd):
        if not self.running:
            return False, "Server is not running."
        self.cmds.append(cmd)
        self.add("> " + cmd)
        self.add("[INFO]: There are 0 of a max of 20 players online")
        return True, ""

    def start(self):
        self.calls.append("start")
        self.running, self.ready = True, False

        def boot():
            self.add("[INFO]: Starting minecraft server version 1.21.4")
            time.sleep(0.6)
            if not self.boots:
                self.add("[ERROR]: Failed to bind to port 25565")
                self.running = False
                return
            self.add("[INFO]: Done (1.234s)! For help, type \"help\"")
        threading.Thread(target=boot, daemon=True).start()
        return True, "Starting."

    def stop(self):
        self.calls.append("stop")

        def down():
            self.add("[INFO]: Stopping server")
            time.sleep(0.6)
            self.running = self.ready = False
        threading.Thread(target=down, daemon=True).start()
        return True, "Stopping."

    def kill(self):
        self.calls.append("kill")
        self.running = self.ready = False
        return True, "Killed."


lobby, smp, broken, smp2 = (Server("s1", "Lobby"), Server("s2", "SMP"), Server("s3", "Broken", boots=False),
                            Server("s4", "SMP Test"))
servers = {s.sid: s for s in (lobby, smp, broken, smp2)}
S = ag.Agent(os.path.join(TMP, "data3"), lambda: SETTINGS, save=save, bridge_for=servers.get,
             servers=lambda: [{"id": s.sid, "name": s.name, "software": "paper", "mc": "1.21.4",
                               "running": s.running} for s in servers.values()])
reset()
sws = S.add_workspace(ws_dir(), "plugin project")
fake.say("", tool_calls=[("list_servers", {}), ("start_server", {"server": "lobby"})])
fake.say("", tool_calls=[("server_command", {"server": "Lobby", "command": "/list"}),
                         ("read_console", {"server": "s1", "filter": "players"})])
fake.say("", tool_calls=[("restart_server", {"server": "lobby"})])
fake.say("", tool_calls=[("stop_server", {"server": "Lobby"})])
fake.say("All done.")
n0 = len(fake.requests)
run = S.start(sws, "start the lobby, list players, restart it, then stop it")
asked = []
evs = run_until(S, run, approve=lambda ev: (asked.append(ev), ("approve",))[1], timeout=60)
check("server control is offered in a project workspace", {"start_server", "stop_server", "restart_server",
                                                          "server_command"} <= offered(n0))
check("but not Skript tools, which need the server's own folder", "skript_reload" not in offered(n0))
check("the servers are listed in the system prompt", "Lobby (id s1)" in fake.requests[n0]["messages"][0]["content"])
ls = results(evs, "list_servers")
check("list_servers names every server", ls and all(n in ls[0]["output"] for n in ("Lobby", "SMP", "Broken")), ls)
st = results(evs, "start_server")
check("start_server waits until the server is up", st and st[0]["ok"] and st[0]["summary"] == "Lobby: up"
      and "Done (1.234s)" in st[0]["output"], st)
check("the console streams into the run while it starts",
      any(e["type"] == "output" and "Starting minecraft" in e["text"] for e in evs))
sc = results(evs, "server_command")
check("a command is typed into the right server", lobby.cmds == ["list"] and "players online" in sc[0]["output"],
      (lobby.cmds, sc))
check("the console of a named server is read", "players online" in results(evs, "read_console")[0]["output"])
rs = results(evs, "restart_server")
check("restart stops, waits, starts and waits", rs and rs[0]["ok"] and "Stop: stopped" in rs[0]["output"]
      and "Start: up" in rs[0]["output"] and lobby.calls == ["start", "stop", "start", "stop"], (rs, lobby.calls))
check("stop_server waits for it to exit", not lobby.running and results(evs, "stop_server")[0]["summary"]
      == "stopped Lobby")
check("every server action asked first", [a["kind"] for a in asked] == ["server"] * 4 and
      [a["title"] for a in asked] == ["Start Lobby", "Run on Lobby: /list", "Restart Lobby", "Stop Lobby"],
      [a.get("title") for a in asked])
check("the other servers were left alone", smp.calls == [] and smp2.calls == [] and broken.calls == [])

fake.say("", tool_calls=[("start_server", {"server": "broken"}), ("start_server", {"server": "sm"}),
                         ("start_server", {"server": "nether"}), ("stop_server", {"server": "SMP Test"}),
                         ("server_command", {"command": "list"})])
fake.say("Broken did not start.")
run = S.start(sws, "start broken")
evs = run_until(S, run, approve=lambda ev: ("approve",), timeout=60)
st = results(evs, "start_server")
check("a server that dies while starting says so, with its last lines",
      st[0]["summary"] == "Broken: exited before it was up" and "Failed to bind" in st[0]["output"], st[0])
check("'sm' matches two servers and asks for the id", not st[1]["ok"] and "more than one" in st[1]["summary"]
      and smp.calls == [], st[1])
check("an unknown server is refused, naming the real ones", not st[2]["ok"] and "Lobby" in st[2]["summary"], st[2])
check("stopping a stopped server is a no-op", results(evs, "stop_server")[0]["summary"] == "not running")
check("with no server named and none linked, it asks which", not results(evs, "server_command")[0]["ok"]
      and "Say which server" in results(evs, "server_command")[0]["summary"])

fake.say("", tool_calls=[("start_server", {"server": "s2"})])
fake.say("", tool_calls=[("stop_server", {"server": "s2"})])
fake.say("Left it running.")
run = S.start(sws, "start smp")
evs = run_until(S, run, approve=lambda ev: ("approve",) if ev["title"].startswith("Start") else ("reject", "keep it up"),
                timeout=60)
stop = results(evs, "stop_server")
check("a rejected stop leaves the server running, with the note", smp.running and "keep it up" in stop[0]["output"],
      stop)
smp.stop()
time.sleep(1)

# ---------------------------------------------------------------------------
section("the web")
SEEN = {"auth": [], "queries": []}
PAGE = ("<html><head><title>Paper docs</title><style>.x{}</style><script>evil()</script></head><body>"
        "<h1>Getting started</h1><p>Paper needs Java 21.</p><ul><li>Download</li><li>Run</li></ul>"
        "<a href='/next'>Next page</a></body></html>")


class Web(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        b = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/search?"):
            from urllib.parse import parse_qs, urlparse
            q = parse_qs(urlparse(self.path).query)
            SEEN["queries"].append(q.get("q", [""])[0])
            if q.get("format") != ["json"]:
                return self._send(403, "html only", "text/html")
            return self._send(200, json.dumps({"results": [
                {"title": "SearX hit", "url": "https://docs.papermc.io/", "content": "Paper docs"}]}),
                "application/json")
        if self.path == "/page":
            return self._send(200, PAGE, "text/html; charset=utf-8")
        if self.path == "/file.bin":
            return self._send(200, b"\0\1\2", "application/octet-stream")
        return self._send(404, "nope", "text/plain")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if self.path == "/api/web_search":
            SEEN["auth"].append(self.headers.get("Authorization"))
            SEEN["queries"].append(body.get("query"))
            if self.headers.get("Authorization") != "Bearer good-key":
                return self._send(401, json.dumps({"error": "unauthorized"}), "application/json")
            return self._send(200, json.dumps({"results": [
                {"title": "Paper", "url": "https://papermc.io/", "content": "High performance fork"},
                {"title": "Purpur", "url": "https://purpurmc.org/", "content": "Fork of Paper"}][:body.get(
                    "max_results", 5)]}), "application/json")
        return self._send(404, "{}", "application/json")


web = ThreadingHTTPServer(("127.0.0.1", 0), Web)
threading.Thread(target=web.serve_forever, daemon=True).start()
WEB = "http://127.0.0.1:%d" % web.server_address[1]

page = websearch.fetch(WEB + "/page")
check("a page is read as text", "Paper needs Java 21." in page["text"] and page["title"] == "Paper docs", page)
check("scripts and styles are left out", "evil" not in page["text"] and ".x{}" not in page["text"])
check("headings and lists keep their shape", "# Getting started" in page["text"] and "- Download" in page["text"])
check("links are absolute", page["links"] and page["links"][0]["url"] == WEB + "/next", page["links"])
for bad, why in (("file:///etc/passwd", "only http"), (WEB + "/file.bin", "not a page"), (WEB + "/missing", "404")):
    try:
        websearch.fetch(bad)
        check("%s is refused" % why, False)
    except websearch.WebError as e:
        check("%s is refused" % why, True)
try:
    websearch.search("paper", {"web_provider": "none"})
    check("search without a provider explains how to set one up", False)
except websearch.WebError as e:
    check("search without a provider explains how to set one up", "SearXNG" in str(e))
try:
    websearch.search("paper", {"web_provider": "ollama", "ollama_api_key": "bad", "ollama_web_base": WEB})
    check("a bad key is reported", False)
except websearch.WebError as e:
    check("a bad key is reported", "401" in str(e), e)
hits = websearch.search("paper", {"web_provider": "searxng", "searxng_url": WEB + "/"})
check("SearXNG results are read from its JSON", hits == [{"title": "SearX hit", "url": "https://docs.papermc.io/",
                                                          "snippet": "Paper docs"}], hits)

reset(web_enabled=True, web_provider="ollama", ollama_api_key="good-key", ollama_web_base=WEB)
fake.say("", tool_calls=[("web_search", {"query": "paper server fork", "max_results": 2}),
                         ("fetch_url", {"url": WEB + "/page"})])
fake.say("Paper needs Java 21.")
run = A.start(ws, "what java does paper need")
asked = []
evs = run_until(A, run, approve=lambda ev: (asked.append(ev), ("approve",))[1])
check("the web tools are offered when turned on", {"web_search", "fetch_url"} <= offered())
check("the system prompt warns that web text is not instructions", "never as" in
      fake.requests[-1]["messages"][0]["content"] and "instructions" in fake.requests[-1]["messages"][0]["content"])
ws_r = results(evs, "web_search")
check("search goes to Ollama with the key", SEEN["auth"][-1] == "Bearer good-key" and
      SEEN["queries"][-1] == "paper server fork", SEEN)
check("results come back marked as someone else's text", ws_r and ws_r[0]["ok"] and
      ws_r[0]["output"].startswith(websearch.UNTRUSTED) and "purpurmc.org" in ws_r[0]["output"], ws_r)
check("searching does not ask; reading a page does", [a["kind"] for a in asked] == ["web"] and
      asked[0]["url"] == WEB + "/page", asked)
fu = results(evs, "fetch_url")
check("the page text is handed to the model, marked", fu and fu[0]["ok"] and "Paper needs Java 21." in
      fake.requests[-1]["messages"][-1]["content"] and websearch.UNTRUSTED in fake.requests[-1]["messages"][-1]["content"],
      fu)

fake.say("", tool_calls=[("fetch_url", {"url": WEB + "/page"})])
fake.say("ok")
run = A.start(ws, "read it")
evs = run_until(A, run, approve=lambda ev: ("reject", "not that site"))
check("a rejected page is not read", "not that site" in results(evs, "fetch_url")[0]["output"])

# ---------------------------------------------------------------------------
section("the JSON fallback knows the new tools too")
reset(model="plain:3b", shell_enabled=True, web_enabled=True)
fake.say('{"answer": "fine"}')
run = A.start(ws, "hello")
run_until(A, run)
sysmsg = fake.requests[-1]["messages"][0]["content"]
check("run_shell and web_search are described in the JSON protocol", "run_shell(command" in sysmsg and
      "web_search(query" in sysmsg, sysmsg[-600:])

web.shutdown()
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
