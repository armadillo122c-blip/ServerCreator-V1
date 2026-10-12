import importlib.util, json, os, shutil, sys, tempfile, threading, time, urllib.request, urllib.error
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

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

spec = importlib.util.spec_from_file_location("lodestone", "lodestone.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

# --- a fake Discord endpoint that records what it receives -----------------
GOT = []
class Hook(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        GOT.append(json.loads(self.rfile.read(n).decode()))
        self.send_response(204); self.send_header("Content-Length","0"); self.end_headers()
hook_srv = ThreadingHTTPServer(("127.0.0.1", 8798), Hook); hook_srv.daemon_threads = True
threading.Thread(target=hook_srv.serve_forever, daemon=True).start()

# --- a throwaway "minecraft server" folder ---------------------------------
root = tempfile.mkdtemp(prefix="lds_test_")
os.makedirs(os.path.join(root, "world", "region"))
open(os.path.join(root, "eula.txt"), "w").write("eula=true\n")
open(os.path.join(root, "server.properties"), "w").write(
    "#Minecraft server properties\npvp=true\ndifficulty=easy\nmotd=A Test Server\nmax-players=20\n")
open(os.path.join(root, "world", "level.dat"), "wb").write(b"\x00fake nbt")
open(os.path.join(root, "world", "region", "r.0.0.mca"), "wb").write(b"x" * 5000)

fails = []
def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  " + str(extra)[:250]) if not cond else ""))
    if not cond: fails.append(name)

P = m.PANEL
orig_active = P.cfg["active"]
srv = m.blank_server("local", "TEST SERVER")
srv["local"]["cwd"] = root
srv["discord"]["hooks"] = [{"id": "h1", "name": "test", "url": "http://127.0.0.1:8798/hook",
                            "enabled": True, "events": ["chat","join_leave","death","advancement",
                                                        "start_stop","backup","console"],
                            "username": "Lodestone", "avatar": "", "embeds": True, "mention": ""}]
P.cfg["servers"].append(srv); P.cfg["active"] = srv["id"]
be = P.backend()
check("backend picks LocalBackend", be.kind == "local" and be.online(), be.kind)

# ---- files
d = be.list_dir("")
names = sorted(i["name"] for i in d["items"])
check("list_dir sees the folder", names == ["eula.txt","server.properties","world"], names)
be.new_file("", "notes.txt"); be.write_file("notes.txt", "hello\nworld\n")
r = be.read_file("notes.txt")
check("write then read round-trips", r["content"] == "hello\nworld\n", r["content"])
try:
    be.write_file("notes.txt", "clobber", expect_mtime=1)
    check("stale-mtime write is refused", False, "no Conflict raised")
except m.Conflict:
    check("stale-mtime write is refused", True)
be.mkdir("", "plugins"); be.rename("notes.txt", "notes2.txt"); be.copy("notes2.txt")
after = sorted(i["name"] for i in be.list_dir("")["items"])
check("mkdir/rename/copy", after == ["eula.txt","notes2 copy.txt","notes2.txt","plugins",
                                     "server.properties","world"], after)
be.delete("notes2 copy.txt"); be.delete("plugins")
try:
    be.read_file("../../../etc/passwd"); check("path escape blocked", False, "read succeeded")
except Exception as e:
    check("path escape blocked", "outside" in str(e) or "Not a file" in str(e), e)
try:
    be.resolve("../..")
    check("traversal is neutralised, not escaped", be.resolve("../..") == be.root())
except RuntimeError:
    check("resolve refuses to leave root", True)

# ---- properties
pr = P.props_get()
keys = {i["key"]: i for i in pr["items"]}
check("props parsed", pr["ok"] and keys["pvp"]["type"] == "bool"
      and keys["difficulty"]["type"] == "enum" and keys["motd"]["type"] == "text", list(keys))
P.props_save({"pvp": "false", "motd": "Changed by Lodestone", "new-key": "1"})
raw = open(os.path.join(root, "server.properties")).read()
check("props saved, comments kept", "pvp=false" in raw and "Changed by Lodestone" in raw
      and raw.startswith("#Minecraft") and "new-key=1" in raw, raw[:160])

# ---- log ingestion -> discord routing
P.ingest("[12:00:00] [Server thread/INFO]: Steve joined the game")
P.ingest("[12:00:05] [Server thread/INFO]: <Steve> hi everyone")
P.ingest("[12:00:09] [Server thread/INFO]: Steve was slain by Zombie")
P.ingest("[12:00:11] [Server thread/INFO]: Steve has made the advancement [Stone Age]")
P.ingest("[12:00:12] [Server thread/INFO]: Done (7.352s)! For help, type help")
P.ingest("[12:00:13] [Server thread/ERROR]: something exploded")
P.ingest("[12:00:14] [Server thread/INFO]: Steve left the game")
check("join tracked", "Steve" not in P.players, sorted(P.players))
time.sleep(2.5)
kinds = []
for g in GOT:
    if "embeds" in g: kinds.append(g["embeds"][0].get("title") or "")
    else: kinds.append("CHAT:" + g.get("content",""))
check("join event posted", any("Steve joined" in k for k in kinds), kinds)
check("chat relayed as the player", any(k.startswith("CHAT:hi everyone") for k in kinds), kinds)
check("chat uses player avatar", any(g.get("avatar_url","").endswith("Steve/64.png") for g in GOT))
check("death event posted", any("Steve died" in k for k in kinds), kinds)
check("advancement posted", any("advancement" in k for k in kinds), kinds)
check("server-up posted", any("Server is up" in k for k in kinds), kinds)
check("console error posted", any("Console error" in k for k in kinds), kinds)
check("leave posted", any("Steve left" in k for k in kinds), kinds)
check("embeds carry footer/colour", all(("footer" in g["embeds"][0] and "color" in g["embeds"][0])
                                        for g in GOT if "embeds" in g))

# ---- unsubscribed events must NOT be delivered
before = len(GOT)
P.discord.event("files", "should not appear", "")
time.sleep(1.2)
check("unsubscribed event filtered out", len(GOT) == before, (before, len(GOT)))

# ---- backups
n0 = len(P.backups_list()["items"])
ok, msg = P.backup_create("test", "world")
check("backup starts", ok, msg)
for _ in range(60):
    if not P.backup["busy"]: break
    time.sleep(0.5)
lst = P.backups_list()
check("backup produced a zip", len(lst["items"]) == n0 + 1 and lst["items"][0]["bytes"] > 0, lst["items"][:1])
import zipfile
zp = os.path.join(lst["dir"], lst["items"][0]["name"])
with zipfile.ZipFile(zp) as z: inner = z.namelist()
check("backup contains the world files", any("level.dat" in n for n in inner)
      and any("r.0.0.mca" in n for n in inner), inner[:8])
time.sleep(1.5)
check("backup event posted", any("Backup finished" in (g.get("embeds",[{}])[0].get("title") or "")
                                 for g in GOT if "embeds" in g))
# restore into a wiped world
shutil.rmtree(os.path.join(root, "world"))
P.backup_restore(lst["items"][0]["name"])
for _ in range(60):
    if not P.backup["busy"]: break
    time.sleep(0.5)
check("restore put the world back", os.path.exists(os.path.join(root, "world", "region", "r.0.0.mca")),
      os.listdir(root))
P.backup_delete(lst["items"][0]["name"])

# ---- command with nothing to talk to
ok, msg = P.command("say hi")
check("command without process or rcon explains itself", not ok and "not running" in msg.lower(), msg)

# cleanup
P.cfg["servers"] = [s for s in P.cfg["servers"] if s["id"] != srv["id"]]
P.cfg["active"] = orig_active
P.backends.clear(); P.save()
shutil.rmtree(os.path.join(m.BACKUP_ROOT, srv["id"]), ignore_errors=True)
shutil.rmtree(root, ignore_errors=True)
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
