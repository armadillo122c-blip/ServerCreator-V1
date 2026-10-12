"""ViaVersion and friends, installed only where they actually work.

The Via plugins let clients on other Minecraft versions connect. Which build you
need depends entirely on what the server *is*, and getting that wrong is not a
harmless mistake: a Bukkit jar dropped into a Velocity proxy fails to load, and
either one dropped into Fabric or Vanilla does nothing at all while looking like
it worked. So this module is built around four platforms rather than a yes/no:

    bukkit   Paper, Purpur, Spigot, CraftBukkit and forks  -> plugins/
    proxy    Velocity, Waterfall, BungeeCord               -> plugins/
    fabric   Fabric, Quilt                                 -> mods/
    modded   Forge, NeoForge                               -> nothing, and say why
    vanilla  Vanilla and snapshots                         -> nothing, and say why

Fabric is the awkward one: it takes mods happily, but Via is not among them, so
"can anything be installed here" and "does Via belong here" are two questions
with two answers. `compatible` answers the second, `preset_ok` the first.

Two things shape the rest:

* **Idempotence.** Running this twice must not leave two copies of ViaVersion in
  `plugins/`, and must not re-download a file already on disk. The check is the
  file's own SHA-1 against the hash the index publishes, not a version string in
  a sidecar file, because the sidecar can be deleted or edited and the jar is
  the truth.

* **Verify before installing.** A half-downloaded jar stops a server booting,
  which is worse than not installing at all. Everything is fetched to a
  temporary file, hashed, and only then moved into place.

Replacing a plugin only ever deletes its *jar*. `plugins/ViaVersion/` and the
config inside it are left exactly where they are, so an update never costs
someone their settings.

Downloads come from Modrinth's public API, which needs no account and publishes
a SHA-1 for every file. Standard library only, like the rest of Lodestone.
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

API = "https://api.modrinth.com/v2"
UA = "Lodestone/1.0 (Minecraft server panel; +https://github.com)"

# Slug on Modrinth, and the name people know it by.
VIA_PLUGINS = (("viaversion", "ViaVersion"),
               ("viabackwards", "ViaBackwards"),
               ("viarewind", "ViaRewind"))

PLATFORM_BUKKIT = "bukkit"
PLATFORM_PROXY = "proxy"
PLATFORM_FABRIC = "fabric"
PLATFORM_MODDED = "modded"
PLATFORM_VANILLA = "vanilla"

# Which Modrinth loaders count as "a build that will load here".
PLATFORM_LOADERS = {
    PLATFORM_BUKKIT: ("paper", "purpur", "spigot", "bukkit", "folia"),
    PLATFORM_PROXY: ("velocity", "bungeecord", "waterfall"),
    PLATFORM_FABRIC: ("fabric", "quilt"),
}

# Plugins go in plugins/. Mods go in mods/. Putting either in the other folder
# is a silent no-op, which is the worst way for this to fail.
TARGET_DIR = {PLATFORM_BUKKIT: "plugins", PLATFORM_PROXY: "plugins",
              PLATFORM_FABRIC: "mods"}

# What a freshly created server gets. Modrinth slugs, in install order.
#
# Two omissions worth knowing about rather than discovering later:
#   * Vault is not published on Modrinth at all (it lives on SpigotMC), so it
#     cannot be fetched from here.
#   * spark's Modrinth project only publishes mod-loader builds; its Paper
#     build is distributed elsewhere. It is listed for Bukkit anyway so that
#     the report says plainly that no build was found, rather than silently
#     leaving it out - and it starts working the day one is published.
PRESETS = {
    PLATFORM_BUKKIT: ("viaversion", "viabackwards", "viarewind", "luckperms",
                      "placeholderapi", "spark"),
    PLATFORM_PROXY: ("viaversion", "viabackwards", "luckperms"),
    PLATFORM_FABRIC: ("fabric-api", "lithium", "ferrite-core", "krypton",
                      "spark", "modmenu"),
}

# Pretty names for anything not simply capitalised.
PACKAGE_NAMES = {
    "viaversion": "ViaVersion", "viabackwards": "ViaBackwards",
    "viarewind": "ViaRewind", "luckperms": "LuckPerms",
    "placeholderapi": "PlaceholderAPI", "spark": "spark",
    "fabric-api": "Fabric API", "lithium": "Lithium",
    "ferrite-core": "FerriteCore", "krypton": "Krypton",
    "modmenu": "Mod Menu",
}


def package_name(slug):
    return PACKAGE_NAMES.get(slug, slug.replace("-", " ").title())

# Every flavour worth naming, and the platform it belongs to. The forks matter:
# they are all "Paper but faster" and they all take the same plugins, so the
# installer should not stall on a name it has not seen.
FLAVOUR_PLATFORM = {}
for _f in ("paper", "purpur", "spigot", "bukkit", "craftbukkit", "pufferfish",
           "folia", "leaves", "airplane", "yatopia", "gale", "sugarcane",
           "mirai", "petal", "scissors"):
    FLAVOUR_PLATFORM[_f] = PLATFORM_BUKKIT
for _f in ("velocity", "waterfall", "bungeecord"):
    FLAVOUR_PLATFORM[_f] = PLATFORM_PROXY
# Fabric and Quilt take mods, so they are installable even though Via is not
# the thing to install there. Forge and NeoForge get nothing by default.
for _f in ("fabric", "quilt"):
    FLAVOUR_PLATFORM[_f] = PLATFORM_FABRIC
for _f in ("forge", "neoforge"):
    FLAVOUR_PLATFORM[_f] = PLATFORM_MODDED
for _f in ("vanilla", "snapshot"):
    FLAVOUR_PLATFORM[_f] = PLATFORM_VANILLA

PRETTY_FLAVOUR = {"neoforge": "NeoForge", "bungeecord": "BungeeCord",
                  "craftbukkit": "CraftBukkit", "viaversion": "ViaVersion"}

WHY_NOT = {
    "vanilla": "Vanilla has no plugin loader at all, so there is nothing for a "
               "Bukkit plugin to load into. Use Paper if you want ViaVersion — "
               "it runs vanilla worlds unchanged and accepts plugins.",
    "snapshot": "This is a snapshot build, which has no plugin loader. Snapshots "
                "also change the protocol week to week, so Via would have "
                "nothing stable to translate to.",
    "fabric": "Fabric uses mods, not Bukkit plugins. The Via team ship a separate "
              "Fabric build (ViaFabric) which this installer does not handle.",
    "quilt": "Quilt uses mods, not Bukkit plugins. ViaFabric may work there, but "
             "it is a different artefact and not installed from here.",
    "forge": "Forge uses mods, not Bukkit plugins. ViaVersion has no Forge build.",
    "neoforge": "NeoForge uses mods, not Bukkit plugins. ViaVersion has no "
                "NeoForge build.",
}

RETRIES = 3
TIMEOUT = 30


class InstallError(Exception):
    """Something went wrong that the caller should report, not crash on."""


def pretty(flavour):
    return PRETTY_FLAVOUR.get(flavour, (flavour or "").title())


def platform_of(flavour):
    return FLAVOUR_PLATFORM.get((flavour or "").lower(), PLATFORM_VANILLA)


# ---------------------------------------------------------------------------
#  what is this server?
# ---------------------------------------------------------------------------

# Longest names first, so "neoforge" is not swallowed by "forge" and
# "craftbukkit" is not swallowed by "bukkit".
_JAR_NAMES = ("craftbukkit", "bungeecord", "pufferfish", "neoforge", "waterfall",
              "sugarcane", "yatopia", "velocity", "airplane", "scissors",
              "purpur", "fabric", "folia", "leaves", "quilt", "paper",
              "spigot", "petal", "mirai", "forge", "gale", "bukkit")


def _jar_flavour(jar):
    """Guess the flavour from the jar's filename. Cheap, and usually right."""
    name = os.path.basename(jar or "").lower()
    if not name:
        return ""
    for flavour in _JAR_NAMES:
        if flavour in name:
            return flavour
    return ""


def _jar_contents_flavour(jar):
    """Look inside the jar when the name gives nothing away.

    A plain `server.jar` is the ambiguous case that matters: it is what both a
    Vanilla download and a renamed Paper build are called.
    """
    if not jar or not os.path.isfile(jar):
        return ""
    try:
        with zipfile.ZipFile(jar) as z:
            names = z.namelist()
            joined = "\n".join(names[:4000])
            if "net/md_5/bungee/" in joined:
                return "bungeecord"
            if "com/velocitypowered/" in joined:
                return "velocity"
            if "org/bukkit/" in joined or "META-INF/versions/paper" in joined:
                return "paper"
            if "net/neoforged/" in joined:
                return "neoforge"
            if "net/minecraftforge/" in joined:
                return "forge"
            if "fabric-installer.json" in joined or "net/fabricmc/" in joined:
                return "fabric"
            if "META-INF/MANIFEST.MF" in names:
                mf = z.read("META-INF/MANIFEST.MF").decode("utf-8", "replace").lower()
                if "velocity" in mf:
                    return "velocity"
                if "bungee" in mf or "waterfall" in mf:
                    return "bungeecord"
                if "paperclip" in mf or "bukkit" in mf:
                    return "paper"
                if "fabric" in mf:
                    return "fabric"
    except (zipfile.BadZipFile, OSError, KeyError):
        return ""
    return ""


# A proxy has a plugins/ folder too, so proxy evidence has to be looked for
# before "there is a plugins/ directory, therefore Bukkit".
_PROXY_MARKERS = (("velocity.toml", "velocity"),
                  ("waterfall.yml", "waterfall"),
                  ("forwarding.secret", "velocity"))

_BUKKIT_MARKERS = (("purpur.yml", "purpur"),
                   ("paper.yml", "paper"),
                   (os.path.join("config", "paper-global.yml"), "paper"),
                   ("spigot.yml", "spigot"),
                   ("bukkit.yml", "bukkit"))

_MODDED_MARKERS = (("fabric-server-launcher.properties", "fabric"),
                   (os.path.join("config", "fabric_loader_dependencies.json"), "fabric"))

SNAPSHOT_RE = re.compile(r"\b(\d{2}w\d{1,2}[a-z]|.+?-(?:pre|rc)\d+)\b", re.I)


def detect(cwd, jar=""):
    """Work out the server flavour, its platform, and what can be installed.

    Folder evidence beats the jar name: a server that has actually booted has
    written config files that say exactly what it is, whereas a jar can be
    renamed to anything.
    """
    cwd = cwd or ""
    flavour = ""
    how = ""

    def marker(pairs, note):
        for rel, name in pairs:
            if cwd and os.path.isfile(os.path.join(cwd, rel)):
                return name, note % rel
        return "", ""

    flavour, how = marker(_PROXY_MARKERS, "found %s, which only a proxy writes")
    if not flavour:
        flavour, how = marker(_BUKKIT_MARKERS, "found %s in the server folder")
    if not flavour:
        flavour, how = marker(_MODDED_MARKERS, "found %s")

    # BungeeCord's own config is just config.yml, which is too common a name to
    # trust on its own; its modules/ folder alongside it is the giveaway.
    if not flavour and cwd and os.path.isfile(os.path.join(cwd, "config.yml")) \
            and os.path.isdir(os.path.join(cwd, "modules")):
        flavour, how = "bungeecord", "found config.yml beside a modules/ folder"

    if not flavour and cwd:
        if os.path.isdir(os.path.join(cwd, "mods")):
            flavour, how = "fabric", "the server folder has a mods/ directory"
        elif os.path.isdir(os.path.join(cwd, "plugins")):
            flavour, how = "bukkit", "the server folder already has a plugins/ directory"

    if not flavour:
        flavour = _jar_flavour(jar)
        if flavour:
            how = "the jar is named %s" % os.path.basename(jar)

    if not flavour:
        flavour = _jar_contents_flavour(jar)
        if flavour:
            how = "looked inside %s" % os.path.basename(jar or "the jar")

    version = mc_version(cwd, jar)
    if not flavour:
        flavour, how = "vanilla", "nothing identifies this as a plugin server"

    # A snapshot is still vanilla, but it deserves its own explanation.
    if flavour == "vanilla" and is_snapshot(cwd, jar):
        flavour, how = "snapshot", (how + " (and it looks like a snapshot build)")

    plat = platform_of(flavour)
    # "compatible" has always meant "can Via go here", and still does. Fabric
    # answers no to that and yes to "can anything be installed here", so the
    # two questions get two fields rather than one overloaded one.
    compatible = plat in (PLATFORM_BUKKIT, PLATFORM_PROXY)
    reason = "" if compatible else WHY_NOT.get(
        flavour, "%s does not load Bukkit or proxy plugins." % pretty(flavour))
    preset_ok = plat in PRESETS
    preset_reason = "" if preset_ok else (
        "%s has no recommended package set - it takes neither Bukkit plugins "
        "nor Fabric mods." % pretty(flavour))
    return {"flavour": flavour, "platform": plat, "compatible": compatible,
            "how": how, "reason": reason, "mc_version": version,
            "label": pretty(flavour), "preset_ok": preset_ok,
            "preset_reason": preset_reason,
            "preset": list(PRESETS.get(plat, ())),
            "target_dir": TARGET_DIR.get(plat, "plugins")}


VERSION_RE = re.compile(r"\b(1\.\d{1,2}(?:\.\d{1,2})?)\b")


def _version_string(cwd, jar=""):
    if cwd:
        try:
            with open(os.path.join(cwd, "version_history.json"),
                      encoding="utf-8", errors="replace") as fh:
                cur = (json.load(fh) or {}).get("currentVersion") or ""
            if cur:
                return cur
        except (OSError, ValueError):
            pass
    return os.path.basename(jar or "")


def mc_version(cwd, jar=""):
    """The Minecraft version this server runs, or "" if it cannot be told."""
    text = _version_string(cwd, jar)
    got = re.search(r"MC:\s*([0-9][0-9.]*)", text) or VERSION_RE.search(text)
    return got.group(1) if got else ""


def is_snapshot(cwd, jar=""):
    """True for a weekly snapshot or a pre-release / release candidate."""
    text = _version_string(cwd, jar)
    return bool(SNAPSHOT_RE.search(text)) or "snapshot" in text.lower()


# ---------------------------------------------------------------------------
#  talking to the index
# ---------------------------------------------------------------------------

def _get(url, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": UA,
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def latest_release(slug, game_version="", loaders=None):
    """The newest build of one plugin that will load on this platform.

    Returns (release, problem). `problem` is "" on success, "no-build" when the
    index answered but publishes nothing for this platform, and "unreachable"
    when it could not be asked. The caller needs to tell those apart: one is a
    fact to report calmly, the other is worth retrying.
    """
    loaders = tuple(loaders or PLATFORM_LOADERS[PLATFORM_BUKKIT])
    base = "%s/project/%s/version?loaders=%s" % (
        API, urllib.parse.quote(slug),
        urllib.parse.quote(json.dumps(list(loaders))))
    tries = []
    if game_version:
        tries.append(base + "&game_versions=" +
                     urllib.parse.quote(json.dumps([game_version])))
    tries.append(base)

    answered = False
    for url in tries:
        try:
            versions = _get(url)
            answered = True
        except (urllib.error.URLError, ValueError, OSError):
            continue
        usable = []
        for v in versions or []:
            if not any(l in loaders for l in (v.get("loaders") or [])):
                continue
            f = next((x for x in (v.get("files") or []) if x.get("primary")),
                     None) or next(iter(v.get("files") or []), None)
            if not f or not f.get("url"):
                continue
            usable.append({
                "version": v.get("version_number") or v.get("id") or "?",
                "published": v.get("date_published") or "",
                "channel": (v.get("version_type") or "release").lower(),
                "url": f["url"],
                "filename": f.get("filename") or (slug + ".jar"),
                "sha1": ((f.get("hashes") or {}).get("sha1") or "").lower(),
                "size": f.get("size") or 0,
                "exact_match": "game_versions=" in url,
            })
        if usable:
            usable.sort(key=lambda x: x["published"], reverse=True)
            # A stable release beats a newer snapshot: this runs unattended and
            # drops jars into a server someone else is going to start.
            stable = [x for x in usable if x["channel"] == "release"]
            return (stable or usable)[0], ""
    return None, ("no-build" if answered else "unreachable")


def _sha1(path):
    h = hashlib.sha1()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(131072), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _download(url, dest, expect_sha1="", expect_size=0, log=lambda m: None):
    """Fetch to dest, verifying before it counts. Retries a few times."""
    last = ""
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r, \
                    open(dest, "wb") as out:
                shutil.copyfileobj(r, out, 131072)
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = str(e)
            log("      attempt %d/%d failed: %s" % (attempt, RETRIES, last))
            time.sleep(min(2 * attempt, 5))
            continue

        size = os.path.getsize(dest) if os.path.isfile(dest) else 0
        if expect_size and size != expect_size:
            last = "size mismatch (got %d bytes, expected %d)" % (size, expect_size)
            log("      attempt %d/%d failed: %s" % (attempt, RETRIES, last))
            time.sleep(1)
            continue
        if expect_sha1:
            got = _sha1(dest)
            if got != expect_sha1:
                last = "checksum mismatch"
                log("      attempt %d/%d failed: %s" % (attempt, RETRIES, last))
                time.sleep(1)
                continue
        return True, ""
    try:
        if os.path.isfile(dest):
            os.remove(dest)
    except OSError:
        pass
    return False, last or "download failed"


def _existing(plugins_dir, pretty_name):
    """Jars in plugins/ that are this plugin.

    Only ever returns files ending in .jar, which is what keeps a plugin's
    config directory - plugins/ViaVersion/ - out of the replace path.
    """
    out = []
    low = pretty_name.lower()
    try:
        entries = os.listdir(plugins_dir)
    except OSError:
        return out
    for entry in entries:
        full = os.path.join(plugins_dir, entry)
        if not entry.lower().endswith(".jar") or not os.path.isfile(full):
            continue
        stem = entry.lower()
        if stem.startswith(low) or stem.replace("-", "").startswith(low):
            out.append(full)
    return out


# ---------------------------------------------------------------------------
#  the installer
# ---------------------------------------------------------------------------

def install_preset(cwd, jar="", log=None):
    """Install the recommended set for whatever this server turns out to be.

    This is what a newly created server gets: the packages that almost everyone
    ends up installing by hand, chosen per platform.
    """
    log = log or (lambda m: None)
    info = detect(cwd, jar)
    if not info["preset_ok"]:
        log(">>> Package installer: skipping — %s" % info["preset_reason"])
        return {"ok": True, "compatible": False, "preset_ok": False,
                "flavour": info["flavour"], "platform": info["platform"],
                "label": info["label"], "reason": info["preset_reason"],
                "results": [], "installed": 0, "updated": 0, "skipped": 0,
                "failed": 0}
    return install(cwd, jar, log=log, packages=PRESETS[info["platform"]],
                   via_only=False)


def install(cwd, jar="", log=None, only=None, packages=None, via_only=True):
    """Put the right Via builds into cwd/plugins, and say what happened.

    Never raises for an ordinary failure - a report is returned either way, so
    a caller driving this from a button or from server creation can log it and
    carry on.
    """
    log = log or (lambda m: None)
    pre = ">>> %s: " % ("Via installer" if via_only else "Package installer")
    report = {"ok": False, "compatible": False, "flavour": "", "platform": "",
              "reason": "", "results": [], "installed": 0, "updated": 0,
              "skipped": 0, "failed": 0}

    info = detect(cwd, jar)
    report.update({"flavour": info["flavour"], "platform": info["platform"],
                   "compatible": info["compatible"],
                   "mc_version": info["mc_version"], "label": info["label"],
                   "target_dir": info["target_dir"]})
    log(pre + "detected %s (%s)" % (info["label"], info["how"]))

    # Installing Via is gated on Via making sense here; installing a preset is
    # gated on the platform taking packages at all.
    blocked = (not info["compatible"]) if via_only else (not info["preset_ok"])
    if blocked:
        report["reason"] = info["reason"] if via_only else info["preset_reason"]
        report["ok"] = True          # nothing failed; there was nothing to do
        log(pre + "skipping — %s" % report["reason"])
        return report

    kind = {PLATFORM_PROXY: "proxy", PLATFORM_FABRIC: "Fabric",
            PLATFORM_BUKKIT: "Bukkit"}.get(info["platform"], "Bukkit")
    log(pre + "%s takes %s %s, installed into %s/"
        % (info["label"], kind,
           "mods" if info["platform"] == PLATFORM_FABRIC else "plugins",
           info["target_dir"]))
    if info["mc_version"]:
        log(pre + "server looks like Minecraft %s" % info["mc_version"])
    else:
        log(pre + "could not tell the Minecraft version; will take "
            "the newest %s build of each plugin" % kind)

    plugins_dir = os.path.join(cwd, info["target_dir"])
    try:
        os.makedirs(plugins_dir, exist_ok=True)
    except OSError as e:
        report["reason"] = "Could not create %s: %s" % (plugins_dir, e)
        log(pre + "%s" % report["reason"])
        return report

    loaders = PLATFORM_LOADERS[info["platform"]]
    chosen = packages if packages is not None else [s for s, _ in VIA_PLUGINS]
    wanted = [(s, package_name(s)) for s in chosen if not only or s in only]
    for slug, name in wanted:
        entry = {"plugin": name, "action": "failed", "version": "", "detail": ""}
        log(pre + "%s" % name)

        rel = problem = None
        for attempt in range(1, RETRIES + 1):
            rel, problem = latest_release(slug, info["mc_version"], loaders)
            if rel or problem == "no-build":
                break
            log("      attempt %d/%d: could not reach the release index"
                % (attempt, RETRIES))
            time.sleep(min(2 * attempt, 5))

        if not rel:
            if problem == "no-build":
                # Not every plugin ships for every platform, and that is a fact
                # about the plugin rather than a failure of this run.
                entry["action"] = "skipped"
                entry["detail"] = "no %s build is published" % kind
                log("      skipped — no %s build is published" % kind)
                report["skipped"] += 1
            else:
                entry["detail"] = "could not reach the release index"
                log("      FAILED after %d attempts — %s" % (RETRIES, entry["detail"]))
                report["failed"] += 1
            report["results"].append(entry)
            continue

        entry["version"] = rel["version"]
        if not rel["exact_match"] and info["mc_version"]:
            log("      no build listed for %s; using the newest %s build"
                % (info["mc_version"], kind))

        have = _existing(plugins_dir, name)
        target = os.path.join(plugins_dir, rel["filename"])

        # Already the right file? Then there is nothing to do, and saying so is
        # the whole point of running this more than once.
        if rel["sha1"]:
            for path in have:
                if _sha1(path) == rel["sha1"]:
                    entry["action"] = "skipped"
                    entry["detail"] = "%s already installed" % rel["version"]
                    log("      already up to date (%s)" % rel["version"])
                    report["skipped"] += 1
                    break
        if entry["action"] == "skipped":
            report["results"].append(entry)
            continue

        log("      downloading %s (%s)" % (rel["filename"], rel["version"]))
        fd, tmp = tempfile.mkstemp(prefix="via-", suffix=".jar", dir=plugins_dir)
        os.close(fd)
        ok, err = _download(rel["url"], tmp, rel["sha1"], rel["size"], log)
        if not ok:
            entry["detail"] = err
            log("      FAILED after %d attempts — %s" % (RETRIES, err))
            report["failed"] += 1
            report["results"].append(entry)
            continue
        log("      verified (sha1 %s)" % (rel["sha1"][:12] or "not published"))

        replaced = []
        for path in have:
            if os.path.abspath(path) == os.path.abspath(target):
                continue
            try:
                os.remove(path)
                replaced.append(os.path.basename(path))
            except OSError as e:
                log("      could not remove old %s: %s"
                    % (os.path.basename(path), e))
        try:
            if os.path.exists(target):
                os.remove(target)
            shutil.move(tmp, target)
        except OSError as e:
            entry["detail"] = "could not place the jar: %s" % e
            log("      FAILED — %s" % entry["detail"])
            report["failed"] += 1
            try:
                os.remove(tmp)
            except OSError:
                pass
            report["results"].append(entry)
            continue

        if replaced:
            entry["action"] = "updated"
            entry["detail"] = "replaced " + ", ".join(replaced)
            log("      updated to %s (replaced %s; config kept)"
                % (rel["version"], ", ".join(replaced)))
            report["updated"] += 1
        else:
            entry["action"] = "installed"
            entry["detail"] = "new install"
            log("      installed %s" % rel["version"])
            report["installed"] += 1
        report["results"].append(entry)

    report["ok"] = report["failed"] == 0
    log(pre + "%d installed, %d updated, %d skipped, %d failed"
        % (report["installed"], report["updated"],
           report["skipped"], report["failed"]))
    return report
