"""Setting playit.gg up for the user: how it would be installed on each OS,
winget's answers read right, playit's own setup driven and its claim link
passed on, the CLI found beside its service, and the whole job through the
engine's routes - against a fake playit CLI that behaves like playit 1.x."""
import importlib.util
import json
import os
import stat
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
_TESTHOME = tempfile.mkdtemp(prefix="lodestone_playit_")
for k in ("HOME", "USERPROFILE", "LODESTONE_TEST_HOME"):
    os.environ[k] = _TESTHOME

from networking import playit_setup as ps
from networking import tunnel

fails = []


def check(n, c, e=""):
    print(("  ok   " if c else "  FAIL ") + n + (("  " + str(e)[:300]) if not c else ""))
    if not c:
        fails.append(n)


def section(t):
    print("\n-- " + t)


# a fake playit 1.x CLI: the service-style help, setup with a claim link, and
# start/status/stop of a service that reports its tunnel
FAKE = os.path.join(_TESTHOME, "bin", "playit")
os.makedirs(os.path.dirname(FAKE))
STATE = os.path.join(_TESTHOME, "playit-state")
open(FAKE, "w").write('''#!%s
import os, sys, time
state = %r
cmd = sys.argv[1] if len(sys.argv) > 1 else ""
mode = open(state + ".mode").read().strip() if os.path.exists(state + ".mode") else "fresh"
if cmd == "--help":
    print("Usage: playit-cli [OPTIONS] [COMMAND]\\n\\nCommands:\\n  version  Print version\\n  start    Start the installed playitd service\\n  stop     Stop\\n  status   Show the status\\n  setup    Setup playit by provisioning a new secret to playitd")
elif cmd == "version":
    print("1.0.12")
elif cmd == "setup":
    if mode == "claimed":
        print("The playit service already has a configured secret. Run `playit reset` before claiming a new agent.", file=sys.stderr)
        sys.exit(1)
    if mode == "starting":
        open(state + ".mode", "w").write("fresh")
        print("The playit service is still starting. Try setup again in a few seconds.", file=sys.stderr)
        sys.exit(1)
    print("Preparing setup...", flush=True)
    print("Open this link to finish setting up playit:\\nhttps://playit.gg/claim/a1b2c3d4e5", flush=True)
    time.sleep(0.8)
    if mode == "reject":
        print("Setup was not approved in the browser.", flush=True)
        sys.exit(1)
    print("Program approved. Finishing setup...", flush=True)
    open(state + ".mode", "w").write("claimed")
    print("playit setup is complete. The background service is ready.", flush=True)
elif cmd == "start":
    open(state + ".run", "w").write("1")
    print("playitd service started")
elif cmd == "stop":
    if os.path.exists(state + ".run"):
        os.remove(state + ".run")
    print("stopped")
elif cmd == "status":
    if os.path.exists(state + ".run"):
        print("playitd is running\\nTunnel: brave-fox.ply.gg:41234 -> 127.0.0.1:25565 (tunnel is running)")
    else:
        print("playitd is not running")
''' % (sys.executable, STATE))
os.chmod(FAKE, os.stat(FAKE).st_mode | stat.S_IEXEC)


def set_mode(m):
    open(STATE + ".mode", "w").write(m)


# ---------------------------------------------------------------------------
section("how playit would be installed")
p = ps.plan("win32", which=lambda n: r"C:\Users\x\AppData\Local\Microsoft\WindowsApps\winget.exe" if n == "winget" else None)
check("Windows with winget installs automatically", p["can_auto"] and p["method"] == "winget", p)
p = ps.plan("win32", which=lambda n: None)
check("Windows without winget says how to get it", not p["can_auto"] and "App Installer" in p["detail"], p)
p = ps.plan("linux")
check("Linux gets playit's official apt commands", not p["can_auto"] and any("apt install playit" in c
                                                                              for c in p["commands"]), p)
check("the winget command is the official package, exact and silent",
      ps.WINGET_ARGS[:4] == ["install", "--id", "DevelopedMethods.playit", "--exact"] and "--silent" in ps.WINGET_ARGS)

# ---------------------------------------------------------------------------
section("installing")
real_find = ps.find_agent
calls, logs = [], []
state = {"installed": False}
ps.find_agent = lambda explicit="": FAKE if state["installed"] else ""
WIN = lambda n: "winget.exe" if n == "winget" else None
_plan = ps.plan
ps.plan = lambda platform=None, which=None: _plan("win32", which=WIN)


def winget_ok(argv, log, timeout, cancel=None, on_line=None):
    calls.append(argv)
    log("Found playit [DevelopedMethods.playit] Version 1.0.12")
    log("Successfully verified installer hash")
    state["installed"] = True
    return 0, "Successfully installed"


got = ps.install(logs.append, runner=winget_ok)
check("winget runs, then the CLI is found", got == FAKE and calls[0][0] == "winget.exe"
      and calls[0][1:] == ps.WINGET_ARGS, (got, calls))
check("winget's own lines reach the log", "Successfully verified installer hash" in logs)
calls.clear()
check("already installed: nothing is run", ps.install(logs.append, runner=winget_ok) == FAKE and not calls)
state["installed"] = False


def winget_already(argv, log, timeout, cancel=None, on_line=None):
    state["installed"] = True
    return -1978335189, "Found an existing package already installed. No available upgrade found."


check("winget's 'already installed' code is not a failure", ps.install(logs.append, runner=winget_already) == FAKE)
state["installed"] = False


def winget_cancelled(argv, log, timeout, cancel=None, on_line=None):
    return 1602, "Installer failed with exit code: 1602"


try:
    ps.install(logs.append, runner=winget_cancelled)
    check("a cancelled administrator prompt is explained", False)
except ps.SetupError as e:
    check("a cancelled administrator prompt is explained", "permission" in str(e), e)
ps.plan = lambda platform=None, which=None: _plan("linux")
try:
    ps.install(logs.append, runner=winget_ok)
    check("where it cannot install, it says how", False)
except ps.SetupError as e:
    check("where it cannot install, it says how", "terminal" in str(e), e)
ps.plan = _plan
ps.find_agent = real_find

# ---------------------------------------------------------------------------
section("running a command, cleanly")
noisy = os.path.join(_TESTHOME, "noisy.py")
open(noisy, "w").write('import sys, time\nsys.stdout.write("  -\\r  \\\\\\r  |\\r")\nprint("Downloading x")\n'
                       'print("  ████████▒▒▒▒  1.5 MB / 3.0 MB")\nprint("Done")\nsys.exit(3)\n')
seen = []
code, text = ps._stream([sys.executable, noisy], seen.append, 30)
check("spinners and progress bars are dropped, words kept", seen == ["Downloading x", "Done"] and code == 3, seen)
slow = os.path.join(_TESTHOME, "slow.py")
open(slow, "w").write("import time\nprint('waiting', flush=True)\ntime.sleep(30)\n")
ev = threading.Event()
threading.Timer(0.8, ev.set).start()
t0 = time.time()
try:
    ps._stream([sys.executable, slow], lambda s: None, 60, ev)
    check("Stop ends it", False)
except ps.SetupError as e:
    check("Stop ends it", "cancelled" in str(e) and time.time() - t0 < 10, e)

# ---------------------------------------------------------------------------
section("playit's own setup")
set_mode("fresh")
claims, logs = [], []
how = ps.setup(FAKE, logs.append, claims.append)
check("setup finishes once approved", how == "set up", how)
check("the claim link is passed on as soon as it is printed", claims == ["https://playit.gg/claim/a1b2c3d4e5"], claims)
check("only once", len(claims) == 1)
check("already set up is fine", ps.setup(FAKE, logs.append) == "already set up")
set_mode("starting")
check("a service still starting is tried again", ps.setup(FAKE, logs.append) == "set up")
set_mode("reject")
try:
    ps.setup(FAKE, logs.append)
    check("a rejected claim says what to do", False)
except ps.SetupError as e:
    check("a rejected claim says what to do", "not approved" in str(e), e)

# ---------------------------------------------------------------------------
section("finding the CLI beside its service")
d = os.path.join(_TESTHOME, "playit_gg", "bin")
os.makedirs(d)
for n in ("playitd", "playitd-tray", "playit"):
    open(os.path.join(d, n), "w").write("")
real_dirs = tunnel._candidate_dirs
tunnel._candidate_dirs = lambda: [d]
found = tunnel.find_agent()
check("playit is picked, not the playitd service", found == os.path.join(d, "playit"), found)
os.remove(os.path.join(d, "playit"))
check("and the service alone is not mistaken for it", tunnel.find_agent() == "", tunnel.find_agent())
tunnel._candidate_dirs = real_dirs

# ---------------------------------------------------------------------------
section("the whole job through the engine")
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
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}")


srvdir = tempfile.mkdtemp(dir=_TESTHOME)
open(os.path.join(srvdir, "server.properties"), "w").write("server-port=25565\n")
srv = m.PANEL.upsert({"kind": "local", "name": "SMP", "local": {"cwd": srvdir}})
m.PANEL.cfg["active"] = srv["id"]
s, j = call("GET", "/api/hosting/playit")
check("the plan is there for the page", s == 200 and "can_auto" in j["plan"], j)

set_mode("fresh")
ps.find_agent = lambda explicit="": FAKE      # 'installed' - the winget step was tested above
s, j = call("POST", "/api/hosting/playit/setup", {})
check("the job starts", s == 200 and j["job"]["kind"] == "playit", j)
jid = j["job"]["id"]
s, again = call("POST", "/api/hosting/playit/setup", {})
check("pressing it twice does not start a second", again["job"]["id"] == jid)
claim, job, end = "", {}, time.time() + 40
while time.time() < end:
    s, jj = call("GET", "/api/jobs?id=" + jid)
    job = jj["job"]
    claim = claim or job.get("claim_url", "")
    if job["state"] != "running":
        break
    time.sleep(0.3)
check("the claim link reaches the page while it waits", claim == "https://playit.gg/claim/a1b2c3d4e5", job)
check("the job finishes", job["state"] == "done", job.get("error") or job)
check("every step is done", [st["state"] for st in job["steps"]] == ["done"] * 3, job["steps"])
check("the tunnel address is handed back", (job.get("result") or {}).get("address") == "brave-fox.ply.gg:41234",
      job.get("result"))
hc = m.PANEL.hosting_cfg(srv["id"])
check("the agent's path is saved for next time", hc.get("tunnel_agent") == FAKE and hc.get("tunnel_provider") == "playit",
      hc)
check("and the server is in Tunnel mode", m.PANEL.hosting_manager(srv["id"]).mode == "tunnel")
m.PANEL.hosting_manager(srv["id"]).tunnel.provider.stop()
ps.find_agent = real_find

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
