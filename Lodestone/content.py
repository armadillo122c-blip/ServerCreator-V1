"""Mods and plugins: finding them, checking they fit, installing what they need.

Three places publish them, and they are not equally informative:

    Modrinth   mods and plugins   per-version loaders, game versions,
                                  dependencies and sha1/sha512 hashes
    Hangar     Paper/Velocity     per-version platform versions, plugin
               plugins            dependencies and a sha256
    SpigotMC   Bukkit plugins     (through the Spiget mirror) a "tested
                                  versions" list, no dependencies, and only
                                  the latest file - premium and externally
                                  hosted resources cannot be fetched at all

So the honest thing is to check what can be checked and say plainly what
cannot. Modrinth answers "will this load on Fabric 1.21.1" exactly; SpigotMC
can only say what the author claims to have tested; and a plugin whose
dependency is hosted elsewhere is installed with a warning naming it rather
than silently half-installed.

Mods and plugins are kept apart by what the server is, not by what the user
asks for. A Fabric server is offered mods for Fabric and nothing else - the
plugin sources are not even queried - and a Paper server is offered plugins.
Putting either in the other's folder is a silent no-op at best, which is the
worst way for this to fail.

What was installed, from where, and what it pulled in as a dependency, is
written to `<server>/.lodestone/content.json`. That is what makes updating and
removing sensible: an update replaces exactly the jar the old version put
there, and removing something says which other installs needed it. Jars that
were put there by hand are identified by asking Modrinth about their hash, and
by reading their own metadata (fabric.mod.json, mods.toml, plugin.yml) - which
also catches a Forge mod dropped into a Fabric server.

Standard library only, like the rest of Lodestone.
"""

import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

UA = "Lodestone/1.0 (Minecraft server panel; mod and plugin browser)"
MODRINTH_API = "https://api.modrinth.com/v2"
HANGAR_API = "https://hangar.papermc.io/api/v1"
SPIGET_API = "https://api.spiget.org/v2"

MODRINTH_SITE = "https://modrinth.com"
HANGAR_SITE = "https://hangar.papermc.io"
SPIGOT_SITE = "https://www.spigotmc.org"

RETRIES = 3
TIMEOUT = 30
MANIFEST = os.path.join(".lodestone", "content.json")

# What each server runs, and so what it may be given.
#   kind     "mod" -> mods/, "plugin" -> plugins/, "" -> nothing at all
#   loaders  Modrinth loader tags whose builds load here
#   hangar   Hangar platform, or "" when Hangar's builds are not for it
#   spigot   whether SpigotMC (Bukkit) plugins load here
PROFILES = {
    "paper": {"kind": "plugin", "loaders": ("paper", "spigot", "bukkit"),
              "hangar": "PAPER", "spigot": True},
    "purpur": {"kind": "plugin", "loaders": ("purpur", "paper", "spigot", "bukkit"),
               "hangar": "PAPER", "spigot": True},
    "spigot": {"kind": "plugin", "loaders": ("spigot", "bukkit"),
               "hangar": "", "spigot": True},
    "folia": {"kind": "plugin", "loaders": ("folia",), "hangar": "", "spigot": False},
    "velocity": {"kind": "plugin", "loaders": ("velocity",), "hangar": "VELOCITY",
                 "spigot": False},
    "bungeecord": {"kind": "plugin", "loaders": ("bungeecord", "waterfall"),
                   "hangar": "WATERFALL", "spigot": False},
    "fabric": {"kind": "mod", "loaders": ("fabric",), "hangar": "", "spigot": False},
    "quilt": {"kind": "mod", "loaders": ("quilt", "fabric"), "hangar": "", "spigot": False},
    "forge": {"kind": "mod", "loaders": ("forge",), "hangar": "", "spigot": False},
    "neoforge": {"kind": "mod", "loaders": ("neoforge",), "hangar": "", "spigot": False},
}
# Forks that take the same things as the server they are a fork of.
ALIASES = {"pufferfish": "paper", "leaves": "paper", "gale": "paper",
           "airplane": "paper", "scissors": "paper", "petal": "paper",
           "mirai": "paper", "sugarcane": "paper", "yatopia": "paper",
           "bukkit": "spigot", "craftbukkit": "spigot", "waterfall": "bungeecord"}
NAMES = {"paper": "Paper", "purpur": "Purpur", "spigot": "Spigot", "folia": "Folia",
         "velocity": "Velocity", "bungeecord": "BungeeCord", "fabric": "Fabric",
         "quilt": "Quilt", "forge": "Forge", "neoforge": "NeoForge",
         "vanilla": "Vanilla", "snapshot": "a snapshot"}
SOURCE_NAMES = {"modrinth": "Modrinth", "hangar": "Hangar", "spigot": "SpigotMC"}


class ContentError(Exception):
    """Something the user should be told, in a sentence."""


def profile(software, mc=""):
    """What a server running `software` on Minecraft `mc` can be given."""
    sw = ALIASES.get((software or "").lower(), (software or "").lower())
    p = PROFILES.get(sw)
    name = NAMES.get(sw, (software or "this server").title())
    if not p:
        why = ("Vanilla has no mod or plugin loader. Create the server as Paper "
               "(plugins) or Fabric/NeoForge (mods) to add content."
               if sw in ("vanilla", "snapshot", "") else
               "%s is not a server Lodestone can install content for." % name)
        return {"software": sw, "mc": mc or "", "kind": "", "loaders": [],
                "dir": "", "sources": [], "label": name, "reason": why}
    sources = ["modrinth"]
    if p["hangar"]:
        sources.append("hangar")
    if p["spigot"]:
        sources.append("spigot")
    return {"software": sw, "mc": mc or "", "kind": p["kind"],
            "loaders": list(p["loaders"]), "hangar": p["hangar"],
            "dir": "mods" if p["kind"] == "mod" else "plugins",
            "sources": sources, "label": name, "reason": ""}


# ---------------------------------------------------------------------------
#  HTTP
# ---------------------------------------------------------------------------

_cache = {}
_cache_lock = threading.Lock()
CACHE_SECONDS = 300


def _call(url, body=None, cache=True):
    key = url + ("|" + json.dumps(body, sort_keys=True) if body is not None else "")
    if cache:
        with _cache_lock:
            hit = _cache.get(key)
            if hit and time.time() - hit[0] < CACHE_SECONDS:
                return hit[1]
    data = None if body is None else json.dumps(body).encode()
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    last = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                out = json.loads(r.read().decode("utf-8", "replace") or "null")
            if cache:
                with _cache_lock:
                    _cache[key] = (time.time(), out)
            return out
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise ContentError("Not found on %s." % _host(url))
            if e.code == 429:
                last = "rate limited"
                time.sleep(3 + attempt * 3)
                continue
            if 400 <= e.code < 500:
                raise ContentError("%s refused the request (HTTP %d)." % (_host(url), e.code))
            last = "HTTP %d" % e.code
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = getattr(e, "reason", None) or e
        time.sleep(min(1 + attempt * 2, 5))
    raise ContentError("Could not reach %s: %s" % (_host(url), last))


def _host(url):
    return urllib.parse.urlparse(url).netloc or url


def _q(v):
    return urllib.parse.quote(str(v), safe="")


def clear_cache():
    with _cache_lock:
        _cache.clear()


# ---------------------------------------------------------------------------
#  compatibility
# ---------------------------------------------------------------------------

def _mc_tuple(v):
    nums = re.match(r"^(\d+(?:\.\d+)*)", v or "")
    return tuple(int(x) for x in nums.group(1).split(".")) if nums else ()


def tested_covers(tested, mc):
    """SpigotMC's tested versions are often "1.21" meaning the whole line."""
    if not mc:
        return None
    if not tested:
        return None
    want = _mc_tuple(mc)
    for t in tested:
        tt = _mc_tuple(t)
        if tt and want[:len(tt)] == tt:
            return True
    return False


# ---------------------------------------------------------------------------
#  search
# ---------------------------------------------------------------------------

SORTS = {"relevance": ("relevance", "-downloads", "-downloads"),
         "downloads": ("downloads", "-downloads", "-downloads"),
         "updated": ("updated", "-updated", "-updateDate"),
         "newest": ("newest", "-newest", "-releaseDate")}


# Hangar's categories are a fixed set; its API takes these ids as they are.
HANGAR_CATEGORIES = (("admin_tools", "Admin tools"), ("chat", "Chat"), ("dev_tools", "Developer tools"),
                     ("economy", "Economy"), ("gameplay", "Gameplay"), ("games", "Games"),
                     ("protection", "Protection"), ("role_playing", "Role playing"),
                     ("world_management", "World management"), ("misc", "Miscellaneous"))


def categories(source, prof):
    """The categories a source can filter by: [{"id", "name"}]. SpigotMC: none."""
    if source == "modrinth":
        tags = _call("%s/tag/category" % MODRINTH_API) or []
        out = [{"id": t["name"], "name": t["name"].replace("-", " ").capitalize()}
               for t in tags if t.get("project_type") == "mod" and t.get("header") == "categories"
               and t.get("name") not in PROFILES]
        return sorted(out, key=lambda c: c["name"])
    if source == "hangar":
        return [{"id": k, "name": v} for k, v in HANGAR_CATEGORIES]
    return []


def search(source, prof, query="", offset=0, limit=20, sort="relevance",
           compatible_only=True, category=""):
    """One page of results: {"items": [...], "total", "offset", "source"}.

    Every item carries `compatible` (True, False, or None for "the source does
    not say") and a `note` explaining anything that will stop it installing.
    """
    if not prof.get("kind"):
        raise ContentError(prof.get("reason") or "This server takes no mods or plugins.")
    if source not in prof["sources"]:
        raise ContentError("%s has nothing for %s servers." % (SOURCE_NAMES.get(source, source),
                                                               prof["label"]))
    limit = max(1, min(int(limit or 20), 50))
    offset = max(0, int(offset or 0))
    sort = sort if sort in SORTS else "relevance"
    category = re.sub(r"[^a-z0-9_-]", "", (category or "").lower())
    if source == "modrinth":
        return _search_modrinth(prof, query, offset, limit, sort, compatible_only, category)
    if source == "hangar":
        return _search_hangar(prof, query, offset, limit, sort, compatible_only, category)
    return _search_spiget(prof, query, offset, limit, sort, compatible_only)


def _search_modrinth(prof, query, offset, limit, sort, compatible_only, category=""):
    facets = [["categories:%s" % l for l in prof["loaders"]]]
    if prof["kind"] == "mod":
        facets.append(["project_type:mod"])
        # A client-only mod (a minimap, a shader loader) does nothing on a server.
        facets.append(["server_side:required", "server_side:optional"])
    else:
        facets.append(["project_type:plugin"])
    if compatible_only and prof["mc"]:
        facets.append(["versions:%s" % prof["mc"]])
    if category:
        facets.append(["categories:%s" % category])
    url = "%s/search?limit=%d&offset=%d&index=%s&facets=%s" % (
        MODRINTH_API, limit, offset, SORTS[sort][0], _q(json.dumps(facets)))
    if query:
        url += "&query=" + _q(query)
    data = _call(url)
    items = []
    for h in data.get("hits") or []:
        cats = h.get("display_categories") or h.get("categories") or []
        loaders_ok = any(l in (h.get("categories") or []) for l in prof["loaders"])
        mc_ok = (not prof["mc"]) or prof["mc"] in (h.get("versions") or [])
        note = ""
        if not loaders_ok:
            note = "No %s build." % prof["label"]
        elif not mc_ok:
            note = "No build for Minecraft %s." % prof["mc"]
        items.append({
            "source": "modrinth", "id": h.get("project_id"), "slug": h.get("slug"),
            "name": h.get("title") or h.get("slug"), "author": h.get("author") or "",
            "description": h.get("description") or "", "icon": h.get("icon_url") or "",
            "downloads": h.get("downloads") or 0, "updated": h.get("date_modified") or "",
            "categories": [c for c in cats if c not in PROFILES and c not in ("minecraft",)][:4],
            "url": "%s/%s/%s" % (MODRINTH_SITE, "plugin" if prof["kind"] == "plugin" else "mod",
                                 h.get("slug") or h.get("project_id")),
            "compatible": loaders_ok and mc_ok, "installable": True, "note": note,
            "server_side": h.get("server_side") or "",
        })
    return {"items": items, "total": data.get("total_hits") or 0, "offset": offset,
            "limit": limit, "source": "modrinth"}


def _search_hangar(prof, query, offset, limit, sort, compatible_only, category=""):
    url = "%s/projects?limit=%d&offset=%d&platform=%s" % (HANGAR_API, limit, offset, prof["hangar"])
    if category:
        url += "&category=" + _q(category)
    if compatible_only and prof["mc"]:
        url += "&version=" + _q(prof["mc"])
    if query:
        url += "&q=" + _q(query)
    if sort != "relevance" or not query:
        url += "&sort=" + SORTS[sort][1]
    data = _call(url)
    items = []
    for r in data.get("result") or []:
        ns = r.get("namespace") or {}
        supported = (r.get("supportedPlatforms") or {}).get(prof["hangar"]) or []
        mc_ok = (not prof["mc"]) or prof["mc"] in supported
        items.append({
            "source": "hangar", "id": ns.get("slug") or r.get("name"), "slug": ns.get("slug"),
            "name": r.get("name") or ns.get("slug"), "author": ns.get("owner") or "",
            "description": r.get("description") or "", "icon": r.get("avatarUrl") or "",
            "downloads": (r.get("stats") or {}).get("downloads") or 0,
            "updated": r.get("lastUpdated") or "",
            "categories": [c for c in [r.get("category")] if c][:4],
            "url": "%s/%s/%s" % (HANGAR_SITE, ns.get("owner") or "", ns.get("slug") or ""),
            "compatible": mc_ok, "installable": True,
            "note": "" if mc_ok else "Not marked for Minecraft %s." % prof["mc"],
        })
    pag = data.get("pagination") or {}
    return {"items": items, "total": pag.get("count") or len(items), "offset": offset,
            "limit": limit, "source": "hangar"}


def _spiget_item(r, prof):
    tested = r.get("testedVersions") or []
    fits = tested_covers(tested, prof["mc"])
    external = bool(r.get("external"))
    premium = bool(r.get("premium")) or float(r.get("price") or 0) > 0
    note = ""
    if premium:
        note = "Premium resource - buy and download it on SpigotMC."
    elif external:
        note = "The author hosts the file elsewhere, so it cannot be fetched from here."
    elif fits is False:
        note = "Only tested on %s." % ", ".join(tested[-4:])
    elif fits is None:
        note = "The author lists no tested versions."
    icon = ((r.get("icon") or {}).get("url") or "")
    return {
        "source": "spigot", "id": str(r.get("id")), "slug": str(r.get("id")),
        "name": r.get("name") or str(r.get("id")), "author": "",
        "description": r.get("tag") or "",
        "icon": ("%s/%s" % (SPIGOT_SITE, icon)) if icon else "",
        "downloads": r.get("downloads") or 0,
        "updated": r.get("updateDate") or 0,
        "categories": [], "tested": tested,
        "url": "%s/resources/%s/" % (SPIGOT_SITE, r.get("id")),
        "compatible": fits, "installable": not (premium or external), "note": note,
    }


def _search_spiget(prof, query, offset, limit, sort, compatible_only):
    page = offset // limit + 1
    fields = "id,name,tag,downloads,updateDate,testedVersions,external,premium,price,icon,file"
    if query:
        url = "%s/search/resources/%s?field=name&size=%d&page=%d&sort=%s&fields=%s" % (
            SPIGET_API, _q(query), limit, page, SORTS[sort][2], fields)
    else:
        url = "%s/resources/free?size=%d&page=%d&sort=%s&fields=%s" % (
            SPIGET_API, limit, page, SORTS[sort][2], fields)
    try:
        data = _call(url) or []
    except ContentError as e:
        if "Not found" in str(e):          # Spiget answers 404 for "no matches"
            data = []
        else:
            raise
    # Not filtered on "tested versions": that list is the author's claim and
    # routinely stops years short of what a plugin really runs on (Vault).
    items = [_spiget_item(r, prof) for r in data]
    total = offset + len(data) + (limit if len(data) == limit else 0)
    return {"items": items, "total": total, "offset": offset, "limit": limit,
            "source": "spigot"}


# ---------------------------------------------------------------------------
#  one project, and its versions
# ---------------------------------------------------------------------------

def _mr_version(v, prof):
    files = v.get("files") or []
    f = next((x for x in files if x.get("primary")), None) or (files[0] if files else None)
    loaders = v.get("loaders") or []
    games = v.get("game_versions") or []
    problems = []
    if not any(l in loaders for l in prof["loaders"]):
        problems.append("Built for %s, not %s." % (", ".join(loaders) or "nothing", prof["label"]))
    if prof["mc"] and prof["mc"] not in games:
        problems.append("Built for Minecraft %s, not %s." % (_range(games), prof["mc"]))
    if v.get("environment") == "client_only":
        problems.append("Client-only: it does nothing on a server.")
    return {
        "source": "modrinth", "id": v.get("id"), "project_id": v.get("project_id"),
        "name": v.get("name") or v.get("version_number"), "number": v.get("version_number") or "",
        "date": v.get("date_published") or "", "channel": v.get("version_type") or "release",
        "game_versions": games, "loaders": loaders,
        "file": ({"url": f.get("url"), "filename": f.get("filename"), "size": f.get("size") or 0,
                  "hashes": {k: v2 for k, v2 in (f.get("hashes") or {}).items()
                             if k in ("sha1", "sha512")}} if f else None),
        "deps": [{"project_id": d.get("project_id"), "version_id": d.get("version_id"),
                  "type": d.get("dependency_type"), "file_name": d.get("file_name")}
                 for d in v.get("dependencies") or []],
        "compatible": not problems and bool(f), "problems": problems if f else problems + ["No file."],
    }


def _range(games):
    if not games:
        return "no listed version"
    if len(games) <= 3:
        return ", ".join(games)
    return "%s … %s" % (games[0], games[-1])


def _hangar_version(v, prof, slug):
    plat = prof["hangar"]
    d = (v.get("downloads") or {}).get(plat) or {}
    fi = d.get("fileInfo") or {}
    games = (v.get("platformDependencies") or {}).get(plat) or []
    problems = []
    if not d:
        problems.append("No %s build." % prof["label"])
    if prof["mc"] and games and prof["mc"] not in games:
        problems.append("Built for Minecraft %s, not %s." % (_range(sorted(games, key=_mc_tuple)), prof["mc"]))
    url = d.get("downloadUrl")
    if d and not url:
        problems.append("Hosted elsewhere (%s) - download it by hand." % (d.get("externalUrl") or "external link"))
    deps = []
    for dep in (v.get("pluginDependencies") or {}).get(plat) or []:
        deps.append({"project_id": dep.get("projectId") or dep.get("name"),
                     "name": dep.get("name"), "type": "required" if dep.get("required") else "optional",
                     "external": dep.get("externalUrl") or ""})
    ch = ((v.get("channel") or {}).get("name") or "Release")
    return {
        "source": "hangar", "id": v.get("name"), "project_id": slug,
        "name": v.get("name"), "number": v.get("name"), "date": v.get("createdAt") or "",
        "channel": "release" if ch.lower() == "release" else ch.lower(),
        "game_versions": games, "loaders": [plat.lower()],
        "file": ({"url": url, "filename": fi.get("name") or (slug + ".jar"),
                  "size": fi.get("sizeBytes") or 0,
                  "hashes": {"sha256": fi.get("sha256Hash")} if fi.get("sha256Hash") else {}}
                 if url else None),
        "deps": deps, "compatible": not problems, "problems": problems,
    }


def project(source, pid, prof):
    """Details and installable versions of one project, newest first.

    Versions that do not fit this server are still returned (marked, with the
    reason) when nothing fits, so the UI can say *why* instead of "none".
    """
    if source == "modrinth":
        p = _call("%s/project/%s" % (MODRINTH_API, _q(pid)))
        loaders = _q(json.dumps(prof["loaders"]))
        url = "%s/project/%s/version?loaders=%s" % (MODRINTH_API, _q(pid), loaders)
        fit = _call(url + ("&game_versions=" + _q(json.dumps([prof["mc"]])) if prof["mc"] else ""))
        versions = [_mr_version(v, prof) for v in fit or []]
        if not versions:
            versions = [_mr_version(v, prof) for v in (_call(url) or [])[:15]]
        if not versions:
            versions = [_mr_version(v, prof) for v in (_call("%s/project/%s/version" % (MODRINTH_API, _q(pid))) or [])[:15]]
        return {
            "source": "modrinth", "id": p.get("id"), "slug": p.get("slug"),
            "name": p.get("title"), "description": p.get("description") or "",
            "icon": p.get("icon_url") or "", "downloads": p.get("downloads") or 0,
            "url": "%s/%s/%s" % (MODRINTH_SITE, p.get("project_type") or "mod", p.get("slug")),
            "server_side": p.get("server_side") or "", "client_side": p.get("client_side") or "",
            "loaders": p.get("loaders") or [], "versions": versions[:40],
            # Screenshots, featured first - what the mod looks like in game.
            "gallery": [{"url": g.get("url"), "title": g.get("title") or ""}
                        for g in sorted(p.get("gallery") or [], key=lambda g: (not g.get("featured"),
                                                                               g.get("ordering") or 0))
                        if g.get("url")][:6],
            "categories": [c for c in p.get("categories") or [] if c not in PROFILES],
            "body": (p.get("body") or "")[:6000], "license": (p.get("license") or {}).get("id") or "",
        }
    if source == "hangar":
        p = _call("%s/projects/%s" % (HANGAR_API, _q(pid)))
        slug = (p.get("namespace") or {}).get("slug") or pid
        base = "%s/projects/%s/versions?limit=25&platform=%s" % (HANGAR_API, _q(slug), prof["hangar"])
        fit = []
        if prof["mc"]:
            fit = (_call(base + "&platformVersion=" + _q(prof["mc"])) or {}).get("result") or []
        if not fit:
            fit = (_call(base) or {}).get("result") or []
        versions = [_hangar_version(v, prof, slug) for v in fit]
        # Release channel first: Hangar happily lists last night's snapshot on top.
        versions.sort(key=lambda v: (v["channel"] != "release", ), reverse=False)
        ns = p.get("namespace") or {}
        return {
            "source": "hangar", "id": slug, "slug": slug, "name": p.get("name") or slug,
            "description": p.get("description") or "", "icon": p.get("avatarUrl") or "",
            "downloads": (p.get("stats") or {}).get("downloads") or 0,
            "url": "%s/%s/%s" % (HANGAR_SITE, ns.get("owner") or "", slug),
            "versions": versions, "body": (p.get("mainPageContent") or "")[:6000],
        }
    if source == "spigot":
        r = _call("%s/resources/%s" % (SPIGET_API, _q(pid)))
        item = _spiget_item(r, prof)
        latest = {}
        try:
            latest = _call("%s/resources/%s/versions/latest" % (SPIGET_API, _q(pid))) or {}
        except ContentError:
            latest = {}
        problems = []
        if not item["installable"]:
            problems.append(item["note"])
        elif item["compatible"] is False:
            problems.append(item["note"])
        v = {
            "source": "spigot", "id": str(latest.get("id") or (r.get("version") or {}).get("id") or ""),
            "project_id": str(r.get("id")), "name": latest.get("name") or "latest",
            "number": latest.get("name") or "latest", "date": latest.get("releaseDate") or "",
            "channel": "release", "game_versions": item["tested"], "loaders": ["spigot"],
            "file": ({"url": "%s/resources/%s/download" % (SPIGET_API, _q(pid)),
                      "filename": _jarname(r.get("name") or pid, latest.get("name") or ""),
                      "size": 0, "hashes": {}} if item["installable"] else None),
            "deps": [], "compatible": not problems, "problems": problems,
            "unverified": item["compatible"] is None,
        }
        return {
            "source": "spigot", "id": str(r.get("id")), "slug": str(r.get("id")),
            "name": r.get("name"), "description": r.get("tag") or "", "icon": item["icon"],
            "downloads": r.get("downloads") or 0, "url": item["url"], "versions": [v],
            "tested": item["tested"], "note": item["note"],
            "body": "SpigotMC only offers the latest file, and lists no dependencies - check the "
                    "resource page for anything it needs.",
        }
    raise ContentError("Unknown source: %s" % source)


def _jarname(name, version):
    base = re.sub(r"[^A-Za-z0-9._-]+", "", (name or "plugin").replace(" ", "-"))[:60] or "plugin"
    ver = re.sub(r"[^A-Za-z0-9._-]+", "", version or "")[:30]
    return "%s%s.jar" % (base, ("-" + ver) if ver else "")


def best_version(source, pid, prof):
    """The newest release that fits; else the newest that fits at all."""
    p = project(source, pid, prof)
    fits = [v for v in p["versions"] if v["compatible"]]
    rel = [v for v in fits if v["channel"] == "release"]
    pick = (rel or fits or [None])[0]
    return p, pick


# ---------------------------------------------------------------------------
#  the manifest
# ---------------------------------------------------------------------------

_locks = {}
_locks_guard = threading.Lock()


def _lock(cwd):
    key = os.path.normcase(os.path.abspath(cwd))
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.RLock()
        return _locks[key]


def load_manifest(cwd):
    path = os.path.join(cwd, MANIFEST)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("items"), dict):
            data.setdefault("unknown", {})
            return data
    except (OSError, ValueError):
        pass
    return {"version": 1, "items": {}, "unknown": {}}


def save_manifest(cwd, data):
    path = os.path.join(cwd, MANIFEST)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)


def _base(fname):
    return fname[:-9] if fname.lower().endswith(".disabled") else fname


def _sha1(path):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(262144), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
#  what a jar says about itself
# ---------------------------------------------------------------------------

def jar_meta(path):
    """{"platform", "platforms", "name", "version", "id"} from inside a jar, or {}.

    One jar can carry several: ViaVersion ships a single file with both a
    plugin.yml and a fabric.mod.json. So every platform found is listed, and
    the jar counts as loadable if any of them fits the server.
    """
    found = []
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())

            def read(n):
                return z.read(n).decode("utf-8", "replace")

            if "fabric.mod.json" in names:
                try:
                    d = json.loads(read("fabric.mod.json"), strict=False)
                except ValueError:
                    d = {}
                found.append({"platform": "fabric", "name": d.get("name") or d.get("id") or "",
                              "version": str(d.get("version") or ""), "id": d.get("id") or ""})
            if "quilt.mod.json" in names:
                try:
                    d = (json.loads(read("quilt.mod.json"), strict=False).get("quilt_loader") or {})
                except ValueError:
                    d = {}
                found.append({"platform": "quilt", "name": (d.get("metadata") or {}).get("name") or d.get("id") or "",
                              "version": str(d.get("version") or ""), "id": d.get("id") or ""})
            for toml, plat in (("META-INF/neoforge.mods.toml", "neoforge"),
                               ("META-INF/mods.toml", "forge")):
                if toml in names:
                    text = read(toml)
                    name = re.search(r'(?m)^\s*displayName\s*=\s*"([^"]*)"', text)
                    ver = re.search(r'(?m)^\s*version\s*=\s*"([^"]*)"', text)
                    mid = re.search(r'(?m)^\s*modId\s*=\s*"([^"]*)"', text)
                    found.append({"platform": plat, "name": name.group(1) if name else "",
                                  "version": ver.group(1) if ver and "${" not in ver.group(1) else "",
                                  "id": mid.group(1) if mid else ""})
            for yml, plat in (("paper-plugin.yml", "paper"), ("plugin.yml", "bukkit"),
                              ("bungee.yml", "bungeecord")):
                if yml in names:
                    text = read(yml)
                    name = re.search(r"(?m)^name:\s*['\"]?([^'\"\n]+)", text)
                    ver = re.search(r"(?m)^version:\s*['\"]?([^'\"\n]+)", text)
                    found.append({"platform": plat, "name": name.group(1).strip() if name else "",
                                  "version": ver.group(1).strip() if ver else "",
                                  "id": name.group(1).strip().lower() if name else ""})
            if "velocity-plugin.json" in names:
                try:
                    d = json.loads(read("velocity-plugin.json"))
                except ValueError:
                    d = {}
                found.append({"platform": "velocity", "name": d.get("name") or d.get("id") or "",
                              "version": str(d.get("version") or ""), "id": d.get("id") or ""})
    except (zipfile.BadZipFile, OSError, KeyError, RuntimeError):
        return {"platform": "broken", "platforms": ["broken"], "name": "", "version": "", "id": ""}
    if not found:
        return {}
    first = dict(found[0])
    first["platforms"] = [f["platform"] for f in found]
    for f in found:                     # a plugin.yml name beats a mod id
        if f["name"] and not first["name"]:
            first["name"] = f["name"]
        if f["version"] and not first["version"]:
            first["version"] = f["version"]
    return first


def jar_icon_path(path):
    """Where inside a jar its own icon lives, or "" when it has none.

    Fabric and Quilt name it in their metadata (a path, or sizes mapped to
    paths - the biggest is taken); Forge and NeoForge use logoFile. Plugins
    carry no icon at all.
    """
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            cand = ""
            if "fabric.mod.json" in names:
                try:
                    icon = json.loads(z.read("fabric.mod.json").decode("utf-8", "replace"), strict=False).get("icon")
                except ValueError:
                    icon = None
                if isinstance(icon, dict) and icon:
                    icon = icon[max(icon, key=lambda k: int(k) if str(k).isdigit() else 0)]
                cand = icon if isinstance(icon, str) else ""
            elif "quilt.mod.json" in names:
                try:
                    meta = (json.loads(z.read("quilt.mod.json").decode("utf-8", "replace"), strict=False)
                            .get("quilt_loader") or {}).get("metadata") or {}
                except ValueError:
                    meta = {}
                icon = meta.get("icon")
                if isinstance(icon, dict) and icon:
                    icon = icon[max(icon, key=lambda k: int(k) if str(k).isdigit() else 0)]
                cand = icon if isinstance(icon, str) else ""
            else:
                for toml in ("META-INF/neoforge.mods.toml", "META-INF/mods.toml"):
                    if toml in names:
                        m = re.search(r'(?m)^\s*logoFile\s*=\s*"([^"]+)"', z.read(toml).decode("utf-8", "replace"))
                        cand = m.group(1) if m else ""
                        break
            cand = (cand or "").lstrip("/")
            return cand if cand in names and cand.lower().endswith(".png") else ""
    except (zipfile.BadZipFile, OSError, KeyError, RuntimeError):
        return ""


def jar_icon(path, limit=512 * 1024):
    """The PNG bytes of a jar's own icon, or None."""
    inner = jar_icon_path(path)
    if not inner:
        return None
    try:
        with zipfile.ZipFile(path) as z:
            info = z.getinfo(inner)
            if info.file_size > limit:
                return None
            data = z.read(inner)
    except (zipfile.BadZipFile, OSError, KeyError, RuntimeError):
        return None
    return data if data[:8] == b"\x89PNG\r\n\x1a\n" else None


# Which jar platforms load on which server.
LOADS_ON = {
    "fabric": {"fabric", "quilt"}, "quilt": {"quilt"}, "forge": {"forge"},
    "neoforge": {"neoforge"}, "paper": {"paper", "purpur", "folia"},
    "bukkit": {"paper", "purpur", "spigot"}, "bungeecord": {"bungeecord"},
    "velocity": {"velocity"},
}


def platform_problem(meta, prof):
    plats = (meta or {}).get("platforms") or ([meta["platform"]] if (meta or {}).get("platform") else [])
    if not plats:
        return ""
    if "broken" in plats:
        return "This file is not a valid jar - it will stop the server loading it."
    if any(prof["software"] in LOADS_ON.get(p, {prof["software"]}) for p in plats):
        return ""
    plat = plats[0]
    what = {"bukkit": "a Bukkit plugin", "paper": "a Paper plugin",
            "bungeecord": "a BungeeCord plugin", "velocity": "a Velocity plugin"}.get(
        plat, "a %s mod" % NAMES.get(plat, plat.title()))
    return "This is %s; this server runs %s, so it will not load." % (what, prof["label"])


# ---------------------------------------------------------------------------
#  installed
# ---------------------------------------------------------------------------

def installed(cwd, prof, identify=True):
    """What is in mods/ or plugins/ right now, with what is known about each."""
    if not prof.get("dir"):
        return {"items": [], "dir": "", "reason": prof.get("reason") or ""}
    folder = os.path.join(cwd, prof["dir"])
    with _lock(cwd):
        man = load_manifest(cwd)
        files = []
        try:
            entries = sorted(os.listdir(folder), key=str.lower)
        except OSError:
            entries = []
        for name in entries:
            full = os.path.join(folder, name)
            low = name.lower()
            if not os.path.isfile(full) or not (low.endswith(".jar") or low.endswith(".jar.disabled")):
                continue
            files.append(name)

        # Anything not in the manifest: ask Modrinth what it is, once.
        changed = False
        if identify:
            todo = {}
            for name in files:
                base = _base(name)
                if base in man["items"]:
                    continue
                sha = _sha1(os.path.join(folder, name))
                if sha in man["unknown"]:
                    continue
                todo[sha] = base
            if todo:
                try:
                    found = _call("%s/version_files" % MODRINTH_API,
                                  {"hashes": list(todo), "algorithm": "sha1"}, cache=False) or {}
                except ContentError:
                    found = None
                if found is not None:
                    pids = list({v.get("project_id") for v in found.values() if v.get("project_id")})
                    meta = _mr_meta(pids)
                    titles = {k: m["title"] for k, m in meta.items()}
                    for sha, base in todo.items():
                        v = found.get(sha)
                        if not v:
                            man["unknown"][sha] = int(time.time())
                            continue
                        man["items"][base] = {
                            "source": "modrinth", "project_id": v.get("project_id"),
                            "version_id": v.get("id"), "name": titles.get(v.get("project_id")) or base,
                            "version": v.get("version_number") or "", "kind": prof["kind"],
                            "game_versions": v.get("game_versions") or [],
                            "loaders": v.get("loaders") or [], "auto": False,
                            "requires": [d.get("project_id") for d in v.get("dependencies") or []
                                         if d.get("dependency_type") == "required" and d.get("project_id")],
                            "identified": True, "installed_at": 0,
                            "icon": (meta.get(v.get("project_id")) or {}).get("icon", ""),
                        }
                        changed = True
        # Entries recorded before icons were kept: look them up once.
        if identify:
            need = [e["project_id"] for e in man["items"].values()
                    if e.get("source") == "modrinth" and e.get("project_id") and "icon" not in e]
            if need:
                meta = _mr_meta(need)
                for e in man["items"].values():
                    if e.get("source") == "modrinth" and "icon" not in e and e.get("project_id") in meta:
                        e["icon"] = meta[e["project_id"]].get("icon") or ""
                        changed = True
        if changed:
            save_manifest(cwd, man)

        by_pid = {}
        for base, e in man["items"].items():
            if e.get("project_id"):
                by_pid[e["project_id"]] = e.get("name") or base
        present = {_base(n) for n in files}
        items = []
        for name in files:
            full = os.path.join(folder, name)
            base = _base(name)
            e = man["items"].get(base) or {}
            meta = jar_meta(full)
            problems, notes = [], []
            pp = platform_problem(meta, prof)
            if pp:
                problems.append(pp)
            if e.get("source") == "spigot":
                fits = tested_covers(e.get("game_versions") or [], prof["mc"])
                if fits is False:
                    notes.append("Its author lists tests on %s only." % _range(e["game_versions"]))
            elif e.get("game_versions") and prof["mc"] and prof["mc"] not in e["game_versions"]:
                problems.append("Built for Minecraft %s; this server is %s."
                                % (_range(e["game_versions"]), prof["mc"]))
            missing = []
            for req in e.get("requires") or []:
                if not any(x.get("project_id") == req and _present(x, b, present)
                           for b, x in man["items"].items()):
                    missing.append(req)
            if missing:
                names = _mr_titles(missing) if e.get("source") == "modrinth" else {}
                problems.append("Needs %s, which is not installed."
                                % ", ".join(str(names.get(m, m)) for m in missing))
            required_by = sorted({x.get("name") or b for b, x in man["items"].items()
                                  if e.get("project_id") and e["project_id"] in (x.get("requires") or [])
                                  and _present(x, b, present)})
            st = os.stat(full)
            items.append({
                "file": name, "base": base, "enabled": not name.lower().endswith(".disabled"),
                "name": e.get("name") or meta.get("name") or base[:-4],
                "version": e.get("version") or meta.get("version") or "",
                "source": e.get("source") or "", "project_id": e.get("project_id") or "",
                "version_id": e.get("version_id") or "", "auto": bool(e.get("auto")),
                "platform": meta.get("platform") or "", "size": st.st_size, "mtime": st.st_mtime,
                "problems": problems, "notes": notes, "required_by": required_by,
                "icon": e.get("icon") or "", "jar_icon": bool(jar_icon_path(full)),
                "installed_at": e.get("installed_at") or int(st.st_mtime),
                "url": _page(e) if e.get("source") else "",
            })
    return {"items": items, "dir": prof["dir"], "reason": ""}


def _present(entry, base, present):
    return base in present


def _page(e):
    if e.get("source") == "modrinth":
        return "%s/project/%s" % (MODRINTH_SITE, e.get("project_id"))
    if e.get("source") == "hangar":
        return "%s/%s" % (HANGAR_SITE, e.get("project_id"))
    if e.get("source") == "spigot":
        return "%s/resources/%s/" % (SPIGOT_SITE, e.get("project_id"))
    return ""


def _mr_meta(pids):
    """{project id: {"title", "icon"}} for a batch of Modrinth projects."""
    pids = [p for p in pids if p]
    if not pids:
        return {}
    try:
        got = _call("%s/projects?ids=%s" % (MODRINTH_API, _q(json.dumps(sorted(set(pids)))))) or []
    except ContentError:
        return {}
    return {p.get("id"): {"title": p.get("title"), "icon": p.get("icon_url") or ""} for p in got}


def _mr_titles(pids):
    return {k: v["title"] for k, v in _mr_meta(pids).items()}


# ---------------------------------------------------------------------------
#  planning an install: the thing, plus everything it needs
# ---------------------------------------------------------------------------

def plan(cwd, prof, wanted):
    """Work out exactly which files an install would put down.

    wanted is [{"source", "project_id", "version_id"?}]. Returns
    {"install": [...], "warnings": [...], "optional": [...], "blocked": [...]}.
    Each install entry is a concrete file with the reason it is there.
    Nothing is downloaded here.
    """
    if not prof.get("kind"):
        raise ContentError(prof.get("reason") or "This server takes no mods or plugins.")
    man = load_manifest(cwd) if cwd and os.path.isdir(cwd) else {"items": {}}
    have = {(e.get("source"), e.get("project_id")) for e in man["items"].values()}
    out = {"install": [], "warnings": [], "optional": [], "blocked": []}
    queued = {}
    optional_ids = {}
    # An update keeps what the old version was: a dependency stays one.
    updating = {(w.get("source") or "modrinth", w.get("project_id") or w.get("id"))
                for w in wanted if w.get("update")}

    def add(source, pid, version_id, reason, depth, requested):
        """Queue one project and what it needs. Returns its canonical id."""
        key = (source, pid)
        if key in queued:
            return queued[key]
        if not requested and key in have:
            return pid
        if depth > 6:
            out["warnings"].append("Stopped following dependencies of %s - the chain is too deep." % pid)
            return pid
        try:
            icon = ""
            if version_id and source == "modrinth":
                v = _mr_version(_call("%s/version/%s" % (MODRINTH_API, _q(version_id))), prof)
                meta = _mr_meta([v["project_id"]]).get(v["project_id"]) or {}
                name = meta.get("title") or str(pid)
                icon = meta.get("icon") or ""
            elif version_id:
                p = project(source, pid, prof)
                name, icon = p["name"], p.get("icon") or ""
                v = next((x for x in p["versions"] if str(x["id"]) == str(version_id)), None)
                if v is None:
                    raise ContentError("Version %s of %s is not offered any more." % (version_id, name))
            else:
                p, v = best_version(source, pid, prof)
                name, icon = p["name"], p.get("icon") or ""
                if v is None and source == "spigot" and p["versions"] and p["versions"][0]["file"]:
                    v = p["versions"][0]           # judged below, as a warning
                if v is None:
                    why = "; ".join((p["versions"][0]["problems"] if p["versions"] else [])
                                    ) or "it has no build for this server"
                    out["blocked"].append({"name": name, "source": source, "project_id": pid,
                                           "reason": "%s cannot be installed: %s" % (name, why),
                                           "requested": requested})
                    return pid
        except ContentError as e:
            out["blocked"].append({"name": str(pid), "source": source, "project_id": pid,
                                   "reason": str(e), "requested": requested})
            return pid
        # Hangar names a dependency by number and installs by slug; compare
        # on what the project calls itself so neither is fetched twice.
        canon_id = v.get("project_id") or pid
        canon = (source, canon_id)
        if canon in queued:
            queued[key] = canon_id
            return canon_id
        if not requested and canon in have:
            return canon_id
        hard = [x for x in v["problems"]
                if not (source == "spigot" and x.startswith("Only tested on"))]
        if hard or not v.get("file"):
            out["blocked"].append({"name": name, "source": source, "project_id": pid,
                                   "reason": "%s %s: %s" % (name, v["number"], " ".join(hard or v["problems"])),
                                   "requested": requested})
            return canon_id
        queued[key] = queued[canon] = canon_id
        if source == "spigot":
            # SpigotMC's tested list is the author's claim, not build metadata:
            # plugins tested on 1.17 routinely run on 1.21. So it warns, not blocks.
            if v["problems"]:
                out["warnings"].append("%s: its author has %s; it may still work on Minecraft %s."
                                       % (name, v["problems"][0][0].lower() + v["problems"][0][1:].rstrip("."),
                                          prof["mc"] or "this version"))
            elif v.get("unverified"):
                out["warnings"].append("%s: SpigotMC lists no tested versions, so Lodestone cannot "
                                       "check it supports Minecraft %s." % (name, prof["mc"] or "this version"))
            out["warnings"].append("%s: SpigotMC does not list dependencies - check its page "
                                   "for anything else it needs." % name)
        requires = []
        for d in v["deps"]:
            dtype = d.get("type")
            dep_pid = d.get("project_id")
            if dtype == "required":
                if source == "hangar" and d.get("external"):
                    out["warnings"].append("%s needs %s, which is hosted at %s - install it by hand."
                                           % (name, d.get("name"), d["external"]))
                    continue
                if not dep_pid and d.get("version_id") and source == "modrinth":
                    try:
                        dep_pid = _call("%s/version/%s" % (MODRINTH_API, _q(d["version_id"]))).get("project_id")
                    except ContentError:
                        dep_pid = None
                if not dep_pid:
                    if d.get("file_name"):
                        out["warnings"].append("%s needs a file called %s that it does not say where to "
                                               "get." % (name, d["file_name"]))
                    continue
                if source == "hangar" and not (source, dep_pid) in queued:
                    # Resolve the number to the slug the manifest records.
                    try:
                        slug = project("hangar", dep_pid, prof)["slug"]
                        if ("hangar", slug) in have:
                            requires.append(slug)
                            continue
                    except ContentError:
                        pass
                requires.append(add(source, dep_pid,
                                    d.get("version_id") if source == "modrinth" else None,
                                    "needed by %s" % name, depth + 1, False))
            elif dtype == "incompatible" and dep_pid and (source, dep_pid) in have:
                out["warnings"].append("%s says it does not work alongside an installed project (%s)."
                                       % (name, dep_pid))
            elif dtype == "optional" and dep_pid:
                optional_ids.setdefault((source, dep_pid), name)
        out["install"].append({
            "source": source, "project_id": canon_id, "name": name,
            "version_id": v["id"], "version": v["number"], "file": v["file"],
            "reason": reason, "auto": not requested, "requires": [str(r) for r in requires if r],
            "update": key in updating or canon in updating,
            "game_versions": v["game_versions"], "loaders": v["loaders"], "dir": prof["dir"],
            "icon": icon,
        })
        return canon_id

    for w in wanted:
        add(w.get("source") or "modrinth", w.get("project_id") or w.get("id"),
            w.get("version_id") or None, "requested", 0, True)

    # Optional extras worth mentioning, named rather than as bare ids.
    opt = [(s, p, by) for (s, p), by in optional_ids.items()
           if (s, p) not in queued and (s, p) not in have]
    titles = _mr_titles([p for s, p, _ in opt if s == "modrinth"])
    for s, p, by in opt[:12]:
        out["optional"].append({"source": s, "project_id": p, "name": titles.get(p) or p,
                                "for": by})
    # Requested things first, dependencies after, so a failure report reads naturally.
    out["install"].sort(key=lambda x: x["auto"])
    return out


# ---------------------------------------------------------------------------
#  doing it
# ---------------------------------------------------------------------------

def _download(url, dest, hashes, size, log):
    folder = os.path.dirname(dest)
    os.makedirs(folder, exist_ok=True)
    last = ""
    for attempt in range(1, RETRIES + 1):
        fd, tmp = tempfile.mkstemp(prefix=".lodestone-", suffix=".part", dir=folder)
        os.close(fd)
        try:
            algos = {k: hashlib.new(k) for k in hashes}
            got = 0
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r, open(tmp, "wb") as out:
                for chunk in iter(lambda: r.read(262144), b""):
                    out.write(chunk)
                    got += len(chunk)
                    for h in algos.values():
                        h.update(chunk)
            if size and got != size:
                raise ContentError("size mismatch (%d bytes, expected %d)" % (got, size))
            for k, want in hashes.items():
                if algos[k].hexdigest() != (want or "").lower():
                    raise ContentError("%s checksum mismatch" % k)
            try:
                with zipfile.ZipFile(tmp) as z:
                    z.namelist()
            except zipfile.BadZipFile:
                raise ContentError("the download is not a jar (the site may have sent a web page)")
            return tmp
        except urllib.error.HTTPError as e:
            last = "HTTP %d" % e.code
            if 400 <= e.code < 500 and e.code != 429:
                _rm(tmp)
                break
        except ContentError as e:
            last = str(e)
        except (urllib.error.URLError, OSError, ValueError) as e:
            last = str(getattr(e, "reason", None) or e)
        _rm(tmp)
        log("      attempt %d/%d failed: %s" % (attempt, RETRIES, last))
        time.sleep(min(2 * attempt, 5))
    raise ContentError(last or "download failed")


def _rm(p):
    try:
        if p and os.path.isfile(p):
            os.remove(p)
    except OSError:
        pass


def apply(cwd, prof, the_plan, log=None):
    """Download and place everything in a plan. Returns a report.

    Each file is verified before it replaces anything. An earlier version of
    the same project (by the manifest) is removed only after its replacement is
    safely in place, and only its jar - its config folder stays.
    """
    log = log or (lambda m: None)
    folder = os.path.join(cwd, prof["dir"])
    report = {"installed": [], "updated": [], "failed": [], "skipped": []}
    with _lock(cwd):
        man = load_manifest(cwd)
        for it in the_plan.get("install") or []:
            f = it.get("file") or {}
            name = it["name"]
            if not f.get("url"):
                report["failed"].append({"name": name, "error": "no downloadable file"})
                continue
            fname = os.path.basename(f.get("filename") or "") or _jarname(name, it.get("version"))
            if not fname.lower().endswith(".jar"):
                fname += ".jar"
            target = os.path.join(folder, fname)
            old = [b for b, e in man["items"].items()
                   if e.get("source") == it["source"] and e.get("project_id") == it["project_id"]]
            if fname in old and os.path.isfile(target) and \
                    man["items"][fname].get("version_id") == it["version_id"]:
                log("      %s %s is already installed" % (name, it["version"]))
                report["skipped"].append({"name": name, "version": it["version"]})
                continue
            log(">>> %s %s (%s)%s" % (name, it["version"], SOURCE_NAMES.get(it["source"], it["source"]),
                                      "" if it["reason"] == "requested" else " - " + it["reason"]))
            hashes = {k: v for k, v in (f.get("hashes") or {}).items() if v}
            try:
                tmp = _download(f["url"], target, hashes, f.get("size") or 0, log)
            except ContentError as e:
                log("      FAILED: %s" % e)
                report["failed"].append({"name": name, "error": str(e)})
                continue
            if os.path.exists(target):
                os.remove(target)
            os.replace(tmp, target)
            removed = []
            for b in old:
                if b == fname:
                    continue
                for cand in (b, b + ".disabled"):
                    p = os.path.join(folder, cand)
                    if os.path.isfile(p):
                        try:
                            os.remove(p)
                            removed.append(cand)
                        except OSError as e:
                            log("      could not remove the old %s: %s" % (cand, e))
                man["items"].pop(b, None)
            prev_auto = all(man["items"].get(b, {}).get("auto", True) for b in old) if old else True
            auto = prev_auto if (it.get("update") and old) else (bool(it.get("auto")) and prev_auto)
            man["items"][fname] = {
                "source": it["source"], "project_id": it["project_id"], "version_id": it["version_id"],
                "name": name, "version": it["version"], "kind": prof["kind"],
                "game_versions": it.get("game_versions") or [], "loaders": it.get("loaders") or [],
                # Something first pulled in as a dependency and later asked for
                # by name is the user's own choice from then on; an update
                # leaves that as it was.
                "auto": auto,
                "requires": it.get("requires") or [], "installed_at": int(time.time()),
                "icon": it.get("icon") or "",
            }
            save_manifest(cwd, man)
            if removed:
                log("      updated (replaced %s; config kept)" % ", ".join(removed))
                report["updated"].append({"name": name, "version": it["version"], "file": fname})
            else:
                log("      installed %s" % fname)
                report["installed"].append({"name": name, "version": it["version"], "file": fname})
    report["ok"] = not report["failed"]
    return report


def remove(cwd, prof, fname):
    """Delete one jar (its config folder stays). Returns who needed it."""
    folder = os.path.join(cwd, prof["dir"])
    base = _base(os.path.basename(fname))
    with _lock(cwd):
        man = load_manifest(cwd)
        e = man["items"].get(base) or {}
        gone = []
        for cand in (base, base + ".disabled"):
            p = os.path.join(folder, cand)
            if os.path.isfile(p):
                os.remove(p)
                gone.append(cand)
        if not gone:
            raise ContentError("%s is not in %s/." % (base, prof["dir"]))
        man["items"].pop(base, None)
        save_manifest(cwd, man)
        needed_by = sorted({x.get("name") or b for b, x in man["items"].items()
                            if e.get("project_id") and e["project_id"] in (x.get("requires") or [])})
    return {"removed": gone, "name": e.get("name") or base, "needed_by": needed_by}


def toggle(cwd, prof, fname):
    """Disable by renaming to .jar.disabled, the way server owners already do."""
    folder = os.path.join(cwd, prof["dir"])
    name = os.path.basename(fname)
    src = os.path.join(folder, name)
    if not os.path.isfile(src):
        raise ContentError("%s is not in %s/." % (name, prof["dir"]))
    new = name[:-9] if name.lower().endswith(".disabled") else name + ".disabled"
    os.replace(src, os.path.join(folder, new))
    return {"file": new, "enabled": not new.lower().endswith(".disabled")}


def updates(cwd, prof):
    """Newer compatible versions of what is installed: [{"file", "name", "current", "latest", ...}]."""
    listing = installed(cwd, prof)["items"]
    out, errors = [], []
    folder = os.path.join(cwd, prof["dir"])
    mr = {}
    for it in listing:
        if it["source"] == "modrinth":
            try:
                mr[_sha1(os.path.join(folder, it["file"]))] = it
            except OSError:
                pass
    if mr:
        body = {"hashes": list(mr), "algorithm": "sha1", "loaders": prof["loaders"]}
        if prof["mc"]:
            body["game_versions"] = [prof["mc"]]
        try:
            got = _call("%s/version_files/update" % MODRINTH_API, body, cache=False) or {}
            for sha, v in got.items():
                it = mr.get(sha)
                if it and v.get("id") and v.get("id") != it["version_id"]:
                    out.append({"file": it["file"], "name": it["name"], "source": "modrinth",
                                "project_id": it["project_id"], "current": it["version"],
                                "latest": v.get("version_number"), "version_id": v.get("id")})
        except ContentError as e:
            errors.append("Modrinth: %s" % e)
    for it in listing:
        if it["source"] in ("hangar", "spigot"):
            try:
                _, best = best_version(it["source"], it["project_id"], prof)
            except ContentError as e:
                errors.append("%s: %s" % (it["name"], e))
                continue
            if best and str(best["id"]) != str(it["version_id"]):
                out.append({"file": it["file"], "name": it["name"], "source": it["source"],
                            "project_id": it["project_id"], "current": it["version"],
                            "latest": best["number"], "version_id": best["id"]})
    return {"items": out, "errors": errors, "checked": len(listing)}
