"""Two local servers, up at the same time.

Until now "running" meant "is the active server, and Panel.proc is alive", so a
second server could not be up by construction. Runtime state is per server id
now, and this suite holds that line: two real child processes, two consoles,
two player lists, and a port check so the second one cannot silently fail to
bind a port the first already has.

The children are scripts rather than a JVM - what is being tested is Lodestone's
process bookkeeping, not Minecraft.
"""

import importlib.util
import io
import os
import sys
import tempfile
import time

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

spec = importlib.util.spec_from_file_location("lodestone", "lodestone.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.PANEL.save = lambda *a, **k: True

fails = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name +
          (("  " + str(extra)[:300]) if not cond else ""))
    if not cond:
        fails.append(name)


def section(title):
    print("\n-- %s" % title)


CHILD = os.path.join(_TESTHOME, "fake_server.py")
io.open(CHILD, "w", encoding="utf-8").write(
    "import sys, time\n"
    "tag = sys.argv[1]\n"
    "print('[%s] Starting minecraft server version 1.21' % tag, flush=True)\n"
    "print('[%s] Done (1.234s)! For help, type \"help\"' % tag, flush=True)\n"
    "for line in sys.stdin:\n"
    "    line = line.strip()\n"
    "    if line == 'stop':\n"
    "        print('[%s] Stopping server' % tag, flush=True)\n"
    "        break\n"
    "    print('[%s] echo %s' % (tag, line), flush=True)\n")


def make_server(name, port, tag):
    """A local server whose 'jar' is a script, with its own folder and port."""
    folder = os.path.join(_TESTHOME, "srv_" + tag)
    os.makedirs(folder, exist_ok=True)
    io.open(os.path.join(folder, "eula.txt"), "w").write("eula=true\n")
    io.open(os.path.join(folder, "server.properties"), "w").write(
        "server-port=%d\nserver-ip=\n" % port)
    s = m.blank_server("local", name)
    s["local"]["cwd"] = folder
    s["local"]["server_type"] = "script"
    s["local"]["run_cmd"] = '"%s" -u "%s" %s' % (sys.executable, CHILD, tag)
    m.PANEL.servers().append(s)
    return s


def wait_until(fn, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.2)
    return False


section("two servers, two ports")
alpha = make_server("Alpha", 25570, "alpha")
beta = make_server("Beta", 25571, "beta")
check("ports are read from each server's own properties",
      m.PANEL._local_port(alpha) == 25570 and m.PANEL._local_port(beta) == 25571,
      (m.PANEL._local_port(alpha), m.PANEL._local_port(beta)))

ok_a, msg_a = m.PANEL.start(alpha["id"])
check("the first server starts", ok_a, msg_a)
check("  and reports itself running",
      wait_until(lambda: m.PANEL.is_running(alpha["id"])), "never came up")

ok_b, msg_b = m.PANEL.start(beta["id"])
check("the second server starts while the first is up", ok_b, msg_b)
check("  and it too reports running",
      wait_until(lambda: m.PANEL.is_running(beta["id"])), "never came up")

check("both are running at once",
      m.PANEL.is_running(alpha["id"]) and m.PANEL.is_running(beta["id"]))
check("  running_ids lists both",
      set(m.PANEL.running_ids()) >= {alpha["id"], beta["id"]},
      m.PANEL.running_ids())
check("  and neither is the active server, which is neither of them",
      m.PANEL.cfg.get("active") not in (alpha["id"], beta["id"]),
      m.PANEL.cfg.get("active"))

section("separate consoles")
check("each server has its own runtime object",
      m.PANEL.runtime(alpha["id"]) is not m.PANEL.runtime(beta["id"]))
wait_until(lambda: any("Done" in t for _, t in m.PANEL.recent(50, alpha["id"])))
wait_until(lambda: any("Done" in t for _, t in m.PANEL.recent(50, beta["id"])))
a_lines = [t for _, t in m.PANEL.recent(50, alpha["id"])]
b_lines = [t for _, t in m.PANEL.recent(50, beta["id"])]
check("alpha's console has alpha's output", any("[alpha]" in t for t in a_lines),
      a_lines[-3:])
check("  and none of beta's", not any("[beta]" in t for t in a_lines), a_lines[-3:])
check("beta's console has beta's output", any("[beta]" in t for t in b_lines),
      b_lines[-3:])
check("  and none of alpha's", not any("[alpha]" in t for t in b_lines), b_lines[-3:])

section("commands go to the right server")
m.PANEL.command("hello-alpha", quiet=True, sid=alpha["id"])
got = wait_until(lambda: any("echo hello-alpha" in t
                             for _, t in m.PANEL.recent(60, alpha["id"])))
check("a command reaches the server it names", got)
check("  and not the other one",
      not any("hello-alpha" in t for _, t in m.PANEL.recent(60, beta["id"])))

section("port conflicts are refused, not left to fail silently")
clash_srv = make_server("Clash", 25570, "clash")
ok_c, msg_c = m.PANEL.start(clash_srv["id"])
check("a server on a port already in use is refused", not ok_c)
check("  and the message names the server holding it",
      "Alpha" in (msg_c or ""), msg_c)
check("  and no process was spawned", not m.PANEL.is_running(clash_srv["id"]))
check("  while the server that had the port is untouched",
      m.PANEL.is_running(alpha["id"]))

section("stopping one leaves the other alone")
m.PANEL.stop(alpha["id"])
check("the stopped one goes down",
      wait_until(lambda: not m.PANEL.is_running(alpha["id"])), "still running")
check("  and the other keeps running", m.PANEL.is_running(beta["id"]))
check("  its console survived the other stopping",
      any("[beta]" in t for _, t in m.PANEL.recent(50, beta["id"])))
check("the freed port can now be taken",
      m.PANEL.start(clash_srv["id"])[0])
wait_until(lambda: m.PANEL.is_running(clash_srv["id"]))
m.PANEL.stop(clash_srv["id"])

section("switching servers does not disturb a running one")
before = len(m.PANEL.recent(500, beta["id"]))
m.PANEL.activate(beta["id"])
check("switching keeps the console it switched to",
      len(m.PANEL.recent(500, beta["id"])) >= before,
      (before, len(m.PANEL.recent(500, beta["id"]))))
check("  and the server is still up", m.PANEL.is_running(beta["id"]))
check("the bare attributes now follow the active server",
      m.PANEL.proc is m.PANEL.runtime(beta["id"]).proc)

m.PANEL.stop(beta["id"])
wait_until(lambda: not m.PANEL.is_running(beta["id"]))
check("everything is stopped at the end", not m.PANEL.running_ids(),
      m.PANEL.running_ids())

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
