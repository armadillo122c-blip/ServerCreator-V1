"""Grab the Minehut dashboard login from Firefox and give it to Lodestone.

Minehut has no official API and no way to log in from a script, so Lodestone uses the two values the dashboard keeps in
the browser (minehut_auth_token and minehut_session_id - see lodestone.py's Minehut section). They expire; run this
again whenever Lodestone's Minehut actions start failing with 401.

What it does:
  1. finds the Firefox profile that has Local Storage for a minehut.com page (log in at dashboard.minehut.com first),
  2. reads the two values from a COPY of that storage (Firefox may have the real file open),
  3. hands them to the running Lodestone (POST /api/servers/save, only the minehut group, so nothing else changes) for
     every Minehut server it knows, switches Minehut on for the ones with a server id, and runs Lodestone's own
     Minehut test on them.
The values are never printed, logged or sent anywhere but Lodestone on this PC (127.0.0.1). If Lodestone isn't running
they're written into ~/.lodestone.json directly (where Lodestone keeps them anyway).

  py minehut_login.py            do it
  py minehut_login.py --check    only say whether both values were found (and how long they are)
  env FIREFOX_PROFILES=<folder>  look in another profiles folder (for testing)
"""
import glob
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request

API = os.environ.get("LODESTONE_API", "http://127.0.0.1:8744")
KEYS = ("minehut_auth_token", "minehut_session_id")


# ---------------------------------------------------------------- Firefox Local Storage

def profiles_dir():
    return os.environ.get("FIREFOX_PROFILES") or os.path.join(os.environ.get("APPDATA", ""), "Mozilla", "Firefox", "Profiles")


def snappy_decompress(buf):
    """Raw snappy (Firefox compresses bigger Local Storage values with it): a varint length, then literals and copies."""
    pos, shift, length = 0, 0, 0
    while True:
        b = buf[pos]
        pos += 1
        length |= (b & 0x7F) << shift
        if b < 0x80:
            break
        shift += 7
    out = bytearray()
    while pos < len(buf):
        tag = buf[pos]
        pos += 1
        kind = tag & 3
        if kind == 0:                                       # literal
            n = tag >> 2
            if n >= 60:
                extra = n - 59
                n = int.from_bytes(buf[pos:pos + extra], "little")
                pos += extra
            n += 1
            out += buf[pos:pos + n]
            pos += n
            continue
        if kind == 1:                                       # copy, 1-byte offset
            n = ((tag >> 2) & 7) + 4
            off = ((tag >> 5) << 8) | buf[pos]
            pos += 1
        elif kind == 2:                                     # copy, 2-byte offset
            n = (tag >> 2) + 1
            off = int.from_bytes(buf[pos:pos + 2], "little")
            pos += 2
        else:                                               # copy, 4-byte offset
            n = (tag >> 2) + 1
            off = int.from_bytes(buf[pos:pos + 4], "little")
            pos += 4
        for _ in range(n):
            out.append(out[-off])
    if len(out) != length:
        raise ValueError("snappy: got %d bytes, expected %d" % (len(out), length))
    return bytes(out)


def text_of(raw, compressed):
    if isinstance(raw, str):
        v = raw
    else:
        data = snappy_decompress(raw) if compressed else raw
        v = data.decode("utf-16-le") if data.count(b"\x00") > len(data) // 4 else data.decode("utf-8")
    v = v.strip()
    # the dashboard may keep them JSON-encoded ("...")
    if len(v) > 1 and v[0] == '"' and v[-1] == '"':
        try:
            v = json.loads(v)
        except ValueError:
            v = v[1:-1]
    return v


def copy_db(path):
    """a copy of the SQLite file and its journal files (Firefox can hold the real ones open)"""
    tmp = tempfile.mkdtemp(prefix="mh_ls_")
    dst = os.path.join(tmp, "data.sqlite")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            shutil.copyfile(path + suffix, dst + suffix)
    return tmp, dst


def read_lsng(path):
    """Firefox's Local Storage (storage/default/<origin>/ls/data.sqlite)"""
    tmp, db = copy_db(path)
    try:
        con = sqlite3.connect(db)
        cols = [r[1] for r in con.execute("PRAGMA table_info(data)")]
        has_comp = "compression_type" in cols
        q = "SELECT key, value%s FROM data" % (", compression_type" if has_comp else "")
        found = {}
        for row in con.execute(q):
            if row[0] in KEYS:
                found[row[0]] = text_of(row[1], has_comp and row[2] == 1)
        con.close()
        return found
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def read_old(path):
    """older Firefox: webappsstore.sqlite in the profile, origins reversed ("moc.tuhenim.draobhsad.:https:443")"""
    tmp, db = copy_db(path)
    try:
        con = sqlite3.connect(db)
        found = {}
        for key, value, origin in con.execute("SELECT key, value, originKey FROM webappsstore2"):
            if key in KEYS and "tuhenim" in origin:
                found[key] = text_of(value, False)
        con.close()
        return found
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def find_login():
    """the newest pair of values in any Firefox profile; (values, where) or ({}, reason)"""
    base = profiles_dir()
    if not os.path.isdir(base):
        return {}, "no Firefox profiles folder at " + base
    best, best_time, where = {}, -1, ""
    for db in glob.glob(os.path.join(base, "*", "storage", "default", "*minehut.com*", "ls", "data.sqlite")):
        try:
            got = read_lsng(db)
        except (sqlite3.Error, OSError, ValueError, UnicodeDecodeError) as e:
            print("  (couldn't read %s: %s)" % (db, e))
            continue
        t = os.path.getmtime(db)
        if len(got) > len(best) or (len(got) == len(best) and t > best_time):
            best, best_time, where = got, t, db
    if len(best) < 2:
        for db in glob.glob(os.path.join(base, "*", "webappsstore.sqlite")):
            try:
                got = read_old(db)
            except (sqlite3.Error, OSError) as e:
                continue
            if len(got) > len(best):
                best, where = got, db
    if not best:
        if new_login(base):
            return {}, ("Minehut's dashboard no longer keeps minehut_auth_token / minehut_session_id - it switched to a "
                        "secure login cookie (Auth.js, __Secure-authjs.session-token) that pages can't read. Lodestone's "
                        "Minehut connection needs updating for that; until then start / stop / restart from the dashboard.")
        return {}, "no minehut.com login found - log in at https://dashboard.minehut.com in Firefox, then run this again"
    return best, where


def new_login(base):
    """is the new Auth.js login there (the cookie's NAME only - never its value)?"""
    for db in glob.glob(os.path.join(base, "*", "cookies.sqlite")):
        try:
            tmp, copy = copy_db(db)
            con = sqlite3.connect(copy)
            n = con.execute("SELECT count(*) FROM moz_cookies WHERE host LIKE '%minehut.com' AND name LIKE '%authjs.session-token%'").fetchone()[0]
            con.close()
            shutil.rmtree(tmp, ignore_errors=True)
            if n:
                return True
        except (sqlite3.Error, OSError):
            continue
    return False


# ---------------------------------------------------------------- Lodestone

def api(path, body=None, timeout=60):
    req = urllib.request.Request(API + path, None if body is None else json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def minehut_id(s):
    """the server id Lodestone can use for an entry ("" if it isn't a Minehut one)"""
    mh = s.get("minehut") or {}
    sid = (mh.get("server_id") or "").strip().rstrip("/").split("/")[-1]
    sf = s.get("sftp") or {}
    if not sid and "minehut.com" in (sf.get("host") or ""):
        sid = (sf.get("user") or "").strip()
    return sid


def give_to_lodestone(token, session):
    try:
        items = api("/api/servers")["items"]
    except (urllib.error.URLError, OSError):
        return False
    done = []
    for it in items:
        try:
            s = api("/api/servers/get?id=" + it["id"])
            s = s.get("server", s)
        except (urllib.error.URLError, OSError, ValueError):
            continue
        sid = minehut_id(s)
        if not sid:
            continue
        mh = {"enabled": True, "server_id": (s.get("minehut") or {}).get("server_id") or sid, "token": token, "session_id": session}
        api("/api/servers/save", {"id": it["id"], "minehut": mh})
        done.append(it)
    for it in done:
        try:
            r = api("/api/minehut/test", {"id": it["id"]}, timeout=90)
            ok = r.get("ok")
            msg = "works" if ok else (r.get("error") or r.get("message") or "failed")
        except urllib.error.HTTPError as e:
            ok, msg = False, e.read().decode("utf-8", "replace")[:160]
        except (urllib.error.URLError, OSError) as e:
            ok, msg = False, str(e)
        print("  %s %s - Minehut test: %s" % ("OK  " if ok else "FAIL", it["name"], msg))
    if not done:
        print("  Lodestone has no server with a Minehut server id - add it under the server's Minehut section first.")
    return True


def write_config(token, session):
    """Lodestone isn't running: straight into its config"""
    p = os.path.expanduser("~/.lodestone.json")
    cfg = json.load(open(p, encoding="utf-8"))
    n = 0
    for s in cfg.get("servers", []):
        sid = minehut_id(s)
        if sid:
            mh = s.setdefault("minehut", {})
            mh.update({"enabled": True, "token": token, "session_id": session})
            mh["server_id"] = mh.get("server_id") or sid
            n += 1
    shutil.copyfile(p, p + ".bak-minehut-login")
    tmp = p + ".tmp"
    json.dump(cfg, open(tmp, "w", encoding="utf-8"), indent=1)
    os.replace(tmp, p)
    print("  Lodestone isn't running - saved into ~/.lodestone.json for %d server(s) (it picks them up on its next start)." % n)


def main():
    print("Minehut login -> Lodestone")
    got, where = find_login()
    if len(got) < 2:
        missing = [k for k in KEYS if k not in got]
        print("  " + (where if not got else "found only some of it, missing: " + ", ".join(missing)))
        if got:
            print("  Log in at https://dashboard.minehut.com in Firefox (open the dashboard once), then run this again.")
        return 1
    token, session = got[KEYS[0]], got[KEYS[1]]
    print("  found both in Firefox (%s): token %d chars, session id %d chars" % (os.path.basename(os.path.dirname(os.path.dirname(where))),
                                                                          len(token), len(session)))
    if "--check" in sys.argv:
        return 0
    if not give_to_lodestone(token, session):
        write_config(token, session)
    return 0


if __name__ == "__main__":
    code = main()
    if "--no-pause" not in sys.argv and sys.stdin and sys.stdin.isatty():
        input("\nPress Enter to close.")
    sys.exit(code)
