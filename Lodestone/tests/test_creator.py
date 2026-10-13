"""Creating servers, and the mod and plugin browser.

Every outside service is a real HTTP server here speaking that service's real
response shape - Mojang's manifest, PaperMC's Fill v3 builds, Modrinth's
search / version / version_files, Hangar's versions, Spiget's resources - for
the same reason as the Via suite: what breaks in this code is wire-shaped. A
checksum that does not match, a dependency named by number in one place and by
slug in another, a jar that carries two platforms' metadata at once.

Then the panel end to end: a create job that downloads a server and its mods
and puts it on the shelf, and one that fails half way and must leave nothing
behind.

Nothing in this suite touches the internet, and no installer jar is run.
"""

import hashlib
import importlib.util
import io
import json
import os
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
    _TESTHOME = tempfile.mkdtemp(prefix="lodestone_create_")
    os.environ["LODESTONE_TEST_HOME"] = _TESTHOME
os.environ["HOME"] = _TESTHOME
os.environ["USERPROFILE"] = _TESTHOME
os.environ["HOMEDRIVE"] = os.path.splitdrive(_TESTHOME)[0] or "C:"
os.environ["HOMEPATH"] = os.path.splitdrive(_TESTHOME)[1]

import creator as cr
import content as ct

fails = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name +
          (("  " + str(extra)[:300]) if not cond else ""))
    if not cond:
        fails.append(name)


def section(title):
    print("\n-- %s" % title)


def jar(**files):
    """A small but real jar: a manifest plus whatever metadata is asked for."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("META-INF/MANIFEST.MF", "Manifest-Version: 1.0\n")
        for name, text in files.items():
            z.writestr(name.replace("__", "/"), text)
    return buf.getvalue()


def sha(data, algo):
    return hashlib.new(algo, data).hexdigest()


# ---------------------------------------------------------------------------
#  the fake internet
# ---------------------------------------------------------------------------

ROUTES = {}          # path (no query) -> (status, bytes, ctype) or callable(query, body)
HITS = []


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _answer(self):
        u = urlparse(self.path)
        HITS.append(u.path)
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"null") if n else None
        r = ROUTES.get(u.path)
        if callable(r):
            r = r(parse_qs(u.query), body)
        if r is None:
            r = (404, b'{"error":"not found"}', "application/json")
        status, data, ctype = r
        if isinstance(data, (dict, list)):
            data = json.dumps(data).encode()
        elif isinstance(data, str):
            data = data.encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = _answer
    do_POST = _answer


srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % srv.server_address[1]


def J(obj, status=200):
    return (status, obj, "application/json")


def B(data, status=200):
    return (status, data, "application/java-archive")


cr.MOJANG_MANIFEST = BASE + "/mojang/manifest.json"
cr.PAPER_API = BASE + "/paper/v3"
cr.PURPUR_API = BASE + "/purpur/v2/purpur"
cr.FABRIC_META = BASE + "/fabric/v2"
cr.QUILT_META = BASE + "/quilt/v3"
cr.FORGE_MAVEN = BASE + "/forge/maven"
cr.FORGE_PROMOS = BASE + "/forge/promotions_slim.json"
cr.NEOFORGE_MAVEN = BASE + "/neo/maven"
cr.NEOFORGE_VERSIONS = BASE + "/neo/versions"
cr.SPIGOT_VERSIONS = BASE + "/spigot/versions/"
cr.ADOPTIUM_API = BASE + "/adoptium/v3"
cr.RETRIES = 2
ct.MODRINTH_API = BASE + "/modrinth/v2"
ct.HANGAR_API = BASE + "/hangar/v1"
ct.SPIGET_API = BASE + "/spiget/v2"
ct.RETRIES = 2

# -- Mojang
SERVER_JAR = jar()
ROUTES["/mojang/manifest.json"] = J({
    "latest": {"release": "1.21.4", "snapshot": "25w01a"},
    "versions": [
        {"id": "25w01a", "type": "snapshot", "url": BASE + "/mojang/25w01a.json"},
        {"id": "1.21.4", "type": "release", "url": BASE + "/mojang/1.21.4.json"},
        {"id": "1.21.1", "type": "release", "url": BASE + "/mojang/1.21.1.json"},
        {"id": "1.20.1", "type": "release", "url": BASE + "/mojang/1.20.1.json"},
        {"id": "1.12.2", "type": "release", "url": BASE + "/mojang/1.12.2.json"},
        {"id": "b1.7.3", "type": "old_beta", "url": BASE + "/mojang/b.json"},
    ]})
for mc, java in (("1.21.4", 21), ("1.21.1", 21), ("1.20.1", 17), ("1.12.2", 8), ("25w01a", 21)):
    ROUTES["/mojang/%s.json" % mc] = J({
        "id": mc, "javaVersion": {"component": "x", "majorVersion": java},
        "downloads": {"server": {"url": BASE + "/mojang/server-%s.jar" % mc,
                                 "sha1": sha(SERVER_JAR, "sha1"), "size": len(SERVER_JAR)}}})
    ROUTES["/mojang/server-%s.jar" % mc] = B(SERVER_JAR)

# -- Paper (Fill v3)
PAPER_JAR = jar(**{"META-INF__versions__paper": "x"})
ROUTES["/paper/v3/projects/paper"] = J({"project": {"id": "paper"},
                                        "versions": {"1.21": ["1.21.4", "1.21.1"], "1.20": ["1.20.1"]}})
ROUTES["/paper/v3/projects/paper/versions/1.21.4/builds"] = J([
    {"id": 230, "channel": "BETA"}, {"id": 232, "channel": "STABLE"}, {"id": 231, "channel": "STABLE"}])
ROUTES["/paper/v3/projects/paper/versions/1.21.4/builds/232"] = J({
    "id": 232, "channel": "STABLE",
    "downloads": {"server:default": {"name": "paper-1.21.4-232.jar", "url": BASE + "/paper/files/paper-232.jar",
                                     "size": len(PAPER_JAR), "checksums": {"sha256": sha(PAPER_JAR, "sha256")}}}})
ROUTES["/paper/files/paper-232.jar"] = B(PAPER_JAR)
# a build whose published checksum is wrong
ROUTES["/paper/v3/projects/paper/versions/1.21.1/builds"] = J([{"id": 100, "channel": "STABLE"}])
ROUTES["/paper/v3/projects/paper/versions/1.21.1/builds/100"] = J({
    "id": 100, "downloads": {"server:default": {"name": "paper-1.21.1-100.jar", "url": BASE + "/paper/files/paper-100.jar",
                                                "size": len(PAPER_JAR), "checksums": {"sha256": "0" * 64}}}})
ROUTES["/paper/files/paper-100.jar"] = B(PAPER_JAR)

# -- Purpur
ROUTES["/purpur/v2/purpur"] = J({"project": "purpur", "versions": ["1.20.1", "1.21.1", "1.21.4"]})
ROUTES["/purpur/v2/purpur/1.21.4"] = J({"builds": {"latest": "2416", "all": ["2414", "2415", "2416"]}})
ROUTES["/purpur/v2/purpur/1.21.4/2416"] = J({"build": "2416", "md5": sha(PAPER_JAR, "md5")})
ROUTES["/purpur/v2/purpur/1.21.4/2416/download"] = B(PAPER_JAR)

# -- Fabric
FABRIC_LAUNCHER = jar(**{"fabric-server-launch.properties": "x"})
ROUTES["/fabric/v2/versions/game"] = J([{"version": "25w01a", "stable": False},
                                        {"version": "1.21.4", "stable": True}, {"version": "1.21.1", "stable": True}])
ROUTES["/fabric/v2/versions/loader/1.21.1"] = J([{"loader": {"version": "0.17.0", "stable": False}},
                                                 {"loader": {"version": "0.16.9", "stable": True}}])
ROUTES["/fabric/v2/versions/installer"] = J([{"version": "1.1.2", "stable": True, "url": "x"}])
ROUTES["/fabric/v2/versions/loader/1.21.1/0.16.9/1.1.2/server/jar"] = B(FABRIC_LAUNCHER)

# -- Quilt, Forge, NeoForge, Spigot: listings only
ROUTES["/quilt/v3/versions/game"] = J([{"version": "1.21.1", "stable": True}])
ROUTES["/quilt/v3/versions/loader/1.21.1"] = J([{"loader": {"version": "0.20.0-beta.9"}},
                                                {"loader": {"version": "0.26.4"}},
                                                {"loader": {"version": "0.20.0-beta.8"}}])
ROUTES["/forge/maven/maven-metadata.xml"] = (200, "<metadata><versioning><versions>"
                                             "<version>1.21.1-52.1.0</version><version>1.21.1-52.0.9</version>"
                                             "<version>1.20.1-47.4.0</version><version>1.12.2-14.23.5.2860</version>"
                                             "</versions></versioning></metadata>", "text/xml")
ROUTES["/forge/promotions_slim.json"] = J({"promos": {"1.21.1-recommended": "52.0.9", "1.21.1-latest": "52.1.0"}})
ROUTES["/neo/versions"] = J({"versions": ["20.2.86", "21.1.77", "21.1.80-beta", "21.0.5-beta",
                                          "26.3.0.76", "0.25w14craftmine.3-beta"]})
ROUTES["/spigot/versions/"] = (200, '<a href="1.21.4.json">x</a><a href="1.21.1.json">x</a>'
                                    '<a href="4458.json">build</a><a href="1.12.2.json">x</a>', "text/html")


# ---------------------------------------------------------------------------
section("version arithmetic")
check("1.21.10 sorts after 1.21.9", cr.version_key("1.21.10") > cr.version_key("1.21.9"))
check("26.1 sorts after 1.21.11", cr.version_key("26.1") > cr.version_key("1.21.11"))
check("a release candidate sorts below its release", cr.version_key("1.21-rc1") < cr.version_key("1.21"))
for build, want in (("21.1.77", "1.21.1"), ("20.4.237", "1.20.4"), ("21.0.5-beta", "1.21"),
                    ("26.3.0.76", "26.3"), ("26.1.2.5", "26.1.2"), ("0.25w14craftmine.3-beta", "")):
    check("NeoForge %s is for %r" % (build, want), cr.neoforge_mc(build) == want, cr.neoforge_mc(build))
check("java 1.8.0_402 is major 8", cr.java_major("1.8.0_402") == 8)
check("java 21.0.5 is major 21", cr.java_major("21.0.5") == 21)

# ---------------------------------------------------------------------------
section("which software runs which version")
cat = cr.catalog()
ids = {v["id"]: v for v in cat["versions"]}
check("the catalog leaves snapshots out by default", "25w01a" not in ids, list(ids))
check("old betas are never offered", "b1.7.3" not in ids)
check("1.21.1 is offered by every service that publishes it",
      set(ids["1.21.1"]["software"]) == {"vanilla", "paper", "purpur", "spigot", "fabric", "quilt", "forge", "neoforge"},
      ids["1.21.1"]["software"])
check("1.12.2 is only vanilla, spigot and forge here",
      set(ids["1.12.2"]["software"]) == {"vanilla", "spigot", "forge"}, ids["1.12.2"]["software"])
check("NeoForge is not offered for 1.20.1", "neoforge" not in ids["1.20.1"]["software"])
check("latest is the newest release", cat["latest"] == "1.21.4", cat["latest"])
check("no errors when every service answers", not cat["errors"], cat["errors"])
snap = cr.catalog(include_snapshots=True)
check("snapshots come back when asked for", any(v["id"] == "25w01a" for v in snap["versions"]))

saved = cr.FABRIC_META
cr.FABRIC_META = BASE + "/nowhere"
cr.clear_cache()
down = cr.catalog()
cr.FABRIC_META = saved
cr.clear_cache()
check("one service down is reported, not fatal", "fabric" in down["errors"] and
      "paper" in {s for v in down["versions"] for s in v["software"]}, down["errors"])

section("builds")
b = cr.builds("paper", "1.21.4")
check("Paper builds are newest first", [x["id"] for x in b] == ["232", "231", "230"], b)
check("Paper recommends the newest STABLE build, not a newer beta",
      next(x for x in b if x["recommended"])["id"] == "232")
f = cr.builds("forge", "1.21.1")
check("Forge recommends its promoted build", next(x for x in f if x["recommended"])["id"] == "52.0.9", f)
fb = cr.builds("fabric", "1.21.1")
check("Fabric recommends a stable loader over a newer beta", next(x for x in fb if x["recommended"])["id"] == "0.16.9")
q = cr.builds("quilt", "1.21.1")
check("Quilt loaders are sorted by version, not as the API lists them", q[0]["id"] == "0.26.4", [x["id"] for x in q])
n = cr.builds("neoforge", "1.21.1")
check("NeoForge prefers a non-beta build", next(x for x in n if x["recommended"])["id"] == "21.1.77", n)
try:
    cr.builds("purpur", "1.12.2")
    check("asking for a build that does not exist raises", False)
except cr.CreateError as e:
    check("asking for a build that does not exist raises a sentence", "answered 404" in str(e) or "no build" in str(e), e)

# ---------------------------------------------------------------------------
section("downloads are verified before they count")
d = tempfile.mkdtemp(dir=_TESTHOME)
res = cr.install_server("paper", "1.21.4", "232", d)
check("Paper lands under its published name", os.path.basename(res["jar"]) == "paper-1.21.4-232.jar", res)
check("and is the exact bytes published", open(res["jar"], "rb").read() == PAPER_JAR)
d2 = tempfile.mkdtemp(dir=_TESTHOME)
try:
    cr.install_server("paper", "1.21.1", "100", d2)
    check("a checksum mismatch fails the install", False)
except cr.CreateError as e:
    check("a checksum mismatch fails the install", "checksum" in str(e), e)
check("and leaves no jar or temp file behind", os.listdir(d2) == [], os.listdir(d2))
d3 = tempfile.mkdtemp(dir=_TESTHOME)
r3 = cr.install_server("purpur", "1.21.4", "2416", d3)
check("Purpur is held to its md5", os.path.isfile(r3["jar"]))
d4 = tempfile.mkdtemp(dir=_TESTHOME)
r4 = cr.install_server("fabric", "1.21.1", "0.16.9", d4)
check("Fabric gets its launcher", os.path.basename(r4["jar"]).startswith("fabric-server-mc.1.21.1-loader.0.16.9"), r4)
check("and the vanilla jar the launcher would fetch on first start",
      os.path.isfile(os.path.join(d4, "server.jar")))
check("and remembers the loader version", r4["loader_version"] == "0.16.9")
d5 = tempfile.mkdtemp(dir=_TESTHOME)
r5 = cr.install_server("vanilla", "1.21.4", "", d5)
check("vanilla defaults its build to the version itself", r5["build"] == "1.21.4" and r5["jar"].endswith("server.jar"))

section("first-run files")
props = cr.write_properties(d5, {"server-port": 25570, "motd": "Héllo §aworld", "pvp": False,
                                 "not-a-key": "x", "level-seed": ""})
text = open(props, encoding="utf-8").read()
check("chosen keys are written", "server-port=25570" in text and "pvp=false" in text, text)
check("non-ASCII becomes \\u escapes", "H\\u00e9llo \\u00a7aworld" in text, text)
check("unknown keys are not written", "not-a-key" not in text)
check("blank values are left for the server's default", "level-seed" not in text)
eula = open(cr.write_eula(d5), encoding="utf-8").read()
check("eula.txt says true and why", "eula=true" in eula and "wizard" in eula)

section("Java")
check("Minecraft 1.20.1 needs Java 17 (from Mojang's file)", cr.java_required("1.20.1") == 17)
check("an unknown version falls back to a sensible guess", cr.java_required("26.9") == 25)
javas = [{"path": "/j/25", "major": 25}, {"path": "/j/21", "major": 21}, {"path": "/j/8", "major": 8}]
check("the exact major is preferred", cr.pick_java(javas, 21, "paper", "1.21.1")["path"] == "/j/21")
check("else the oldest new-enough one", cr.pick_java(javas, 17, "paper", "1.20.1")["path"] == "/j/21")
check("Forge and NeoForge get the exact one or nothing (so it is fetched)",
      cr.pick_java(javas, 17, "forge", "1.20.1") is None)
check("too old never fits", not cr.java_fits(17, 21, "paper", "1.21.1")[0])
check("Forge for 1.12.2 refuses Java 17", not cr.java_fits(17, 8, "forge", "1.12.2")[0])


# ---------------------------------------------------------------------------
#  Modrinth, Hangar, Spiget
# ---------------------------------------------------------------------------

LITH = jar(**{"fabric.mod.json": json.dumps({"id": "lithium", "name": "Lithium", "version": "0.14"})})
FAPI = jar(**{"fabric.mod.json": json.dumps({"id": "fabric-api", "name": "Fabric API", "version": "0.116"})})
FAPI2 = jar(**{"fabric.mod.json": json.dumps({"id": "fabric-api", "name": "Fabric API", "version": "0.117"})})
FORGEMOD = jar(**{"META-INF__mods.toml": 'modId="jei"\ndisplayName="JEI"\nversion="1.0"\n'})
PNG1 = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
ICONMOD = jar(**{"fabric.mod.json": json.dumps({"id": "iconic", "name": "Iconic", "version": "1",
                                                 "icon": {"16": "assets/iconic/small.png", "128": "assets/iconic/big.png"}}),
                 "assets__iconic__small.png": b"\x89PNG\r\n\x1a\nsmall",
                 "assets__iconic__big.png": PNG1})
TOMLMOD = jar(**{"META-INF__mods.toml": 'logoFile="logo.png"\n[[mods]]\nmodId="x"\n', "logo.png": PNG1})
VIA = jar(**{"plugin.yml": "name: ViaVersion\nversion: 5.0\n",
             "fabric.mod.json": json.dumps({"id": "viaversion", "name": "ViaVersion", "version": "5.0"})})
VAULT = jar(**{"plugin.yml": "name: Vault\nversion: 1.7.3\n"})


def mr_file(name, data, good=True):
    return {"url": BASE + "/files/" + name, "filename": name, "primary": True, "size": len(data),
            "hashes": {"sha1": sha(data, "sha1"), "sha512": sha(data, "sha512") if good else "0" * 128}}


def mr_version(vid, pid, number, data, name, loaders=("fabric",), games=("1.21.1",), deps=(), env=None, good=True):
    v = {"id": vid, "project_id": pid, "version_number": number, "name": number,
         "version_type": "release", "loaders": list(loaders), "game_versions": list(games),
         "date_published": "2026-01-0%s" % (len(number) % 9), "files": [mr_file(name, data, good)],
         "dependencies": [{"project_id": p, "version_id": None, "dependency_type": t} for p, t in deps]}
    if env:
        v["environment"] = env
    ROUTES["/files/" + name] = B(data)
    ROUTES["/modrinth/v2/version/" + vid] = J(v)
    return v


LITH_V = mr_version("lv1", "lith", "0.14", LITH, "lithium-0.14.jar", deps=[("fapi", "required"), ("opt1", "optional")])
FAPI_V = mr_version("fv1", "fapi", "0.116", FAPI, "fabric-api-0.116.jar")
FAPI_V2 = mr_version("fv2", "fapi", "0.117", FAPI2, "fabric-api-0.117.jar")
SODIUM_V = mr_version("sv1", "sodium", "0.6", jar(), "sodium.jar", env="client_only")
OLD_V = mr_version("ov1", "oldmod", "1.0", jar(), "oldmod.jar", games=("1.20.1",))
BAD_V = mr_version("bv1", "badhash", "1.0", jar(), "badhash.jar", good=False)
JEI_V = mr_version("jv1", "jei", "1.0", FORGEMOD, "jei-forge.jar", loaders=("forge",))

PROJECTS = {"lith": "Lithium", "fapi": "Fabric API", "sodium": "Sodium", "oldmod": "Old Mod",
            "badhash": "Bad Hash", "opt1": "Optional One", "jei": "JEI"}
VERSIONS = {"lith": [LITH_V], "fapi": [FAPI_V], "sodium": [SODIUM_V], "oldmod": [OLD_V],
            "badhash": [BAD_V], "opt1": [], "jei": [JEI_V]}
for pid, title in PROJECTS.items():
    ROUTES["/modrinth/v2/project/" + pid] = J({"id": pid, "slug": pid, "title": title, "project_type": "mod",
                                               "description": title + " does things", "loaders": ["fabric"],
                                               "icon_url": "https://cdn.example/%s.png" % pid,
                                               "categories": ["fabric", "optimization"],
                                               "gallery": [{"url": "https://cdn.example/%s-1.png" % pid, "featured": False,
                                                            "ordering": 0},
                                                           {"url": "https://cdn.example/%s-hero.png" % pid, "featured": True,
                                                            "title": "Hero", "ordering": 5}]})
ROUTES["/modrinth/v2/tag/category"] = J([
    {"name": "optimization", "project_type": "mod", "header": "categories"},
    {"name": "game-mechanics", "project_type": "mod", "header": "categories"},
    {"name": "fabric", "project_type": "mod", "header": "categories"},
    {"name": "32x", "project_type": "resourcepack", "header": "resolutions"}])


def mr_versions(pid):
    def answer(q, body):
        loaders = json.loads(q.get("loaders", ["[]"])[0])
        games = json.loads(q.get("game_versions", ["[]"])[0])
        out = [v for v in VERSIONS[pid] if (not loaders or set(loaders) & set(v["loaders"]))
               and (not games or set(games) & set(v["game_versions"]))]
        return J(out)
    return answer


for pid in PROJECTS:
    ROUTES["/modrinth/v2/project/%s/version" % pid] = mr_versions(pid)


def mr_projects(q, body):
    want = json.loads(q.get("ids", ["[]"])[0])
    return J([{"id": p, "title": PROJECTS[p], "icon_url": "https://cdn.example/%s.png" % p} for p in want if p in PROJECTS])


ROUTES["/modrinth/v2/projects"] = mr_projects


def mr_search(q, body):
    facets = json.loads(q.get("facets", ["[]"])[0])
    HITS.append("facets=" + json.dumps(facets))
    return J({"total_hits": 2, "hits": [
        {"project_id": "lith", "slug": "lith", "title": "Lithium", "author": "jelly", "description": "fast",
         "categories": ["fabric", "optimization"], "versions": ["1.21.1"], "downloads": 1500000},
        {"project_id": "jei", "slug": "jei", "title": "JEI", "author": "mezz", "description": "items",
         "categories": ["forge"], "versions": ["1.21.1"], "downloads": 900}]})


ROUTES["/modrinth/v2/search"] = mr_search
ALL_MR = {sha(LITH, "sha1"): LITH_V, sha(FAPI, "sha1"): FAPI_V, sha(FAPI2, "sha1"): FAPI_V2,
          sha(FORGEMOD, "sha1"): JEI_V}


def mr_version_files(q, body):
    return J({h: ALL_MR[h] for h in (body or {}).get("hashes", []) if h in ALL_MR})


def mr_update(q, body):
    out = {}
    for h in (body or {}).get("hashes", []):
        v = ALL_MR.get(h)
        if v and v["project_id"] == "fapi":
            out[h] = FAPI_V2
        elif v:
            out[h] = v
    return J(out)


ROUTES["/modrinth/v2/version_files"] = mr_version_files
ROUTES["/modrinth/v2/version_files/update"] = mr_update

# Hangar: ViaBackwards needs ViaVersion, named by its numeric id
VIAB = jar(**{"plugin.yml": "name: ViaBackwards\nversion: 5.0\n"})


def hangar_version(slug, name, data, deps=()):
    ROUTES["/files/%s.jar" % slug] = B(data)
    return {"name": name, "createdAt": "2026-01-01", "channel": {"name": "Release"},
            "downloads": {"PAPER": {"fileInfo": {"name": "%s-%s.jar" % (slug, name), "sizeBytes": len(data),
                                                 "sha256Hash": sha(data, "sha256")},
                                    "downloadUrl": BASE + "/files/%s.jar" % slug, "externalUrl": None}},
            "pluginDependencies": {"PAPER": [{"name": n, "projectId": pid, "required": True, "externalUrl": ext}
                                             for n, pid, ext in deps]},
            "platformDependencies": {"PAPER": ["1.21.1", "1.21.4"]}}


ROUTES["/hangar/v1/projects/ViaVersion"] = J({"name": "ViaVersion", "namespace": {"owner": "Via", "slug": "ViaVersion"}})
ROUTES["/hangar/v1/projects/12"] = J({"name": "ViaVersion", "namespace": {"owner": "Via", "slug": "ViaVersion"}})
ROUTES["/hangar/v1/projects/ViaBackwards"] = J({"name": "ViaBackwards", "namespace": {"owner": "Via", "slug": "ViaBackwards"}})
ROUTES["/hangar/v1/projects/ViaVersion/versions"] = J({"result": [hangar_version("ViaVersion", "5.0", VIA)]})
ROUTES["/hangar/v1/projects/ViaBackwards/versions"] = J({"result": [hangar_version(
    "ViaBackwards", "5.0", VIAB, deps=[("ViaVersion", 12, None), ("SomeLib", None, "https://example.invalid/lib")])]})
ROUTES["/hangar/v1/projects"] = J({"pagination": {"count": 1}, "result": [
    {"name": "ViaVersion", "namespace": {"owner": "Via", "slug": "ViaVersion"}, "description": "versions",
     "stats": {"downloads": 5}, "supportedPlatforms": {"PAPER": ["1.21.1"]}}]})

# Spiget: Vault, tested only on old versions; a premium resource
ROUTES["/spiget/v2/resources/34315"] = J({"id": 34315, "name": "Vault", "tag": "economy", "external": False,
                                          "testedVersions": ["1.13", "1.17"], "downloads": 100})
ROUTES["/spiget/v2/resources/34315/versions/latest"] = J({"id": 344916, "name": "1.7.3"})
ROUTES["/spiget/v2/resources/34315/download"] = B(VAULT)
ROUTES["/spiget/v2/resources/999"] = J({"id": 999, "name": "Paid", "premium": True, "price": 5.0,
                                        "testedVersions": ["1.21"]})

# ---------------------------------------------------------------------------
section("what a server can be given")
fab = ct.profile("fabric", "1.21.1")
pap = ct.profile("paper", "1.21.4")
check("Fabric takes mods into mods/", fab["kind"] == "mod" and fab["dir"] == "mods")
check("Fabric is only offered Modrinth", fab["sources"] == ["modrinth"])
check("Paper takes plugins from all three", pap["kind"] == "plugin" and pap["sources"] == ["modrinth", "hangar", "spigot"])
check("Spigot is not offered Hangar (Paper-only plugins)", "hangar" not in ct.profile("spigot", "1.21.1")["sources"])
check("Quilt also loads Fabric mods", ct.profile("quilt", "1.21.1")["loaders"] == ["quilt", "fabric"])
check("a Paper fork is treated as Paper", ct.profile("pufferfish", "1.21.1")["software"] == "paper")
check("Vanilla takes nothing, and says why", not ct.profile("vanilla", "1.21.1")["kind"] and
      "no mod or plugin loader" in ct.profile("vanilla", "")["reason"])
try:
    ct.search("hangar", fab, "via")
    check("a Fabric server cannot be offered Hangar plugins", False)
except ct.ContentError as e:
    check("a Fabric server cannot be offered Hangar plugins", "nothing for Fabric" in str(e), e)

section("search")
r = ct.search("modrinth", fab, "lith")
facets = [h for h in HITS if h.startswith("facets=")][-1]
check("Modrinth is asked for Fabric loaders, mods, server-side and this version",
      "categories:fabric" in facets and "project_type:mod" in facets and "server_side:required" in facets
      and "versions:1.21.1" in facets, facets)
names = {i["name"]: i for i in r["items"]}
check("a Fabric mod is marked as fitting", names["Lithium"]["compatible"] is True)
check("a Forge-only result is marked and says why", names["JEI"]["compatible"] is False and "Fabric" in names["JEI"]["note"])
check("download counts survive", names["Lithium"]["downloads"] == 1500000)
h = ct.search("hangar", pap, "via")
check("Hangar results carry the slug as id", h["items"][0]["id"] == "ViaVersion")
check("a project not marked for this version is flagged", h["items"][0]["compatible"] is False)

section("planning: dependencies are found, not guessed")
d = tempfile.mkdtemp(dir=_TESTHOME)
pl = ct.plan(d, fab, [{"source": "modrinth", "project_id": "lith"}])
inst = [(i["name"], i["reason"]) for i in pl["install"]]
check("asking for Lithium also plans Fabric API", inst == [("Lithium", "requested"), ("Fabric API", "needed by Lithium")], inst)
check("the dependency is marked automatic", pl["install"][1]["auto"] is True)
check("Lithium records what it needs", pl["install"][0]["requires"] == ["fapi"])
check("optional extras are named, not installed", [o["name"] for o in pl["optional"]] == ["Optional One"], pl["optional"])
pl2 = ct.plan(d, fab, [{"source": "modrinth", "project_id": "sodium"}])
check("a client-only mod is refused", not pl2["install"] and "Client-only" in pl2["blocked"][0]["reason"], pl2)
pl3 = ct.plan(d, fab, [{"source": "modrinth", "project_id": "oldmod"}])
check("a mod with no build for this version is refused with the reason",
      not pl3["install"] and "1.20.1" in pl3["blocked"][0]["reason"], pl3)
pl4 = ct.plan(d, fab, [{"source": "modrinth", "project_id": "jei"}])
check("a Forge mod is refused on Fabric", not pl4["install"] and pl4["blocked"], pl4)

section("installing")
rep = ct.apply(d, fab, pl)
check("both files are installed", len(rep["installed"]) == 2 and rep["ok"], rep)
check("into mods/", sorted(os.listdir(os.path.join(d, "mods"))) == ["fabric-api-0.116.jar", "lithium-0.14.jar"])
man = ct.load_manifest(d)
check("the manifest records source, version and dependency", man["items"]["fabric-api-0.116.jar"]["auto"] is True
      and man["items"]["lithium-0.14.jar"]["version_id"] == "lv1")
again = ct.plan(d, fab, [{"source": "modrinth", "project_id": "lith"}])
check("planning again does not re-add the installed dependency", [i["name"] for i in again["install"]] == ["Lithium"])
rep2 = ct.apply(d, fab, again)
check("and the same version is not downloaded twice", rep2["skipped"] and not rep2["installed"], rep2)
badplan = ct.plan(d, fab, [{"source": "modrinth", "project_id": "badhash"}])
rep3 = ct.apply(d, fab, badplan)
check("a checksum mismatch is a failure", rep3["failed"] and not rep3["ok"], rep3)
check("and nothing half-downloaded is left", sorted(os.listdir(os.path.join(d, "mods"))) ==
      ["fabric-api-0.116.jar", "lithium-0.14.jar"], os.listdir(os.path.join(d, "mods")))

section("the installed list")
listing = ct.installed(d, fab)["items"]
byname = {i["name"]: i for i in listing}
check("Fabric API knows Lithium needs it", byname["Fabric API"]["required_by"] == ["Lithium"], byname)
check("nothing is flagged as a problem", not any(i["problems"] for i in listing), listing)
# a jar dropped in by hand is identified by its hash
with open(os.path.join(d, "mods", "random-name.jar"), "wb") as fh:
    fh.write(FORGEMOD)
listing = ct.installed(d, fab)["items"]
byfile = {i["file"]: i for i in listing}
check("a jar added by hand is identified through Modrinth", byfile["random-name.jar"]["name"] == "JEI", byfile)
check("and flagged as a Forge mod on a Fabric server",
      any("Forge mod" in p for p in byfile["random-name.jar"]["problems"]), byfile["random-name.jar"])
os.remove(os.path.join(d, "mods", "random-name.jar"))

section("updates")
os.makedirs(os.path.join(d, "config", "fabric-api"), exist_ok=True)
up = ct.updates(d, fab)
check("a newer Fabric API is found", [(u["name"], u["latest"]) for u in up["items"]] == [("Fabric API", "0.117")], up)
uplan = ct.plan(d, fab, [{"source": "modrinth", "project_id": "fapi", "version_id": "fv2", "update": True}])
urep = ct.apply(d, fab, uplan)
mods = sorted(os.listdir(os.path.join(d, "mods")))
check("the update replaces the old jar", mods == ["fabric-api-0.117.jar", "lithium-0.14.jar"], mods)
check("and keeps its config", os.path.isdir(os.path.join(d, "config", "fabric-api")))
check("it is reported as an update", urep["updated"] and urep["updated"][0]["version"] == "0.117", urep)
check("a dependency stays a dependency after updating", ct.load_manifest(d)["items"]["fabric-api-0.117.jar"]["auto"])

section("disable and remove")
t = ct.toggle(d, fab, "lithium-0.14.jar")
check("disabling renames to .jar.disabled", t["file"] == "lithium-0.14.jar.disabled" and not t["enabled"])
check("a disabled jar is still listed", any(i["file"] == "lithium-0.14.jar.disabled" and not i["enabled"]
                                            for i in ct.installed(d, fab)["items"]))
ct.toggle(d, fab, "lithium-0.14.jar.disabled")
rm = ct.remove(d, fab, "fabric-api-0.117.jar")
check("removing says who needed it", rm["needed_by"] == ["Lithium"], rm)
left = {i["name"]: i for i in ct.installed(d, fab)["items"]}
check("and Lithium now reports the missing dependency",
      any("Fabric API" in p for p in left["Lithium"]["problems"]), left["Lithium"])

section("Hangar and SpigotMC")
dp = tempfile.mkdtemp(dir=_TESTHOME)
hp = ct.plan(dp, pap, [{"source": "hangar", "project_id": "ViaBackwards"}, {"source": "spigot", "project_id": "34315"}])
names = [(i["name"], i["reason"]) for i in hp["install"]]
check("ViaBackwards brings ViaVersion, named by its number on Hangar",
      ("ViaVersion", "needed by ViaBackwards") in names, names)
check("a dependency hosted elsewhere is a warning that names it",
      any("SomeLib" in w and "example.invalid" in w for w in hp["warnings"]), hp["warnings"])
check("Vault's old tested versions warn rather than block",
      ("Vault", "requested") in names and any("only tested on" in w.lower() for w in hp["warnings"]), hp)
hrep = ct.apply(dp, pap, hp)
check("Hangar files are held to their sha256 and installed", hrep["ok"] and len(hrep["installed"]) == 3, hrep)
hl = {i["name"]: i for i in ct.installed(dp, pap)["items"]}
check("ViaBackwards' requirement resolves to the installed ViaVersion", not hl["ViaBackwards"]["problems"], hl["ViaBackwards"])
check("a jar with both plugin.yml and fabric.mod.json counts as a plugin", not hl["ViaVersion"]["problems"], hl["ViaVersion"])
check("Vault gets a soft note, not a problem", hl["Vault"]["notes"] and not hl["Vault"]["problems"], hl["Vault"])
prem = ct.plan(dp, pap, [{"source": "spigot", "project_id": "999"}])
check("a premium SpigotMC resource is refused with the reason",
      not prem["install"] and "Premium" in prem["blocked"][0]["reason"], prem)


section("pictures and sorting")
import art
png = art.png("quilt", 128)
check("pixel art is a real PNG", png[:8] == b"\x89PNG\r\n\x1a\n")
import struct as _st
check("scaled by a whole number, so pixels stay square", _st.unpack(">II", png[16:24]) == (128, 128))
check("every kind of server has art", all(art.png(n) for n in cr.SOFTWARE_IDS))
check("an unknown name has none", art.png("not-a-server") is None)
check("art grids are all 16x16", all(len(a["rows"]) == 16 and all(len(r) == 16 for r in a["rows"]) for a in art.ART.values()))
ij = os.path.join(_TESTHOME, "iconic.jar")
open(ij, "wb").write(ICONMOD)
check("a Fabric mod's icon is found, the biggest size", ct.jar_icon_path(ij) == "assets/iconic/big.png", ct.jar_icon_path(ij))
check("and read as PNG bytes", ct.jar_icon(ij) == PNG1)
tj = os.path.join(_TESTHOME, "toml.jar")
open(tj, "wb").write(TOMLMOD)
check("a Forge mod's logoFile is found", ct.jar_icon_path(tj) == "logo.png")
vj = os.path.join(_TESTHOME, "vault.jar")
open(vj, "wb").write(VAULT)
check("a plugin has no icon, and that is not an error", ct.jar_icon(vj) is None)
cats = ct.categories("modrinth", fab)
check("Modrinth categories are the mod ones, loaders left out",
      [c["id"] for c in cats] == ["game-mechanics", "optimization"], cats)
check("Hangar categories are its fixed set", ct.categories("hangar", pap)[0]["id"] == "admin_tools")
check("SpigotMC has none", ct.categories("spigot", pap) == [])
ct.search("modrinth", fab, "", category="optimization")
facets = [h for h in HITS if h.startswith("facets=")][-1]
check("a category becomes a Modrinth facet", "categories:optimization" in facets, facets)
ct.search("modrinth", fab, "", category="bad\"; drop")
facets = [h for h in HITS if h.startswith("facets=")][-1]
check("a category is cleaned before it is sent", "categories:baddrop" in facets, facets)
pj = ct.project("modrinth", "lith", fab)
check("project screenshots come featured first", [g["title"] for g in pj["gallery"]][:1] == ["Hero"], pj["gallery"])
check("project categories leave the loaders out", pj["categories"] == ["optimization"], pj["categories"])
di = tempfile.mkdtemp(dir=_TESTHOME)
ct.apply(di, fab, ct.plan(di, fab, [{"source": "modrinth", "project_id": "lith"}]))
li = {i["name"]: i for i in ct.installed(di, fab)["items"]}
check("installed items carry their project icon", li["Lithium"]["icon"] == "https://cdn.example/lith.png", li["Lithium"])
check("and when they were installed", li["Lithium"]["installed_at"] > 0)
man = ct.load_manifest(di)
for e in man["items"].values():
    e.pop("icon", None)
ct.save_manifest(di, man)
li = {i["name"]: i for i in ct.installed(di, fab)["items"]}
check("entries saved before icons were kept get one looked up", li["Fabric API"]["icon"] == "https://cdn.example/fapi.png")


# ---------------------------------------------------------------------------
#  the panel: a whole create job
# ---------------------------------------------------------------------------

spec = importlib.util.spec_from_file_location("lodestone", "lodestone.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.PANEL.save = lambda *a, **k: True
# A Java that answers like Java 21, so the job does not go looking for a real one.
FAKEJAVA = os.path.join(_TESTHOME, "java")
io.open(FAKEJAVA, "w").write('#!/bin/sh\necho \'openjdk version "21.0.5" 2024-10-15\' 1>&2\n')
os.chmod(FAKEJAVA, 0o755)


def wait(jid, limit=30):
    t = time.time()
    while time.time() - t < limit:
        j = m.PANEL.job_get(jid)
        if j["state"] != "running":
            return j
        time.sleep(0.1)
    return m.PANEL.job_get(jid)


section("validation")
root = tempfile.mkdtemp(dir=_TESTHOME)
base = {"name": "Test", "software": "fabric", "mc": "1.21.1", "dir": os.path.join(root, "fab"),
        "min_ram": "1G", "max_ram": "2G", "port": 25601, "java": FAKEJAVA}


def refused(change, needle, label):
    body = dict(base, **change)
    try:
        m.PANEL.create_validate(body)
        check(label, False, "accepted")
    except RuntimeError as e:
        check(label, needle in str(e), e)


refused({"name": ""}, "name", "a name is required")
refused({"software": "bukkit-ish"}, "software", "unknown software is refused")
refused({"max_ram": "lots"}, "2G or 2048M", "RAM must look like RAM")
refused({"min_ram": "8G", "max_ram": "2G"}, "more than maximum", "min above max is refused")
refused({"port": 80}, "between 1024", "a privileged port is refused")
refused({"dir": "relative/path"}, "full path", "a relative folder is refused")
full = os.path.join(root, "full")
os.makedirs(full)
io.open(os.path.join(full, "world.dat"), "w").write("x")
refused({"dir": full}, "already has files", "a folder with files in it is refused")
refused({"software": "vanilla", "content": [{"source": "modrinth", "project_id": "lith"}]},
        "no mod or plugin loader", "mods on a vanilla server are refused")
refused({"content": [{"source": "hangar", "project_id": "ViaVersion"}]}, "nothing for Fabric",
        "a Hangar plugin on a Fabric server is refused")
ok = m.PANEL.create_validate(dict(base))
check("a good spec is accepted and the folder normalised", os.path.isabs(ok["dir"]) and ok["port"] == 25601)

section("a create job, end to end")
before = len(m.PANEL.servers())
jid = m.PANEL.create_start(dict(base, eula=True, activate=True, properties={"motd": "hi", "difficulty": "hard"},
                                content=[{"source": "modrinth", "project_id": "lith", "name": "Lithium"}]))
job = wait(jid)
check("the job finishes", job["state"] == "done", (job["error"], job["log"][-5:]))
check("every step is done", all(s["state"] == "done" for s in job["steps"]), job["steps"])
check("progress reaches 100", job["pct"] == 100)
created = m.PANEL.find(job["result"]["server_id"])
check("the server is on the shelf", created and len(m.PANEL.servers()) == before + 1)
lc = created["local"]
check("it remembers what it is", lc["server_type"] == "fabric" and lc["mc_version"] == "1.21.1"
      and lc["loader_version"] == "0.16.9", lc)
check("with its jar, Java and memory", lc["jar"].endswith(".jar") and lc["java"] == FAKEJAVA and lc["max_ram"] == "2G")
check("and its port", created["hosting"]["port"] == 25601)
check("mods and their dependencies are in", sorted(os.listdir(os.path.join(lc["cwd"], "mods"))) ==
      ["fabric-api-0.116.jar", "lithium-0.14.jar"])
check("the EULA was written because it was ticked", m.PANEL._eula_ok(lc["cwd"]))
check("server.properties has the choices", "difficulty=hard" in io.open(os.path.join(lc["cwd"], "server.properties")).read())
check("it became the selected server", m.PANEL.cfg["active"] == created["id"])
st = m.PANEL.status()
check("status names its software and version", st["server"]["software"] == "fabric" and st["server"]["mc"] == "1.21.1")
prof = m.PANEL.content_profile(created["id"])
check("the browser trusts what the wizard installed", prof["software"] == "fabric" and prof["mc"] == "1.21.1"
      and prof["detected"] == "created by Lodestone", prof)
inst = m.PANEL.content_installed(created["id"])
check("and lists the installed mods", {i["name"] for i in inst["items"]} == {"Lithium", "Fabric API"})

section("the shelf knows enough to sort and draw itself")
row = next(x for x in m.PANEL.listing() if x["id"] == created["id"])
check("a new server has a creation time", row["created"] > 0)
check("and its port", row["port"] == 25601)
check("and no icon yet", row["has_icon"] is False)
icon_png = art.png("paper", 64)
open(os.path.join(lc["cwd"], "server-icon.png"), "wb").write(icon_png)
row = next(x for x in m.PANEL.listing() if x["id"] == created["id"])
check("a server-icon.png is noticed", row["has_icon"] is True)
check("and served by id, even when it is not the selected server",
      m.PANEL.icon_bytes_for(created["id"]) == icon_png)
dup = m.PANEL.duplicate(created["id"])
check("a duplicate is newer than its original", dup["created"] >= created["created"])
m.PANEL.cfg["servers"] = [x for x in m.PANEL.servers() if x["id"] != dup["id"]]

section("a create job that fails half way")
bad = dict(base, name="Broken", software="paper", mc="1.21.1", dir=os.path.join(root, "broken"), port=25602)
jid = m.PANEL.create_start(bad)
job = wait(jid)
check("it fails", job["state"] == "failed", job["state"])
check("with a readable reason", "checksum" in job["error"], job["error"])
check("the software step is the one marked failed",
      [s["key"] for s in job["steps"] if s["state"] == "failed"] == ["software"], job["steps"])
check("the folder it made is gone", not os.path.exists(bad["dir"]))
check("nothing was added to the shelf", not any(s.get("name") == "Broken" for s in m.PANEL.servers()))

section("no EULA, no pretending")
active_before = m.PANEL.cfg["active"]
nj = wait(m.PANEL.create_start(dict(base, name="No eula", software="vanilla", mc="1.21.4",
                                    dir=os.path.join(root, "noeula"), port=25603)))
check("the server is still created", nj["state"] == "done", nj["error"])
check("but eula.txt is not written", not os.path.exists(os.path.join(root, "noeula", "eula.txt")))
check("and the job says so", any("EULA" in w for w in nj["warnings"]), nj["warnings"])
check("without activate, the selected server is left alone", m.PANEL.cfg["active"] == active_before)

section("adding a server you already have")
m.PANEL.cfg["auto_via"] = False      # no plugin downloads from a test
have = tempfile.mkdtemp(dir=_TESTHOME)


def folder(name, files):
    d = os.path.join(have, name)
    os.makedirs(d)
    for rel, data in files.items():
        p = os.path.join(d, *rel.split("/"))
        os.makedirs(os.path.dirname(p), exist_ok=True)
        io.open(p, "wb").write(data if isinstance(data, bytes) else data.encode())
    return d


ARGS = "unix_args.txt" if os.name != "nt" else "win_args.txt"
paperdir = folder("Survival", {"paper-1.21.4-232.jar": jar(**{"io__papermc__Main.class": "x"}),
                               "forge-1.20.1-installer.jar": jar(), "server.properties": "server-port=25610\nmotd=hi\n",
                               "eula.txt": "eula=true\n", "world/level.dat": "x"})
forgedir = folder("Modpack", {"libraries/net/minecraftforge/forge/1.20.1-47.3.0/" + ARGS: "-cp x",
                              "server.properties": "server-port=25611\n", "run.sh": "java @user_jvm_args.txt",
                              "user_jvm_args.txt": "# -Xmx4G is the default\n-Xmx6G\n"})
neodir = folder("Neo", {"libraries/net/neoforged/neoforge/21.1.77/" + ARGS: "-cp x", "server.properties": ""})
vandir = folder("Plain", {"server.jar": jar(**{"version.json": json.dumps({"id": "1.21.4"}),
                                               "net__minecraft__server__Main.class": "x"})})
fabdir = folder("Fabby", {"fabric-server-launch.jar": jar(), "server.jar": jar(**{"version.json": json.dumps({"id": "1.21.1"})}),
                          "server.properties": ""})
emptydir = folder("Empty", {})
junkdir = folder("Junk", {"notes.txt": "hello"})

d = cr.detect_folder(paperdir)
check("a Paper folder is recognised", d["ok"] and d["software"] == "paper" and d["mc"] == "1.21.4", d)
check("its jar is the server, not the installer beside it", os.path.basename(d["jar"]) == "paper-1.21.4-232.jar", d["jars"])
check("with its port, world and EULA", d["port"] == 25610 and d["has_world"] and d["eula"], d)
check("and the folder's name as the server's", d["name"] == "Survival")
d = cr.detect_folder(forgedir)
check("modern Forge runs from its args file", d["software"] == "forge" and d["args_file"].endswith(ARGS)
      and not d["jar"] and d["mc"] == "1.20.1" and d["loader_version"] == "1.20.1-47.3.0", d)
check("a memory size uncommented in user_jvm_args.txt is noticed", d.get("max_ram") == "6G", d.get("max_ram"))
d = cr.detect_folder(neodir)
check("NeoForge's version gives its Minecraft version", d["software"] == "neoforge" and d["mc"] == "1.21.1", d)
d = cr.detect_folder(vandir)
check("a plain server.jar is looked inside", d["software"] == "vanilla" and d["mc"] == "1.21.4", d)
d = cr.detect_folder(fabdir)
check("Fabric's launcher is preferred, and the jar beside it gives the version",
      d["software"] == "fabric" and os.path.basename(d["jar"]) == "fabric-server-launch.jar" and d["mc"] == "1.21.1", d)
d = cr.detect_folder(emptydir)
check("an empty folder is not a server, and says so", not d["ok"] and d["empty"] and "empty" in d["error"], d)
d = cr.detect_folder(junkdir)
check("nor is a folder of other things", not d["ok"] and d["empty"], d)
d = cr.detect_folder(os.path.join(have, "nope"))
check("a missing folder is an error", not d["ok"] and "does not exist" in d["error"])

pd = m.PANEL.detect_folder(paperdir)
check("the panel suggests the Java that Minecraft wants", pd["java_need"] == 21, pd)
check("and memory for this PC", pd["max_ram"] in ("2G", "4G", "6G"), pd["max_ram"])
check("a new folder is not in the library yet", "existing" not in pd)
before = len(m.PANEL.servers())
added = m.PANEL.add_local(dict(pd, java=FAKEJAVA))
check("one step puts it in the library", len(m.PANEL.servers()) == before + 1 and added["name"] == "Survival")
lc = added["local"]
check("with its folder, jar and Java", lc["cwd"] == paperdir and lc["jar"].endswith("paper-1.21.4-232.jar")
      and lc["java"] == FAKEJAVA, lc)
row = next(x for x in m.PANEL.listing() if x["id"] == added["id"])
check("and the library shows what it runs", row["software"] == "paper" and row["mc"] == "1.21.4" and row["port"] == 25610, row)
check("a folder already in the library says which entry",
      m.PANEL.detect_folder(paperdir).get("existing", {}).get("id") == added["id"])
try:
    m.PANEL.add_local(dict(pd))
    check("adding the same folder twice is refused", False, "accepted")
except RuntimeError as e:
    check("adding the same folder twice is refused", "already in your library" in str(e), e)
fd = m.PANEL.detect_folder(forgedir)
fadded = m.PANEL.add_local(dict(fd, java=FAKEJAVA))
cmd, _ = m.PANEL._build_cmd(fadded["local"])
check("a Forge server added this way starts from its args file", "@" + fd["args_file"] in cmd and "-jar" not in cmd, cmd)
check("the server writes its console in UTF-8, so chat plugins' symbols are not mangled",
      all(f in cmd for f in ("-Dfile.encoding=UTF-8", "-Dstdout.encoding=UTF-8", "-Dstderr.encoding=UTF-8")), cmd)
own, _ = m.PANEL._build_cmd(dict(fadded["local"], extra="-Dfile.encoding=Cp1252"))
check("but an encoding the user set themselves wins", "-Dfile.encoding=UTF-8" not in own and "-Dfile.encoding=Cp1252" in own
      and "-Dstdout.encoding=UTF-8" in own, own)
check("and reads user_jvm_args.txt before it, as Forge's run script does",
      cmd.index("@user_jvm_args.txt") < cmd.index("@" + fd["args_file"]) and cmd.index("-Xmx" + fadded["local"]["max_ram"])
      < cmd.index("@user_jvm_args.txt"), cmd)
try:
    m.PANEL.add_local({"path": junkdir})
    check("a folder with no server cannot be added", False, "accepted")
except RuntimeError as e:
    check("a folder with no server cannot be added", "jar" in str(e), e)
try:
    m.PANEL.add_local(dict(cr.detect_folder(vandir), max_ram="lots"))
    check("memory has to look like memory", False, "accepted")
except RuntimeError as e:
    check("memory has to look like memory", "4G" in str(e), e)

srv.shutdown()
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
