"""The Minehut host API (against a fake that speaks the real protocol), the
world manager, and the server-icon / MOTD settings."""
import importlib.util
import os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
_TESTHOME = os.environ.get("LODESTONE_TEST_HOME")
if not _TESTHOME:
    _TESTHOME = tempfile.mkdtemp(prefix="lodestone_testhome_")
    os.environ["LODESTONE_TEST_HOME"] = _TESTHOME
os.environ["HOME"] = _TESTHOME
os.environ["USERPROFILE"] = _TESTHOME
os.environ["HOMEDRIVE"] = os.path.splitdrive(_TESTHOME)[0] or "C:"
os.environ["HOMEPATH"] = os.path.splitdrive(_TESTHOME)[1]

import json
import shutil
import struct
import threading
import time
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

spec = importlib.util.spec_from_file_location("lodestone", "lodestone.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

fails = []


def check(n, c, e=""):
    print(("  ok   " if c else "  FAIL ") + n + (("  " + str(e)[:230]) if not c else ""))
    if not c:
        fails.append(n)


# ------------------------------------------------- a fake Minehut endpoint ---
MH = {"online": False, "service": False, "calls": [], "props": {}}
TOKEN, SESSION = "tok-123", "sess-456"


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth(self):
        return (self.headers.get("authorization") == TOKEN
                and self.headers.get("x-session-id") == SESSION)

    def do_GET(self):
        # like the real one: a server's info is public (server lists read it), the rest needs the login
        public = self.path.startswith("/server/") and self.headers.get("authorization") is None
        if not public and not self._auth():
            return self._send({"error": "unauthorized"}, 401)
        MH["calls"].append("GET " + self.path + (" (public)" if public else ""))
        if self.path.startswith("/v1/server/") and self.path.endswith("/backups"):
            return self._send({"backups": [{"id": "b1"}], "rolling_backup": {}})
        if self.path.startswith("/server/"):
            return self._send({"server": {
                "name": "FischSim", "online": MH["online"], "service_online": MH["service"],
                "playerCount": 1 if MH["online"] else 0, "maxPlayers": 500,
                "motd": "x", "server_plan": "Standard", "server_version": "Paper 1.21.11",
                "suspended": False, "visibility": True, "backup_slots": 2}})
        return self._send({}, 404)

    def do_POST(self):
        if not self._auth():
            return self._send({"error": "unauthorized"}, 401)
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n).decode() if n else ""
        MH["calls"].append("POST " + self.path + ((" " + raw) if raw.strip() not in ("", "{}") else ""))
        if self.path.endswith("/start_service"):
            MH["service"] = True
        elif self.path.endswith("/start"):
            if not MH["service"]:
                return self._send({"error": "service offline"}, 400)
            MH["online"] = True
        elif self.path.endswith("/shutdown"):
            MH["online"] = False
        elif self.path.endswith("/destroy_service"):
            MH["online"] = MH["service"] = False
        elif self.path.endswith("/edit_server_properties"):
            b = json.loads(raw or "{}")
            MH["props"][b.get("field")] = b.get("value")
        return self._send({})


srv_http = ThreadingHTTPServer(("127.0.0.1", 8790), Fake)
srv_http.daemon_threads = True
threading.Thread(target=srv_http.serve_forever, daemon=True).start()
m.MINEHUT_BASE = "http://127.0.0.1:8790"

# ------------------------------------------------------------- the client ---
good = m.Minehut({"server_id": "abc", "token": TOKEN, "session_id": SESSION})
st = good.status()
check("status is normalised", st["ok"] and st["max_players"] == 500 and st["plan"] == "Standard", st)
check("a fresh server reads as hibernating", st["hibernating"] and not st["online"], st)

MH["calls"].clear()
ok, msg = good.start()
check("start succeeds", ok, msg)
check("a cold start wakes the service first, then starts",
      [c.split()[1] for c in MH["calls"] if c.startswith("POST")] ==
      ["/server/abc/start_service", "/server/abc/start"],
      [c for c in MH["calls"] if c.startswith("POST")])
check("the server is now online", good.status()["online"] is True)

ok, msg = good.start()
check("starting an already-online server is refused politely", ok is False and "already" in msg.lower(), msg)

MH["calls"].clear()
good.command("say hi")
check("a console command posts the right body",
      any("/command" in c and '"command": "say hi"' in c for c in MH["calls"]), MH["calls"])
good.set_property("motd", "new motd")
check("editing a property posts field and value",
      MH["props"].get("motd") == "new motd", MH["props"])
check("backups can be listed", good.backups() == [{"id": "b1"}], good.backups())
good.stop()
check("stop takes it offline", good.status()["online"] is False)
good.hibernate()
check("hibernate takes the service down too", good.status()["service_online"] is False)

bad = m.Minehut({"server_id": "abc", "token": "nope", "session_id": "nope"})
okb, msgb = bad.test()
check("bad credentials fail with a readable message",
      okb is False and ("credential" in msgb.lower() or "401" in msgb), msgb)
MH["calls"].clear()
pub = m.Minehut({"server_id": "abc"}).status()
check("without a login the status comes from Minehut's public info", pub["ok"] and pub["max_players"] == 500, pub)
check("  and no login headers were sent", MH["calls"] and all(c.endswith("(public)") for c in MH["calls"]), MH["calls"])
try:
    m.Minehut({"server_id": "abc"}).stop()
    check("an action without a login is caught before any request", False)
except m.MinehutError as e:
    check("an action without a login is caught before any request", "not set" in str(e).lower(), e)
url = m.Minehut({"server_id": "https://dashboard.minehut.com/servers/abc/", "token": TOKEN, "session_id": SESSION})
check("a pasted dashboard URL becomes the server id", url.server_id() == "abc", url.server_id())
check("  and works", url.status()["ok"])
MH["calls"].clear()
stale = m.Minehut({"server_id": "abc", "token": "expired", "session_id": "expired"}).status()
check("an expired login still shows the status (public)", stale["ok"], stale)
try:
    m.Minehut({"token": TOKEN, "session_id": SESSION}).status()
    check("a missing server id is caught", False)
except m.MinehutError as e:
    check("a missing server id is caught", "server id" in str(e).lower(), e)

unreachable = m.Minehut({"server_id": "abc", "token": TOKEN, "session_id": SESSION})
old_base = m.MINEHUT_BASE
m.MINEHUT_BASE = "http://127.0.0.1:9"
oku, msgu = unreachable.test()
check("an unreachable host fails clearly rather than hanging",
      oku is False and "reach" in msgu.lower(), msgu)
m.MINEHUT_BASE = old_base

# ---------------------------------------------------- panel-level wiring ----
root = tempfile.mkdtemp(prefix="lds_mh_")
for w in ("world", "world_nether", "world_the_end", "FischFisch"):
    os.makedirs(os.path.join(root, w, "region"), exist_ok=True)
    open(os.path.join(root, w, "level.dat"), "wb").write(b"\x00nbt")
os.makedirs(os.path.join(root, "plugins"), exist_ok=True)   # must not count as a world
os.makedirs(os.path.join(root, "logs"), exist_ok=True)
open(os.path.join(root, "eula.txt"), "w").write("eula=true\n")
open(os.path.join(root, "server.properties"), "w", encoding="utf-8").write(
    "#props\nmotd=old motd\nlevel-name=FischFisch\nmax-players=500\nresource-pack=\n")

P = m.PANEL
P.save = lambda *a, **k: True
rec = m.blank_server("sftp", "FischSim")
rec["minehut"] = {"enabled": True, "server_id": "abc", "token": TOKEN, "session_id": SESSION}
P.cfg["servers"].append(rec)
local = m.blank_server("local", "Local fixture")
local["local"]["cwd"] = root
P.cfg["servers"].append(local)
P.cfg["active"] = rec["id"]
P.backends.clear()

check("the panel exposes minehut for that server", P.minehut() is not None)
ok, msg = P.minehut_action("start")
check("panel start goes through minehut", ok, msg)
okc, outc = P.command("say routed")
check("commands route to minehut when there is no rcon", okc, outc)
ok, msg = P.minehut_action("stop")
check("panel stop works", ok, msg)
ok, msg = P.minehut_action("nonsense")
check("an unknown action is refused", ok is False, msg)

# ------------------------------------------------------------- the worlds ---
P.cfg["active"] = local["id"]
P.backends.clear()
w = P.worlds_list()
names = sorted(x["name"] for x in w["items"])
check("worlds are found by level.dat",
      names == ["FischFisch", "world", "world_nether", "world_the_end"], names)
check("plugins and logs are not mistaken for worlds", "plugins" not in names and "logs" not in names)
check("the live world is the one level-name points at",
      w["active"] == "FischFisch" and
      [x for x in w["items"] if x["active"]][0]["name"] == "FischFisch", w["active"])
check("the live world sorts first", w["items"][0]["name"] == "FischFisch")
check("nether and end are tagged",
      [x for x in w["items"] if x["name"] == "world_nether"][0]["nether"] is True)

P.world_activate("world")
check("switching writes level-name",
      "level-name=world\n" in open(os.path.join(root, "server.properties"), encoding="utf-8").read())
check("and the panel now reports it live", P.worlds_list()["active"] == "world")

P.world_reset("FischFisch", "contents")
check("a contents reset clears the chunks but keeps the folder",
      os.path.isdir(os.path.join(root, "FischFisch"))
      and not os.path.exists(os.path.join(root, "FischFisch", "region"))
      and not os.path.exists(os.path.join(root, "FischFisch", "level.dat")),
      os.listdir(os.path.join(root, "FischFisch")))
P.world_reset("world_the_end", "all")
check("a full reset removes the folder", not os.path.exists(os.path.join(root, "world_the_end")))
try:
    P.world_reset("", "all")
    check("resetting with no name is refused", False)
except RuntimeError:
    check("resetting with no name is refused", True)

# ---------------------------------------------------------- icon and MOTD ---
def png(w_, h_):
    raw = b"".join(b"\x00" + bytes((34, 197, 94, 255)) * w_ for _ in range(h_))

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w_, h_, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


check("png_size reads the header", m.Panel.png_size(png(64, 64)) == (64, 64))
check("png_size rejects a non-png", m.Panel.png_size(b"GIF89a nope") is None)
check("no icon reports absent", P.icon_info()["present"] is False, P.icon_info())
P.icon_set(png(64, 64))
check("a valid icon is written", P.icon_info()["present"] is True
      and os.path.exists(os.path.join(root, "server-icon.png")))
check("and can be read back", m.Panel.png_size(P.icon_bytes()) == (64, 64))
for bad_png, why in ((png(32, 32), "wrong size"), (b"not a png at all", "not a png")):
    try:
        P.icon_set(bad_png)
        check("an icon that is %s is refused" % why, False)
    except RuntimeError as e:
        check("an icon that is %s is refused" % why,
              "64" in str(e) or "PNG" in str(e), e)
P.icon_clear()
check("the icon can be removed", P.icon_info()["present"] is False)

got = P.motd_get()
check("appearance reads the motd", got["values"]["motd"] == "old motd", got["values"])
P.motd_save({"motd": "&aNew &bMOTD", "max-players": "250", "not-allowed": "x"})
raw = open(os.path.join(root, "server.properties"), encoding="utf-8").read()
check("the motd is saved", "motd=&aNew &bMOTD" in raw, raw)
check("max-players is saved", "max-players=250" in raw)
check("keys outside the allow-list are ignored", "not-allowed" not in raw)
try:
    P.motd_save({"nothing": "here"})
    check("saving nothing valid is refused", False)
except RuntimeError:
    check("saving nothing valid is refused", True)

# an SFTP server on Minehut is a Minehut server even without the Minehut section: its SFTP user is the id
mhs = m.PANEL.upsert({"kind": "sftp", "name": "On Minehut", "sftp": {"host": "ftp.minehut.com", "port": 2223, "user": "abc", "root": "/server"}})
check("an SFTP server on ftp.minehut.com knows its Minehut id", m.PANEL.minehut_id(mhs["id"]) == "abc",
      m.PANEL.minehut_id(mhs["id"]))
pst = m.PANEL.minehut_status(mhs["id"])
check("  and its Minehut status works without a login", pst.get("ok") and pst.get("max_players") == 500, pst)
plain = m.PANEL.upsert({"kind": "sftp", "name": "Elsewhere", "sftp": {"host": "sftp.example.com", "user": "abc"}})
check("  while another SFTP host is not taken for Minehut", m.PANEL.minehut_id(plain["id"]) == "")
ok_a, msg_a = m.PANEL.minehut_action("stop", mhs["id"])
check("  and start / stop still need the Minehut login turned on", ok_a is False, msg_a)

srv_http.shutdown()
shutil.rmtree(root, ignore_errors=True)
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
