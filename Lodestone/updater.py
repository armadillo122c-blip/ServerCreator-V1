"""Keeping Lodestone up to date from its GitHub repository.

Lodestone's source lives in a GitHub repo (in a `Lodestone/` folder of it), and
this is what brings a copy on someone's PC up to whatever is on the branch. It
is built around three questions, each answered carefully:

* **Is there anything new?** GitHub is asked for the newest commit that touched
  the Lodestone folder - not the newest commit to the repo, so a README change
  elsewhere does not count as an update. The installed version is a stamp file
  written by the last update, `.lodestone-version.json`.

* **Is it safe to put in place?** The repo is downloaded as one zip, only the
  Lodestone folder is taken out of it, and every Python file is compiled before
  anything is touched - an update with a syntax error is refused, not installed.
  Files are then swapped one at a time, and the ones replaced are copied aside
  first, so a failure half way puts everything back. Build output, logs and
  backups (`dist/`, `bin/`, `obj/`, ...) are never touched, and a file is only
  ever deleted if the previous update put it there.

* **Does it still start?** The new code takes effect when Lodestone restarts.
  `python updater.py --relaunch` is the restart: it waits for the old window and
  engine to close, rebuilds the desktop app if its source changed, starts
  Lodestone again, and if nothing answers on the panel's port within a minute
  and a half it puts the previous version back and starts that instead.

A private repository needs a login. Lodestone uses, in order: a token typed into
its settings, GITHUB_TOKEN / GH_TOKEN, the GitHub CLI's login, and finally the
login git itself already has (Git Credential Manager on Windows). The last two
are asked only for github.com, used only for these requests, and never written
anywhere.

If the folder is a git checkout of the repo, none of the zip machinery is used:
it is `git fetch` and a fast-forward merge, and local changes stop it.

Standard library only, like the rest of Lodestone.
"""

import fnmatch
import hashlib
import json
import os
import py_compile
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

API = "https://api.github.com"
UA = "Lodestone/1.0 (self-update)"
TIMEOUT = 30
STAMP = ".lodestone-version.json"
WORK = ".lodestone-update"
REBUILD_FLAG = "rebuild-desktop"
KEEP_BACKUPS = 3
HEALTH_SECONDS = 90         # how long a relaunched Lodestone gets to answer
CLOSE_SECONDS = 180         # how long the old one gets to close

DEFAULTS = {
    "enabled": True,            # check by itself
    "auto_install": True,       # download and put new files in place by itself
    "auto_restart": False,      # restart by itself when no server is running
    "repo": "armadillo122c-blip/CLAUDE",
    "branch": "main",
    "path": "Lodestone",        # the folder inside the repo that is Lodestone
    "hours": 6,                 # how often to check
    "token": "",                # optional; see the module docstring
    "use_git_login": True,      # borrow git's / the GitHub CLI's login
}

# Never written over or deleted by an update: what builds, runs and logs here.
PROTECTED = ("dist/*", "bin/*", "obj/*", "*/bin/*", "*/obj/*", "*/dist/*", WORK + "/*",
             ".git/*", "logs/*", "backups/*", "LATESTVERSIONEXE/*", "__pycache__/*",
             "*/__pycache__/*", "*.log", "*.bak*", "*.pyc", STAMP)


class UpdateError(Exception):
    """Something the user should be told, in a sentence."""


def settings(cfg):
    out = dict(DEFAULTS)
    out.update({k: v for k, v in (cfg or {}).items() if k in DEFAULTS})
    out["repo"] = (out["repo"] or "").strip().strip("/")
    out["path"] = (out["path"] or "").strip().strip("/")
    try:
        out["hours"] = max(1, min(168, int(out["hours"])))
    except (TypeError, ValueError):
        out["hours"] = DEFAULTS["hours"]
    return out


def protected(rel):
    rel = rel.replace("\\", "/")
    return any(fnmatch.fnmatch(rel, p) for p in PROTECTED)


# ---------------------------------------------------------------------------
#  logging in to GitHub
# ---------------------------------------------------------------------------

_auth_cache = {}


def _quiet_env():
    env = dict(os.environ)
    # never let a credential helper pop a window or wait on a terminal
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", GIT_ASKPASS="",
               SSH_ASKPASS="", GH_PROMPT_DISABLED="1")
    return env


def _run(argv, inp=None, timeout=20):
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        r = subprocess.run(argv, input=inp, capture_output=True, text=True, timeout=timeout,
                           env=_quiet_env(), creationflags=flags)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.SubprocessError):
        return 1, "", ""


def auth(cfg):
    """(token, where it came from). ("", "none") when there is no login."""
    s = settings(cfg)
    if s["token"]:
        return s["token"], "the token in Lodestone's settings"
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var], "the %s environment variable" % var
    if not s["use_git_login"]:
        return "", "none"
    if "found" in _auth_cache:
        return _auth_cache["found"]
    found = ("", "none")
    if shutil.which("gh"):
        code, out, _ = _run(["gh", "auth", "token", "--hostname", "github.com"])
        if code == 0 and out.strip():
            found = (out.strip(), "the GitHub CLI's login")
    if not found[0] and shutil.which("git"):
        code, out, _ = _run(["git", "credential", "fill"], "protocol=https\nhost=github.com\n\n")
        if code == 0:
            fields = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
            if fields.get("password"):
                found = (fields["password"], "git's GitHub login")
    _auth_cache["found"] = found
    return found


def forget_login():
    _auth_cache.clear()


def _request(url, token="", accept="application/vnd.github+json"):
    headers = {"User-Agent": UA, "Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
    if token and urllib.parse.urlparse(url).hostname in ("api.github.com", "codeload.github.com"):
        headers["Authorization"] = "Bearer " + token
    return urllib.request.Request(url, headers=headers)


def api(path, token="", timeout=TIMEOUT):
    url = path if path.startswith("http") else API + path
    try:
        with urllib.request.urlopen(_request(url, token), timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise UpdateError("GitHub says that repository or branch does not exist. If it is private, Lodestone needs "
                              "a GitHub login: sign in with git on this PC, or paste a token in Settings > Updates."
                              if not token else
                              "GitHub could not find that repository, branch or folder with the login Lodestone "
                              "has. Check the repository name, and that the login can see it.")
        if e.code == 401:
            raise UpdateError("GitHub refused the login (401). The token may have expired - paste a new one in "
                              "Settings > Updates, or sign in with git again.")
        if e.code == 403:
            reset = e.headers.get("X-RateLimit-Reset")
            if e.headers.get("X-RateLimit-Remaining") == "0" and reset:
                wait = max(1, int(int(reset) - time.time()) // 60)
                raise UpdateError("GitHub's rate limit is used up; it resets in about %d minutes." % wait)
            raise UpdateError("GitHub refused the request (403).")
        raise UpdateError("GitHub answered %d." % e.code)
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise UpdateError("Could not reach GitHub: %s" % (getattr(e, "reason", None) or e))


# ---------------------------------------------------------------------------
#  where this copy came from
# ---------------------------------------------------------------------------

def stamp(root):
    try:
        with open(os.path.join(root, STAMP), encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_stamp(root, data):
    tmp = os.path.join(root, STAMP + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, os.path.join(root, STAMP))


def git_root(root, repo):
    """The git checkout this folder is part of - only if its origin is the repo."""
    if not shutil.which("git"):
        return ""
    code, top, _ = _run(["git", "-C", root, "rev-parse", "--show-toplevel"])
    if code != 0 or not top.strip():
        return ""
    code, url, _ = _run(["git", "-C", root, "remote", "get-url", "origin"])
    want = (repo or "").lower().rstrip("/")
    got = re.sub(r"\.git$", "", url.strip().lower().rstrip("/"))
    if code != 0 or not want or not (got.endswith("/" + want) or got.endswith(":" + want)):
        return ""
    return os.path.normpath(top.strip())


# ---------------------------------------------------------------------------
#  is there anything new?
# ---------------------------------------------------------------------------

def _commit(c):
    cm = c.get("commit") or {}
    msg = (cm.get("message") or "").strip()
    return {"sha": c.get("sha") or "", "short": (c.get("sha") or "")[:7],
            "title": msg.splitlines()[0] if msg else "", "message": msg[:2000],
            "date": ((cm.get("committer") or {}).get("date") or (cm.get("author") or {}).get("date") or ""),
            "author": (cm.get("author") or {}).get("name") or (c.get("author") or {}).get("login") or ""}


def check(root, cfg):
    """What is installed, what is newest, and the commits in between.

    {"mode", "current", "latest", "behind", "commits", "available", "auth"}
    """
    s = settings(cfg)
    if not s["repo"] or "/" not in s["repo"]:
        raise UpdateError("Set the repository as owner/name in Settings > Updates.")
    groot = git_root(root, s["repo"])
    if groot:
        return _check_git(root, groot, s)
    token, source = auth(cfg)
    q = "?sha=%s&per_page=40" % urllib.parse.quote(s["branch"])
    if s["path"]:
        q += "&path=" + urllib.parse.quote(s["path"])
    commits = api("/repos/%s/commits%s" % (s["repo"], q), token)
    if not commits:
        raise UpdateError("The branch %s has no commits in %s/." % (s["branch"], s["path"] or "the repo"))
    latest = _commit(commits[0])
    have = stamp(root)
    newer = []
    for c in commits:
        if have.get("sha") and c.get("sha") == have["sha"]:
            break
        newer.append(_commit(c))
    known = bool(have.get("sha")) and any(c.get("sha") == have["sha"] for c in commits)
    current = ({"sha": have.get("sha"), "short": have.get("sha", "")[:7], "title": have.get("title", ""),
                "date": have.get("date", ""), "installed_at": have.get("installed_at", 0)}
               if have.get("sha") else None)
    available = not current or current["sha"] != latest["sha"]
    return {"mode": "download", "current": current, "latest": latest,
            # None: this copy has no stamp, or one older than the commits asked for.
            "behind": len(newer) if known else None,
            "commits": newer[:30], "available": available, "auth": source,
            "repo": s["repo"], "branch": s["branch"], "path": s["path"]}


def _check_git(root, groot, s):
    code, _, err = _run(["git", "-C", groot, "fetch", "--quiet", "origin", s["branch"]], timeout=120)
    if code != 0:
        raise UpdateError("git fetch failed: %s" % (err.strip().splitlines() or ["no output"])[-1])
    rel = os.path.relpath(root, groot).replace("\\", "/")
    spec = [] if rel in (".", "") else ["--", rel]

    def log(rng, n=40):
        code, out, _ = _run(["git", "-C", groot, "log", "-n", str(n), "--format=%H%x1f%s%x1f%cI%x1f%an", rng] + spec)
        items = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 4:
                items.append({"sha": parts[0], "short": parts[0][:7], "title": parts[1], "message": parts[1],
                              "date": parts[2], "author": parts[3]})
        return items

    head = log("HEAD", 1)
    newer = log("HEAD..origin/" + s["branch"])
    latest = log("origin/" + s["branch"], 1)
    return {"mode": "git", "current": head[0] if head else None, "latest": latest[0] if latest else None,
            "behind": len(newer), "commits": newer[:30], "available": bool(newer), "auth": "git",
            "repo": s["repo"], "branch": s["branch"], "path": rel if spec else ""}


# ---------------------------------------------------------------------------
#  fetching it
# ---------------------------------------------------------------------------

def download(root, cfg, sha, log=None, progress=None):
    """Download the repo at `sha` and unpack just the Lodestone folder.

    Returns the staging folder. Every .py file in it has been compiled.
    """
    log = log or (lambda m: None)
    s = settings(cfg)
    token, _ = auth(cfg)
    work = os.path.join(root, WORK)
    os.makedirs(work, exist_ok=True)
    zpath = os.path.join(work, "%s.zip" % sha[:12])
    url = "%s/repos/%s/zipball/%s" % (API, s["repo"], urllib.parse.quote(sha))
    log(">>> downloading %s@%s" % (s["repo"], sha[:7]))
    tmp = zpath + ".part"
    try:
        with urllib.request.urlopen(_request(url, token, "application/vnd.github+json"), timeout=120) as r, \
                open(tmp, "wb") as out:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            for chunk in iter(lambda: r.read(262144), b""):
                out.write(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
        os.replace(tmp, zpath)
    except urllib.error.HTTPError as e:
        _rm(tmp)
        raise UpdateError("Downloading the update failed: GitHub answered %d." % e.code)
    except (urllib.error.URLError, OSError) as e:
        _rm(tmp)
        raise UpdateError("Downloading the update failed: %s" % (getattr(e, "reason", None) or e))

    stage = os.path.join(work, "stage-" + sha[:12])
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    try:
        with zipfile.ZipFile(zpath) as z:
            names = z.namelist()
            if not names:
                raise UpdateError("The download is empty.")
            top = names[0].split("/")[0]
            if sha[:7].lower() not in top.lower():
                raise UpdateError("The download is not the commit that was asked for (%s)." % top)
            prefix = top + "/" + (s["path"] + "/" if s["path"] else "")
            count = 0
            for info in z.infolist():
                if not info.filename.startswith(prefix) or info.is_dir():
                    continue
                rel = info.filename[len(prefix):]
                if not rel or protected(rel):
                    continue
                dest = os.path.normpath(os.path.join(stage, rel))
                if not dest.startswith(os.path.abspath(stage) + os.sep):
                    raise UpdateError("The download contains an unsafe path: %s" % info.filename)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with z.open(info) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                count += 1
    except zipfile.BadZipFile:
        raise UpdateError("The download is not a valid zip.")
    finally:
        _rm(zpath)
    if not os.path.isfile(os.path.join(stage, "lodestone.py")):
        shutil.rmtree(stage, ignore_errors=True)
        raise UpdateError("The %s/ folder of that commit has no lodestone.py - check the folder in Settings > Updates."
                          % (s["path"] or "top"))
    # Refuse an update that would not even start.
    for dirpath, _, files in os.walk(stage):
        for f in files:
            if f.endswith(".py"):
                full = os.path.join(dirpath, f)
                try:
                    py_compile.compile(full, cfile=os.path.join(tempfile.gettempdir(), ".lodestone-check.pyc"),
                                       doraise=True)
                except py_compile.PyCompileError as e:
                    shutil.rmtree(stage, ignore_errors=True)
                    raise UpdateError("The update has a broken file (%s), so it was not installed: %s"
                                      % (os.path.relpath(full, stage), str(e).strip().splitlines()[-1]))
    log("      %d files, all Python compiles" % count)
    return stage


def _rm(p):
    try:
        if p and os.path.isfile(p):
            os.remove(p)
    except OSError:
        pass


def _hash(path):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(262144), b""):
            h.update(chunk)
    return h.hexdigest()


def _files(base):
    out = []
    for dirpath, dirs, files in os.walk(base):
        for f in files:
            out.append(os.path.relpath(os.path.join(dirpath, f), base).replace("\\", "/"))
    return sorted(out)


# ---------------------------------------------------------------------------
#  putting it in place
# ---------------------------------------------------------------------------

def apply(root, stage, latest, cfg, log=None):
    """Swap the staged files in. Returns a report; puts everything back on failure."""
    log = log or (lambda m: None)
    s = settings(cfg)
    old = stamp(root)
    new_files = [f for f in _files(stage) if not protected(f)]
    changed, added = [], []
    for rel in new_files:
        cur = os.path.join(root, rel)
        if not os.path.exists(cur):
            added.append(rel)
        elif _hash(cur) != _hash(os.path.join(stage, rel)):
            changed.append(rel)
    # Only what the last update installed may be deleted: anything else in the
    # folder is the user's.
    removed = [f for f in old.get("files") or []
               if f not in new_files and not protected(f) and os.path.isfile(os.path.join(root, f))]

    backup = os.path.join(root, WORK, "backup-%s-%d" % ((old.get("sha") or "unknown")[:7], int(time.time())))
    os.makedirs(backup, exist_ok=True)
    for rel in changed + removed:
        dest = os.path.join(backup, "files", rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(os.path.join(root, rel), dest)
    if old:
        shutil.copy2(os.path.join(root, STAMP), os.path.join(backup, STAMP))
    with open(os.path.join(backup, "undo.json"), "w", encoding="utf-8") as fh:
        json.dump({"restore": changed + removed, "delete": added, "had_stamp": bool(old),
                   "from": old.get("sha", ""), "to": latest["sha"]}, fh, indent=2)

    done = []
    try:
        for rel in changed + added:
            src = os.path.join(stage, rel)
            dest = os.path.join(root, rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            tmp = dest + ".lodestone-new"
            shutil.copy2(src, tmp)
            try:
                os.replace(tmp, dest)
            except OSError as e:
                _rm(tmp)
                raise UpdateError("%s is in use and could not be replaced (%s). Close whatever has it open and "
                                  "try again." % (rel, e.strerror or e))
            done.append(rel)
        for rel in removed:
            os.remove(os.path.join(root, rel))
            done.append(rel)
    except Exception as e:
        log("!!! %s - putting the previous version back" % e)
        rollback(root, backup)
        raise e if isinstance(e, UpdateError) else UpdateError(str(e))

    _write_stamp(root, {"sha": latest["sha"], "title": latest.get("title", ""), "date": latest.get("date", ""),
                        "repo": s["repo"], "branch": s["branch"], "path": s["path"],
                        "installed_at": int(time.time()), "files": new_files})
    touched = changed + added + removed
    desktop = any(f.startswith("desktop/") for f in touched)
    if desktop:
        # The desktop window is compiled; Lodestone.vbs and --relaunch rebuild it.
        open(os.path.join(root, WORK, REBUILD_FLAG), "w").close()
    _prune(root)
    shutil.rmtree(stage, ignore_errors=True)
    log(">>> updated to %s: %d changed, %d new, %d removed%s"
        % (latest["sha"][:7], len(changed), len(added), len(removed),
           " (the desktop app will be rebuilt on restart)" if desktop else ""))
    return {"sha": latest["sha"], "changed": changed, "added": added, "removed": removed,
            "desktop": desktop, "backup": backup, "engine": any(f.endswith(".py") for f in touched)}


def rollback(root, backup):
    """Put back what an update replaced, and take away what it added."""
    with open(os.path.join(backup, "undo.json"), encoding="utf-8") as fh:
        undo = json.load(fh)
    for rel in undo.get("delete") or []:
        _rm(os.path.join(root, rel))
    for rel in undo.get("restore") or []:
        src = os.path.join(backup, "files", rel)
        dest = os.path.join(root, rel)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copy2(src, dest)
    if undo.get("had_stamp") and os.path.isfile(os.path.join(backup, STAMP)):
        shutil.copy2(os.path.join(backup, STAMP), os.path.join(root, STAMP))
    elif not undo.get("had_stamp"):
        _rm(os.path.join(root, STAMP))
    return undo


def latest_backup(root):
    work = os.path.join(root, WORK)
    try:
        items = [os.path.join(work, d) for d in os.listdir(work)
                 if d.startswith("backup-") and os.path.isfile(os.path.join(work, d, "undo.json"))]
    except OSError:
        return ""
    return max(items, key=os.path.getmtime) if items else ""


def _prune(root):
    work = os.path.join(root, WORK)
    items = sorted((os.path.join(work, d) for d in os.listdir(work) if d.startswith("backup-")),
                   key=os.path.getmtime, reverse=True)
    for old in items[KEEP_BACKUPS:]:
        shutil.rmtree(old, ignore_errors=True)


def apply_git(root, cfg, log=None):
    """A git checkout: fast-forward only, and never over local changes."""
    log = log or (lambda m: None)
    s = settings(cfg)
    groot = git_root(root, s["repo"])
    code, dirty, _ = _run(["git", "-C", groot, "status", "--porcelain", "--untracked-files=no"])
    if dirty.strip():
        raise UpdateError("This folder is a git checkout with local changes; commit or stash them, then update.")
    code, before, _ = _run(["git", "-C", groot, "rev-parse", "HEAD"])
    code, _, err = _run(["git", "-C", groot, "merge", "--ff-only", "origin/" + s["branch"]], timeout=120)
    if code != 0:
        raise UpdateError("git could not fast-forward: %s" % (err.strip().splitlines() or ["no output"])[-1])
    code, after, _ = _run(["git", "-C", groot, "rev-parse", "HEAD"])
    rel = os.path.relpath(root, groot).replace("\\", "/")
    spec = [] if rel in (".", "") else ["--", rel]
    code, names, _ = _run(["git", "-C", groot, "diff", "--name-only", before.strip(), after.strip()] + spec)
    touched = [n[len(rel) + 1:] if spec else n for n in names.split()]
    desktop = any(f.startswith("desktop/") for f in touched)
    if desktop:
        os.makedirs(os.path.join(root, WORK), exist_ok=True)
        open(os.path.join(root, WORK, REBUILD_FLAG), "w").close()
    log(">>> git: fast-forwarded to %s (%d files)" % (after.strip()[:7], len(touched)))
    return {"sha": after.strip(), "changed": touched, "added": [], "removed": [], "desktop": desktop,
            "backup": "", "engine": any(f.endswith(".py") for f in touched), "git_from": before.strip()}


# ---------------------------------------------------------------------------
#  the restart
# ---------------------------------------------------------------------------

def pid_alive(pid):
    """Whether a process exists - without ever signalling it.

    On Windows os.kill() terminates processes, so it must not be used to ask.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)            # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259                # STILL_ACTIVE
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    # An exited process nobody has reaped yet still "exists"; it is gone all the same.
    try:
        with open("/proc/%d/stat" % pid) as fh:
            return fh.read().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def port_answers(port, timeout=1.0):
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def panel_answers(port):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/me" % int(port), timeout=3) as r:
            return b"Lodestone" in r.read()
    except (OSError, ValueError):
        return False


def find_dotnet():
    exe = shutil.which("dotnet")
    if exe:
        return exe
    for p in (os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "dotnet", "dotnet.exe"),
              "/usr/local/share/dotnet/dotnet", "/usr/share/dotnet/dotnet",
              os.path.expanduser("~/.dotnet/dotnet")):
        if os.path.isfile(p):
            return p
    return ""


def rebuild_desktop(root, log):
    """Rebuild the desktop window the way this PC builds it (desktop/build.py --dll)."""
    flag = os.path.join(root, WORK, REBUILD_FLAG)
    build = os.path.join(root, "desktop", "build.py")
    if not os.path.isfile(flag):
        return True
    if not os.path.isfile(build):
        _rm(flag)
        return True
    if not find_dotnet():
        log("desktop app not rebuilt: the .NET 8 SDK is not installed. The engine is updated; the window "
            "keeps its previous build until it can be rebuilt.")
        return False
    log("rebuilding the desktop app (desktop/build.py --dll)")
    env = dict(os.environ)
    env["PATH"] = os.path.dirname(find_dotnet()) + os.pathsep + env.get("PATH", "")
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    py = sys.executable
    # pythonw has no console, so anything it starts (dotnet) would open a
    # window of its own; console Python under CREATE_NO_WINDOW keeps it hidden.
    if os.name == "nt" and py.lower().endswith("pythonw.exe"):
        cand = py[:-len("pythonw.exe")] + "python.exe"
        py = cand if os.path.isfile(cand) else py
    try:
        r = subprocess.run([py, build, "--dll"], cwd=root, capture_output=True, text=True,
                           timeout=1200, env=env, creationflags=flags)
    except (OSError, subprocess.SubprocessError) as e:
        log("desktop build could not run: %s" % e)
        return False
    for line in (r.stdout + r.stderr).splitlines()[-25:]:
        log("  | " + line)
    if r.returncode == 0:
        _rm(flag)
        log("desktop app rebuilt")
        return True
    log("desktop build failed (exit %s); the previous window build is kept" % r.returncode)
    return False


def launch(root, ui, log, port=0):
    """Start Lodestone the way the user does, on the port it had."""
    flags = 0
    if os.name == "nt":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    vbs = os.path.join(root, "Lodestone.vbs")
    try:
        if os.name == "nt" and ui != "browser" and os.path.isfile(vbs):
            subprocess.Popen(["wscript.exe", vbs], cwd=root, creationflags=flags, close_fds=True)
            log("started Lodestone.vbs")
            return
        py = sys.executable
        if os.name == "nt":
            pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
            py = pyw if os.path.isfile(pyw) else py
        subprocess.Popen([py, os.path.join(root, "lodestone.py")] + ([str(port)] if port else []),
                         cwd=root, creationflags=flags,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, start_new_session=(os.name != "nt"))
        log("started lodestone.py")
    except OSError as e:
        log("could not start Lodestone: %s" % e)


def relaunch(root, wait_pids, port, backup="", ui="desktop", log_path=""):
    """Run detached by the engine: finish an update and start Lodestone again."""
    log_path = log_path or os.path.join(os.path.expanduser("~"), ".lodestone", "update.log")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)

    def log(msg):
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))

    log("restart after update: waiting for %s to close" % (", ".join(map(str, wait_pids)) or "nothing"))
    deadline = time.time() + CLOSE_SECONDS
    while time.time() < deadline and (any(pid_alive(p) for p in wait_pids) or port_answers(port)):
        time.sleep(0.5)
    if any(pid_alive(p) for p in wait_pids):
        log("the old Lodestone did not close in time; not starting a second one")
        return 1
    rebuild_desktop(root, log)
    launch(root, ui, log, port)
    deadline = time.time() + HEALTH_SECONDS
    while time.time() < deadline:
        if panel_answers(port):
            log("Lodestone is up on port %d" % port)
            return 0
        time.sleep(1)
    if backup and os.path.isdir(backup):
        log("the updated Lodestone did not answer within %d s - putting the previous version back" % HEALTH_SECONDS)
        try:
            rollback(root, backup)
            launch(root, ui, log, port)
            log("previous version restored and started")
        except Exception as e:                                  # pragma: no cover
            log("could not restore the previous version: %s" % e)
        return 2
    log("Lodestone did not answer within %d s; see the engine log" % HEALTH_SECONDS)
    return 2


def spawn_relaunch(root, wait_pids, port, backup="", ui="desktop"):
    """Start `updater.py --relaunch` detached, so it outlives the engine."""
    argv = [sys.executable, os.path.join(root, "updater.py"), "--relaunch", "--root", root,
            "--port", str(port), "--wait", ",".join(str(p) for p in wait_pids if p), "--ui", ui]
    if backup:
        argv += ["--backup", backup]
    kw = {"cwd": root, "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
          "stderr": subprocess.DEVNULL, "close_fds": True}
    if os.name == "nt":
        kw["creationflags"] = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                               | subprocess.CREATE_NO_WINDOW)
    else:
        kw["start_new_session"] = True
    subprocess.Popen(argv, **kw)


def _main(argv):
    if "--relaunch" not in argv:
        print(__doc__)
        return 0

    def arg(name, default=""):
        return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else default

    root = arg("--root", os.path.dirname(os.path.abspath(__file__)))
    pids = [p for p in arg("--wait").split(",") if p.strip()]
    return relaunch(root, pids, int(arg("--port", "8744") or 8744), arg("--backup"), arg("--ui", "desktop"))


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
