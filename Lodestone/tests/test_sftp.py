"""SftpBackend against a real SFTP server, including the staged diagnosis and
the root auto-correction. The fixture mirrors a Minehut account: you land at /
and the server files are one folder down in /server."""
import importlib.util
import os, sys, tempfile
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT_DIR)
sys.path.insert(0, ROOT_DIR)
_TESTHOME = os.environ.get("LODESTONE_TEST_HOME")
if not _TESTHOME:
    _TESTHOME = tempfile.mkdtemp(prefix="lodestone_testhome_")
    os.environ["LODESTONE_TEST_HOME"] = _TESTHOME
os.environ["HOME"] = _TESTHOME
os.environ["USERPROFILE"] = _TESTHOME
os.environ["HOMEDRIVE"] = os.path.splitdrive(_TESTHOME)[0] or "C:"
os.environ["HOMEPATH"] = os.path.splitdrive(_TESTHOME)[1]

import shutil
import socket
import threading
import time

spec = importlib.util.spec_from_file_location("lodestone", "lodestone.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

fails = []


def check(n, c, e=""):
    print(("  ok   " if c else "  FAIL ") + n + (("  " + str(e)[:280]) if not c else ""))
    if not c:
        fails.append(n)


if not m.HAVE_PARAMIKO:
    print("  note   paramiko is not installed — skipping the SFTP suite")
    sys.exit(0)

import paramiko

SRV_ROOT = tempfile.mkdtemp(prefix="lds_sftproot_")
os.makedirs(os.path.join(SRV_ROOT, "server", "plugins"), exist_ok=True)
os.makedirs(os.path.join(SRV_ROOT, "server", "logs"), exist_ok=True)
os.makedirs(os.path.join(SRV_ROOT, "junk"), exist_ok=True)
open(os.path.join(SRV_ROOT, "server", "server.properties"), "w").write("motd=hi\nlevel-name=world\n")
open(os.path.join(SRV_ROOT, "server", "eula.txt"), "w").write("eula=true\n")
open(os.path.join(SRV_ROOT, "server", "logs", "latest.log"), "w").write(
    "[1] [Server thread/INFO]: hello\n")

USER, PASS = "69adca27fcc54932ea495d09", "hunter2"
HOSTKEY = paramiko.RSAKey.generate(2048)


class Server(paramiko.ServerInterface):
    def check_auth_password(self, username, password):
        return (paramiko.AUTH_SUCCESSFUL if (username == USER and password == PASS)
                else paramiko.AUTH_FAILED)

    def get_allowed_auths(self, username):
        return "password"

    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED


class Handle(paramiko.SFTPHandle):
    def stat(self):
        try:
            return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)


class SFTP(paramiko.SFTPServerInterface):
    def _real(self, path):
        return os.path.normpath(os.path.join(SRV_ROOT, path.lstrip("/")))

    def list_folder(self, path):
        try:
            out = []
            for fn in os.listdir(self._real(path)):
                a = paramiko.SFTPAttributes.from_stat(os.stat(os.path.join(self._real(path), fn)))
                a.filename = fn
                out.append(a)
            return out
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(self._real(path)))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    lstat = stat

    def canonicalize(self, path):
        return "/" if path in (".", "") else path

    def open(self, path, flags, attr):
        real = self._real(path)
        try:
            fd = os.open(real, flags | getattr(os, "O_BINARY", 0))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        mode = "r+b" if (flags & os.O_RDWR) else ("wb" if (flags & os.O_WRONLY) else "rb")
        try:
            f = os.fdopen(fd, mode)
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        h = Handle(flags)
        h.filename, h.readfile, h.writefile = real, f, f
        return h

    def mkdir(self, path, attr):
        try:
            os.mkdir(self._real(path))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        return paramiko.SFTP_OK

    def remove(self, path):
        try:
            os.remove(self._real(path))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        return paramiko.SFTP_OK

    def rename(self, old, new):
        try:
            os.rename(self._real(old), self._real(new))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        return paramiko.SFTP_OK


def serve(sock):
    while True:
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        t = paramiko.Transport(conn)
        t.add_server_key(HOSTKEY)
        t.set_subsystem_handler("sftp", paramiko.SFTPServer, SFTP)
        try:
            t.start_server(server=Server())
        except Exception:
            continue


listener = socket.socket()
listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
listener.bind(("127.0.0.1", 0))
listener.listen(16)
PORT = listener.getsockname()[1]
threading.Thread(target=serve, args=(listener,), daemon=True).start()
time.sleep(0.4)


def mk(root, password=PASS, user=USER, port=None):
    return m.SftpBackend({"host": "127.0.0.1", "port": port or PORT, "user": user,
                          "password": password, "root": root})


# ------------------------------------------------------------- happy path ---
be = mk("/server")
ok, msg = be.connect()
check("connects with the right folder", ok, msg)
check("lists the server folder",
      sorted(i["name"] for i in be.list_dir("")["items"]) ==
      ["eula.txt", "logs", "plugins", "server.properties"],
      [i["name"] for i in be.list_dir("")["items"]])
check("reads a file", "motd=hi" in be.read_file("server.properties")["content"])
be.write_file("server.properties", "motd=changed\nlevel-name=world\n")
check("writes a file",
      "motd=changed" in open(os.path.join(SRV_ROOT, "server", "server.properties")).read())
check("tails the log", "Server thread" in be.tail_log("logs/latest.log", 0).get("text", ""))

# ---------------------------------------------- safe replace, cut-off uploads ---
import io
be.upload("plugins", "Thing.jar", io.BytesIO(b"old jar"), 7)
be.upload("plugins", "Thing.jar", io.BytesIO(b"new jar bytes"), 13)
real_jar = os.path.join(SRV_ROOT, "server", "plugins", "Thing.jar")
check("an upload over an existing file replaces it", open(real_jar, "rb").read() == b"new jar bytes")
check("and leaves no temporary file behind",
      not any(n.endswith(".lodestone-part") for n in os.listdir(os.path.dirname(real_jar))),
      os.listdir(os.path.dirname(real_jar)))
try:
    be.upload("plugins", "Thing.jar", io.BytesIO(b"cut"), 5000)
    cut_err = ""
except RuntimeError as e:
    cut_err = str(e)
check("an upload that stops short is refused", "cut off" in cut_err, cut_err)
check("and the file it would have replaced is untouched", open(real_jar, "rb").read() == b"new jar bytes")
check("temporary files never show in a listing",
      not any(i["name"].endswith(".lodestone-part") for i in be.list_dir("plugins")["items"]))
lb_dir = tempfile.mkdtemp(prefix="lodestone_localup_")
lb = m.LocalBackend({"cwd": lb_dir})
lb.upload("", "keep.txt", io.BytesIO(b"keep"), 4)
try:
    lb.upload("", "keep.txt", io.BytesIO(b"x"), 100)
    lcut = ""
except RuntimeError as e:
    lcut = str(e)
check("a local upload that stops short is refused too", "cut off" in lcut, lcut)
check("and the local file stays whole", open(os.path.join(lb_dir, "keep.txt"), "rb").read() == b"keep")
check("with no temporary file left", os.listdir(lb_dir) == ["keep.txt"], os.listdir(lb_dir))
check("reads raw bytes", be.read_raw("server.properties").startswith(b"motd="))
be.close()

# --------------------------------------------------- root auto-correction ---
be2 = mk("/nope")
ok2, msg2 = be2.connect()
check("a wrong folder self-corrects instead of failing", ok2 is True, (ok2, msg2))
check("and says what it did", "switched to /server" in msg2, msg2)
check("the backend now points at the real folder", be2.cfg["root"] == "/server", be2.cfg["root"])
check("and can list it", "server.properties" in [i["name"] for i in be2.list_dir("")["items"]])
be2.close()

# ------------------------------------------------------ staged diagnosis ----
d = mk("/server").diagnose()
names = [x["step"] for x in d["steps"]]
check("diagnosis walks the stages in order",
      names[:5] == ["settings", "paramiko", "dns", "tcp", "ssh"], names)
check("a good setup passes end to end", d["ok"] is True, d["steps"])
check("it reports the login folder", any(x["step"] == "login folder" and x["ok"] for x in d["steps"]))
check("it confirms the server folder", any(x["step"] == "server folder" and x["ok"] for x in d["steps"]))
check("it lists what is on the account", bool(d.get("listing")), d.get("listing"))
check("it reports the host key type",
      any(x["step"] == "ssh" and "host key" in x["detail"] for x in d["steps"]),
      [x for x in d["steps"] if x["step"] == "ssh"])

dbad = mk("/nowhere").diagnose()
sf = [x for x in dbad["steps"] if x["step"] == "server folder"]
check("a wrong folder is pinned as the failing step", sf and sf[0]["ok"] is False, dbad["steps"])
check("and the right one is suggested", dbad.get("suggest_root") == "/server", dbad.get("suggest_root"))

dpw = mk("/server", password="wrong").diagnose()
sign = [x for x in dpw["steps"] if x["step"] == "sign in"]
check("a bad password stops at sign in, with ssh already green",
      sign and sign[0]["ok"] is False and any(x["step"] == "ssh" and x["ok"] for x in dpw["steps"]),
      dpw["steps"])
check("the sign-in message covers the Minehut password gotcha",
      sign and "Minehut" in sign[0]["detail"], sign[0]["detail"] if sign else "")
mh_msg = m.SftpBackend._friendly(Exception("Authentication failed."), "ftp.minehut.com")
check("on a Minehut host it says the SFTP password changes every start", "every time the server" in mh_msg, mh_msg)
check("  and that SFTP only works while the server runs", "while it is running" in mh_msg, mh_msg)
check("  other hosts keep the general message",
      "every time" not in m.SftpBackend._friendly(Exception("Authentication failed."), "sftp.example.com"))

dport = mk("/server", port=9).diagnose()
tcp = [x for x in dport["steps"] if x["step"] == "tcp"]
check("a dead port stops at tcp, with dns already green",
      tcp and tcp[0]["ok"] is False and any(x["step"] == "dns" and x["ok"] for x in dport["steps"]),
      dport["steps"])

ddns = m.SftpBackend({"host": "does-not-exist.invalid", "port": 2223, "user": USER,
                      "password": PASS, "root": "/server"}).diagnose()
dns = [x for x in ddns["steps"] if x["step"] == "dns"]
check("a bad hostname stops at dns", dns and dns[0]["ok"] is False, ddns["steps"])

dblank = m.SftpBackend({"host": "", "port": 22, "user": "", "password": "", "root": "/"}).diagnose()
check("an empty form is refused before any network call",
      dblank["ok"] is False and dblank["steps"][0]["step"] == "settings", dblank["steps"])

# ---------------------------------------------------- error message quality ---
for cfg, want in ((mk("/server", password="wrong"), "credentials"),
                  (mk("/server", port=9), "port"),
                  (m.SftpBackend({"host": "does-not-exist.invalid", "port": 22, "user": USER,
                                  "password": PASS, "root": "/"}), "hostname")):
    okx, msgx = cfg.connect()
    check("failure message mentions '%s'" % want, okx is False and want in msgx.lower(), msgx)
    cfg.close()


# ------------------------------------------------------ concurrency safety ---
# paramiko's SFTPClient multiplexes ONE channel and is not thread safe. Lodestone
# tails logs/latest.log on a background thread while the UI issues its own calls,
# so before serialisation this deadlocked the channel permanently.
be_c = mk("/server")
okc, _ = be_c.connect()
check("connected for the concurrency check", okc)
errs, stop = [], threading.Event()


def tailer():
    # the real loop polls every couple of seconds; a tight spin would just
    # measure the mock server rather than the locking
    while not stop.is_set():
        try:
            be_c.tail_log("logs/latest.log", 0, wait=False)
        except Exception as e:
            errs.append(("tail", type(e).__name__, str(e)[:80]))
        stop.wait(0.5)


def hammer(kind):
    for _ in range(20):
        try:
            if kind == "read":
                be_c.read_file("server.properties")
            else:
                be_c.list_dir("")
        except Exception as e:
            errs.append((kind, type(e).__name__, str(e)[:80]))


workers = [threading.Thread(target=hammer, args=(k,), daemon=True)
           for k in ("read", "list", "read", "list")]
tail_t = threading.Thread(target=tailer, daemon=True)
t0 = time.time()
for w in workers:
    w.start()
tail_t.start()
for w in workers:
    w.join(timeout=60)
stop.set()
time.sleep(0.3)
hung = [w for w in workers if w.is_alive()]
check("concurrent SFTP work does not deadlock the channel", not hung,
      "%d thread(s) still running after 60s" % len(hung))
check("and none of it errors", not errs, errs[:4])
elapsed = time.time() - t0
print("        80 serialised operations took %.1fs against the python mock" % elapsed)
check("it stays responsive under contention", elapsed < 40, "%.1fs" % elapsed)
check("the tail yields instead of blocking the UI",
      be_c.tail_log("logs/latest.log", 0, wait=True) is not None)
be_c.close()

listener.close()
shutil.rmtree(SRV_ROOT, ignore_errors=True)
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
