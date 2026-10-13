"""The assistant doing the work instead of describing it: a request to make
something that gets a tutorial back is sent back to the tools; the matching
skill is loaded; a task sees only the tools it needs (and more_tools turns on
the rest); the instructions and tools always fit the context; and a project the
assistant creates can be written, built and put on a server in the same task -
replaying "add /spawn with plugin and java to 26.2 paper" in Agent mode."""
import json
import os
import stat
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
_TESTHOME = tempfile.mkdtemp(prefix="lodestone_act_")
for k in ("HOME", "USERPROFILE"):
    os.environ[k] = _TESTHOME

import agent as ag
import toolchain as tc
from fake_ollama import FakeOllama

fails = []


def check(n, c, e=""):
    print(("  ok   " if c else "  FAIL ") + n + (("  " + str(e)[:300]) if not c else ""))
    if not c:
        fails.append(n)


def section(t):
    print("\n-- " + t)


TUTORIAL = ("To add a `/spawn` command to your 26.2 Paper server using a plugin, you'll need to create a custom plugin. "
            "Here's a step-by-step guide:\n\n**Step 1: Set Up Your Project**\n1. Create a new folder for your plugin\n"
            "```yaml\nname: MySpawnPlugin\nmain: com.myspawnplugin.Main\n```\n")


class Ops:
    """Lodestone's side, faked: a project that builds with a fake gradlew."""

    def __init__(self):
        self.jars = []

    def java_required(self, mc):
        return 25

    def projects_root(self):
        return os.path.join(_TESTHOME, "Lodestone Projects")

    def scaffold_paper(self, folder, name, group, mc, java):
        return tc.scaffold_paper(folder, name, group, mc, java)

    def make_wrapper(self, folder, log):
        gw = os.path.join(folder, "gradlew")
        open(gw, "w").write("#!/bin/sh\nmkdir -p build/libs && echo jar > build/libs/SpawnPlugin-1.0.0.jar\n"
                            "echo '> Task :compileJava'\necho 'BUILD SUCCESSFUL in 3s'\n")
        os.chmod(gw, os.stat(gw).st_mode | stat.S_IEXEC)
        return True

    def install_jar(self, sid, jar):
        self.jars.append((sid, jar))
        return os.path.join(SRV, "plugins", os.path.basename(jar))

    def build_env(self):
        return dict(os.environ)

    def catalog(self):
        return {"software": [], "versions": [], "latest": ""}


class Bridge:
    def __init__(self):
        self.calls, self.running, self.seq = [], True, 0

    def info(self):
        return {"name": "Survival", "running": self.running, "ready": self.running, "can_command": self.running,
                "can_start": True, "local": True, "players": []}

    def console(self, n):
        return ["[INFO]: [SpawnPlugin] Enabling SpawnPlugin v1.0.0"]

    def last_seq(self):
        return self.seq

    def since(self, seq):
        return []

    def stop(self):
        self.calls.append("stop")
        self.running = False
        return True, "Stopping."

    def start(self):
        self.calls.append("start")
        self.running = True
        return True, "Starting."


fake = FakeOllama()
fake.add_model("qwen2.5-coder:7b", tools=True, ctx=32768)
SETTINGS = {"base_url": fake.base, "model": "qwen2.5-coder:7b", "num_ctx": 8192, "approval_mode": "ask"}
SRV = os.path.join(_TESTHOME, "srv")
os.makedirs(os.path.join(SRV, "plugins"))
open(os.path.join(SRV, "server.properties"), "w").write("server-port=25565\n")
OPS, BR = Ops(), Bridge()
A = ag.Agent(os.path.join(_TESTHOME, "data"), lambda: SETTINGS, save=SETTINGS.update, ops=OPS,
             bridge_for=lambda sid: BR if sid == "s1" else None,
             servers=lambda: [{"id": "s1", "name": "Survival", "software": "paper", "mc": "26.2", "running": True}])
sws = {"id": "srv-s1", "name": "Survival", "root": SRV, "kind": "server", "server_id": "s1"}


def run_until(run, approve=None, timeout=40):
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


def tools_of(req):
    return {t["function"]["name"] for t in req.get("tools") or []}


# ---------------------------------------------------------------------------
section("'add /spawn with plugin and java to 26.2 paper', in Agent mode")
PROJ = os.path.join(_TESTHOME, "Lodestone Projects", "SpawnPlugin")
JAVA = os.path.join(PROJ, "src", "main", "java", "com", "example", "spawnplugin", "SpawnPlugin.java")
n0 = len(fake.requests)
fake.say(TUTORIAL)
fake.say("", tool_calls=[("create_project", {"template": "paper-plugin", "name": "SpawnPlugin", "mc": "26.2"})])
fake.say("", tool_calls=[("write_file", {"path": JAVA, "content": "package com.example.spawnplugin;\n// /spawn\n"})])
fake.say("", tool_calls=[("run_build", {"folder": PROJ})])
fake.say("", tool_calls=[("install_jar", {"jar": os.path.join(PROJ, "build", "libs", "SpawnPlugin-1.0.0.jar")}),
                         ("restart_server", {})])
fake.say("Done: made SpawnPlugin with /spawn, built it, put it in plugins/ and restarted Survival.")
run = A.start(sws, "add /spawn with plugin and java to 26.2 paper", mode="agent")
asked = []
evs = run_until(run, approve=lambda ev: (asked.append(ev), ("approve",))[1])
first = fake.requests[n0]
check("the paper-plugin skill is loaded with the request", "<skill>" in first["messages"][-1]["content"]
      and "Paper plugins" in first["messages"][-1]["content"]
      and any(e["type"] == "skill" and e["name"] == "paper-plugin" and e["how"] == "matched" for e in evs))
check("the first step already has the tools to create, build and install", {"create_project", "install_jar",
                                                                             "run_build", "restart_server"}
      <= tools_of(first), sorted(tools_of(first)))
check("but not git or the web, which the request does not need", not {"git_commit", "github_search"} & tools_of(first))
check("the tutorial reply is sent back to the tools", any(e["type"] == "note" and "explained instead" in e["text"]
                                                          for e in evs)
      and fake.requests[n0 + 1]["messages"][-1]["content"].startswith("Do not explain the steps"))
check("then it creates the project", results(evs, "create_project") and results(evs, "create_project")[0]["ok"],
      results(evs, "create_project"))
check("writes the plugin's code inside it - outside the server's folder", results(evs, "write_file")[0]["ok"]
      and "/spawn" in open(JAVA).read(), results(evs, "write_file"))
check("builds it there", results(evs, "run_build")[0]["ok"] and "BUILD SUCCESSFUL" in results(evs, "run_build")[0]["output"],
      results(evs, "run_build"))
check("puts the jar on the server", OPS.jars == [("s1", os.path.join(PROJ, "build", "libs", "SpawnPlugin-1.0.0.jar"))],
      OPS.jars)
check("and restarts it", BR.calls == ["stop", "start"], BR.calls)
check("nothing waited for approval in Agent mode", not asked)
check("the answer is the summary, not the tutorial", run.final.startswith("Done: made SpawnPlugin"), run.final)

# ---------------------------------------------------------------------------
section("which tools a task sees")
PW = A.add_workspace(PROJ, "proj")


def first_tools(message, ws=PW, mode=None):
    fake.say("ok")
    r = A.start(ws, message, mode=mode)
    run_until(r)
    return tools_of(fake.requests[-1])


t = first_tools("hello")
check("a greeting: the core tools and more_tools", {"read_file", "write_file", "run_build", "more_tools"} <= t
      and not {"start_server", "create_server", "git_commit"} & t, sorted(t))
check("start the server: the server tools", "start_server" in first_tools("start the server please"))
check("commit: git", "git_commit" in first_tools("commit my changes"))
check("a server workspace always has its server tools", "read_console" in first_tools("hello", sws))
check("research: the web, and nothing that changes things", "web_search" not in first_tools("hello")
      and not {"write_file", "run_build"} & first_tools("why is it slow", mode="research"))

section("more_tools, and tools from groups that are off")
fake.say("", tool_calls=[("more_tools", {"groups": ["git"]})])
fake.say("", tool_calls=[("git_status", {})])
fake.say("ok")
run = A.start(PW, "hello")
evs = run_until(run)
check("more_tools turns a group on for the next step", "git_status" in tools_of(fake.requests[-2])
      and results(evs, "more_tools")[0]["ok"], [results(evs, "more_tools")])
fake.say("", tool_calls=[("git_log", {})])
fake.say("ok")
run = A.start(PW, "hello again")
evs = run_until(run)
check("a tool from a group that is off just works (its group turns on)", results(evs, "git_log")[0]["ok"],
      results(evs, "git_log"))
fake.say("", tool_calls=[("write_file", {"path": "x.txt", "content": "x"})])
fake.say("ok")
run = A.start(PW, "look at it", mode="research")
evs = run_until(run)
check("but never a writing tool in Research mode", not results(evs, "write_file")[0]["ok"]
      and not os.path.exists(os.path.join(PROJ, "x.txt")))

section("explaining instead of doing")
fake.say(TUTORIAL)
fake.say(TUTORIAL)
fake.say(TUTORIAL)
run = A.start(PW, "make a /home command", mode="agent")
evs = run_until(run)
check("it is sent back at most twice, then the answer stands", sum(1 for e in evs if e["type"] == "note"
                                                                   and "explained" in e["text"]) == 2
      and run.final.startswith("To add"), run.final[:60])
fake.say(TUTORIAL)
run = A.start(PW, "how would I add a /home command myself?")
evs = run_until(run)
check("a question about how is answered, not pushed", not any(e["type"] == "note" and "explained" in e["text"]
                                                             for e in evs))

section("the instructions and tools always fit")
SETTINGS["num_ctx"] = 2048
fake.say("ok")
run = A.start(PW, "start the server and commit")
evs = run_until(run)
check("a context too small for the tools is raised, and said", fake.requests[-1]["options"]["num_ctx"] == 16384
      and SETTINGS["num_ctx"] == 16384 and any(e["type"] == "note" and "Raised the context" in e["text"] for e in evs),
      (fake.requests[-1]["options"].get("num_ctx"), SETTINGS["num_ctx"]))
msgs = [{"role": "system", "content": "s" * 3000}] + [{"role": "tool", "content": "x" * 6000} for _ in range(12)] + \
       [{"role": "user", "content": "now"}]
A._fit(msgs, 8192, reserve=4000)
check("trimming counts what the tools take", A._tokens(msgs) <= int(8192 * 0.8) - 4000 + 200 and msgs[-1]["content"] == "now",
      A._tokens(msgs))

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
