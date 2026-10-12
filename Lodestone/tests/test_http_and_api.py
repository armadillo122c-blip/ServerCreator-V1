import importlib.util, json, sys, threading, time, urllib.request, urllib.error
import os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
# lodestone.py reads Path.home() at import time, so redirect it BEFORE loading
# the module: every test then runs against a throwaway config, never the real one.
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
print("module loaded; version", m.VERSION)

from http.server import ThreadingHTTPServer
httpd = ThreadingHTTPServer(("127.0.0.1", 8799), m.Handler)
httpd.daemon_threads = True
threading.Thread(target=httpd.serve_forever, daemon=True).start()
time.sleep(0.4)

def get(path, hdrs=None):
    req = urllib.request.Request("http://127.0.0.1:8799" + path, headers=hdrs or {})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

def post(path, body, hdrs=None):
    h = {"Content-Type": "application/json"}
    h.update(hdrs or {})
    req = urllib.request.Request("http://127.0.0.1:8799" + path,
                                 data=json.dumps(body).encode(), headers=h, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

fails = []
def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  " + str(extra)[:200]) if not cond else ""))
    if not cond: fails.append(name)

s, b = get("/")
check("GET / serves the page", s == 200 and b"Lodestone" in b and b"__CSS__" not in b, (s, b[:120]))
check("page has no unreplaced tokens", b"__ICON__" not in b and b"__EVENTS__" not in b and b"__VER__" not in b)
s, b = get("/icon.svg"); check("GET /icon.svg", s == 200 and b"svg" in b, s)
s, b = get("/api/status"); st = json.loads(b)
check("GET /api/status", s == 200 and "server" in st, b[:200])
s, b = get("/api/me"); check("GET /api/me", s == 200 and json.loads(b).get("version") == m.VERSION, b[:200])
s, b = get("/api/servers"); check("GET /api/servers", s == 200 and "items" in json.loads(b), b[:200])
s, b = get("/api/features"); check("GET /api/features", s == 200 and "macros" in json.loads(b), b[:200])
s, b = get("/api/recent"); check("GET /api/recent", s == 200 and "lines" in json.loads(b), b[:200])
# ?sid= asks about a server that is NOT the selected one (scripts driving one server while the UI shows another)
other = m.PANEL.upsert({"kind": "local", "name": "Side server"})
rt = m.PANEL.runtime(other["id"])
with rt.log_lock:
    rt.seq += 1
    rt.buf.append((rt.seq, "[12:00:00 INFO]: only the side server said this"))
rt.players.add("SideSteve")
s, b = get("/api/status?sid=" + other["id"]); so = json.loads(b) if s == 200 else {}
check("GET /api/status?sid= answers for that server", s == 200 and so.get("server", {}).get("id") == other["id"], b[:200])
check("  and says it is not the selected one", so.get("selected") is False, so)
check("  with its own players", so.get("players") == ["SideSteve"], so.get("players"))
s, b = get("/api/status"); check("  while the plain call still describes the selected server",
                                 json.loads(b)["server"]["id"] != other["id"], b[:120])
s, b = get("/api/recent?sid=" + other["id"]); lines = json.loads(b).get("lines", []) if s == 200 else []
check("GET /api/recent?sid= reads that server's console", any("only the side server" in l[1] for l in lines), lines[-2:])
s, b = get("/api/recent"); lines = json.loads(b).get("lines", [])
check("  and the selected server's console doesn't have it", not any("only the side server" in l[1] for l in lines))
s, b = get("/api/status?sid=nope"); check("an unknown sid is a 404", s == 404, (s, b[:80]))
s, b = get("/api/recent?sid=nope"); check("  for the console too", s == 404, (s, b[:80]))
s, b = get("/api/discord"); check("GET /api/discord", s == 200 and "hooks" in json.loads(b), b[:200])
s, b = get("/api/tokens"); check("GET /api/tokens", s == 200 and "items" in json.loads(b), b[:200])
s, b = get("/api/backups"); check("GET /api/backups", s == 200 and "items" in json.loads(b), b[:200])
# the desktop app reads the Discord event list from here instead of baking it in
s, b = get("/api/events"); ev = json.loads(b).get("items", []) if s == 200 else []
check("GET /api/events matches EVENTS", [e[0] for e in ev] == m.EVENT_KEYS and all(len(e) == 3 for e in ev), b[:200])

# API v1 should be closed until enabled — force the starting state rather than
# assuming it, so the check means something on a config that has been used
post("/api/tokens/enable", {"enabled": False})
s, b = get("/api/v1/health"); check("api/v1 closed while disabled", s == 503, (s, b[:120]))
s, b = post("/api/tokens/enable", {"enabled": True}); check("enable API", s == 200, b[:200])
s, b = get("/api/v1/health"); check("api/v1 401 without token", s == 401, (s, b[:120]))
s, b = post("/api/tokens/create", {"name": "smoke", "scopes": ["read", "control"]})
tok = json.loads(b).get("token", "")
check("token created", s == 200 and tok.startswith("lds_"), b[:200])
H = {"Authorization": "Bearer " + tok}
s, b = get("/api/v1/health", H); check("api/v1 health with token", s == 200 and json.loads(b)["ok"], b[:200])
s, b = get("/api/v1/status", H); check("api/v1 status (read scope)", s == 200, b[:200])
s, b = get("/api/v1/health", {"Authorization": "Bearer lds_wrong"}); check("bad token rejected", s == 401, s)
# files scope not granted -> 403
s, b = post("/api/v1/webhook", {"title": "x"}, H); check("control scope allowed", s == 200, b[:200])
import urllib.request as _u
req = _u.Request("http://127.0.0.1:8799/api/v1/file", data=json.dumps({"path":"x","content":"y"}).encode(),
                 headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"}, method="PUT")
try:
    with _u.urlopen(req, timeout=8) as r: s2 = r.status
except urllib.error.HTTPError as e: s2 = e.code
check("files scope enforced (403)", s2 == 403, s2)

# rcon should fail cleanly, not hang
t0 = time.time()
s, b = post("/api/rcon/test", {"id": st["server"]["id"]})
check("rcon test fails cleanly", s == 200 and json.loads(b)["ok"] is False and time.time()-t0 < 15, (s, b[:150]))

# discord: add a hook, save, test against a dead URL
s, b = post("/api/discord/add", {}); check("add webhook", s == 200, b[:200])
s, b = get("/api/discord"); hooks = json.loads(b)["hooks"]
check("hook persisted with defaults", len(hooks) >= 1 and hooks[-1]["events"], b[:200])
hooks[-1]["url"] = "http://127.0.0.1:9/nope"
s, b = post("/api/discord", {"hooks": hooks}); check("save hooks", s == 200, b[:200])
s, b = post("/api/discord/test", {"id": hooks[-1]["id"]})
check("dead webhook reports failure not crash", s == 200 and json.loads(b)["ok"] is False, b[:200])

# cleanup the smoke token + hook
s, b = get("/api/tokens"); items = json.loads(b)["items"]
for t in items:
    if t["name"] == "smoke": post("/api/tokens/delete", {"id": t["id"]})
post("/api/tokens/enable", {"enabled": False})
s, b = get("/api/discord"); hh = [h for h in json.loads(b)["hooks"] if h.get("url") != "http://127.0.0.1:9/nope"]
post("/api/discord", {"hooks": hh})
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
