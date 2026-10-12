import importlib.util, os, shutil, socket, struct, sys, tempfile, threading, time, zipfile
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
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

fails = []
def check(n, c, e=""):
    print(("  ok   " if c else "  FAIL ") + n + (("  " + str(e)[:220]) if not c else ""))
    if not c: fails.append(n)

# ---------- a mock RCON server that speaks the real protocol ----------------
PASSWORD = "s3cret"
def rcon_server(sock, behave="normal"):
    conn, _ = sock.accept()
    try:
        def read_pkt():
            hdr = conn.recv(4)
            if len(hdr) < 4: return None
            (ln,) = struct.unpack("<i", hdr)
            data = b""
            while len(data) < ln: data += conn.recv(ln - len(data))
            rid, kind = struct.unpack("<ii", data[:8])
            return rid, kind, data[8:-2].decode()
        def send(rid, kind, payload):
            body = struct.pack("<ii", rid, kind) + payload.encode() + b"\x00\x00"
            conn.sendall(struct.pack("<i", len(body)) + body)
        pkt = read_pkt()
        if pkt is None: return
        rid, kind, payload = pkt
        if behave == "badpass" or payload != PASSWORD:
            send(-1, 2, ""); return
        send(rid, 2, "")
        while True:
            pkt = read_pkt()
            if pkt is None: break
            rid, kind, cmd = pkt
            if cmd == "list":
                send(rid, 0, "There are 2 of a max of 20 players online: Alex, Steve")
            elif cmd == "big":
                send(rid, 0, "y" * 4200); send(rid, 0, "tail")
            else:
                send(rid, 0, "ran: " + cmd)
    except Exception:
        pass
    finally:
        try: conn.close()
        except Exception: pass

def spin(behave="normal"):
    s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 0)); s.listen(1)
    port = s.getsockname()[1]
    threading.Thread(target=rcon_server, args=(s, behave), daemon=True).start()
    return port, s

port, srv = spin()
out = m.Rcon.run("127.0.0.1", port, PASSWORD, "list")
check("rcon auth + command round-trip", "Alex, Steve" in out, out)
srv.close()

port, srv = spin()
out = m.Rcon.run("127.0.0.1", port, PASSWORD, "say hello")
check("rcon echoes arbitrary commands", out == "ran: say hello", out)
srv.close()

port, srv = spin("badpass")
try:
    m.Rcon.run("127.0.0.1", port, "wrong", "list")
    check("wrong password raises", False, "no exception")
except m.RconError as e:
    check("wrong password raises RconError", "rejected" in str(e).lower(), e)
srv.close()

t0 = time.time()
try:
    m.Rcon.run("127.0.0.1", 1, PASSWORD, "list", timeout=3)
    check("dead port raises", False)
except m.RconError as e:
    check("dead port raises quickly", time.time() - t0 < 6 and "reach" in str(e).lower(), (time.time()-t0, e))

port, srv = spin()
out = m.Rcon.run("127.0.0.1", port, PASSWORD, "big")
check("multi-packet reply is reassembled", out.startswith("y"*100) and out.endswith("tail"), out[:40] + "…" + out[-12:])
srv.close()

# colour codes must not leak into the panel
port, srv = spin()
raw = m.strip_codes("\xa7aGreen \xa7cRed \xa7rplain")
check("colour codes stripped", raw == "Green Red plain", repr(raw))
srv.close()

# ---------- sftp path math (pure, testable without a server) ---------------
be = m.SftpBackend({"host": "h", "user": "u", "root": "/home/mc"})
check("abs joins under root", be.abs("plugins/config.yml") == "/home/mc/plugins/config.yml", be.abs("plugins/config.yml"))
check("abs of empty is root", be.abs("") == "/home/mc", be.abs(""))
check("abs refuses traversal", be.abs("../../etc/passwd") == "/home/mc/etc/passwd", be.abs("../../etc/passwd"))
check("rel is the inverse", be.rel("/home/mc/world/level.dat") == "world/level.dat", be.rel("/home/mc/world/level.dat"))
check("rel of root is empty", be.rel("/home/mc") == "", repr(be.rel("/home/mc")))
be2 = m.SftpBackend({"host": "h", "user": "u", "root": "/"})
check("root of / behaves", be2.abs("world") == "/world" and be2.rel("/world") == "world", (be2.abs("world"), be2.rel("/world")))

# ---------- multi-world backup keeps each folder distinct ------------------
root = tempfile.mkdtemp(prefix="lds_mw_")
for w in ("world", "world_nether", "world_the_end"):
    os.makedirs(os.path.join(root, w))
    open(os.path.join(root, w, "level.dat"), "w").write(w)
open(os.path.join(root, "server.properties"), "w").write("motd=x\n")
open(os.path.join(root, "eula.txt"), "w").write("eula=true\n")
P = m.PANEL
keep_active = P.cfg["active"]
srv_rec = m.blank_server("local", "MW TEST"); srv_rec["local"]["cwd"] = root
P.cfg["servers"].append(srv_rec); P.cfg["active"] = srv_rec["id"]
P.backup_create("mw", "world")
for _ in range(60):
    if not P.backup["busy"]: break
    time.sleep(0.5)
lst = P.backups_list()["items"]
zp = os.path.join(P.backups_list()["dir"], lst[0]["name"])
with zipfile.ZipFile(zp) as z: names = sorted(z.namelist())
check("three worlds stay in separate folders",
      names == ["world/level.dat", "world_nether/level.dat", "world_the_end/level.dat"], names)
with zipfile.ZipFile(zp) as z:
    check("each world keeps its own contents", z.read("world_nether/level.dat") == b"world_nether")
shutil.rmtree(os.path.join(root, "world_nether"))
P.backup_restore(lst[0]["name"])
for _ in range(60):
    if not P.backup["busy"]: break
    time.sleep(0.5)
check("restore rebuilds the right folder",
      open(os.path.join(root, "world_nether", "level.dat")).read() == "world_nether",
      os.listdir(root))
check("restore did not scatter files into the root",
      sorted(os.listdir(root)) == ["eula.txt","server.properties","world","world_nether","world_the_end"],
      sorted(os.listdir(root)))

P.cfg["servers"] = [x for x in P.cfg["servers"] if x["id"] != srv_rec["id"]]
P.cfg["active"] = keep_active; P.backends.clear(); P.save()
shutil.rmtree(os.path.join(m.BACKUP_ROOT, srv_rec["id"]), ignore_errors=True)
shutil.rmtree(root, ignore_errors=True)
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
