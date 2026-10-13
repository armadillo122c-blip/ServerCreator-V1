"""Git with the real git binary (a repository, a bare 'remote' beside it, a
second clone), GitHub against a fake of its REST API, the assistant's git and
GitHub tools against a fake Ollama, and the Code page's git routes."""
import base64
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))
_TESTHOME = tempfile.mkdtemp(prefix="lodestone_git_")
for k in ("HOME", "USERPROFILE", "LODESTONE_TEST_HOME"):
    os.environ[k] = _TESTHOME
# a known identity, and nothing from the machine's own git config
GITCFG = os.path.join(_TESTHOME, "gitconfig")
open(GITCFG, "w").write("[user]\n\tname = Test Person\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n")
os.environ["GIT_CONFIG_GLOBAL"] = GITCFG
os.environ["GIT_CONFIG_NOSYSTEM"] = "1"

import agent as ag
import githubapi as gh
import gittools as gt
from fake_ollama import FakeOllama

fails = []


def check(n, c, e=""):
    print(("  ok   " if c else "  FAIL ") + n + (("  " + str(e)[:300]) if not c else ""))
    if not c:
        fails.append(n)


def section(t):
    print("\n-- " + t)


def sh(cwd, *args):
    return subprocess.run(["git"] + list(args), cwd=cwd, capture_output=True, text=True, check=True).stdout


if not gt.available():
    print("git is not installed here - nothing to test")
    sys.exit(0)

# ---------------------------------------------------------------------------
section("a repository")
REPO = os.path.join(_TESTHOME, "plugin")
os.makedirs(REPO)
open(os.path.join(REPO, "build.gradle.kts"), "w").write("plugins { java }\n")
check("not a repository yet", gt.status(REPO)["repo"] is False and gt.status(REPO)["git"] is True)
gt.init(REPO)
check("init makes one, with a .gitignore for a Gradle project", gt.is_repo(REPO)
      and "build/" in open(os.path.join(REPO, ".gitignore")).read())
try:
    gt.init(REPO)
    check("init twice is refused", False)
except gt.GitError:
    check("init twice is refused", True)
st = gt.status(REPO)
check("new files are listed as new", sorted(f["path"] for f in st["files"]) == [".gitignore", "build.gradle.kts"]
      and all(f["label"] == "new" for f in st["files"]) and st["branch"] == "main", st)
c = gt.commit(REPO, "Start the plugin\n\nWith a build file.")
check("commit all", c["subject"] == "Start the plugin" and gt.status(REPO)["clean"], c)
try:
    gt.commit(REPO, "again")
    check("nothing to commit is said", False)
except gt.GitError as e:
    check("nothing to commit is said", "nothing to commit" in str(e), e)
open(os.path.join(REPO, "build.gradle.kts"), "a").write("version = \"1.0\"\n")
open(os.path.join(REPO, "README.md"), "w").write("# Plugin\n")
open(os.path.join(REPO, "notes.txt"), "w").write("scratch\n")
d = gt.diff(REPO, "build.gradle.kts")
check("the diff of a changed file", '+version = "1.0"' in d, d)
d = gt.diff(REPO, "README.md")
check("an untracked file shows as all added", "+# Plugin" in d, d)
st = gt.status(REPO)
labels = {f["path"]: f["label"] for f in st["files"]}
check("modified and new are told apart", labels == {"build.gradle.kts": "modified", "README.md": "new", "notes.txt": "new"},
      labels)
c = gt.commit(REPO, "Add a README", ["README.md"])
left = {f["path"] for f in gt.status(REPO)["files"]}
check("commit only the named files", c["subject"] == "Add a README" and left == {"build.gradle.kts", "notes.txt"}, left)
for bad in ("../outside.txt", "/etc/passwd"):
    try:
        gt.commit(REPO, "x", [bad])
        check("%s is refused" % bad, False)
    except gt.GitError:
        check("%s is refused" % bad, True)
os.rename(os.path.join(REPO, "README.md"), os.path.join(REPO, "READ.md"))
sh(REPO, "add", "-A")
st = gt.status(REPO)
ren = [f for f in st["files"] if f["label"] == "renamed"]
check("a rename shows where it came from", ren and ren[0]["path"] == "READ.md" and ren[0]["from"] == "README.md", st["files"])
gt.commit(REPO, "Rename")
check("the log", [x["subject"] for x in gt.log(REPO, 5)] == ["Rename", "Add a README", "Start the plugin"])

section("branches")
check("create and switch", gt.switch(REPO, "feature/shop", create=True) == "feature/shop"
      and gt.status(REPO)["branch"] == "feature/shop")
check("listed", {b["name"] for b in gt.branches(REPO)} == {"main", "feature/shop"})
gt.switch(REPO, "main")
for bad in ("-D", "a..b", "with space", "/root"):
    try:
        gt.switch(REPO, bad, create=True)
        check("branch name %r is refused" % bad, False)
    except gt.GitError:
        check("branch name %r is refused" % bad, True)

section("a remote: push and pull")
BARE = os.path.join(_TESTHOME, "remote.git")
sh(_TESTHOME, "init", "--bare", BARE)
sh(REPO, "remote", "add", "origin", BARE)
out = gt.push(REPO)
st = gt.status(REPO)
check("the first push sets up origin/<branch>", st["upstream"] == "origin/main" and st["ahead"] == 0, (out, st))
OTHER = os.path.join(_TESTHOME, "other")
sh(_TESTHOME, "clone", BARE, OTHER)
open(os.path.join(OTHER, "CHANGES.md"), "w").write("- more\n")
sh(OTHER, "add", "-A")
sh(OTHER, "commit", "-m", "From elsewhere")
sh(OTHER, "push")
sh(REPO, "fetch")
check("behind is counted", gt.status(REPO)["behind"] == 1, gt.status(REPO))
gt.pull(REPO)
check("pull fast-forwards", os.path.isfile(os.path.join(REPO, "CHANGES.md")) and gt.status(REPO)["behind"] == 0)
open(os.path.join(OTHER, "CHANGES.md"), "a").write("- theirs\n")
sh(OTHER, "commit", "-am", "Theirs")
sh(OTHER, "push")
open(os.path.join(REPO, "build.gradle.kts"), "a").write("// mine\n")
gt.commit(REPO, "Mine")
try:
    gt.pull(REPO)
    check("a pull that would need a merge is refused, not merged", False)
except gt.GitError as e:
    check("a pull that would need a merge is refused, not merged", gt.log(REPO, 1)[0]["subject"] == "Mine", e)
NOWHO = os.path.join(_TESTHOME, "nowho")
os.makedirs(NOWHO)
gt.init(NOWHO)
sh(NOWHO, "config", "user.useConfigOnly", "true")
sh(NOWHO, "config", "user.name", "")
sh(NOWHO, "config", "user.email", "")
open(os.path.join(NOWHO, "a.txt"), "w").write("a")
try:
    gt.commit(NOWHO, "first")
    check("no identity: says how to set one", False)
except gt.GitError as e:
    check("no identity: says how to set one", "git config --global user.name" in str(e), e)

# ---------------------------------------------------------------------------
section("GitHub, against a fake of its API")
SEEN = []


def b64(s):
    return base64.b64encode(s.encode() if isinstance(s, str) else s).decode()


class GH(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        SEEN.append((self.path, self.headers.get("Authorization"), self.headers.get("User-Agent")))
        p = self.path
        if p.startswith("/search/repositories"):
            return self.send(200, {"items": [{"full_name": "PaperMC/Paper", "description": "High performance",
                                              "stargazers_count": 10000, "language": "Java", "pushed_at": "2026-10-01T00:00:00Z",
                                              "html_url": "https://github.com/PaperMC/Paper", "license": {"spdx_id": "GPL-3.0"}}]})
        if p.startswith("/search/code"):
            if not self.headers.get("Authorization"):
                return self.send(401, {"message": "Requires authentication"})
            return self.send(200, {"items": [{"repository": {"full_name": "a/b"}, "path": "src/Main.java", "html_url": "u"}]})
        if p.startswith("/repos/PaperMC/Paper/readme"):
            return self.send(200, {"encoding": "base64", "content": b64("# Paper\nFast."), "path": "README.md"})
        if p.startswith("/repos/PaperMC/Paper/contents/paper-api/src"):
            return self.send(200, [{"name": "main", "type": "dir"}])
        if p.startswith("/repos/PaperMC/Paper/contents/build.gradle.kts"):
            return self.send(200, {"type": "file", "encoding": "base64", "content": b64("plugins { java }"), "size": 16,
                                   "html_url": "h"})
        if p.startswith("/repos/PaperMC/Paper/contents/logo.png"):
            return self.send(200, {"type": "file", "encoding": "base64", "content": b64(b"\x89PNG\0\0"), "size": 6,
                                   "path": "logo.png"})
        if p.startswith("/repos/PaperMC/Paper/contents"):
            return self.send(200, [{"name": "paper-api", "type": "dir"}, {"name": "build.gradle.kts", "type": "file"}])
        if p.startswith("/repos/PaperMC/Paper/releases"):
            return self.send(200, [{"tag_name": "v1", "name": "One", "published_at": "2026-09-01T00:00:00Z", "body": "notes",
                                    "assets": [{"name": "paper.jar", "size": 5, "browser_download_url": "d",
                                                "digest": "sha256:" + "a" * 64}]}])
        if p.startswith("/repos/PaperMC/Paper"):
            return self.send(200, {"description": "High performance", "default_branch": "main", "stargazers_count": 10000,
                                   "license": {"spdx_id": "GPL-3.0"}, "html_url": "https://github.com/PaperMC/Paper"})
        if p.startswith("/repos/limit/me"):
            return self.send(403, {"message": "API rate limit exceeded for 1.2.3.4."})
        return self.send(404, {"message": "Not Found"})


ghs = ThreadingHTTPServer(("127.0.0.1", 0), GH)
threading.Thread(target=ghs.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % ghs.server_address[1]
hits = gh.search_repos("paper server", base=BASE)
check("repositories are found", hits[0]["repo"] == "PaperMC/Paper" and hits[0]["stars"] == 10000
      and hits[0]["license"] == "GPL-3.0", hits)
check("without a token nothing identifies the user", SEEN[-1][1] is None and "Lodestone" in SEEN[-1][2])
try:
    gh.search_code("PlayerJoinEvent", base=BASE)
    check("code search without a token says to add one", False)
except gh.GitHubError as e:
    check("code search without a token says to add one", "token" in str(e))
check("with one, it searches code and sends the token", gh.search_code("x", "tok", base=BASE)[0]["path"] == "src/Main.java"
      and SEEN[-1][1] == "Bearer tok")
r = gh.read("https://github.com/PaperMC/Paper.git", base=BASE)
check("a repository's page: description, README and top folder", r["readme"] == "# Paper\nFast."
      and r["files"] == ["paper-api/", "build.gradle.kts"] and r["default_branch"] == "main", r)
check("a folder lists", gh.read("PaperMC/Paper", "paper-api/src", base=BASE)["files"] == ["main/"])
check("a file reads", gh.read("PaperMC/Paper", "build.gradle.kts", base=BASE)["text"] == "plugins { java }")
for args, why in ((("PaperMC/Paper", "logo.png"), "not a text file"), (("nope/nope",), "nothing at"),
                  (("limit/me",), "limit"), (("not a repo",), "owner/name"), (("PaperMC/Paper", "../x"), "not up")):
    try:
        gh.read(*args, base=BASE)
        check("refused: %s" % why, False)
    except gh.GitHubError as e:
        check("refused: %s" % why, why in str(e), e)
rel = gh.releases("PaperMC/Paper", base=BASE)
check("releases with their files and checksums", rel[0]["assets"][0]["sha256"] == "a" * 64, rel)

# ---------------------------------------------------------------------------
section("the assistant's git and GitHub tools")
fake = FakeOllama()
fake.add_model("coder:7b", tools=True, ctx=32768)
SETTINGS = {"base_url": fake.base, "model": "coder:7b", "num_ctx": 8192, "web_enabled": True,
            "github_api": BASE, "github_token": "tok2"}
A = ag.Agent(os.path.join(_TESTHOME, "data"), lambda: SETTINGS, save=SETTINGS.update)
ws = A.add_workspace(REPO, "plugin")


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


open(os.path.join(REPO, "src.txt"), "w").write("new work\n")
fake.say("", tool_calls=[("git_status", {}), ("git_diff", {"path": "src.txt"}),
                         ("git_commit", {"message": "Add the source notes", "files": ["src.txt"]}), ("git_log", {"n": 2}),
                         ("github_search", {"query": "paper"}), ("github_read", {"repo": "PaperMC/Paper", "releases": True})])
fake.say("Committed.")
run = A.start(ws, "commit my work and look at paper")
asked = []
evs = run_until(run, approve=lambda ev: (asked.append(ev), ("approve",))[1])
tools = {t["function"]["name"] for t in fake.requests[-1]["tools"]}
check("git and GitHub tools are offered", {"git_status", "git_commit", "git_push", "github_search", "github_read"} <= tools)
check("git_status reads the branch and changes", "src.txt" in results(evs, "git_status")[0]["output"]
      and "On main" in results(evs, "git_status")[0]["output"], results(evs, "git_status")[0]["output"])
check("git_diff shows the new file", "+new work" in results(evs, "git_diff")[0]["output"])
check("a commit shows the message and files first", asked and asked[0]["title"].startswith("Commit 1 file(s): Add the source")
      and "src.txt" in asked[0]["command"], asked)
check("and commits", gt.log(REPO, 1)[0]["subject"] == "Add the source notes" and "Add the source notes"
      in results(evs, "git_log")[0]["output"])
gs = results(evs, "github_search")[0]
check("GitHub results are marked as someone else's text, sent with the token", gs["output"].startswith("[Text from the web")
      and "PaperMC/Paper" in gs["output"] and SEEN[-2][1] == "Bearer tok2", (gs["output"][:200], SEEN[-2:]))
check("releases are listed with checksums", "paper.jar" in results(evs, "github_read")[0]["output"]
      and "sha256 " + "a" * 64 in results(evs, "github_read")[0]["output"])
fake.say("", tool_calls=[("git_push", {})])
fake.say("ok")
run = A.start(ws, "push it")
evs = run_until(run, approve=lambda ev: ("reject", "not yet"))
check("a declined push does not push", "not yet" in results(evs, "git_push")[0]["output"] and gt.status(REPO)["ahead"] >= 1)
SETTINGS["web_enabled"] = False
fake.say("ok")
run = A.start(ws, "hi")
run_until(run)
check("GitHub tools need the web turned on", not {"github_search", "github_read"}
      & {t["function"]["name"] for t in fake.requests[-1]["tools"]})

# ---------------------------------------------------------------------------
section("the Code page's git routes")
spec = importlib.util.spec_from_file_location("lodestone", "lodestone.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.PANEL.save = lambda *a, **k: True
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


PROJ = os.path.join(_TESTHOME, "proj")
os.makedirs(PROJ)
open(os.path.join(PROJ, "pom.xml"), "w").write("<project/>\n")
s, j = call("POST", "/api/agent/workspace/add", {"root": PROJ, "name": "proj"})
WS = j["workspace"]["id"]
s, j = call("GET", "/api/dev/git?ws=" + WS)
check("status of a folder that is not a repository", s == 200 and j["repo"] is False and j["git"] is True, j)
s, j = call("POST", "/api/dev/git/init", {"ws": WS})
check("init over HTTP", s == 200 and os.path.isdir(os.path.join(PROJ, ".git")), j)
s, j = call("POST", "/api/dev/git/commit", {"ws": WS, "message": "First"})
s, j = call("GET", "/api/dev/git?ws=" + WS)
check("commit, then status with its log and branches", j["clean"] and j["log"][0]["subject"] == "First"
      and j["branches"][0]["current"], j)
open(os.path.join(PROJ, "pom.xml"), "a").write("<!-- x -->\n")
s, j = call("GET", "/api/dev/git/diff?ws=%s&path=pom.xml" % WS)
check("a file's diff", "+<!-- x -->" in j["diff"], j)
s, j = call("POST", "/api/dev/git/commit", {"ws": WS, "message": ""})
check("a commit with no message is refused", s == 400 and "message" in j.get("error", ""), j)
s, j = call("POST", "/api/dev/git/rebase", {"ws": WS})
check("only the known actions exist", s == 404, j)
s, j = call("POST", "/api/agent/settings", {"github_token": "secret-gh"})
check("the GitHub token is kept but never sent back", s == 200 and j["settings"]["has_github_token"]
      and "secret-gh" not in json.dumps(j) and m.PANEL.dev_cfg()["github_token"] == "secret-gh", j.get("settings"))

ghs.shutdown()
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
