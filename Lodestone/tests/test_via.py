"""The Via plugin installer: detection, downloads, and doing nothing twice.

The index is a real HTTP server speaking Modrinth's response shape rather than a
mock, because the things that break here are wire-shaped: a file list with no
primary file, a checksum that does not match, a release channel that has to be
preferred over a newer snapshot.

Nothing in this suite touches the internet.
"""

import hashlib
import importlib.util
import io
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
_TESTHOME = tempfile.mkdtemp(prefix="lodestone_via_")

import viainstaller as via

fails = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name +
          (("  " + str(extra)[:300]) if not cond else ""))
    if not cond:
        fails.append(name)


def section(title):
    print("\n-- %s" % title)


def folder(name, files=(), dirs=()):
    d = os.path.join(_TESTHOME, name)
    os.makedirs(d, exist_ok=True)
    for rel in files:
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        io.open(p, "w", encoding="utf-8").write("x\n")
    for rel in dirs:
        os.makedirs(os.path.join(d, rel), exist_ok=True)
    return d


# ===========================================================================
section("what kind of server is this")

check("paper.yml means Paper", via.detect(folder("a", ["paper.yml"]))["flavour"] == "paper")
check("purpur.yml means Purpur", via.detect(folder("b", ["purpur.yml"]))["flavour"] == "purpur")
check("spigot.yml means Spigot", via.detect(folder("c", ["spigot.yml"]))["flavour"] == "spigot")
check("the modern paper-global.yml is found too",
      via.detect(folder("d", [os.path.join("config", "paper-global.yml")]))["flavour"] == "paper")
check("an existing plugins/ is enough on its own",
      via.detect(folder("e", dirs=["plugins"]))["flavour"] == "bukkit")

check("Paper is compatible", via.detect(folder("f", ["paper.yml"]))["compatible"])
check("Purpur is compatible", via.detect(folder("g", ["purpur.yml"]))["compatible"])

fab = via.detect(folder("h", ["fabric-server-launcher.properties"]))
check("Fabric is not compatible", not fab["compatible"])
check("  and it says mods, not plugins", "mods" in fab["reason"], fab["reason"])
check("  and it names ViaFabric as the thing that would work",
      "ViaFabric" in fab["reason"], fab["reason"])

mods = via.detect(folder("i", dirs=["mods"]))
check("a mods/ folder alone is treated as a mod loader", not mods["compatible"])

plain = via.detect(folder("j"), "")
check("a folder with nothing in it reads as vanilla", plain["flavour"] == "vanilla")
check("  and is refused with a reason a person can act on",
      "no plugin loader" in plain["reason"], plain["reason"])
check("  and the reason points at Paper", "Paper" in plain["reason"])

check("a jar named paper-1.21.4.jar is believed",
      via.detect(folder("k"), "/x/paper-1.21.4.jar")["flavour"] == "paper")
check("a jar named fabric-server-launch.jar is believed",
      via.detect(folder("l"), "/x/fabric-server-launch.jar")["flavour"] == "fabric")
check("folder evidence beats the jar name",
      via.detect(folder("m", ["purpur.yml"]), "/x/paper-1.21.4.jar")["flavour"] == "purpur")

section("which Minecraft version")
vh = folder("n")
io.open(os.path.join(vh, "version_history.json"), "w").write(
    json.dumps({"currentVersion": "132 (MC: 1.21.11)"}))
check("version_history.json is read", via.mc_version(vh) == "1.21.11", via.mc_version(vh))
check("a version in the jar name is the fallback",
      via.mc_version(folder("o"), "/x/paper-1.20.6-42.jar") == "1.20.6")
check("an unknowable version is empty, not a guess",
      via.mc_version(folder("p"), "/x/server.jar") == "")


# ===========================================================================
#  a fake index, speaking Modrinth's shapes
# ===========================================================================
JARS = {}          # served path -> bytes
STATE = {"fail_times": 0, "corrupt": False, "hits": 0}


def jar_bytes(tag):
    return ("PK\x03\x04 fake jar %s" % tag).encode() * 40


class _Index(BaseHTTPRequestHandler):
    def _send(self, code, payload, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.startswith("/files/"):
            STATE["hits"] += 1
            if STATE["fail_times"] > 0:
                STATE["fail_times"] -= 1
                self.send_response(500)
                self.end_headers()
                return
            data = JARS.get(path, b"")
            if STATE["corrupt"]:
                data = data + b"tampered"
            return self._send(200, data, "application/java-archive")
        if "/version" in path:
            slug = path.split("/")[2]
            return self._send(200, json.dumps(VERSIONS.get(slug, [])).encode())
        self.send_response(404)
        self.end_headers()

    def log_message(self, *a):
        pass


def version_entry(slug, number, tag, channel="release", loaders=None,
                  game_versions=None):
    body = jar_bytes(tag)
    name = "%s-%s.jar" % (slug.capitalize(), number)
    served = "/files/%s" % name
    JARS[served] = body
    return {
        "version_number": number,
        "version_type": channel,
        "date_published": "2026-0%d-01T00:00:00Z" % (2 if channel == "release" else 3),
        "loaders": loaders or ["paper", "spigot", "bukkit"],
        "game_versions": game_versions or ["1.21.11"],
        "files": [{"primary": True, "url": "%s" + served, "filename": name,
                   "size": len(body),
                   "hashes": {"sha1": hashlib.sha1(body).hexdigest()}}],
    }


VERSIONS = {
    "viaversion": [version_entry("viaversion", "9.9.9", "vv-snap", "beta"),
                   version_entry("viaversion", "5.11.0", "vv-rel")],
    "viabackwards": [version_entry("viabackwards", "5.11.0", "vb-rel")],
    "viarewind": [version_entry("viarewind", "4.1.3", "vr-rel")],
}

srv = ThreadingHTTPServer(("127.0.0.1", 0), _Index)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % PORT

# Point the URLs at the fake index now that its port is known.
for _slug, _vs in VERSIONS.items():
    for _v in _vs:
        _v["files"][0]["url"] = _v["files"][0]["url"] % BASE
via.API = BASE


def logs():
    out = []
    return out, lambda m: out.append(m)


section("a first install")
paper = folder("srv_paper", ["paper.yml"])
io.open(os.path.join(paper, "version_history.json"), "w").write(
    json.dumps({"currentVersion": "132 (MC: 1.21.11)"}))
lines, log = logs()
rep = via.install(paper, log=log)
pdir = os.path.join(paper, "plugins")
check("it reports success", rep["ok"], rep)
check("  three plugins installed", rep["installed"] == 3, rep)
check("  plugins/ was created", os.path.isdir(pdir))
jars = sorted(os.listdir(pdir))
check("  three jars are on disk", len(jars) == 3, jars)
check("  and they are the Via ones",
      all(j.lower().startswith(("viaversion", "viabackwards", "viarewind"))
          for j in jars), jars)
check("a stable release beats a newer beta",
      any("5.11.0" in j for j in jars) and not any("9.9.9" in j for j in jars), jars)
check("every step was logged", len(lines) >= 8, len(lines))
check("  including the detected flavour",
      any("paper" in l for l in lines), lines[:2])
check("  and the version it matched", any("1.21.11" in l for l in lines))

section("running it again changes nothing")
before = {j: os.path.getmtime(os.path.join(pdir, j)) for j in os.listdir(pdir)}
hits_before = STATE["hits"]
lines2, log2 = logs()
rep2 = via.install(paper, log=log2)
check("nothing was installed the second time", rep2["installed"] == 0, rep2)
check("  everything was skipped", rep2["skipped"] == 3, rep2)
check("  nothing was downloaded", STATE["hits"] == hits_before, STATE["hits"] - hits_before)
check("  no duplicate jars appeared", len(os.listdir(pdir)) == 3, os.listdir(pdir))
check("  and the files were not touched",
      {j: os.path.getmtime(os.path.join(pdir, j)) for j in os.listdir(pdir)} == before)
check("  the log says so plainly",
      any("already up to date" in l for l in lines2), lines2)

section("an older build is replaced, not added alongside")
old = os.path.join(pdir, "ViaVersion-1.0.0.jar")
for j in list(os.listdir(pdir)):
    if j.lower().startswith("viaversion"):
        os.remove(os.path.join(pdir, j))
io.open(old, "wb").write(b"an old build")
lines3, log3 = logs()
rep3 = via.install(paper, log=log3)
check("the old jar counts as an update", rep3["updated"] == 1, rep3)
check("  the old file is gone", not os.path.exists(old))
check("  exactly one ViaVersion remains",
      len([j for j in os.listdir(pdir) if j.lower().startswith("viaversion")]) == 1,
      os.listdir(pdir))
check("  and still three jars in total", len(os.listdir(pdir)) == 3, os.listdir(pdir))
check("  the log names what it replaced",
      any("replaced" in l for l in lines3), lines3)

section("a flaky download is retried")
for j in list(os.listdir(pdir)):
    if j.lower().startswith("viarewind"):
        os.remove(os.path.join(pdir, j))
STATE["fail_times"] = 2          # fail twice, succeed on the third attempt
lines4, log4 = logs()
rep4 = via.install(paper, log=log4, only=["viarewind"])
check("it recovers within three attempts", rep4["failed"] == 0, rep4)
check("  and the jar is there", any(j.lower().startswith("viarewind")
                                    for j in os.listdir(pdir)), os.listdir(pdir))
check("  the retries were logged",
      len([l for l in lines4 if "attempt" in l]) >= 2, lines4)

section("a download that never verifies is refused")
for j in list(os.listdir(pdir)):
    if j.lower().startswith("viarewind"):
        os.remove(os.path.join(pdir, j))
STATE["corrupt"] = True
lines5, log5 = logs()
rep5 = via.install(paper, log=log5, only=["viarewind"])
STATE["corrupt"] = False
check("a checksum mismatch is a failure", rep5["failed"] == 1, rep5)
check("  and it is not reported as ok", not rep5["ok"])
check("  no jar was left behind",
      not any(j.lower().startswith("viarewind") for j in os.listdir(pdir)),
      os.listdir(pdir))
check("  no temp file was left behind either",
      not any(j.startswith("via-") for j in os.listdir(pdir)), os.listdir(pdir))
check("  and it gave up after three attempts",
      any("after 3 attempts" in l for l in lines5), lines5[-3:])

section("an incompatible server is left alone")
van = folder("srv_vanilla")
io.open(os.path.join(van, "server.properties"), "w").write("server-port=25565\n")
lines6, log6 = logs()
rep6 = via.install(van, log=log6)
check("it refuses", not rep6["compatible"])
check("  reports no failures, because nothing was attempted", rep6["failed"] == 0, rep6)
check("  does not create a plugins folder",
      not os.path.isdir(os.path.join(van, "plugins")))
check("  and explains why in the log",
      any("skipping" in l for l in lines6), lines6)

section("the wider fork list")
for fork in ("pufferfish", "folia", "leaves", "airplane", "yatopia", "gale"):
    d = via.detect(folder("fork_" + fork), "/x/%s-1.21.4.jar" % fork)
    check("%s is a Bukkit fork" % fork,
          d["platform"] == via.PLATFORM_BUKKIT and d["compatible"],
          (d["flavour"], d["platform"]))
check("neoforge is not swallowed by the word forge",
      via.detect(folder("nf"), "/x/neoforge-21.1.jar")["flavour"] == "neoforge")
check("craftbukkit is not swallowed by the word bukkit",
      via.detect(folder("cb"), "/x/craftbukkit-1.21.jar")["flavour"] == "craftbukkit")

section("proxies are their own platform")
vel = via.detect(folder("p_vel", ["velocity.toml"]))
check("velocity.toml means Velocity", vel["flavour"] == "velocity", vel["flavour"])
check("  and it is a proxy", vel["platform"] == via.PLATFORM_PROXY)
check("  which is compatible", vel["compatible"])

wf = via.detect(folder("p_wf", ["waterfall.yml"]))
check("waterfall.yml means Waterfall", wf["flavour"] == "waterfall", wf["flavour"])
check("  and it is a proxy", wf["platform"] == via.PLATFORM_PROXY)

bc = via.detect(folder("p_bc", ["config.yml"], dirs=["modules"]))
check("config.yml beside modules/ means BungeeCord",
      bc["flavour"] == "bungeecord", bc["flavour"])
check("  and it is a proxy", bc["platform"] == via.PLATFORM_PROXY)

check("a bare config.yml is not assumed to be BungeeCord",
      via.detect(folder("p_bare", ["config.yml"]))["flavour"] != "bungeecord")

# A proxy has plugins/ too, so proxy evidence has to win over that rule.
both = via.detect(folder("p_both", ["velocity.toml"], dirs=["plugins"]))
check("a proxy with a plugins/ folder is still a proxy",
      both["platform"] == via.PLATFORM_PROXY, both)

section("snapshots are refused with their own reason")
snap = folder("snap")
io.open(os.path.join(snap, "version_history.json"), "w").write(
    json.dumps({"currentVersion": "25w14a"}))
s = via.detect(snap)
check("a weekly snapshot is spotted", s["flavour"] == "snapshot", s["flavour"])
check("  and refused", not s["compatible"])
check("  with a reason about the protocol changing",
      "protocol" in s["reason"], s["reason"])
check("a release candidate counts too",
      via.is_snapshot(folder("rc"), "/x/1.21.2-rc1.jar"))
check("a plain release does not",
      not via.is_snapshot(folder("rel"), "/x/paper-1.21.4-100.jar"))

section("Fabric takes mods but not Via")
for flavour, kw in (("fabric", {"files": ["fabric-server-launcher.properties"]}),
                    ("quilt", {})):
    d = (via.detect(folder("m_" + flavour, **kw)) if kw
         else via.detect(folder("m_quilt"), "/x/quilt-server.jar"))
    check("%s is its own platform" % flavour,
          d["platform"] == via.PLATFORM_FABRIC, d["platform"])
    check("  Via is still refused there", not d["compatible"])
    check("  with a reason naming ViaFabric",
          "ViaFabric" in d["reason"], d["reason"])
    check("  but a mod preset is offered", d["preset_ok"], d)
    check("  which installs into mods/", d["target_dir"] == "mods", d["target_dir"])

section("Forge and NeoForge get nothing")
for flavour in ("forge", "neoforge"):
    d = via.detect(folder("mj_" + flavour), "/x/%s-server.jar" % flavour)
    check("%s is modded" % flavour, d["platform"] == via.PLATFORM_MODDED, d)
    check("  and says mods, not plugins",
          "mod" in d["reason"].lower(), d["reason"])
    check("  and has no preset", not d["preset_ok"], d)


# ---------------------------------------------------------------------------
section("proxies take proxy builds")

PROXY_LOADERS = ["velocity", "bungeecord", "waterfall"]


def add(slug, number, tag, loaders, channel="release"):
    e = version_entry(slug, number, tag, channel, loaders)
    e["files"][0]["url"] = e["files"][0]["url"] % BASE
    VERSIONS.setdefault(slug, []).append(e)
    return e


add("viaversion", "5.11.0-proxy", "vv-proxy", PROXY_LOADERS)
add("viabackwards", "5.11.0-proxy", "vb-proxy", PROXY_LOADERS)
# ViaRewind deliberately has no proxy build, to prove that reads as a skip
# rather than a failure.

velsrv = folder("srv_velocity", ["velocity.toml"])
lines7, log7 = logs()
rep7 = via.install(velsrv, log=log7)
vdir = os.path.join(velsrv, "plugins")
check("a proxy install succeeds", rep7["ok"], rep7)
check("  it reports the proxy platform", rep7["platform"] == via.PLATFORM_PROXY)
check("  plugins/ was created", os.path.isdir(vdir))
vjars = sorted(os.listdir(vdir))
check("  the proxy builds landed",
      any("proxy" in j for j in vjars) and len(vjars) == 2, vjars)
check("  a plugin with no proxy build is skipped, not failed",
      rep7["failed"] == 0 and rep7["skipped"] == 1, rep7)
check("  and the log says why",
      any("no proxy build is published" in l for l in lines7), lines7)
check("  the Bukkit-only build was not installed by mistake",
      not any("vv-rel" in j or "5.11.0.jar" in j for j in vjars), vjars)

section("a Bukkit server does not get proxy builds")
paper2 = folder("srv_paper2", ["paper.yml"])
rep8 = via.install(paper2, log=lambda m: None)
pjars = sorted(os.listdir(os.path.join(paper2, "plugins")))
check("Bukkit builds only", not any("proxy" in j for j in pjars), pjars)
check("  all three installed", rep8["installed"] == 3, rep8)

section("updating keeps the plugin's config")
cfgdir = os.path.join(paper2, "plugins", "ViaVersion")
os.makedirs(cfgdir, exist_ok=True)
io.open(os.path.join(cfgdir, "config.yml"), "w").write("checkforupdates: false\n")
for j in list(os.listdir(os.path.join(paper2, "plugins"))):
    if j.lower().startswith("viaversion") and j.lower().endswith(".jar"):
        os.remove(os.path.join(paper2, "plugins", j))
io.open(os.path.join(paper2, "plugins", "ViaVersion-0.0.1.jar"), "wb").write(b"old")
lines9, log9 = logs()
rep9 = via.install(paper2, log=log9, only=["viaversion"])
check("the outdated jar is replaced", rep9["updated"] == 1, rep9)
check("  the config directory survives", os.path.isdir(cfgdir))
check("  and the config file is untouched",
      io.open(os.path.join(cfgdir, "config.yml")).read().strip() == "checkforupdates: false")
check("  the log mentions the config was kept",
      any("config kept" in l for l in lines9), lines9)

section("an unreachable index is a failure, a missing build is not")
_saved_api = via.API
via.API = "http://127.0.0.1:1"      # nothing listens here
lines10, log10 = logs()
rep10 = via.install(folder("srv_down", ["paper.yml"]), log=log10, only=["viaversion"])
via.API = _saved_api
check("an unreachable index fails", rep10["failed"] == 1, rep10)
check("  and is not reported as ok", not rep10["ok"])
check("  after three attempts",
      len([l for l in lines10 if "attempt" in l]) >= 3, lines10)


srv.shutdown()
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
