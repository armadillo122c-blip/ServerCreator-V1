"""Self-update from GitHub: checking, installing, going back, and restarting.

GitHub is a real HTTP server here speaking the REST API's shapes - the commits
list filtered by path, and a zipball whose top folder is owner-repo-<sha> -
because that is where this breaks: a commit outside the Lodestone folder
counted as an update, a zip of the wrong commit, a file deleted that was never
the updater's to delete.

The restart is run for real against a stand-in lodestone.py that serves
/api/me, including the case where the new version does not come up and the
old one has to be put back.

Nothing in this suite touches the internet, and nothing is installed into the
real Lodestone folder - every "install" goes into a temporary one.
"""

import hashlib
import importlib.util
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
_TESTHOME = os.environ.get("LODESTONE_TEST_HOME")
if not _TESTHOME:
    _TESTHOME = tempfile.mkdtemp(prefix="lodestone_update_")
    os.environ["LODESTONE_TEST_HOME"] = _TESTHOME
os.environ["HOME"] = _TESTHOME
os.environ["USERPROFILE"] = _TESTHOME
for var in ("GITHUB_TOKEN", "GH_TOKEN"):
    os.environ.pop(var, None)

import updater as up

fails = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name +
          (("  " + str(extra)[:300]) if not cond else ""))
    if not cond:
        fails.append(name)


def section(title):
    print("\n-- %s" % title)


# ---------------------------------------------------------------------------
#  a fake GitHub
# ---------------------------------------------------------------------------

REPO = "owner/lode"
COMMITS = []          # newest first: {"sha", "title", "files": {path: bytes}}
STATUS = {}           # forced replies: path -> (code, headers)
SEEN_AUTH = []


def snapshot(sha):
    """The whole repo at a commit: every file of every commit up to it."""
    files = {}
    for c in reversed(COMMITS):
        files.update(c["files"])
        for gone in c.get("deleted", ()):
            files.pop(gone, None)
        if c["sha"] == sha:
            break
    return files


def zipball(sha, top=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        top = top or "owner-lode-%s" % sha[:7]
        z.writestr(top + "/", "")
        for path, data in snapshot(sha).items():
            z.writestr(top + "/" + path, data)
    return buf.getvalue()


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlparse(self.path)
        SEEN_AUTH.append(self.headers.get("Authorization") or "")
        if u.path in STATUS:
            code, headers = STATUS[u.path]
            self.send_response(code)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")
            return
        if u.path == "/repos/%s/commits" % REPO:
            q = parse_qs(u.query)
            path = (q.get("path") or [""])[0]
            out = []
            for c in COMMITS:
                if path and not any(f.startswith(path + "/") for f in list(c["files"]) + list(c.get("deleted", ()))):
                    continue
                out.append({"sha": c["sha"], "commit": {"message": c["title"] + "\n\nbody",
                            "author": {"name": "Armadillo", "date": "2026-10-12T10:00:00Z"},
                            "committer": {"date": "2026-10-12T10:00:00Z"}}})
            return self._send(200, json.dumps(out).encode(), "application/json")
        if u.path.startswith("/repos/%s/zipball/" % REPO):
            sha = u.path.rsplit("/", 1)[1]
            # GitHub answers with a redirect to codeload; follow the same path here
            self.send_response(302)
            self.send_header("Location", "/codeload/%s" % sha)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if u.path.startswith("/codeload/"):
            sha = u.path.rsplit("/", 1)[1]
            top = WRONG_TOP.get(sha)
            return self._send(200, zipball(sha, top), "application/zip")
        self._send(404, b'{"message":"Not Found"}', "application/json")

    def _send(self, code, data, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


WRONG_TOP = {}
srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
up.API = "http://127.0.0.1:%d" % srv.server_address[1]

CFG = {"repo": REPO, "branch": "main", "path": "Lodestone", "use_git_login": False}


def sha_of(n):
    return hashlib.sha1(("commit-%d" % n).encode()).hexdigest()


GOOD_PY = b"print('lodestone')\n"
COMMITS.insert(0, {"sha": sha_of(1), "title": "First version", "files": {
    "Lodestone/lodestone.py": GOOD_PY,
    "Lodestone/creator.py": b"X = 1\n",
    "Lodestone/old_helper.py": b"Y = 1\n",
    "Lodestone/desktop/MainWindow.cs": b"class A {}\n",
    "Lodestone/dist/should-never-ship.dll": b"binary",
    "README.md": b"top-level readme\n",
}})


def fresh_root():
    d = tempfile.mkdtemp(dir=_TESTHOME)
    os.makedirs(os.path.join(d, "dist"))
    open(os.path.join(d, "dist", "Lodestone.dll"), "wb").write(b"the user's own build")
    open(os.path.join(d, "my-notes.txt"), "w").write("mine")
    return d


# ---------------------------------------------------------------------------
section("settings and what is protected")
st = up.settings({"hours": "999", "repo": " owner/lode/ ", "nonsense": 1})
check("hours are clamped", st["hours"] == 168)
check("the repo is tidied", st["repo"] == "owner/lode")
check("unknown keys are dropped", "nonsense" not in st)
check("the old CLAUDE repo, saved by the settings form, moves to ServerCreator-V1",
      up.settings({"repo": "armadillo122c-blip/CLAUDE"})["repo"] == "armadillo122c-blip/ServerCreator-V1")
check("a repo the user chose themselves is kept",
      up.settings({"repo": "someone/fork"})["repo"] == "someone/fork")
for rel in ("dist/x.dll", "desktop/bin/Debug/a.dll", "desktop/obj/x", ".lodestone-update/x", "logs/latest.log",
            "x.bak-2026", "networking/__pycache__/a.pyc", ".lodestone-version.json"):
    check("%s is never touched" % rel, up.protected(rel))
for rel in ("lodestone.py", "desktop/MainWindow.cs", "networking/hosting.py", "Lodestone.vbs"):
    check("%s is updated" % rel, not up.protected(rel))

section("logging in")
check("no login when git's is off and none is set", up.auth({"use_git_login": False}) == ("", "none"))
check("a token in the settings wins", up.auth({"token": "t1"})[0] == "t1")
os.environ["GH_TOKEN"] = "t2"
check("then GH_TOKEN", up.auth({"use_git_login": False}) == ("t2", "the GH_TOKEN environment variable"))
os.environ.pop("GH_TOKEN")
STATUS["/repos/%s/commits" % REPO] = (404, {})
try:
    up.check(fresh_root(), CFG)
    check("a 404 without a login explains private repos", False)
except up.UpdateError as e:
    check("a 404 without a login explains private repos", "private" in str(e) and "login" in str(e), e)
STATUS["/repos/%s/commits" % REPO] = (401, {})
try:
    up.check(fresh_root(), dict(CFG, token="bad"))
except up.UpdateError as e:
    check("a 401 says the token is wrong", "401" in str(e) and "token" in str(e), e)
check("a token is only ever sent to GitHub itself", SEEN_AUTH[-1] == "", SEEN_AUTH[-1:])  # this host is not github.com
STATUS["/repos/%s/commits" % REPO] = (403, {"X-RateLimit-Remaining": "0",
                                             "X-RateLimit-Reset": str(int(time.time()) + 600)})
try:
    up.check(fresh_root(), CFG)
except up.UpdateError as e:
    check("a used-up rate limit says when it resets", "rate limit" in str(e) and "minutes" in str(e), e)
STATUS.clear()

# ---------------------------------------------------------------------------
section("first install into a copy with no stamp")
root = fresh_root()
info = up.check(root, CFG)
check("an unstamped copy is offered the latest", info["available"] and info["current"] is None)
check("the commit is described", info["latest"]["title"] == "First version" and info["latest"]["short"] == sha_of(1)[:7])
stage = up.download(root, CFG, info["latest"]["sha"])
staged = sorted(os.path.relpath(os.path.join(dp, f), stage).replace("\\", "/")
                for dp, _, fs in os.walk(stage) for f in fs)
check("only the Lodestone folder is unpacked", "README.md" not in staged and "lodestone.py" in staged, staged)
check("build output in the repo is never unpacked", not any(s.startswith("dist/") for s in staged), staged)
rep = up.apply(root, stage, info["latest"], CFG)
check("files are added", sorted(rep["added"]) == ["creator.py", "desktop/MainWindow.cs", "lodestone.py", "old_helper.py"],
      rep["added"])
check("the user's own files are left alone", open(os.path.join(root, "my-notes.txt")).read() == "mine")
check("and so is their build", open(os.path.join(root, "dist", "Lodestone.dll"), "rb").read() == b"the user's own build")
check("the copy is stamped", up.stamp(root)["sha"] == sha_of(1))
check("a desktop change asks for a rebuild", rep["desktop"] and
      os.path.isfile(os.path.join(root, up.WORK, up.REBUILD_FLAG)))
check("the stage is cleaned up", not os.path.exists(stage))
again = up.check(root, CFG)
check("afterwards it is up to date", not again["available"] and again["behind"] == 0, again)

# ---------------------------------------------------------------------------
section("an update: changed, new and removed files")
os.remove(os.path.join(root, up.WORK, up.REBUILD_FLAG))
COMMITS.insert(0, {"sha": sha_of(2), "title": "Not ours", "files": {"README.md": b"changed elsewhere\n"}})
check("a commit outside the folder is not an update", not up.check(root, CFG)["available"])
COMMITS.insert(0, {"sha": sha_of(3), "title": "Second version", "files": {
    "Lodestone/lodestone.py": b"print('lodestone 2')\n",
    "Lodestone/updater_new.py": b"Z = 2\n"},
    "deleted": ["Lodestone/old_helper.py"]})
open(os.path.join(root, "user_plugin.py"), "w").write("mine too\n")
info = up.check(root, CFG)
check("the new commit is offered", info["available"] and info["behind"] == 1 and
      [c["title"] for c in info["commits"]] == ["Second version"], info)
before = {f: open(os.path.join(root, f), "rb").read() for f in ("lodestone.py", "old_helper.py")}
rep = up.apply(root, up.download(root, CFG, info["latest"]["sha"]), info["latest"], CFG)
check("a changed file is reported", rep["changed"] == ["lodestone.py"], rep)
check("a new file is reported", rep["added"] == ["updater_new.py"], rep)
check("a file the update removed is removed", rep["removed"] == ["old_helper.py"] and
      not os.path.exists(os.path.join(root, "old_helper.py")), rep)
check("a file the user added is not", os.path.isfile(os.path.join(root, "user_plugin.py")))
check("no desktop rebuild when the desktop did not change", not rep["desktop"])
check("the new content is in place", open(os.path.join(root, "lodestone.py"), "rb").read() == b"print('lodestone 2')\n")

section("going back")
backup = up.latest_backup(root)
check("there is a backup", backup and os.path.isfile(os.path.join(backup, "undo.json")))
up.rollback(root, backup)
check("changed files are put back", open(os.path.join(root, "lodestone.py"), "rb").read() == before["lodestone.py"])
check("removed files come back", open(os.path.join(root, "old_helper.py"), "rb").read() == before["old_helper.py"])
check("new files go away", not os.path.exists(os.path.join(root, "updater_new.py")))
check("the stamp goes back too", up.stamp(root)["sha"] == sha_of(1))
check("and the user's file is still there", os.path.isfile(os.path.join(root, "user_plugin.py")))

section("refusing what would not work")
COMMITS.insert(0, {"sha": sha_of(4), "title": "Broken", "files": {"Lodestone/lodestone.py": b"def broken(:\n"}})
try:
    up.download(root, CFG, sha_of(4))
    check("an update with a syntax error is refused", False)
except up.UpdateError as e:
    check("an update with a syntax error is refused", "broken file" in str(e) and "lodestone.py" in str(e), e)
check("and nothing was changed", open(os.path.join(root, "lodestone.py"), "rb").read() == before["lodestone.py"])
COMMITS.pop(0)
WRONG_TOP[sha_of(3)] = "owner-lode-0000000"
try:
    up.download(root, CFG, sha_of(3))
    check("a zip of the wrong commit is refused", False)
except up.UpdateError as e:
    check("a zip of the wrong commit is refused", "not the commit" in str(e), e)
WRONG_TOP.clear()
try:
    up.download(root, dict(CFG, path="NotThere"), sha_of(3))
    check("a folder with no lodestone.py is refused", False)
except up.UpdateError as e:
    check("a folder with no lodestone.py is refused", "no lodestone.py" in str(e), e)

section("a file in use puts everything back")
info = up.check(root, CFG)
stage = up.download(root, CFG, info["latest"]["sha"])
real_replace = os.replace
calls = []


def flaky_replace(a, b):
    calls.append(b)
    if b.endswith("updater_new.py"):
        raise PermissionError(13, "in use")
    return real_replace(a, b)


os.replace = flaky_replace
try:
    up.apply(root, stage, info["latest"], CFG)
    check("a failed replace is reported", False)
except up.UpdateError as e:
    check("a failed replace is reported with the file", "updater_new.py" in str(e) and "in use" in str(e), e)
finally:
    os.replace = real_replace
check("and the half-done update was undone", open(os.path.join(root, "lodestone.py"), "rb").read() == before["lodestone.py"]
      and up.stamp(root)["sha"] == sha_of(1))

# ---------------------------------------------------------------------------
section("processes")
check("this process is alive", up.pid_alive(os.getpid()))
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
check("a running child is alive", up.pid_alive(child.pid))
check("asking did not hurt it", child.poll() is None)
child.kill()
child.wait()
check("a finished one is not", not up.pid_alive(child.pid))
check("nonsense is not a process", not up.pid_alive("x") and not up.pid_alive(0))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


FAKE_ENGINE = (
    "import os, sys, threading, http.server\n"
    "threading.Timer(25, os._exit, (0,)).start()\n"
    "class H(http.server.BaseHTTPRequestHandler):\n"
    "    def log_message(self, *a): pass\n"
    "    def do_GET(self):\n"
    "        b = b'{\"app\": \"Lodestone\"}'\n"
    "        self.send_response(200); self.send_header('Content-Length', str(len(b))); self.end_headers()\n"
    "        self.wfile.write(b)\n"
    "http.server.HTTPServer(('127.0.0.1', %d), H).serve_forever()\n")

section("restarting into the update")
up.HEALTH_SECONDS = 8
up.CLOSE_SECONDS = 10
rroot = tempfile.mkdtemp(dir=_TESTHOME)
port = free_port()
open(os.path.join(rroot, "lodestone.py"), "w").write(FAKE_ENGINE % port)
old = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])
log = os.path.join(_TESTHOME, "update.log")
code = up.relaunch(rroot, [str(old.pid)], port, "", "browser", log)
text = open(log).read()
check("it waits for the old process, then starts the new one", code == 0 and "Lodestone is up" in text, text)
check("the old process had exited first", old.poll() is not None)
check("the new Lodestone answers", up.panel_answers(port))

section("an update that will not start is undone")
broot = tempfile.mkdtemp(dir=_TESTHOME)
port2 = free_port()
good = FAKE_ENGINE % port2
open(os.path.join(broot, "lodestone.py"), "w").write("raise SystemExit(3)\n")      # the "new" version
bk = os.path.join(broot, up.WORK, "backup-test")
os.makedirs(os.path.join(bk, "files"))
open(os.path.join(bk, "files", "lodestone.py"), "w").write(good)                  # what it replaced
json.dump({"restore": ["lodestone.py"], "delete": [], "had_stamp": False}, open(os.path.join(bk, "undo.json"), "w"))
log2 = os.path.join(_TESTHOME, "update2.log")
code = up.relaunch(broot, [], port2, bk, "browser", log2)
text = open(log2).read()
check("a version that does not answer is rolled back", code == 2 and "putting the previous version back" in text, text)
check("the previous file is back", open(os.path.join(broot, "lodestone.py")).read() == good)
deadline = time.time() + 8
while time.time() < deadline and not up.panel_answers(port2):
    time.sleep(0.3)
check("and the previous version was started", up.panel_answers(port2))

# ---------------------------------------------------------------------------
section("a copy put there by hand")
hroot = fresh_root()
hinfo = up.check(hroot, CFG)
for rel, data in snapshot(hinfo["latest"]["sha"]).items():
    if rel.startswith("Lodestone/") and not up.protected(rel[len("Lodestone/"):]):
        dest = os.path.join(hroot, rel[len("Lodestone/"):])
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        open(dest, "wb").write(data)
hrep = up.apply(hroot, up.download(hroot, CFG, hinfo["latest"]["sha"]), hinfo["latest"], CFG)
check("matching files are not counted as changes", not (hrep["changed"] or hrep["added"] or hrep["removed"]), hrep)
check("but the copy is stamped", up.stamp(hroot).get("sha") == hinfo["latest"]["sha"])

section("the panel")
spec = importlib.util.spec_from_file_location("lodestone", "lodestone.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.PANEL.save = lambda *a, **k: True
proot = fresh_root()
m.APP_ROOT = proot                      # never the real folder
m.PANEL.cfg["updates"] = dict(CFG)
stt = m.PANEL.update_state()
check("updates are supported from source", stt["supported"], stt["reason"])
check("the token is never handed out", "token" not in stt["settings"] and stt["settings"]["has_token"] is False)
m.PANEL.update_settings({"token": "secret", "hours": 3, "auto_restart": True})
check("a token can be saved", m.PANEL.cfg["updates"]["token"] == "secret" and m.PANEL.update_state()["settings"]["has_token"])
m.PANEL.update_settings({"token": ""})
check("a blank token leaves the saved one", m.PANEL.cfg["updates"]["token"] == "secret")
m.PANEL.update_settings({"token": "-", "auto_restart": False})
check("and - removes it", m.PANEL.cfg["updates"]["token"] == "")
st1 = m.PANEL.update_check()
check("a check finds the update", st1["state"] == "available" and st1["info"]["latest"]["sha"] == sha_of(3), st1["state"])
check("status carries it for the badges", m.PANEL.status()["update"]["state"] == "available")
st2 = m.PANEL.update_install()
check("installing leaves it ready for a restart", st2["state"] == "ready" and st2["applied"]["sha"] == sha_of(3))
check("into the panel's own folder", os.path.isfile(os.path.join(proot, "updater_new.py")))
check("and the badge says so", m.PANEL.status()["update"]["ready"])
check("going back is offered", st2["can_rollback"])
real_running = m.PANEL.running_ids
m.PANEL.running_ids = lambda: ["srv1"]
res = m.PANEL.update_restart(False, 0)
m.PANEL.running_ids = real_running
check("a running server is not restarted without asking", res.get("needs_force") and not res.get("ok"), res)
st3 = m.PANEL.update_rollback()
check("rolling back is ready for a restart too", st3["state"] == "ready" and st3["applied"]["rolled_back"])
check("and put the files back", not os.path.exists(os.path.join(proot, "updater_new.py")))
m.PANEL.cfg["updates"]["repo"] = "nobody"
try:
    m.PANEL.update_check()
    check("a bad repo setting is explained", False)
except RuntimeError as e:
    check("a bad repo setting is explained", "owner/name" in str(e), e)

srv.shutdown()
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
