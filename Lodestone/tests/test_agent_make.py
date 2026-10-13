"""The assistant making things: servers through the Create wizard's own code,
plugins and mods with their dependencies, new projects, and the build tools
they need - with fakes of Lodestone's side (AgentOps) and of Ollama. Plus the
toolchain itself: Gradle's version API and checksum, unpacking, finding a JDK,
and the Paper plugin template."""
import json
import os
import shutil
import stat
import sys
import tempfile
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import agent as ag
import creator as createmod
import toolchain as tc
from fake_ollama import FakeOllama

fails = []


def check(n, c, e=""):
    print(("  ok   " if c else "  FAIL ") + n + (("  " + str(e)[:300]) if not c else ""))
    if not c:
        fails.append(n)


def section(t):
    print("\n-- " + t)


TMP = tempfile.mkdtemp(prefix="agent_make_")

# ---------------------------------------------------------------------------
section("Gradle, from its version API")
ZIPSRC = os.path.join(TMP, "gradle-9.9.9-bin.zip")
with zipfile.ZipFile(ZIPSRC, "w") as z:
    z.writestr("gradle-9.9.9/bin/gradle", "#!/bin/sh\necho gradle\n")
    z.writestr("gradle-9.9.9/bin/gradle.bat", "@echo gradle\n")
    z.writestr("gradle-9.9.9/lib/gradle.jar", "x")
import hashlib
SHA = hashlib.sha256(open(ZIPSRC, "rb").read()).hexdigest()
asked = []


def fetch(url):
    asked.append(url)
    return {"version": "9.9.9", "downloadUrl": "https://services.gradle.org/distributions/gradle-9.9.9-bin.zip",
            "checksum": SHA}


def download(url, dest, hashes=None, size=0, progress=None, log=None, label=""):
    asked.append(url)
    shutil.copy(ZIPSRC, dest)
    got = hashlib.sha256(open(dest, "rb").read()).hexdigest()
    if got != hashes["sha256"]:
        raise createmod.CreateError("checksum mismatch")


TOOLS = os.path.join(TMP, "tools")
logs = []
g = tc.ensure_gradle(TOOLS, logs.append, fetch=fetch, download=download)
check("asks services.gradle.org which release is current", asked[0] == tc.GRADLE_CURRENT, asked)
check("downloads the zip it names, checked against its checksum", asked[1].endswith("gradle-9.9.9-bin.zip"))
check("unpacks to tools/gradle-<version>/bin/gradle", g == tc.gradle_bin(os.path.join(TOOLS, "gradle-9.9.9")) and g, g)
check("and it is executable", os.name == "nt" or os.stat(g).st_mode & stat.S_IXUSR)
asked.clear()
check("the next time, the one here is used", tc.ensure_gradle(TOOLS, logs.append, fetch=fetch, download=download) == g
      and not asked)
for bad, why in (({"version": "1", "downloadUrl": "https://evil.example/gradle.zip", "checksum": "a" * 64}, "not on services"),
                 ({"version": "1", "downloadUrl": "https://services.gradle.org/distributions/x.zip", "checksum": "nope"}, "checksum")):
    try:
        tc.gradle_current(lambda u, b=bad: b)
        check("refused: %s" % why, False)
    except tc.ToolError as e:
        check("refused: %s" % why, True)


def bad_download(url, dest, hashes=None, size=0, progress=None, log=None, label=""):
    open(dest, "wb").write(b"tampered")
    raise createmod.CreateError("The download of Gradle does not match its published SHA-256.")


try:
    tc.ensure_gradle(os.path.join(TMP, "tools2"), fetch=fetch, download=bad_download)
    check("a download that does not match is not unpacked", False)
except tc.ToolError as e:
    check("a download that does not match is not unpacked", "SHA-256" in str(e)
          and not tc.installed_gradles(os.path.join(TMP, "tools2")), e)

section("finding a JDK")
JR = os.path.join(TMP, "java")
real_find = createmod.find_javas
jre = os.path.join(JR, "temurin-21", "bin", "java")
jdk = os.path.join(JR, "temurin-21-jdk", "bin", "java")
for p in (jre, jdk, os.path.join(os.path.dirname(jdk), "javac" + tc.EXE)):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    open(p, "w").write("")
createmod.find_javas = lambda root="", extra=(): [{"path": jre, "major": 21}, {"path": jdk, "major": 21}]
j = tc.find_jdk(JR, 17)
check("a JDK is a java with javac beside it - not the server's runtime", j and j["path"] == jdk, j)
env = tc.build_env(JR, base={"PATH": "/usr/bin"})
check("builds get JAVA_HOME and PATH pointing at it", env["JAVA_HOME"] == os.path.dirname(os.path.dirname(jdk))
      and env["PATH"].startswith(os.path.dirname(jdk)), env)
env = tc.build_env(JR, base={"PATH": "/usr/bin", "JAVA_HOME": TMP})
check("a JAVA_HOME the user set is left alone", env["JAVA_HOME"] == TMP)
createmod.find_javas = real_find

section("the Paper plugin template")
dest = os.path.join(TMP, "MyShop")
files = tc.scaffold_paper(dest, "My Shop", "com.example", "1.21.4", 21)
check("the files of a Gradle Paper plugin", files == [".gitignore", "build.gradle.kts", "settings.gradle.kts",
                                                     "src/main/java/com/example/myshop/MyShop.java",
                                                     "src/main/resources/plugin.yml"], files)
build = open(os.path.join(dest, "build.gradle.kts")).read()
check("paper-api for the server's version, from PaperMC's repository", 'paper-api:1.21.4-R0.1-SNAPSHOT"' in build
      and "repo.papermc.io/repository/maven-public/" in build)
check("compiled for the Java that version needs", "options.release.set(21)" in build)
yml = open(os.path.join(dest, "src/main/resources/plugin.yml")).read()
check("plugin.yml names the main class and the API version", "main: com.example.myshop.MyShop" in yml
      and "api-version: '1.21'" in yml, yml)
check("26.x versions keep both parts as the API version", tc.paper_api_version("26.1.2") == "26.1")
for args, why in (((dest, "x", "com.example", "1.21.4", 21), "a folder with files in it"),
                  ((os.path.join(TMP, "p2"), "x", "example", "1.21.4", 21), "a package with one part"),
                  ((os.path.join(TMP, "p2b"), "x", "com.1x", "1.21.4", 21), "a package part starting with a digit"),
                  ((os.path.join(TMP, "p3"), "x", "com.example", "1.16.5", 8), "Minecraft before 1.17"),
                  ((os.path.join(TMP, "p4"), "x", "com.example", "", 21), "no version")):
    try:
        tc.scaffold_paper(*args)
        check("refused: %s" % why, False)
    except tc.ToolError:
        check("refused: %s" % why, True)

# ---------------------------------------------------------------------------
section("the assistant making servers, content and projects")


class Ops:
    def __init__(self):
        self.created, self.installed, self.jobs = [], [], {}
        self.wrapper_ok = False

    def catalog(self):
        return {"software": [{"id": "paper", "name": "Paper", "about": "plugins"},
                             {"id": "fabric", "name": "Fabric", "about": "mods"}],
                "versions": [{"id": "1.21.4", "software": ["paper", "fabric"]}, {"id": "1.21.3", "software": ["paper"]}],
                "latest": "1.21.4", "errors": {"forge": "files.minecraftforge.net did not answer"}}

    def validate(self, spec):
        if spec["software"] not in ("paper", "fabric"):
            raise RuntimeError("Pick the server software.")
        return dict(spec, dir=spec["dir"] or os.path.join(TMP, "servers", spec["name"]), port=spec["port"],
                    max_ram=spec["max_ram"])

    def create(self, spec):
        self.created.append(spec)
        jid = "job%d" % len(self.created)
        self.jobs[jid] = {"t": time.time(), "spec": spec}
        return jid

    def job(self, jid, since=0):
        j = self.jobs[jid]
        if time.time() - j["t"] < 0.8:
            return {"state": "running", "log": [">>> downloading Paper 1.21.4"][since:], "next": 1}
        return {"state": "done", "log": [">>> downloading Paper 1.21.4", ">>> done"][since:], "next": 2,
                "result": {"server_id": "new1", "dir": j["spec"]["dir"]}, "warnings": []}

    def content_search(self, sid, query, source="modrinth"):
        return {"items": [{"name": "LuckPerms", "slug": "luckperms", "id": "Vebnzrzj", "author": "Luck",
                           "downloads": 900000, "compatible": True, "description": "A permissions plugin"}],
                "profile": {"server": "Lobby", "label": "Paper", "mc": "1.21.4"}}

    def content_plan(self, sid, items):
        return {"install": [{"name": "LuckPerms", "version": "5.4", "auto": False, "requires": []},
                            {"name": "Vault", "version": "1.7", "auto": True, "requires": ["LuckPerms"]}],
                "blocked": [], "warnings": []}

    def content_install(self, sid, items):
        self.installed.append((sid, items))
        return {"installed": [{"name": "LuckPerms"}, {"name": "Vault"}], "updated": [], "warnings": [], "restart": True}

    def java_required(self, mc):
        return 21

    def install_jdk(self, major, log):
        log("downloading JDK %d" % major)
        return "/java/temurin-%d-jdk/bin/java" % major

    def install_gradle(self, log):
        return "/tools/gradle-9.9.9/bin/gradle"

    def projects_root(self):
        return os.path.join(TMP, "projects")

    def scaffold_paper(self, folder, name, group, mc, java):
        return tc.scaffold_paper(folder, name, group, mc, java)

    def make_wrapper(self, folder, log):
        if not self.wrapper_ok:
            raise RuntimeError("Gradle is not installed")
        open(os.path.join(folder, "gradlew"), "w").write("#!/bin/sh\n")
        return True

    def build_env(self):
        return dict(os.environ, LODESTONE_TEST_ENV="1")


fake = FakeOllama()
fake.add_model("coder:7b", tools=True, ctx=32768)
SETTINGS = {"base_url": fake.base, "model": "coder:7b", "num_ctx": 8192, "approval_mode": "ask", "shell_enabled": True}


class Bridge:
    def info(self):
        return {"name": "Lobby", "running": False, "can_command": False, "can_start": True, "local": True}


OPS = Ops()
A = ag.Agent(os.path.join(TMP, "data"), lambda: SETTINGS, save=SETTINGS.update, ops=OPS,
             bridge_for=lambda sid: Bridge() if sid == "s1" else None,
             servers=lambda: [{"id": "s1", "name": "Lobby", "software": "paper", "mc": "1.21.4", "running": False}])
wsdir = tempfile.mkdtemp(dir=TMP)
ws = A.add_workspace(wsdir, "here")


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


fake.say("", tool_calls=[("server_software", {}), ("create_server", {"name": "Survival", "software": "paper", "mc": "1.21.4",
                                                                     "plugins": ["luckperms"]}),
                         ("create_server", {"name": "Bad", "software": "bukkit2", "mc": "1.21.4"})])
fake.say("Made it.")
run = A.start(ws, "make me a paper server with luckperms")
asked = []
evs = run_until(run, approve=lambda ev: (asked.append(ev), ("approve",))[1])
tools = {t["function"]["name"] for t in fake.requests[-1]["tools"]}
check("the making tools are offered", {"server_software", "create_server", "find_plugins", "install_plugins",
                                       "create_project", "install_tool"} <= tools, tools)
sw = results(evs, "server_software")[0]
check("server_software lists software, versions and what could not be reached", "Paper (paper)" in sw["output"]
      and "1.21.4, 1.21.3" in sw["output"] and "files.minecraftforge.net" in sw["output"], sw["output"])
cs = results(evs, "create_server")
check("create_server asks first, saying what and where", asked and asked[0]["kind"] == "server"
      and asked[0]["title"] == "Create Survival: paper 1.21.4" and "luckperms" in asked[0]["command"], asked[:1])
check("then creates it through the wizard's code, without accepting the EULA", OPS.created and
      OPS.created[0]["eula"] is False and OPS.created[0]["content"][0]["project_id"] == "luckperms", OPS.created)
check("follows the job and reports the new server", cs[0]["ok"] and "Server id: new1" in cs[0]["output"]
      and "EULA is not accepted" in cs[0]["output"], cs[0])
check("the install log streams into the chat", any(e["type"] == "output" and "downloading Paper" in e["text"] for e in evs))
check("a spec the wizard refuses is refused before anyone is asked", not cs[1]["ok"]
      and "Pick the server software" in cs[1]["summary"] and len(asked) == 1, cs[1])

SETTINGS["approval_mode"] = "all"
fake.say("", tool_calls=[("create_server", {"name": "Second", "software": "paper", "mc": "1.21.4", "accept_eula": True})])
fake.say("ok")
run = A.start(ws, "another, accept the eula")
asked = []
evs = run_until(run, approve=lambda ev: (asked.append(ev), ("approve",))[1])
check("accepting the EULA always asks the user - even with approvals off", len(asked) == 1
      and "accept the Minecraft EULA" in asked[0]["title"] and asked[0]["eula"] == "https://aka.ms/MinecraftEULA", asked)
check("and only then is it accepted", OPS.created[-1]["eula"] is True)
fake.say("", tool_calls=[("create_server", {"name": "Third", "software": "paper", "mc": "1.21.4", "accept_eula": True})])
fake.say("ok")
run = A.start(ws, "again but I say no")
evs = run_until(run, approve=lambda ev: ("reject", "I will read it first"))
check("declined, nothing is created", len(OPS.created) == 2 and "read it first" in results(evs, "create_server")[0]["output"])
SETTINGS["approval_mode"] = "ask"

fake.say("", tool_calls=[("find_plugins", {"query": "permissions", "server": "Lobby"}),
                         ("install_plugins", {"server": "Lobby", "items": ["luckperms"]})])
fake.say("Installed.")
run = A.start(ws, "add a permissions plugin to Lobby")
asked = []
evs = run_until(run, approve=lambda ev: (asked.append(ev), ("approve",))[1])
fp = results(evs, "find_plugins")[0]
check("find_plugins says what fits which server", "LuckPerms [luckperms]" in fp["output"] and "Paper 1.21.4" in fp["output"], fp)
check("install_plugins shows every file, dependencies included, before installing", asked
      and "Vault 1.7 (needed by LuckPerms)" in asked[0]["command"] and asked[0]["title"].startswith("Install 2 file"), asked)
ip = results(evs, "install_plugins")[0]
check("installs into the named server and says to restart", OPS.installed == [("s1", [{"source": "modrinth",
                                                                                       "project_id": "luckperms"}])]
      and "restart it" in ip["output"], (OPS.installed, ip))

fake.say("", tool_calls=[("create_project", {"template": "paper-plugin", "name": "Shop Plugin", "mc": "1.21.4",
                                             "package": "com.armadillo"})])
fake.say("Started your plugin.")
run = A.start(ws, "start a new paper plugin called Shop Plugin")
asked = []
evs = run_until(run, approve=lambda ev: (asked.append(ev), ("approve",))[1])
cp = results(evs, "create_project")[0]
proj = os.path.join(TMP, "projects", "Shop-Plugin")
check("create_project asks first, saying what and where", asked and asked[0]["kind"] == "write"
      and "Paper plugin for Minecraft 1.21.4 (Java 21" in asked[0]["command"] and proj in asked[0]["command"], asked)
check("writes the plugin", os.path.isfile(os.path.join(proj, "src/main/java/com/armadillo/shopplugin/ShopPlugin.java")))
check("says how to get the wrapper when Gradle is missing", "install_tool gradle" in cp["output"], cp["output"])
check("and opens it as a workspace", any(w["root"] == proj for w in A.project_workspaces())
      and any(e["type"] == "workspace_added" for e in evs))
OPS.wrapper_ok = True
fake.say("", tool_calls=[("create_project", {"template": "paper-plugin", "name": "Shop Plugin", "mc": "1.21.4"}),
                         ("create_project", {"template": "paper-plugin", "name": "Two", "mc": "1.21.4",
                                             "folder": "/etc/lodestone-proj"}),
                         ("create_project", {"template": "paper-plugin", "name": "Wrapped", "mc": "1.21.4"})])
fake.say("ok")
run = A.start(ws, "more projects")
evs = run_until(run, approve=lambda ev: ("approve",))
cps = results(evs, "create_project")
check("a folder with files in it is refused", not cps[0]["ok"] and "already has files" in cps[0]["summary"], cps[0])
check("a system folder is refused", not cps[1]["ok"] and "system folder" in cps[1]["summary"], cps[1])
check("with Gradle there, the wrapper is set up", cps[2]["ok"] and "wrapper set up" in cps[2]["output"]
      and os.path.isfile(os.path.join(TMP, "projects", "Wrapped", "gradlew")), cps[2])

fake.say("", tool_calls=[("install_tool", {"tool": "jdk", "version": 21}), ("install_tool", {"tool": "gradle"}),
                         ("run_shell", {"command": "echo $LODESTONE_TEST_ENV"})])
fake.say("ok")
run = A.start(ws, "set up java and gradle")
asked = []
evs = run_until(run, approve=lambda ev: (asked.append(ev), ("approve",))[1])
it = results(evs, "install_tool")
check("install_tool asks, naming the source", [a["kind"] for a in asked[:2]] == ["install", "install"]
      and "Adoptium" in asked[0]["title"] and "services.gradle.org" in asked[1]["title"], asked)
check("and installs", it[0]["ok"] and "temurin-21-jdk" in it[0]["output"] and it[1]["ok"], it)
check("commands run in the build environment (JAVA_HOME at the JDK)",
      "1" in results(evs, "run_shell")[0]["output"].splitlines()[-1], results(evs, "run_shell")[0]["output"])

B = ag.Agent(os.path.join(TMP, "data2"), lambda: SETTINGS, save=SETTINGS.update)
fake.say("ok")
run = B.start(B.add_workspace(wsdir, "x"), "hi")
seq, end = 0, time.time() + 15
while time.time() < end and not any(e["type"] == "done" for e in run.since(0)):
    time.sleep(0.2)
check("without Lodestone's side, the making tools are not offered",
      not {"create_server", "install_tool"} & {t["function"]["name"] for t in fake.requests[-1]["tools"]})

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
