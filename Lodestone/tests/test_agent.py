"""The development assistant end to end, against a fake Ollama: the tool
loop, approvals, rejections, reverts, commands and builds, cancelling, the
JSON fallback, memory, Skript reloads through a server, and the task limit."""
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import agent as ag
import ollama as ol
from fake_ollama import FakeOllama

fails = []


def check(n, c, e=""):
    print(("  ok   " if c else "  FAIL ") + n + (("  " + str(e)[:300]) if not c else ""))
    if not c:
        fails.append(n)


def section(t):
    print("\n-- " + t)


TMP = tempfile.mkdtemp(prefix="agent_test_")
DATA = os.path.join(TMP, "data")
fake = FakeOllama()
fake.add_model("coder:7b", tools=True, ctx=32768)
fake.add_model("plain:3b", tools=False, ctx=8192)
SETTINGS = {"base_url": fake.base, "model": "coder:7b", "num_ctx": 8192, "max_parallel": 1}
saved = []


def save(changes):
    saved.append(changes)
    SETTINGS.update(changes)


A = ag.Agent(DATA, lambda: SETTINGS, save=save)


def project():
    d = tempfile.mkdtemp(dir=TMP)
    os.makedirs(os.path.join(d, "src/main/java/com/example"))
    os.makedirs(os.path.join(d, "src/main/resources"))
    os.makedirs(os.path.join(d, "build/classes"))
    os.makedirs(os.path.join(d, "world/region"))
    open(os.path.join(d, "world/level.dat"), "wb").write(b"\0\1")
    open(os.path.join(d, "build/classes/Junk.java"), "w").write("class Junk {}")
    open(os.path.join(d, "settings.gradle.kts"), "w").write('rootProject.name = "shop"\n')
    open(os.path.join(d, "build.gradle.kts"), "w").write(
        'plugins { java }\ndependencies { compileOnly("io.papermc.paper:paper-api:1.21.4-R0.1-SNAPSHOT") }\n'
        'java { toolchain.languageVersion.set(JavaLanguageVersion.of(21)) }\n')
    open(os.path.join(d, "src/main/resources/plugin.yml"), "w").write(
        "name: Shop\nversion: 1.0\nmain: com.example.ShopPlugin\napi-version: '1.21'\n")
    open(os.path.join(d, "src/main/resources/config.yml"), "w").write("prefix: '&6[Shop]'\nprice: 10\n")
    open(os.path.join(d, "src/main/java/com/example/ShopPlugin.java"), "w").write(
        "package com.example;\n\npublic class ShopPlugin extends JavaPlugin {\n"
        "    @Override\n    public void onEnable() {\n        getLogger().info(\"hi\");\n    }\n}\n")
    return d


def run_until(run, want_types=("done",), timeout=20, approve=None, agent=None):
    """Read events until one of want_types; answer approvals with approve(ev)."""
    seen, seq = [], 0
    end = time.time() + timeout
    while time.time() < end:
        for ev in run.since(seq, wait=1.0):
            seq = ev["seq"]
            seen.append(ev)
            if ev["type"] == "approval" and approve is not None:
                d = approve(ev)
                if d:
                    (agent or A).approve(run.id, ev["call_id"], d[0], d[1] if len(d) > 1 else "")
            if ev["type"] in want_types:
                return seen
    return seen


def types(evs):
    return [e["type"] for e in evs]


def tool_results(evs):
    return [(e["name"], e["ok"], e["summary"]) for e in evs if e["type"] == "tool_result"]


# ---------------------------------------------------------------------------
section("the sandbox")
proj = project()
box = ag.Sandbox(proj)
for bad in ("../outside.txt", "/etc/passwd", "src/../../x", "~/x"):
    try:
        box.resolve(bad)
        check("%s is refused" % bad, False)
    except ag.AgentError:
        check("%s is refused" % bad, True)
os.symlink("/etc", os.path.join(proj, "escape"))
try:
    box.resolve("escape/passwd")
    check("a symlink out of the folder is refused", False)
except ag.AgentError:
    check("a symlink out of the folder is refused", True)
os.remove(os.path.join(proj, "escape"))
check("a normal path resolves", box.resolve("src/main/resources/plugin.yml").is_file())

section("the project index")
ws = A.add_workspace(proj, "Shop plugin")
idx = A.index(ws)
st = idx.build()
files = idx.data["files"]
check("source files are indexed", "src/main/java/com/example/ShopPlugin.java" in files, list(files))
check("build output is skipped", not any(f.startswith("build/") for f in files), list(files))
check("world folders are skipped", not any(f.startswith("world/") for f in files), list(files))
check("Java symbols are read", "ShopPlugin" in files["src/main/java/com/example/ShopPlugin.java"]["symbols"])
check("the package is read", files["src/main/java/com/example/ShopPlugin.java"].get("package") == "com.example")
check("a second build changes nothing", idx.build()["changed"] == 0)
hits = idx.find("shop plugin enable")
check("find ranks the class first", hits and hits[0]["path"].endswith("ShopPlugin.java"), hits)
check("packages are known for stack traces", idx.packages() == ["com.example"], idx.packages())
paused = ag.ProjectIndex(proj, os.path.join(TMP, "p.json")).build(pause=lambda: True)
check("a paused build stops without indexing", paused["paused"] and paused["files"] == 0, paused)
ov = A.overview(ws)
check("the overview knows it is a Gradle Paper project", ov["java"]["kind"] == "gradle" and ov["java"]["loader"] == "paper", ov.get("java"))

# ---------------------------------------------------------------------------
section("a run: look, edit with approval, verify, answer")
fake.say("Let me look first.", tool_calls=[("project_overview", {})])
fake.say("", tool_calls=[("read_file", {"path": "src/main/resources/config.yml"})])
fake.say("", tool_calls=[("edit_file", {"path": "src/main/resources/config.yml", "old_text": "price: 10",
                                        "new_text": "price: 25"})])
fake.say("", tool_calls=[("check_file", {"path": "src/main/resources/config.yml"})])
fake.say("Changed the price to 25 in `src/main/resources/config.yml:2` and checked the file.")
run = A.start(ws, "Make the price 25")
approvals = []
evs = run_until(run, approve=lambda ev: (approvals.append(ev), ("approve",))[1])
t = types(evs)
check("the run finishes", t[-1] == "done" and evs[-1]["state"] == "done", t)
check("text was streamed before the tool call", t.index("text") < t.index("tool"), t)
check("tools ran in order", [r[0] for r in tool_results(evs)] == ["project_overview", "read_file", "edit_file",
                                                                    "check_file"], tool_results(evs))
check("the edit asked first, with a diff", approvals and "-price: 10" in approvals[0]["diff"]
      and "+price: 25" in approvals[0]["diff"], approvals)
check("the approval shows the check of the new content", approvals[0]["checks"]["ok"] is True, approvals[0].get("checks"))
check("the file changed", "price: 25" in open(os.path.join(proj, "src/main/resources/config.yml")).read())
check("the change is recorded", run.changes and run.changes[0]["path"] == "src/main/resources/config.yml", run.changes)
check("the final message is kept", run.final.startswith("Changed the price"), run.final)
req = fake.requests[-1]
check("native tools were offered", req.get("tools") and any(t["function"]["name"] == "edit_file" for t in req["tools"]))
check("the tool result went back as role tool with its name",
      any(m["role"] == "tool" and m.get("tool_name") == "read_file" and "price: 10" in m["content"]
          for m in req["messages"]), [m["role"] for m in req["messages"]])
check("the system prompt describes the project", "Gradle" in req["messages"][0]["content"] or
      "gradle" in req["messages"][0]["content"], req["messages"][0]["content"][:400])
check("num_ctx went with every request", req["options"]["num_ctx"] == 8192, req.get("options"))
hist = A.store(ws).history()
check("the conversation is saved", hist[-2]["content"] == "Make the price 25" and hist[-1]["state"] == "done", hist[-2:])

section("putting a change back")
ch = run.changes[0]
A.revert(ws, ch["id"])
check("revert restores the old content", "price: 10" in open(os.path.join(proj, "src/main/resources/config.yml")).read())
try:
    A.revert(ws, ch["id"])
    check("reverting twice is refused", False)
except ag.AgentError:
    check("reverting twice is refused", True)

section("a rejected change, with a note")
fake.say("", tool_calls=[("write_file", {"path": "README.md", "content": "# Shop\n"})])
fake.say("Understood, I left the README alone.")
run = A.start(ws, "Write a readme")
evs = run_until(run, approve=lambda ev: ("reject", "not now"))
check("the file was not written", not os.path.exists(os.path.join(proj, "README.md")))
tr = [m for m in fake.requests[-1]["messages"] if m["role"] == "tool"]
check("the model was told, with the note", tr and "rejected" in tr[-1]["content"] and "not now" in tr[-1]["content"], tr)

section("things that are refused before anyone is asked")
fake.say("", tool_calls=[("write_file", {"path": "../escape.txt", "content": "x"}),
                         ("edit_file", {"path": "src/main/resources/config.yml", "old_text": "nope", "new_text": "x"}),
                         ("run_command", {"command": "rm -rf /"}),
                         ("run_command", {"command": "git push"}),
                         ("write_file", {"path": "plugin.jar", "content": "x"}),
                         ("nonsense_tool", {})])
fake.say("Done.")
run = A.start(ws, "try bad things")
evs = run_until(run, approve=lambda ev: ("approve",))
res = tool_results(evs)
check("no approval was asked for any of them", "approval" not in types(evs), types(evs))
check("all six failed", [r[1] for r in res] == [False] * 6, res)
check("a path outside the folder is refused", "outside the workspace" in res[0][2], res[0])
check("an edit whose text is missing says to read again", "not found" in res[1][2], res[1])
check("rm is not on the allowed list", "not on the allowed list" in res[2][2], res[2])
check("git push is not a read-only git command", "may only be run as" in res[3][2], res[3])
check("binary files are refused", "binary" in res[4][2], res[4])
check("an unknown tool is reported", "no tool called" in res[5][2], res[5])
check("the escape file does not exist", not os.path.exists(os.path.join(TMP, "escape.txt")))

# ---------------------------------------------------------------------------
section("a build that fails, read back as errors")
gw = os.path.join(proj, "gradlew")
open(gw, "w").write("#!/bin/sh\necho '> Task :compileJava FAILED'\n"
                    "echo \"$PWD/src/main/java/com/example/ShopPlugin.java:6: error: cannot find symbol\"\n"
                    "echo '        getLoger().info(\"hi\");'\necho '  symbol:   method getLoger()'\n"
                    "echo 'BUILD FAILED in 2s'\nexit 1\n")
os.chmod(gw, os.stat(gw).st_mode | stat.S_IEXEC)
fake.say("", tool_calls=[("run_build", {"task": "build"})])
fake.say("The build fails on a typo.")
run = A.start(ws, "build it")
cmd_ev = []
evs = run_until(run, approve=lambda ev: (cmd_ev.append(ev), ("approve",))[1])
check("the build asked first, showing the exact command", cmd_ev and cmd_ev[0]["kind"] == "command"
      and cmd_ev[0]["command"].endswith("gradlew build"), cmd_ev)
out = [e for e in evs if e["type"] == "output"]
check("output was streamed", out and "BUILD FAILED" in "".join(e["text"] for e in out), out)
tr = [e for e in evs if e["type"] == "tool_result"][0]
check("the errors were parsed with file and line", tr["data"]["build"]["errors"][0]["line"] == 6
      and tr["data"]["build"]["errors"][0]["file"].endswith("ShopPlugin.java"), tr.get("data"))
check("and handed to the model as such", "ERROR src/main/java/com/example/ShopPlugin.java:6" in
      [m for m in fake.requests[-1]["messages"] if m["role"] == "tool"][-1]["content"])

section("always allowing a command remembers exactly that command")
fake.say("", tool_calls=[("run_command", {"command": "git status"})])
fake.say("ok")
run = A.start(ws, "status")
evs = run_until(run, approve=lambda ev: ("always",))
check("the command is saved as trusted", saved and saved[-1].get("trusted_commands") == ["git status"], saved)
fake.say("", tool_calls=[("run_command", {"command": "git status"})])
fake.say("ok")
run = A.start(ws, "status again")
evs = run_until(run)
check("next time it runs without asking", "approval" not in types(evs) and any(
    e["type"] == "approval_done" and e["decision"] == "auto" for e in evs), types(evs))

# ---------------------------------------------------------------------------
section("cancelling")
fake.say("", tool_calls=[("write_file", {"path": "NEW.txt", "content": "x"})])
run = A.start(ws, "write something")
evs = run_until(run, want_types=("approval",))
t0 = time.time()
A.cancel(run.id)
evs = run_until(run)
check("cancel while waiting for approval ends the run", evs and evs[-1]["type"] == "done"
      and evs[-1]["state"] == "cancelled", types(evs))
check("promptly", time.time() - t0 < 3, time.time() - t0)
check("and nothing was written", not os.path.exists(os.path.join(proj, "NEW.txt")))

open(gw, "w").write("#!/bin/sh\necho started\necho $$ > \"$PWD/.pid\"\nsleep 60 &\nwait\n")
fake.say("", tool_calls=[("run_build", {"task": "build"})])
run = A.start(ws, "slow build")
evs = run_until(run, want_types=("output",), approve=lambda ev: ("approve",))
time.sleep(0.3)
pid = int(open(os.path.join(proj, ".pid")).read().strip())
t0 = time.time()
A.cancel(run.id)
evs = run_until(run)
check("cancelling a running build ends the run", evs[-1]["state"] == "cancelled", types(evs))
check("promptly", time.time() - t0 < 5, time.time() - t0)
time.sleep(0.5)
try:
    os.kill(pid, 0)
    alive = True
except OSError:
    alive = False
check("and the build's process is gone", not alive)
os.remove(os.path.join(proj, ".pid"))

fake.delay = 0.05
fake.say("word " * 200)
run = A.start(ws, "talk a lot")
run_until(run, want_types=("text",))
t0 = time.time()
A.cancel(run.id)
evs = run_until(run)
check("cancelling while the model streams ends the run", evs[-1]["state"] == "cancelled", types(evs))
check("promptly", time.time() - t0 < 3)
fake.delay = 0

# ---------------------------------------------------------------------------
section("a model without tool calling")
SETTINGS["model"] = "plain:3b"
fake.say('{"tool": "read_file", "args": {"path": "src/main/resources/plugin.yml"}}')
fake.say('{"answer": "The main class is `com.example.ShopPlugin`."}')
run = A.start(ws, "what is the main class?")
evs = run_until(run)
check("it runs in JSON mode", any(e["type"] == "model" and e["mode"] == "json" for e in evs), evs[:3])
check("json format was asked for and no tools sent", fake.requests[-1].get("format") == "json"
      and not fake.requests[-1].get("tools"), fake.requests[-1].keys())
check("the tool list is in the system prompt instead", "read_file(path" in fake.requests[-1]["messages"][0]["content"])
check("the tool ran", tool_results(evs) and tool_results(evs)[0][:2] == ("read_file", True), tool_results(evs))
check("the answer arrives", run.final == "The main class is `com.example.ShopPlugin`.", run.final)
SETTINGS["tool_mode"] = {"plain:3b": "native"}
check("an override wins over what Ollama says", A.tool_mode("plain:3b")[0] == "native")
SETTINGS["tool_mode"] = {}
SETTINGS["model"] = "coder:7b"

section("a tool call written as text")
fake.say('<tool_call>{"name": "list_files", "arguments": {"path": "src", "depth": 1}}</tool_call>')
fake.say("Listed.")
run = A.start(ws, "list src")
evs = run_until(run)
check("it is carried out", tool_results(evs) and tool_results(evs)[0][0] == "list_files", tool_results(evs))

section("asking the user")
fake.say("", tool_calls=[("ask_user", {"question": "Paper or Spigot?"})])
run = A.start(ws, "make a plugin")
evs = run_until(run)
check("the run ends as a question", evs[-1]["state"] == "question" and run.final == "Paper or Spigot?", evs[-1])

section("memory")
fake.say("", tool_calls=[("remember", {"note": "Prices are integers in config.yml"})])
fake.say("Noted.")
run_until(A.start(ws, "remember that"))
fake.say("Hello.")
run_until(A.start(ws, "hi"))
check("the note is in the next run's system prompt",
      "Prices are integers in config.yml" in fake.requests[-1]["messages"][0]["content"])
check("and earlier turns are in its history", any(m["role"] == "user" and m["content"] == "remember that"
                                                  for m in fake.requests[-1]["messages"]))

section("plans")
fake.say("", tool_calls=[("set_plan", {"steps": ["Read config", "Add command"], "files": ["Shop.java"], "done": 1})])
fake.say("ok")
evs = run_until(A.start(ws, "plan it"))
pl = [e for e in evs if e["type"] == "plan"]
check("the plan reaches the UI", pl and pl[0]["steps"] == ["Read config", "Add command"] and pl[0]["done"] == 1, pl)

section("the step limit")
SETTINGS["max_steps"] = 3
for _ in range(3):
    fake.say("", tool_calls=[("list_files", {"path": "."})])
run = A.start(ws, "loop forever")
evs = run_until(run)
check("it stops and says so", "stopped after 3 steps" in run.final, run.final)
SETTINGS["max_steps"] = 24

# ---------------------------------------------------------------------------
section("a server workspace: console, commands and Skript")
srv = tempfile.mkdtemp(dir=TMP)
os.makedirs(os.path.join(srv, "plugins/Skript/scripts"))
open(os.path.join(srv, "plugins/Skript/scripts/shop.sk"), "w").write("command /shop:\n    trigger:\n        sendd \"hi\"\n")
open(os.path.join(srv, "server.properties"), "w").write("pvp=true\n")


class Bridge:
    def __init__(self):
        self.lines, self.seq, self.cmds = [], 0, []

    def add(self, t):
        self.seq += 1
        self.lines.append((self.seq, t))

    def info(self):
        return {"name": "Survival", "software": "paper", "mc": "1.21.4", "running": True, "can_command": True}

    def console(self, n):
        return [t for _s, t in self.lines[-n:]]

    def last_seq(self):
        return self.seq

    def since(self, seq):
        return [(s, t) for s, t in self.lines if s > seq]

    def command(self, cmd):
        self.cmds.append(cmd)
        if cmd.startswith("sk reload"):
            def later():
                time.sleep(0.3)
                self.add("[12:00:01 INFO]: [Skript] Reloading shop.sk...")
                self.add("[12:00:01 ERROR]: [Skript] Line 3: (shop.sk)")
                self.add("    Can't understand this condition/effect: sendd \"hi\"")
                self.add("    Line: sendd \"hi\"")
                self.add("[12:00:01 INFO]: [Skript] Encountered 1 error while reloading shop.sk! (9ms)")
            threading.Thread(target=later, daemon=True).start()
        else:
            self.add("> " + cmd)
            self.add("[12:00:02 INFO]: There are 0 of a max of 20 players online")
        return True, ""


br = Bridge()
br.add("[12:00:00 INFO]: Done (3.1s)! For help, type \"help\"")
B = ag.Agent(os.path.join(TMP, "data2"), lambda: SETTINGS, bridge_for=lambda sid: br if sid == "s1" else None, save=save)
sws = {"id": "srv-s1", "name": "Survival", "root": srv, "kind": "server", "server_id": "s1"}
fake.say("", tool_calls=[("skript_environment", {}), ("skript_reload", {"script": "shop.sk"}),
                         ("server_command", {"command": "/list"}), ("read_console", {"lines": 5, "filter": "players"})])
fake.say("Skript reports one error on line 3.")
run = B.start(sws, "check my shop script")
asked = []
evs = run_until(run, approve=lambda ev: (asked.append(ev), ("approve",))[1], timeout=30, agent=B)
res = tool_results(evs)
check("server tools are offered", any(t["function"]["name"] == "skript_reload" for t in fake.requests[-1]["tools"]))
check("the reload and the command both asked first", [a["kind"] for a in asked] == ["server", "server"], asked)
check("the command went without its slash", br.cmds[-1] == "list", br.cmds)
check("Skript's verdict is read back", res[1][1] and "1 error" in res[1][2], res)
tr = [e for e in evs if e["type"] == "tool_result" and e["name"] == "skript_reload"][0]
check("with the line and Skript's message", tr["data"]["reload"]["entries"][0]["line"] == 3 and
      "Can't understand" in tr["data"]["reload"]["entries"][0]["message"], tr.get("data"))
check("the console filter works", "players online" in [e for e in evs if e["type"] == "tool_result"
                                                       and e["name"] == "read_console"][0]["output"])
check("the system prompt carries the Skript rules", "skript_reload" in fake.requests[-1]["messages"][0]["content"])
check("a workspace without a server gets no server tools",
      not any(t["function"]["name"] == "skript_reload" for t in ag.tool_schemas(False)))

# ---------------------------------------------------------------------------
section("one task at a time")
proj2 = project()
ws2 = A.add_workspace(proj2, "Other")
fake.hold = threading.Event()
fake.say("first")
fake.say("second")
r1 = A.start(ws, "one")
time.sleep(0.4)
r2 = A.start(ws2, "two")
time.sleep(0.6)
check("the second waits while the first is with the model", r2.state == "queued", r2.state)
try:
    A.start(ws, "again")
    check("the same workspace cannot run twice at once", False)
except ag.AgentError:
    check("the same workspace cannot run twice at once", True)
fake.hold.set()
run_until(r1)
run_until(r2)
fake.hold = None
check("then both finish", r1.state == "done" and r2.state == "done", (r1.state, r2.state))

section("keeping inside the context")
msgs = [{"role": "system", "content": "s" * 300}] + [
    {"role": "tool", "content": "x" * 9000, "tool_name": "read_file"} for _ in range(10)] + \
    [{"role": "user", "content": "last"}]
A._fit(msgs, 8192)
check("old tool results shrink first", A._tokens(msgs) <= 8192 * 0.8 and msgs[-1]["content"] == "last"
      and msgs[0]["content"].startswith("sss"), A._tokens(msgs))

section("models")
ms = A.models()
check("models list tool support and mode", {m["name"]: m["mode"] for m in ms} == {"coder:7b": "native", "plain:3b": "json"}, ms)
check("context length is capped at the model's own", ag.Agent(os.path.join(TMP, "d3"), lambda: dict(SETTINGS, num_ctx=65536)).options("plain:3b").options["num_ctx"] == 8192)

fake.close()
shutil.rmtree(TMP, ignore_errors=True)
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
