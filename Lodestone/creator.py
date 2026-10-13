"""Making a new server: which software runs which Minecraft version, and fetching it.

Eight kinds of server, and each publishes its builds a different way:

    vanilla   Mojang's version manifest          one jar, sha1 published
    paper     PaperMC's Fill API (v3)            one jar, sha256 published
    purpur    Purpur's API                       one jar, md5 published
    spigot    nothing - it is compiled locally   BuildTools, several minutes
    fabric    Fabric meta                        a launcher jar that fetches the rest
    quilt     Quilt meta                         an installer that has to be run
    forge     Forge's maven                      an installer that has to be run
    neoforge  NeoForged's maven                  an installer that has to be run

So "which software can run 1.21.4" is eight questions to eight services, and
the answer is the intersection the wizard filters by. Every list is cached for
a few minutes - a wizard that refetches Mojang's 270 KB manifest on every click
would be slow for no benefit.

Two rules shape the install side, the same ones the Via installer follows:

* **Verify before it counts.** Every download goes to a temporary file, is
  hashed against whatever the publisher states (sha1, sha256 or md5), and only
  then is moved into place. A half-written server jar is worse than none.

* **Say what happened.** Every step reports through a log callback, so the
  wizard's progress view and the server's console both show the same record of
  what was fetched, what was run and why something failed.

Java is part of this too. Minecraft 26.x needs Java 25, 1.20.5 needs 21, 1.16
wants 8 - and an installer run on the wrong Java fails with a class-version
error nobody can read. So the right major is looked up from Mojang's own version
file, installed Javas are found and asked their version, and if none fits, an
Eclipse Temurin runtime can be fetched from Adoptium into Lodestone's own folder.
Nothing is installed system-wide and the PATH is never touched.

Standard library only, like the rest of Lodestone.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

UA = "Lodestone/1.0 (Minecraft server panel; server creation)"

# Every endpoint is a module constant so the tests can point them at a fake.
MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
PAPER_API = "https://fill.papermc.io/v3"
PURPUR_API = "https://api.purpurmc.org/v2/purpur"
FABRIC_META = "https://meta.fabricmc.net/v2"
QUILT_META = "https://meta.quiltmc.org/v3"
FORGE_MAVEN = "https://maven.minecraftforge.net/net/minecraftforge/forge"
FORGE_PROMOS = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
NEOFORGE_MAVEN = "https://maven.neoforged.net/releases/net/neoforged/neoforge"
NEOFORGE_VERSIONS = "https://maven.neoforged.net/api/maven/versions/releases/net/neoforged/neoforge"
SPIGOT_VERSIONS = "https://hub.spigotmc.org/versions/"
BUILDTOOLS_URL = ("https://hub.spigotmc.org/jenkins/job/BuildTools/lastSuccessfulBuild/"
                  "artifact/target/BuildTools.jar")
ADOPTIUM_API = "https://api.adoptium.net/v3"

RETRIES = 3
TIMEOUT = 30
CACHE_SECONDS = 600

# What each one is. "content" is what it loads - mods, plugins or nothing -
# which decides which browser the wizard offers. Order is the order shown.
SOFTWARE = (
    {"id": "vanilla", "name": "Vanilla", "content": "",
     "about": "Mojang's own server. No mods, no plugins - exactly the game."},
    {"id": "paper", "name": "Paper", "content": "plugin",
     "about": "The usual plugin server: fast, fixes exploits, runs Bukkit and Paper plugins."},
    {"id": "purpur", "name": "Purpur", "content": "plugin",
     "about": "Paper with hundreds of extra gameplay options. Runs Paper plugins."},
    {"id": "spigot", "name": "Spigot", "content": "plugin",
     "about": "The original Bukkit server. Compiled on this PC by BuildTools, which takes a few minutes."},
    {"id": "fabric", "name": "Fabric", "content": "mod",
     "about": "Lightweight mod loader. Most performance mods live here."},
    {"id": "quilt", "name": "Quilt", "content": "mod",
     "about": "A Fabric fork. Loads Quilt mods and most Fabric mods."},
    {"id": "forge", "name": "Forge", "content": "mod",
     "about": "The classic mod loader for big modpacks."},
    {"id": "neoforge", "name": "NeoForge", "content": "mod",
     "about": "The community fork of Forge most new mods target (1.20.2 and later)."},
)
SOFTWARE_IDS = tuple(s["id"] for s in SOFTWARE)


class CreateError(Exception):
    """Something the user should be told, in a sentence."""


def software_info(sid):
    for s in SOFTWARE:
        if s["id"] == sid:
            return s
    raise CreateError("Unknown server software: %s" % sid)


# ---------------------------------------------------------------------------
#  HTTP, with a small cache
# ---------------------------------------------------------------------------

_cache = {}
_cache_lock = threading.Lock()


def _request(url, accept="application/json"):
    return urllib.request.Request(url, headers={"User-Agent": UA, "Accept": accept})


def _fetch(url, timeout=TIMEOUT):
    last = None
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(_request(url, "*/*"), timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            # A 404 is an answer, not a hiccup: asking again will not change it.
            if e.code in (400, 401, 403, 404, 410):
                raise CreateError("%s answered %d for %s" % (_host(url), e.code, url))
            last = e
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = e
        time.sleep(min(1 + attempt * 2, 5))
    raise CreateError("Could not reach %s: %s" % (_host(url), _reason(last)))


def _host(url):
    return urllib.parse.urlparse(url).netloc or url


def _reason(e):
    if isinstance(e, urllib.error.URLError) and getattr(e, "reason", None):
        return str(e.reason)
    return str(e) if e else "no answer"


def get_json(url, cache=True):
    if cache:
        with _cache_lock:
            hit = _cache.get(url)
            if hit and time.time() - hit[0] < CACHE_SECONDS:
                return hit[1]
    raw = _fetch(url)
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise CreateError("%s sent something that is not JSON." % _host(url))
    if cache:
        with _cache_lock:
            _cache[url] = (time.time(), data)
    return data


def get_text(url, cache=True):
    if cache:
        with _cache_lock:
            hit = _cache.get(url)
            if hit and time.time() - hit[0] < CACHE_SECONDS:
                return hit[1]
    text = _fetch(url).decode("utf-8", "replace")
    if cache:
        with _cache_lock:
            _cache[url] = (time.time(), text)
    return text


def clear_cache():
    with _cache_lock:
        _cache.clear()


def download(url, dest, hashes=None, size=0, progress=None, log=None, label=""):
    """Fetch url to dest, verified. Raises CreateError when it cannot.

    hashes is {"sha1": ..., "sha256": ..., "sha512": ..., "md5": ...} - any
    the publisher states. All stated ones must match. progress(done, total) is
    called as bytes arrive, total 0 when the server does not say.
    """
    log = log or (lambda m: None)
    hashes = {k: (v or "").lower() for k, v in (hashes or {}).items() if v}
    folder = os.path.dirname(os.path.abspath(dest)) or "."
    os.makedirs(folder, exist_ok=True)
    name = label or os.path.basename(dest)
    last = ""
    for attempt in range(1, RETRIES + 1):
        fd, tmp = tempfile.mkstemp(prefix=".lodestone-", suffix=".part", dir=folder)
        os.close(fd)
        try:
            algos = {k: hashlib.new(k) for k in hashes}
            done = 0
            with urllib.request.urlopen(_request(url, "*/*"), timeout=TIMEOUT) as r, \
                    open(tmp, "wb") as out:
                total = int(r.headers.get("Content-Length") or 0) or size
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    out.write(chunk)
                    for h in algos.values():
                        h.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)
            if size and done != size:
                raise CreateError("size mismatch: got %d bytes, expected %d" % (done, size))
            for algo, want in hashes.items():
                got = algos[algo].hexdigest()
                if got != want:
                    raise CreateError("%s checksum mismatch" % algo)
            if os.path.exists(dest):
                os.remove(dest)
            os.replace(tmp, dest)
            return dest
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
            if e.code in (400, 401, 403, 404, 410):
                _rm(tmp)
                raise CreateError("Could not download %s: %s answered %d."
                                  % (name, _host(url), e.code))
        except CreateError as e:
            last = str(e)
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = _reason(e)
        _rm(tmp)
        log("      attempt %d/%d for %s failed: %s" % (attempt, RETRIES, name, last))
        time.sleep(min(2 * attempt, 5))
    raise CreateError("Could not download %s after %d attempts: %s" % (name, RETRIES, last))


def _rm(path):
    try:
        if path and os.path.isfile(path):
            os.remove(path)
    except OSError:
        pass


def file_hash(path, algo="sha1"):
    h = hashlib.new(algo)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(262144), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
#  Minecraft versions
# ---------------------------------------------------------------------------

def version_key(v):
    """Sort key for Minecraft-ish version strings: 1.21.4 < 1.21.10 < 26.1.

    Pre-releases and release candidates sort just below the release they lead
    up to, which is where people expect them.
    """
    v = (v or "").lower()
    m = re.match(r"^(\d+(?:\.\d+)*)(.*)$", v)
    if not m:
        return ((0,), 0, v)
    nums = tuple(int(x) for x in m.group(1).split("."))
    nums = nums + (0,) * (4 - len(nums))
    rest = m.group(2)
    if not rest:
        return (nums, 1, "")
    num = re.findall(r"\d+", rest)
    return (nums, 0, rest, int(num[-1]) if num else 0)


def is_release(v):
    return bool(re.fullmatch(r"\d+(?:\.\d+){1,3}", v or ""))


def mojang_versions():
    """Mojang's list, newest first: [{"id", "type", "url", "sha1"}]."""
    data = get_json(MOJANG_MANIFEST)
    return [v for v in data.get("versions", [])
            if v.get("type") in ("release", "snapshot")]


def mojang_version(mc):
    for v in mojang_versions():
        if v.get("id") == mc:
            return get_json(v["url"])
    raise CreateError("Mojang has no Minecraft version called %s." % mc)


# Used only when Mojang cannot be asked - the published value always wins.
def java_guess(mc):
    k = version_key(mc)[0]
    if k[0] >= 26:
        return 25
    if k[:2] == (1, 20) and k[2] >= 5 or k[:2] >= (1, 21):
        return 21
    if k[:2] >= (1, 18):
        return 17
    if k[:2] == (1, 17):
        return 16
    return 8


def java_required(mc):
    """The Java major Mojang ships with this version, e.g. 21 for 1.21.4."""
    try:
        got = (mojang_version(mc).get("javaVersion") or {}).get("majorVersion")
        if got:
            return int(got)
    except (CreateError, ValueError, TypeError):
        pass
    return java_guess(mc)


# ---------------------------------------------------------------------------
#  what each software publishes
# ---------------------------------------------------------------------------

def _paper_versions():
    data = get_json(PAPER_API + "/projects/paper")
    out = []
    for group in (data.get("versions") or {}).values():
        out.extend(group)
    return out


def _purpur_versions():
    return list(get_json(PURPUR_API).get("versions") or [])


def _spigot_versions():
    html = get_text(SPIGOT_VERSIONS)
    found = re.findall(r'href="([0-9][0-9A-Za-z.\-_]*)\.json"', html)
    # Only plain versions: BuildTools also lists odd internal revisions.
    return [v for v in set(found) if is_release(v)
            and version_key(v)[0] >= (1, 8, 0, 0)]


def _fabric_versions():
    return [g["version"] for g in get_json(FABRIC_META + "/versions/game")]


def _quilt_versions():
    return [g["version"] for g in get_json(QUILT_META + "/versions/game")]


def _forge_all():
    """{mc: [forge build, ...newest first]} from Forge's maven metadata."""
    xml = get_text(FORGE_MAVEN + "/maven-metadata.xml")
    out = {}
    for full in re.findall(r"<version>([^<]+)</version>", xml):
        if "-" not in full:
            continue
        mc, build = full.split("-", 1)
        out.setdefault(mc, []).append(build)
    for mc in out:
        out[mc].sort(key=version_key, reverse=True)
    return out


def neoforge_mc(build):
    """The Minecraft version a NeoForge build is for.

    21.1.77 -> 1.21.1, 20.4.237 -> 1.20.4, 21.0.5-beta -> 1.21. Since Minecraft
    moved to year versions NeoForge follows them with a build number on the end:
    26.3.0.76 -> 26.3, 26.1.2.5 -> 26.1.2.
    """
    core = build.split("-", 1)[0]
    parts = core.split(".")
    if not parts or not parts[0].isdigit():
        return ""
    major = int(parts[0])
    if major >= 26 and len(parts) >= 4:
        mc = "%s.%s" % (parts[0], parts[1])
        return mc if parts[2] == "0" else mc + "." + parts[2]
    if 20 <= major < 26 and len(parts) >= 3:
        return "1.%s" % parts[0] if parts[1] == "0" else "1.%s.%s" % (parts[0], parts[1])
    return ""


def _neoforge_all():
    data = get_json(NEOFORGE_VERSIONS)
    out = {}
    for b in data.get("versions") or []:
        mc = neoforge_mc(b)
        if mc:
            out.setdefault(mc, []).append(b)
    for mc in out:
        out[mc].sort(key=version_key, reverse=True)
    return out


def software_versions(sid):
    """Every Minecraft version this software has a build for."""
    if sid == "vanilla":
        return [v["id"] for v in mojang_versions()]
    if sid == "paper":
        return _paper_versions()
    if sid == "purpur":
        return _purpur_versions()
    if sid == "spigot":
        return _spigot_versions()
    if sid == "fabric":
        return _fabric_versions()
    if sid == "quilt":
        return _quilt_versions()
    if sid == "forge":
        return list(_forge_all())
    if sid == "neoforge":
        return list(_neoforge_all())
    raise CreateError("Unknown server software: %s" % sid)


def catalog(include_snapshots=False):
    """Minecraft versions, newest first, each with the software that runs it.

    Each service is asked in parallel and a service that cannot be reached is
    reported rather than fatal - Fabric being down should not hide Paper.
    """
    mojang = mojang_versions()
    order = {v["id"]: i for i, v in enumerate(mojang)}
    kinds = {v["id"]: v["type"] for v in mojang}

    results, errors = {}, {}

    def ask(sid):
        try:
            results[sid] = set(software_versions(sid))
        except CreateError as e:
            errors[sid] = str(e)
        except Exception as e:                                  # pragma: no cover
            errors[sid] = "%s: %s" % (type(e).__name__, e)

    threads = [threading.Thread(target=ask, args=(s,), daemon=True)
               for s in SOFTWARE_IDS]
    for t in threads:
        t.start()
    for t in threads:
        t.join(TIMEOUT * RETRIES + 10)

    versions = []
    for mc in sorted(order, key=order.get):
        kind = kinds.get(mc, "release")
        if kind != "release" and not include_snapshots:
            continue
        sw = [s for s in SOFTWARE_IDS if mc in results.get(s, ())]
        if not sw:
            continue
        versions.append({"id": mc, "type": kind, "software": sw})
    return {"versions": versions, "software": list(SOFTWARE),
            "errors": errors,
            "latest": next((v["id"] for v in versions if v["type"] == "release"), "")}


def builds(sid, mc):
    """The builds of one software for one Minecraft version, newest first.

    Each is {"id", "label", "stable", "recommended"}; exactly one is marked
    recommended, and that is what the wizard preselects.
    """
    out = []
    if sid in ("vanilla", "spigot"):
        out = [{"id": mc, "label": "Minecraft %s" % mc, "stable": is_release(mc)}]
    elif sid == "paper":
        data = get_json("%s/projects/paper/versions/%s/builds"
                        % (PAPER_API, urllib.parse.quote(mc)))
        items = data if isinstance(data, list) else data.get("builds") or []
        for b in sorted(items, key=lambda x: int(x.get("id") or 0), reverse=True)[:40]:
            ch = (b.get("channel") or "").upper()
            out.append({"id": str(b.get("id")), "stable": ch == "STABLE",
                        "label": "Build %s%s" % (b.get("id"), "" if ch == "STABLE" else " (%s)" % ch.lower())})
    elif sid == "purpur":
        data = get_json("%s/%s" % (PURPUR_API, urllib.parse.quote(mc)))
        allb = list(((data.get("builds") or {}).get("all")) or [])
        for b in sorted(allb, key=lambda x: int(x) if str(x).isdigit() else 0, reverse=True)[:40]:
            out.append({"id": str(b), "label": "Build %s" % b, "stable": True})
    elif sid == "fabric":
        for x in get_json("%s/versions/loader/%s" % (FABRIC_META, urllib.parse.quote(mc)))[:40]:
            lo = x.get("loader") or {}
            out.append({"id": lo.get("version"), "stable": bool(lo.get("stable")),
                        "label": "Loader %s%s" % (lo.get("version"), "" if lo.get("stable") else " (beta)")})
    elif sid == "quilt":
        seen = []
        for x in get_json("%s/versions/loader/%s" % (QUILT_META, urllib.parse.quote(mc))):
            v = (x.get("loader") or {}).get("version")
            if v and v not in seen:
                seen.append(v)
        seen.sort(key=version_key, reverse=True)
        for v in seen[:40]:
            beta = "beta" in v or "pre" in v
            out.append({"id": v, "stable": not beta,
                        "label": "Loader %s" % v})
    elif sid == "forge":
        promos = get_json(FORGE_PROMOS).get("promos") or {}
        rec = promos.get(mc + "-recommended")
        lat = promos.get(mc + "-latest")
        for b in _forge_all().get(mc, [])[:60]:
            tag = " (recommended)" if b == rec else " (latest)" if b == lat else ""
            out.append({"id": b, "label": "Forge %s%s" % (b, tag),
                        "stable": b == rec or (not rec and b == lat),
                        "promo": "recommended" if b == rec else ("latest" if b == lat else "")})
    elif sid == "neoforge":
        for b in _neoforge_all().get(mc, [])[:60]:
            out.append({"id": b, "label": "NeoForge %s" % b,
                        "stable": "beta" not in b and "alpha" not in b})
    else:
        raise CreateError("Unknown server software: %s" % sid)
    if not out:
        raise CreateError("%s has no build for Minecraft %s." % (software_info(sid)["name"], mc))
    pick = next((b for b in out if b.get("promo") == "recommended"), None) or \
        next((b for b in out if b.get("stable")), None) or out[0]
    for b in out:
        b["recommended"] = b is pick
    return out


def default_build(sid, mc):
    return next(b for b in builds(sid, mc) if b["recommended"])["id"]


# ---------------------------------------------------------------------------
#  Java
# ---------------------------------------------------------------------------

_JAVA_VER_RE = re.compile(r'version "([^"]+)"')
_java_seen = {}


def java_major(version):
    """1.8.0_402 -> 8, 17.0.10 -> 17, 25 -> 25."""
    v = (version or "").strip()
    if v.startswith("1."):
        v = v[2:]
    m = re.match(r"(\d+)", v)
    return int(m.group(1)) if m else 0


def probe_java(path):
    """Ask one java binary its version. None if it is not a working Java."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    key = (path, st.st_mtime)
    if key in _java_seen:
        return _java_seen[key]
    info = None
    try:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        r = subprocess.run([path, "-version"], capture_output=True, text=True,
                           timeout=20, creationflags=flags)
        text = (r.stderr or "") + (r.stdout or "")
        m = _JAVA_VER_RE.search(text)
        if m:
            vendor = ""
            for line in text.splitlines():
                if "Runtime Environment" in line:
                    vendor = line.split("Runtime Environment")[0].strip() or line.strip()
                    break
            info = {"path": path, "version": m.group(1),
                    "major": java_major(m.group(1)), "vendor": vendor}
    except (OSError, subprocess.SubprocessError, ValueError):
        info = None
    _java_seen[key] = info
    return info


def _java_exe():
    return "java.exe" if os.name == "nt" else "java"


def java_candidates(managed_root="", extra=()):
    """Every java binary worth asking, in the order a person would pick them."""
    exe = _java_exe()
    seen, out = set(), []

    def add(p):
        if not p:
            return
        p = os.path.abspath(p)
        # Linux distributions link one JDK in under several names.
        k = os.path.normcase(os.path.realpath(p))
        if k not in seen and os.path.isfile(p):
            seen.add(k)
            out.append(p)

    def scan(root, depth=2):
        # root may itself be a Java home (temurin-25/bin/java) or hold several.
        add(os.path.join(root, "bin", exe))
        add(os.path.join(root, "Contents", "Home", "bin", exe))
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            return
        for e in entries:
            d = os.path.join(root, e)
            if not os.path.isdir(d):
                continue
            for cand in (os.path.join(d, "bin", exe),
                         os.path.join(d, "Contents", "Home", "bin", exe)):
                add(cand)
            if depth > 1:
                scan(d, depth - 1)

    if managed_root:
        scan(managed_root)
    for p in extra or ():
        add(p)
    if os.environ.get("JAVA_HOME"):
        add(os.path.join(os.environ["JAVA_HOME"], "bin", exe))
    w = shutil.which("java")
    if w:
        add(os.path.realpath(w))
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            for vendor in ("Java", "Eclipse Adoptium", "Eclipse Foundation", "Microsoft",
                           "Zulu", "Amazon Corretto", "BellSoft", "Semeru", "OpenJDK",
                           "AdoptOpenJDK", "Temurin"):
                scan(os.path.join(base, vendor), 1)
        # The Minecraft launcher bundles runtimes; they work fine for a server.
        for base in (os.path.join(os.environ.get("APPDATA", ""), ".minecraft", "runtime"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Packages",
                                  "Microsoft.4297127D64EC6_8wekyb3d8bbwe", "LocalCache",
                                  "Local", "runtime")):
            scan(base, 3)
    elif sys.platform == "darwin":
        scan("/Library/Java/JavaVirtualMachines", 1)
    else:
        scan("/usr/lib/jvm", 1)
        scan("/opt", 1)
    return out


def find_javas(managed_root="", extra=()):
    """Working Javas, newest first: [{"path", "version", "major", "vendor", "managed"}]."""
    out = []
    for p in java_candidates(managed_root, extra):
        info = probe_java(p)
        if info:
            info = dict(info)
            info["managed"] = bool(managed_root) and os.path.normcase(p).startswith(
                os.path.normcase(os.path.abspath(managed_root)))
            out.append(info)
    out.sort(key=lambda j: (-j["major"], not j["managed"]))
    return out


def java_fits(major, need, software="", mc=""):
    """(ok, note). Too old never runs; much newer usually does, with exceptions."""
    if major < need:
        return False, "Minecraft %s needs Java %d or newer; this is Java %d." % (mc or "this version", need, major)
    k = version_key(mc)[0] if mc else (99,)
    if software == "forge" and k[:2] <= (1, 16) and major > 8:
        return False, "Forge for Minecraft %s only runs on Java 8." % mc
    if k[:2] <= (1, 16) and major > 16:
        return True, "Old versions can misbehave on Java %d; Java %d is the safe choice." % (major, need)
    return True, ""


# Mod loaders patch the game's bytecode, and their tooling lags new Java
# releases: Forge 1.20.1 on Java 21 is a crash report waiting to happen. So for
# these "automatic" means the exact Java the version shipped with, fetched if
# need be, while plugin servers happily run on anything new enough.
EXACT_JAVA = ("forge", "neoforge")


def pick_java(javas, need, software="", mc=""):
    """The best installed Java for this server: the exact major if there is
    one, else the oldest that is new enough (newer is likelier to break mods).
    None when nothing fits - or, for Forge and NeoForge, nothing fits exactly."""
    fitting = [j for j in javas if java_fits(j["major"], need, software, mc)[0]]
    exact = [j for j in fitting if j["major"] == need]
    if exact:
        return exact[0]
    if software in EXACT_JAVA:
        return None
    fitting.sort(key=lambda j: j["major"])
    return fitting[0] if fitting else None


def _adoptium_os():
    if os.name == "nt":
        return "windows"
    if sys.platform == "darwin":
        return "mac"
    return "linux"


def _adoptium_arch():
    import platform
    m = (platform.machine() or "").lower()
    if m in ("arm64", "aarch64"):
        return "aarch64"
    if m in ("x86", "i386", "i686"):
        return "x32"
    return "x64"


def java_download_info(major, images=("jre", "jdk")):
    """Where Adoptium keeps a Temurin runtime for this machine, or raise.
    A server only needs the runtime (jre); building code needs ("jdk",)."""
    for image in images:
        url = ("%s/assets/latest/%d/hotspot?architecture=%s&image_type=%s&os=%s&vendor=eclipse"
               % (ADOPTIUM_API, major, _adoptium_arch(), image, _adoptium_os()))
        try:
            data = get_json(url, cache=False)
        except CreateError:
            continue
        for a in data or []:
            pkg = (a.get("binary") or {}).get("package") or {}
            if pkg.get("link"):
                return {"url": pkg["link"], "name": pkg.get("name") or "java.zip",
                        "sha256": pkg.get("checksum") or "", "size": pkg.get("size") or 0,
                        "release": (a.get("release_name") or a.get("version", {}).get("semver") or ""),
                        "image": image}
    raise CreateError("Adoptium has no Java %d build for %s/%s."
                      % (major, _adoptium_os(), _adoptium_arch()))


def install_java(major, managed_root, log=None, progress=None, jdk=False):
    """Download and unpack a Temurin runtime into managed_root/temurin-<major>
    (or a full JDK, for compiling, into temurin-<major>-jdk).

    Returns the path of its java binary. A runtime already there is reused.
    """
    log = log or (lambda m: None)
    target = os.path.join(managed_root, "temurin-%d%s" % (major, "-jdk" if jdk else ""))
    have = [j for j in find_javas(target) if j["major"] == major]
    if have:
        log(">>> Java %d is already installed at %s" % (major, have[0]["path"]))
        return have[0]["path"]
    info = java_download_info(major, ("jdk",) if jdk else ("jre", "jdk"))
    log(">>> downloading Java %d (%s, %s)" % (major, info["release"] or "Temurin", info["image"]))
    os.makedirs(managed_root, exist_ok=True)
    work = tempfile.mkdtemp(prefix=".java-", dir=managed_root)
    try:
        archive = os.path.join(work, info["name"])
        download(info["url"], archive, {"sha256": info["sha256"]}, info["size"],
                 progress=progress, log=log, label="Java %d" % major)
        log("      verified, unpacking")
        unpacked = os.path.join(work, "x")
        os.makedirs(unpacked)
        if archive.endswith(".zip"):
            with zipfile.ZipFile(archive) as z:
                _safe_extract_zip(z, unpacked)
        else:
            with tarfile.open(archive) as t:
                _safe_extract_tar(t, unpacked)
        # The archive holds one top folder (jdk-25.0.4.1+1-jre); that folder
        # becomes temurin-25 so the path does not change with every update.
        tops = [os.path.join(unpacked, d) for d in os.listdir(unpacked)]
        home = tops[0] if len(tops) == 1 and os.path.isdir(tops[0]) else unpacked
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
        shutil.move(home, target)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    found = [j for j in find_javas(target) if j["major"] == major]
    if not found:
        raise CreateError("Java %d was unpacked but does not run here." % major)
    if os.name != "nt":
        for j in found:
            try:
                os.chmod(j["path"], 0o755)
            except OSError:
                pass
    log("      Java %d ready: %s" % (major, found[0]["path"]))
    return found[0]["path"]


def _inside(base, path):
    base = os.path.abspath(base)
    path = os.path.abspath(path)
    return path == base or path.startswith(base + os.sep)


def _safe_extract_zip(z, dest):
    for m in z.infolist():
        if not _inside(dest, os.path.join(dest, m.filename)):
            raise CreateError("The Java archive contains an unsafe path: %s" % m.filename)
    z.extractall(dest)
    # zipfile does not restore permissions; java needs its execute bit on Unix.
    if os.name != "nt":
        for m in z.infolist():
            mode = (m.external_attr >> 16) & 0o777
            if mode:
                try:
                    os.chmod(os.path.join(dest, m.filename), mode)
                except OSError:
                    pass


def _safe_extract_tar(t, dest):
    for m in t.getmembers():
        if not _inside(dest, os.path.join(dest, m.name)):
            raise CreateError("The Java archive contains an unsafe path: %s" % m.name)
        if m.issym() or m.islnk():
            link = os.path.join(dest, os.path.dirname(m.name), m.linkname)
            if not _inside(dest, link):
                raise CreateError("The Java archive contains an unsafe link: %s" % m.name)
    t.extractall(dest)


# ---------------------------------------------------------------------------
#  installing the server software
# ---------------------------------------------------------------------------

def _run_installer(argv, cwd, log, what, timeout=1800):
    """Run an installer jar, streaming its output into the log."""
    log("      running %s" % what)
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                bufsize=1, creationflags=flags)
    except OSError as e:
        raise CreateError("Could not run %s: %s" % (what, e))
    tail = []
    started = time.time()
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            tail.append(line)
            del tail[:-30]
            log("      | " + line[:300])
            if time.time() - started > timeout:
                proc.kill()
                raise CreateError("%s took longer than %d minutes and was stopped."
                                  % (what, timeout // 60))
    finally:
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    if proc.returncode != 0:
        hint = ""
        joined = "\n".join(tail)
        if "UnsupportedClassVersionError" in joined or "class file version" in joined:
            hint = " The Java it ran on is too old for this installer."
        raise CreateError("%s failed (exit code %s).%s Last output: %s"
                          % (what, proc.returncode, hint, " / ".join(tail[-3:]) or "none"))
    return tail


def _check_jar(path, what):
    try:
        with zipfile.ZipFile(path) as z:
            if "META-INF/MANIFEST.MF" not in z.namelist():
                raise CreateError("%s is not a runnable jar." % what)
    except zipfile.BadZipFile:
        raise CreateError("%s downloaded but is not a valid jar." % what)


def install_server(sid, mc, build, dest, java="java", log=None, progress=None,
                   work_root=""):
    """Put the server software for (sid, mc, build) into dest.

    Returns {"jar", "args_file", "server_type", "build", "loader_version"}:
    what Lodestone needs to launch it. jar or args_file is set, never neither.
    """
    log = log or (lambda m: None)
    info = software_info(sid)
    os.makedirs(dest, exist_ok=True)
    build = build or default_build(sid, mc)
    out = {"jar": "", "args_file": "", "server_type": sid, "build": build,
           "loader_version": ""}
    log(">>> installing %s for Minecraft %s (%s)" % (info["name"], mc, build))

    if sid == "vanilla":
        out["jar"] = _vanilla_jar(mc, dest, log, progress)

    elif sid == "paper":
        b = get_json("%s/projects/paper/versions/%s/builds/%s"
                     % (PAPER_API, urllib.parse.quote(mc), urllib.parse.quote(str(build))))
        d = (b.get("downloads") or {}).get("server:default") or {}
        if not d.get("url"):
            raise CreateError("Paper build %s has no server download." % build)
        jar = os.path.join(dest, d.get("name") or "paper-%s-%s.jar" % (mc, build))
        log("      downloading %s" % os.path.basename(jar))
        download(d["url"], jar, {"sha256": (d.get("checksums") or {}).get("sha256")},
                 d.get("size") or 0, progress, log)
        out["jar"] = jar

    elif sid == "purpur":
        meta = get_json("%s/%s/%s" % (PURPUR_API, urllib.parse.quote(mc), urllib.parse.quote(str(build))))
        jar = os.path.join(dest, "purpur-%s-%s.jar" % (mc, build))
        log("      downloading %s" % os.path.basename(jar))
        download("%s/%s/%s/download" % (PURPUR_API, urllib.parse.quote(mc), urllib.parse.quote(str(build))),
                 jar, {"md5": meta.get("md5")}, 0, progress, log)
        out["jar"] = jar

    elif sid == "fabric":
        installers = get_json(FABRIC_META + "/versions/installer")
        inst = next((i for i in installers if i.get("stable")), None) or (installers or [None])[0]
        if not inst:
            raise CreateError("Fabric publishes no installer right now.")
        name = "fabric-server-mc.%s-loader.%s-launcher.%s.jar" % (mc, build, inst["version"])
        jar = os.path.join(dest, name)
        log("      downloading the Fabric server launcher (loader %s)" % build)
        download("%s/versions/loader/%s/%s/%s/server/jar"
                 % (FABRIC_META, urllib.parse.quote(mc), urllib.parse.quote(build),
                    urllib.parse.quote(inst["version"])), jar, None, 0, progress, log)
        _check_jar(jar, "The Fabric launcher")
        # The launcher fetches the vanilla jar on first start; fetching it now,
        # verified, makes that first start quicker and means it cannot fail there.
        _vanilla_jar(mc, dest, log, progress)
        out["jar"] = jar
        out["loader_version"] = build

    elif sid == "quilt":
        installers = get_json(QUILT_META + "/versions/installer")
        if not installers:
            raise CreateError("Quilt publishes no installer right now.")
        inst = installers[0]
        # Quilt meta's own "hashes" for the installer do not match the file it
        # links to (checked 2026-10); the .sha1 the maven publishes beside the
        # jar does, so that is what the download is held to.
        sha1 = ""
        try:
            sha1 = get_text(inst["url"] + ".sha1", cache=False).strip().split()[0]
        except (CreateError, IndexError):
            pass
        work = tempfile.mkdtemp(prefix=".quilt-", dir=dest)
        try:
            ij = os.path.join(work, "quilt-installer.jar")
            log("      downloading the Quilt installer %s" % inst["version"])
            download(inst["url"], ij,
                     {"sha1": sha1} if re.fullmatch(r"[0-9a-f]{40}", sha1 or "") else None,
                     inst.get("file_size") or 0, progress, log)
            _run_installer([java, "-jar", ij, "install", "server", mc, build,
                            "--download-server", "--install-dir=" + dest],
                           work, log, "the Quilt installer")
        finally:
            shutil.rmtree(work, ignore_errors=True)
        jar = os.path.join(dest, "quilt-server-launch.jar")
        if not os.path.isfile(jar):
            raise CreateError("The Quilt installer finished but left no quilt-server-launch.jar.")
        out["jar"] = jar
        out["loader_version"] = build

    elif sid in ("forge", "neoforge"):
        out.update(_install_forgelike(sid, mc, build, dest, java, log, progress))

    elif sid == "spigot":
        out["jar"] = _install_spigot(mc, dest, java, log, progress, work_root)

    if out["jar"]:
        _check_jar(out["jar"], os.path.basename(out["jar"]))
    log("      %s %s installed" % (info["name"], build))
    return out


def _vanilla_jar(mc, dest, log, progress):
    v = mojang_version(mc)
    srv = (v.get("downloads") or {}).get("server")
    if not srv:
        raise CreateError("Mojang publishes no server jar for %s." % mc)
    jar = os.path.join(dest, "server.jar")
    if os.path.isfile(jar) and file_hash(jar, "sha1") == srv.get("sha1"):
        log("      server.jar for %s already here and verified" % mc)
        return jar
    log("      downloading the Minecraft %s server jar" % mc)
    download(srv["url"], jar, {"sha1": srv.get("sha1")}, srv.get("size") or 0, progress, log,
             label="server.jar")
    return jar


def _install_forgelike(sid, mc, build, dest, java, log, progress):
    if sid == "forge":
        full = "%s-%s" % (mc, build)
        url = "%s/%s/forge-%s-installer.jar" % (FORGE_MAVEN, full, full)
        lib = os.path.join("libraries", "net", "minecraftforge", "forge", full)
        name = "Forge"
    else:
        full = build
        url = "%s/%s/neoforge-%s-installer.jar" % (NEOFORGE_MAVEN, build, build)
        lib = os.path.join("libraries", "net", "neoforged", "neoforge", build)
        name = "NeoForge"
    sha1 = ""
    try:
        sha1 = get_text(url + ".sha1", cache=False).strip().split()[0]
    except (CreateError, IndexError):
        pass
    work = tempfile.mkdtemp(prefix=".%s-" % sid, dir=dest)
    try:
        ij = os.path.join(work, "installer.jar")
        log("      downloading the %s %s installer" % (name, build))
        download(url, ij, {"sha1": sha1} if re.fullmatch(r"[0-9a-f]{40}", sha1 or "") else None,
                 0, progress, log, label="%s installer" % name)
        _run_installer([java, "-jar", ij, "--installServer", dest], dest, log,
                       "the %s installer" % name)
    finally:
        shutil.rmtree(work, ignore_errors=True)
        for leftover in ("installer.jar.log", "installer.log"):
            p = os.path.join(dest, leftover)
            if os.path.isfile(p) and os.path.getsize(p) == 0:
                _rm(p)
    out = {"loader_version": build}
    # 1.17 and later: no server jar, a JVM args file per platform instead.
    args = os.path.join(lib, "win_args.txt" if os.name == "nt" else "unix_args.txt")
    if os.path.isfile(os.path.join(dest, args)):
        out["args_file"] = args
        out["jar"] = ""
        return out
    # Older Forge: a forge-<mc>-<build>(-universal).jar in the folder itself.
    for cand in sorted(os.listdir(dest)):
        low = cand.lower()
        if low.endswith(".jar") and low.startswith(sid + "-") and "installer" not in low:
            out["jar"] = os.path.join(dest, cand)
            return out
    raise CreateError("The %s installer finished but left neither a server jar nor an args file." % name)


def _install_spigot(mc, dest, java, log, progress, work_root):
    work_root = work_root or tempfile.gettempdir()
    bt_dir = os.path.join(work_root, "buildtools")
    os.makedirs(bt_dir, exist_ok=True)
    bt = os.path.join(bt_dir, "BuildTools.jar")
    log("      downloading BuildTools")
    download(BUILDTOOLS_URL, bt, None, 0, progress, log, label="BuildTools.jar")
    _check_jar(bt, "BuildTools.jar")
    log("      compiling Spigot %s - this takes several minutes the first time" % mc)
    _run_installer([java, "-jar", bt, "--rev", mc, "--compile", "spigot",
                    "--output-dir", dest], bt_dir, log, "BuildTools", timeout=3600)
    for cand in os.listdir(dest):
        if cand.lower().startswith("spigot-") and cand.lower().endswith(".jar"):
            return os.path.join(dest, cand)
    raise CreateError("BuildTools finished but no spigot jar appeared in the folder.")


# ---------------------------------------------------------------------------
#  first-run files
# ---------------------------------------------------------------------------

# What the wizard may set. Anything else in server.properties is left for the
# server to write with its own defaults on first start.
PROPERTY_KEYS = ("server-port", "motd", "max-players", "difficulty", "gamemode",
                 "hardcore", "pvp", "online-mode", "white-list", "level-seed",
                 "level-type", "view-distance", "simulation-distance",
                 "spawn-protection", "allow-flight", "enable-command-block",
                 "server-ip")


def _prop_escape(value):
    out = []
    for ch in str(value):
        if ch == "\\":
            out.append("\\\\")
        elif ch in "\n\r":
            continue
        elif ord(ch) > 126:
            out.append("\\u%04x" % ord(ch))
        else:
            out.append(ch)
    return "".join(out)


def write_properties(dest, values):
    """A starting server.properties with only the keys the user chose.

    Non-ASCII is written as \\uXXXX, which every Minecraft version reads back
    correctly whether it parses the file as Latin-1 or UTF-8.
    """
    lines = ["#Minecraft server properties",
             "#Written by Lodestone on %s" % time.strftime("%Y-%m-%d %H:%M")]
    for key in PROPERTY_KEYS:
        if key in values and values[key] is not None and str(values[key]) != "":
            v = values[key]
            if isinstance(v, bool):
                v = "true" if v else "false"
            lines.append("%s=%s" % (key, _prop_escape(v)))
    path = os.path.join(dest, "server.properties")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return path


def write_eula(dest):
    path = os.path.join(dest, "eula.txt")
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("# Accepted by the user in Lodestone's Create server wizard on %s\n"
                 "# https://aka.ms/MinecraftEULA\neula=true\n"
                 % time.strftime("%Y-%m-%d %H:%M"))
    return path


# ---------------------------------------------------------------------------
#  a server that already exists
# ---------------------------------------------------------------------------
#
# "Add a server I already have" used to mean an empty editor and two Browse
# buttons, and knowing which of six jars in the folder is the one that runs.
# This looks at the folder the way a person would and fills all of it in.

# Launcher jars by name, best first. Each is (pattern, software).
_LAUNCHERS = (
    (r"fabric-server-launch\.jar$", "fabric"),
    (r"fabric-server-mc\..*-launcher\..*\.jar$", "fabric"),
    (r"quilt-server-launch\.jar$", "quilt"),
    (r"purpur.*\.jar$", "purpur"),
    (r"paper.*\.jar$", "paper"),
    (r"(?:folia|pufferfish|leaf).*\.jar$", "paper"),
    (r"spigot.*\.jar$", "spigot"),
    (r"craftbukkit.*\.jar$", "spigot"),
    (r"neoforge.*\.jar$", "neoforge"),
    (r"forge.*\.jar$", "forge"),
    (r"velocity.*\.jar$", "velocity"),
    (r"waterfall.*\.jar$", "waterfall"),
    (r"bungeecord.*\.jar$", "bungeecord"),
    (r"minecraft_server.*\.jar$", "vanilla"),
    (r"server\.jar$", ""),
)
_NOT_A_SERVER = re.compile(r"installer|-sources|-javadoc|-api\b|buildtools|-shim\.jar$", re.I)
_FORGE_LIBS = (("forge", ("libraries", "net", "minecraftforge", "forge")),
               ("neoforge", ("libraries", "net", "neoforged", "neoforge")),
               ("neoforge", ("libraries", "net", "neoforged", "forge")))


def read_properties(folder):
    """server.properties as a dict; {} when there is none."""
    out = {}
    try:
        with open(os.path.join(folder, "server.properties"), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line[0] in "#!" or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def _jar_version(path):
    """The Minecraft version a vanilla (or bundler) jar says it is."""
    try:
        with zipfile.ZipFile(path) as z:
            if "version.json" in z.namelist():
                data = json.loads(z.read("version.json").decode("utf-8", "replace"))
                return str(data.get("id") or data.get("name") or "")
    except (OSError, zipfile.BadZipFile, ValueError, KeyError):
        pass
    return ""


def _args_file(folder):
    """Forge and NeoForge 1.17+: the newest win/unix args file, relative to the folder."""
    want = "win_args.txt" if os.name == "nt" else "unix_args.txt"
    for software, parts in _FORGE_LIBS:
        base = os.path.join(folder, *parts)
        try:
            builds = sorted(os.listdir(base), key=version_key, reverse=True)
        except OSError:
            continue
        for b in builds:
            rel = os.path.join(*(parts + (b, want)))
            if os.path.isfile(os.path.join(folder, rel)):
                return software, b, rel
    return "", "", ""


def _user_jvm_xmx(folder):
    """An uncommented -Xmx in Forge's user_jvm_args.txt, as "6G"; "" when none."""
    try:
        with open(os.path.join(folder, "user_jvm_args.txt"), encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.split("#", 1)[0]
                m = re.search(r"-Xmx(\d+[MmGg])\b", line)
                if m:
                    return m.group(1).upper()
    except OSError:
        pass
    return ""


def _forge_mc(software, build):
    if software == "neoforge" and not build.startswith("1."):
        return neoforge_mc(build)
    # Forge (and NeoForge's 1.20.1 fork) put the Minecraft version first: 1.20.1-47.3.0
    return build.split("-", 1)[0] if "-" in build else ""


def detect_folder(folder):
    """What is in this folder, as the fields a local server needs.

    {"ok", "path", "name", "jar", "jars", "args_file", "software", "mc",
     "loader_version", "port", "has_world", "has_properties", "eula", "notes",
     "empty", "blank", "error"}. "software" is "" when nothing gave it away;
    "empty" means there is no server here at all (offer to create one instead),
    and "blank" that the folder has nothing in it, so a new server can go there.
    """
    out = {"ok": False, "path": "", "name": "", "jar": "", "jars": [], "args_file": "",
           "software": "", "mc": "", "loader_version": "", "port": 25565, "has_world": False,
           "has_properties": False, "eula": False, "notes": [], "empty": False, "blank": False,
           "error": ""}
    folder = os.path.abspath(os.path.expanduser((folder or "").strip().strip('"')))
    out["path"] = folder
    if not folder or not os.path.isdir(folder):
        out["error"] = "That folder does not exist."
        return out
    out["name"] = os.path.basename(folder.rstrip("\\/")) or "My server"
    try:
        names = os.listdir(folder)
    except OSError as e:
        out["error"] = "Lodestone cannot read that folder: %s" % e
        return out
    out["blank"] = not names

    props = read_properties(folder)
    out["has_properties"] = bool(props) or "server.properties" in names
    try:
        out["port"] = int(props.get("server-port") or 25565)
    except ValueError:
        pass
    level = props.get("level-name") or "world"
    out["has_world"] = os.path.isdir(os.path.join(folder, level))
    try:
        with open(os.path.join(folder, "eula.txt"), encoding="utf-8", errors="replace") as fh:
            out["eula"] = bool(re.search(r"^\s*eula\s*=\s*true\s*$", fh.read(), re.I | re.M))
    except OSError:
        pass

    jars = []
    for n in names:
        p = os.path.join(folder, n)
        if n.lower().endswith(".jar") and os.path.isfile(p) and not _NOT_A_SERVER.search(n):
            try:
                size = os.path.getsize(p)
            except OSError:
                size = 0
            jars.append((n, size))

    def rank(item):
        n = item[0].lower()
        for i, (pat, _sw) in enumerate(_LAUNCHERS):
            if re.search(pat, n):
                return (i, -item[1])
        return (len(_LAUNCHERS), -item[1])
    jars.sort(key=rank)
    out["jars"] = [n for n, _ in jars]

    sw, build, args = _args_file(folder)
    if args:
        out.update(software=sw, args_file=args, loader_version=build, mc=_forge_mc(sw, build))
        out["notes"].append("Runs %s %s from its args file - there is no server jar to pick."
                            % ("NeoForge" if sw == "neoforge" else "Forge", build))
        # Forge's user_jvm_args.txt is read on every start; a memory size set
        # there (uncommented) is what the server's owner chose.
        xmx = _user_jvm_xmx(folder)
        if xmx:
            out["max_ram"] = xmx
            out["notes"].append("user_jvm_args.txt asks for -Xmx%s, which wins over the memory set here." % xmx)
    elif jars:
        best = jars[0][0]
        out["jar"] = os.path.join(folder, best)
        for pat, s in _LAUNCHERS:
            if re.search(pat, best.lower()):
                out["software"] = s
                break
        if len(jars) > 1:
            out["notes"].append("Found %d jars; picked %s." % (len(jars), best))

    if not out["software"]:
        # Folder evidence, the same markers the Via installer trusts.
        for rel, s in (("purpur.yml", "purpur"), (os.path.join("config", "paper-global.yml"), "paper"),
                       ("paper.yml", "paper"), ("spigot.yml", "spigot"), ("bukkit.yml", "spigot"),
                       ("velocity.toml", "velocity"), (".fabric", "fabric"), (".quilt", "quilt"),
                       ("fabric-server-launcher.properties", "fabric")):
            if os.path.exists(os.path.join(folder, rel)):
                out["software"] = s
                break
    if out["jar"] and not out["software"]:
        try:
            with zipfile.ZipFile(out["jar"]) as z:
                joined = "\n".join(z.namelist()[:4000])
            if "org/bukkit/" in joined or "io/papermc/" in joined:
                out["software"] = "paper"
            elif "net/fabricmc/" in joined:
                out["software"] = "fabric"
            elif "org/quiltmc/" in joined:
                out["software"] = "quilt"
            elif "net/minecraft/" in joined or "META-INF/versions.list" in joined:
                out["software"] = "vanilla"
        except (OSError, zipfile.BadZipFile):
            pass

    if not out["mc"] and out["jar"]:
        out["mc"] = _jar_version(out["jar"])
    if not out["mc"]:
        try:
            with open(os.path.join(folder, "version_history.json"), encoding="utf-8") as fh:
                cur = (json.load(fh) or {}).get("currentVersion") or ""
            m = re.search(r"MC:\s*([0-9][0-9.]*)", cur)
            out["mc"] = m.group(1) if m else ""
        except (OSError, ValueError, AttributeError):
            pass
    if not out["mc"]:
        # Fabric and Quilt launch beside the vanilla jar they wrap, and 1.18+
        # unpacks the real server into versions/<mc>/.
        for n, _size in jars:
            if out["mc"]:
                break
            out["mc"] = _jar_version(os.path.join(folder, n))
        try:
            vers = [d for d in os.listdir(os.path.join(folder, "versions"))
                    if re.fullmatch(r"\d+\.\d+(?:\.\d+)?", d)]
        except OSError:
            vers = []
        if not out["mc"] and vers:
            out["mc"] = sorted(vers, key=version_key)[-1]
    if not out["mc"] and out["jar"]:
        m = re.search(r"(?<!\d)(?<!\d\.)(1\.\d{1,2}(?:\.\d{1,2})?|2\d\.\d(?:\.\d{1,2})?)(?!\d)(?!\.\d)",
                      os.path.basename(out["jar"]))
        out["mc"] = m.group(1) if m else ""

    if not out["jar"] and not out["args_file"]:
        if not out["has_properties"] and not names:
            out["empty"] = True
            out["error"] = "This folder is empty - there is no server in it yet."
        elif not out["has_properties"]:
            out["empty"] = True
            out["error"] = "No Minecraft server here: no server jar and no server.properties."
        else:
            out["error"] = ("There is a server.properties but no server jar. Put the jar in the folder, "
                            "or pick it after adding.")
            out["ok"] = True
        return out
    out["ok"] = True
    return out
