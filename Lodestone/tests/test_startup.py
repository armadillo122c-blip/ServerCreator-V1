import json, subprocess, sys, time, urllib.request
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

fails = []
def check(n, c, e=""):
    print(("  ok   " if c else "  FAIL ") + n + (("  " + str(e)[:200]) if not c else ""))
    if not c: fails.append(n)

p = subprocess.Popen([sys.executable, os.path.join("tests", "_runner.py"), "8797"],
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
banner, deadline = "", time.time() + 20
while time.time() < deadline:
    line = p.stdout.readline()
    if not line: break
    banner += line
    if "Ctrl+C to stop" in line or "WOULD_OPEN" in line:
        if "WOULD_OPEN" in banner and "Ctrl+C" in banner: break
print(banner.rstrip())
try:
    with urllib.request.urlopen("http://127.0.0.1:8797/api/status", timeout=8) as r:
        st = json.loads(r.read())
except Exception as e:
    st = {}; print("  (no answer: %s)" % e)
check("main() honoured the port from argv", "8797" in banner, banner[:200])
check("banner names app and version", "Lodestone 1.0.0" in banner)
check("banner reports config, backups, discord, api",
      all(k in banner for k in ("config", "backups", "discord", "api")))
check("server answers after main()", st.get("version") == "1.0.0", st)
check("opened the UI once, on loopback", banner.count("WOULD_OPEN") == 1 and "127.0.0.1:8797" in banner)
p.terminate(); p.wait(timeout=10)

# a second instance on the same port must explain itself, not traceback.
# Start the first one and wait until it is genuinely listening before racing it,
# otherwise whichever process wins the bind is arbitrary.
RUNNER = os.path.join("tests", "_runner.py")
first = subprocess.Popen([sys.executable, RUNNER, "8796"],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
listening = False
for _ in range(40):
    line = first.stdout.readline()
    if not line:
        break
    if "Ctrl+C to stop" in line:
        break
for _ in range(20):
    try:
        urllib.request.urlopen("http://127.0.0.1:8796/api/status", timeout=3).read()
        listening = True
        break
    except Exception:
        time.sleep(0.3)
check("the first instance is listening", listening)

second = subprocess.Popen([sys.executable, RUNNER, "8796"],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    out2, _ = second.communicate(timeout=20)
except subprocess.TimeoutExpired:
    second.kill()
    out2 = "(the second instance never exited - it took the port)"
check("a second instance hands over to the first instead of failing",
      second.returncode == 0 and "already running" in out2,
      (second.returncode, out2[:200]))
check("and it opens the window that is already there",
      "WOULD_OPEN" in out2 and "8796" in out2, out2[:200])
check("without raising", "Traceback" not in out2, out2[:200])
# A port held by something that is NOT Lodestone must still be a clean refusal,
# not a silent hand-over to a stranger's HTTP server.
import socket as _socket
squat = _socket.socket()
squat.bind(("127.0.0.1", 0))
squat.listen(1)
squat_port = squat.getsockname()[1]
third = subprocess.Popen([sys.executable, RUNNER, str(squat_port)],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
try:
    out3, _ = third.communicate(timeout=30)
except subprocess.TimeoutExpired:
    third.kill()
    out3 = "(never exited)"
check("a port held by something else is refused, not adopted",
      third.returncode == 1, (third.returncode, out3[:200]))
check("and the refusal says what to do",
      "not Lodestone" in out3 or "could not start" in out3.lower(), out3[:250])
squat.close()

first.terminate()
try:
    first.wait(timeout=10)
except subprocess.TimeoutExpired:
    first.kill()

# --headless is how the desktop app (desktop/) runs the engine: the API comes
# up, no browser opens, and a second headless launch leaves the first alone.
eng = subprocess.Popen([sys.executable, "-u", RUNNER, "8795", "--headless"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
hb = ""
for _ in range(60):
    line = eng.stdout.readline()
    if not line:
        break
    hb += line
    if "Ctrl+C to stop" in line:
        break
up = False
for _ in range(20):
    try:
        urllib.request.urlopen("http://127.0.0.1:8795/api/events", timeout=3).read()
        up = True
        break
    except Exception:
        time.sleep(0.3)
check("--headless still serves the API", up, hb[:200])
time.sleep(1.0)  # open_ui would have fired 0.6 s after the banner
eng.terminate()
try:
    rest, _ = eng.communicate(timeout=10)
except subprocess.TimeoutExpired:
    eng.kill()
    rest = ""
check("--headless opens no browser", "WOULD_OPEN" not in hb + (rest or ""), (hb + (rest or ""))[:200])

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
