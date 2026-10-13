#!/usr/bin/env python3
"""
Lodestone — one panel for every Minecraft server you run
--------------------------------------------------------
A single-file local web app that controls Minecraft servers wherever they
live: a JAR on this machine, or a hosted box you only reach over SFTP.
Both kinds sit on the same shelf and open in the same UI.

  * Console      live stdout for local servers, live log tail for remote ones,
                 with a command line that talks over stdin or RCON.
  * Files        browse / edit / upload / download / zip, local or remote.
  * Properties   server.properties as a real form.
  * Backups      zipped worlds, on a schedule, restorable.
  * Players      who is online, with op / kick / ban / whitelist.
  * Automation   restarts, broadcasts, clear-lag, timed commands, macros.
  * Discord      many webhooks, each subscribed to the events you pick —
                 chat relay, joins, deaths, crashes, backups, console errors.
  * REST API     token-authenticated /api/v1 for scripts and other tools.

Standard library only for local servers. Remote servers need paramiko
(pip install paramiko) — the Servers tab has a one-click installer.

SECURITY
  * The UI has no login. Whoever can open the address can use the panel.
  * Binds to 127.0.0.1 by default. LAN access is a switch in Settings; there
    is still no login, so only turn it on for a network you trust.
  * The REST API is separate and *is* token-authenticated. Tokens are shown
    once, then stored hashed. Turning the API on does not give the UI a login.
  * SFTP and RCON passwords are stored in plain text in ~/.lodestone.json,
    chmod 600 where the OS supports it (a no-op on Windows).
  * This is a self-hosted tool, not a hardened public service. Do not expose
    it to the open internet without a reverse proxy and real auth.

Run:   py lodestone.py            (or: py lodestone.py 8744)
"""

import base64
import gzip
import hashlib
import hmac
import json
import os
import posixpath
import re
import secrets
import shutil
import socket
import stat as statmod
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
import zipfile
from collections import deque
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import paramiko
    HAVE_PARAMIKO = True
except Exception:
    paramiko = None
    HAVE_PARAMIKO = False

# The networking package makes a local server reachable from the internet:
# UPnP / NAT-PMP / PCP port mapping, public address detection, CGNAT detection,
# a real external reachability test and the Windows Firewall rule. Standard
# library only, but kept out of this file because it is a self-contained layer.
# If it is missing the panel still runs — Local and LAN hosting need none of it.
try:
    import networking as netmod
    HAVE_NET = True
    NET_ERROR = ""
except Exception as _net_err:                                   # pragma: no cover
    netmod = None
    HAVE_NET = False
    NET_ERROR = "The networking package could not be loaded: %s" % _net_err

# The Via plugins (ViaVersion and friends) let clients on other Minecraft
# versions connect. Installing them is a small self-contained job - work out
# whether the server takes Bukkit plugins at all, then fetch and verify three
# jars - so it lives in its own module for the same reason the networking layer
# does. Missing it costs only that one button.
try:
    import viainstaller as viamod
    HAVE_VIA = True
    VIA_ERROR = ""
except Exception as _via_err:                                   # pragma: no cover
    viamod = None
    HAVE_VIA = False
    VIA_ERROR = "The Via installer could not be loaded: %s" % _via_err

# Creating a server - which software runs which Minecraft version, fetching it,
# and finding or fetching a Java to run it on - and the mod and plugin browser
# are two more self-contained jobs that talk to outside services, so they live
# beside the Via installer for the same reason. Missing either costs only the
# feature it provides.
try:
    import creator as createmod
    HAVE_CREATE = True
    CREATE_ERROR = ""
except Exception as _create_err:                                # pragma: no cover
    createmod = None
    HAVE_CREATE = False
    CREATE_ERROR = "The server creator could not be loaded: %s" % _create_err

try:
    import content as contentmod
    HAVE_CONTENT = True
    CONTENT_ERROR = ""
except Exception as _content_err:                               # pragma: no cover
    contentmod = None
    HAVE_CONTENT = False
    CONTENT_ERROR = "The mod and plugin browser could not be loaded: %s" % _content_err

# Pixel art for each kind of server, rendered to PNG on request. Purely
# decorative: without it the UIs fall back to their plain icons.
try:
    import art as artmod
    HAVE_ART = True
except Exception:                                               # pragma: no cover
    artmod = None
    HAVE_ART = False

# Minecraft's own item and block textures, read out of the official client jar
# the first time they are wanted. Every instance icon is drawn from them.
try:
    import textures as texmod
    HAVE_TEX = True
except Exception:                                               # pragma: no cover
    texmod = None
    HAVE_TEX = False

# Self-update from the GitHub repository Lodestone's source lives in. Its own
# module because it also runs on its own: `updater.py --relaunch` is what
# finishes an update once this process has quit.
try:
    import updater as updmod
    HAVE_UPDATER = True
    UPDATER_ERROR = ""
except Exception as _upd_err:                                   # pragma: no cover
    updmod = None
    HAVE_UPDATER = False
    UPDATER_ERROR = "The updater could not be loaded: %s" % _upd_err

# The folder this engine runs from - what an update replaces.
APP_ROOT = os.path.dirname(os.path.abspath(__file__))

# Finding memory to give a modded server, by showing what is holding it. Kept
# separate because it is the one part of the panel that can close somebody
# else's application, and that deserves its own file to argue with.
try:
    import ramtools as rammod
    HAVE_RAM = True
    RAM_ERROR = ""
except Exception as _ram_err:                                   # pragma: no cover
    rammod = None
    HAVE_RAM = False
    RAM_ERROR = "The memory tools could not be loaded: %s" % _ram_err

# The development assistant (a local model working inside a project through
# tools), the checks it and the UI share, and the performance numbers. Each is
# self-contained; if one fails to load, the rest of the panel still works.
try:
    import agent as agentmod
    import devtools as devmod
    import ollama as ollamamod
    import skripttools as skmod
    HAVE_DEV = True
    DEV_ERROR = ""
except Exception as _dev_err:                                   # pragma: no cover
    agentmod = devmod = ollamamod = skmod = None
    HAVE_DEV = False
    DEV_ERROR = "The development assistant could not be loaded: %s" % _dev_err

try:
    import perfstats as perfmod
    HAVE_PERF = True
except Exception:                                               # pragma: no cover
    perfmod = None
    HAVE_PERF = False

APP = "Lodestone"
VERSION = "1.0.0"
CONFIG_PATH = Path.home() / ".lodestone.json"
DEFAULT_PORT = 8744
LOG_PATH = "logs/latest.log"
MAX_EDIT_BYTES = 3_000_000
PRESENCE_DIR = ".lodestone/presence"
PRESENCE_STALE = 45

# -- log line shapes ---------------------------------------------------------
JOIN_RE = re.compile(r"]:\s+([A-Za-z0-9_]{2,16})\s+joined the game")
LEAVE_RE = re.compile(r"]:\s+([A-Za-z0-9_]{2,16})\s+left the game")
# Chat is the awkward one. Vanilla logs `<Name> message`, but Paper logs chat
# from an "Async Chat Thread" and any formatting plugin (or a Skript that cancels
# the chat event and re-broadcasts) rewrites the line into whatever it likes:
#   [Async Chat Thread - #2/INFO]: Armadillo122: heya
#   [Async Chat Thread - #0/INFO]: <#BF0000>O<#C70D09>W...R <#E03325>Armadillo122: fuh
# So: recognise the thread, strip colour codes, split on the separator, and take
# the last name-shaped token before it. See parse_chat().
CHAT_RE = re.compile(r"]:\s+<([A-Za-z0-9_]{2,16})>\s+(.*)$")   # vanilla only; kept for reference
CHAT_THREAD_RE = re.compile(r"chat thread", re.I)
LINE_RE = re.compile(r"^\[[^\]]*\]\s*\[([^\]]*?)/(\w+)\]:?\s?(.*)$", re.S)
VANILLA_CHAT_RE = re.compile(r"^<([A-Za-z0-9_]{2,16})>\s*(.*)$", re.S)
NAME_RE = re.compile(r"[A-Za-z0-9_]{3,16}")
TOKEN_SPLIT_RE = re.compile(r"[\s\[\]{}()<>|,]+")
CHAT_SEPS = ("\u00bb", "\u203a", "\u2192", "\u27a4", "\u25b8", ":", ">")
NOT_CHAT_RE = re.compile(r"^(?:\[?[A-Za-z]+\]?\s*)?(?:issued server command|lost connection|"
                         r"joined the game|left the game)\b", re.I)
ADVANCE_RE = re.compile(
    r"]:\s+([A-Za-z0-9_]{2,16})\s+has (?:made the advancement|completed the challenge|"
    r"reached the goal)\s+\[(.+?)]")
DEATH_RE = re.compile(
    r"]:\s+([A-Za-z0-9_]{2,16})\s+((?:was|were|died|drowned|blew|hit|fell|burned|walked|"
    r"went|starved|suffocated|withered|froze|discovered|experienced|got|left|tried)\b.*)$")
ERROR_RE = re.compile(r"[/\[](ERROR|SEVERE|FATAL)]", re.I)
WARN_RE = re.compile(r"[/\[]WARN(?:ING)?]", re.I)
DONE_RE = re.compile(r"Done \(([\d.]+)s\)! For help")
STOP_RE = re.compile(r"Stopping the server|Stopping server")

BOOL_KEYS = {
    "pvp", "online-mode", "white-list", "spawn-npcs", "spawn-animals",
    "spawn-monsters", "allow-flight", "allow-nether", "enable-command-block",
    "hardcore", "force-gamemode", "generate-structures", "enable-rcon",
    "enable-query", "enforce-whitelist", "prevent-proxy-connections",
    "broadcast-console-to-ops", "broadcast-rcon-to-ops", "sync-chunk-writes",
    "enable-jmx-monitoring", "enable-status", "hide-online-players",
    "require-resource-pack", "log-ips", "accepts-transfers",
}
ENUM_KEYS = {
    "gamemode": ["survival", "creative", "adventure", "spectator"],
    "difficulty": ["peaceful", "easy", "normal", "hard"],
    "level-type": ["minecraft:normal", "minecraft:flat", "minecraft:large_biomes",
                   "minecraft:amplified", "minecraft:single_biome_surface"],
}
# The development assistant's settings. Context, threads and GPU layers are
# Ollama's own options; blank means "Ollama decides". num_ctx is set rather
# than left blank because Ollama's default (4096) is too small for a model
# that reads files and gets tool definitions, and every request must ask for
# the same value or Ollama reloads the model.
DEV_DEFAULTS = {
    "base_url": "http://localhost:11434", "model": "", "num_ctx": 8192, "temperature": 0.2,
    "keep_alive": "", "num_thread": "", "num_gpu": "", "think": False, "tool_mode": {},
    "max_parallel": 1, "max_steps": 24, "command_timeout": 900,
    "auto_approve_edits": False, "allowed_commands": [], "trusted_commands": [],
    "index_enabled": True, "perf_mode": False,
    # How far the assistant reaches. All off by default; the user turns them on
    # in AI Assistant > Model & resources. approval_mode: ask | edits | all.
    "access": "workspace", "shell_enabled": False, "approval_mode": "ask",
    "web_enabled": False, "web_provider": "none", "ollama_api_key": "", "searxng_url": "",
}
DEV_SECRETS = ("ollama_api_key",)

TEXT_EXTS = {"", ".txt", ".properties", ".json", ".yml", ".yaml", ".log", ".sh",
             ".bat", ".cmd", ".md", ".cfg", ".conf", ".ini", ".toml", ".csv",
             ".sk", ".js", ".css", ".html", ".xml", ".mcmeta", ".lang", ".env"}

# ---------------------------------------------------------------------------
#  small helpers
# ---------------------------------------------------------------------------


def fmt_size(n):
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%d %s" % (n, unit)) if unit == "B" else ("%.1f %s" % (n, unit))
        n /= 1024.0
    return "—"


def fmt_dur(secs):
    secs = int(secs or 0)
    if secs < 60:
        return "%ds" % secs
    if secs < 3600:
        return "%dm %ds" % (secs // 60, secs % 60)
    if secs < 86400:
        return "%dh %dm" % (secs // 3600, (secs % 3600) // 60)
    return "%dd %dh" % (secs // 86400, (secs % 86400) // 3600)


def looks_text(raw):
    if b"\x00" in raw[:8000]:
        return False
    try:
        raw[:8000].decode("utf-8")
        return True
    except UnicodeDecodeError:
        return False


def safe_name(name):
    name = (name or "").strip().replace("\\", "/").split("/")[-1]
    return "" if name in ("", ".", "..") else name


def safe_rel(path):
    """Normalise a browser-supplied relative path; refuse to escape the root."""
    parts = []
    for chunk in (path or "").replace("\\", "/").split("/"):
        if chunk in ("", "."):
            continue
        if chunk == "..":
            if parts:
                parts.pop()
            continue
        parts.append(chunk)
    return "/".join(parts)


def local_ip():
    s = None
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        if s:
            try:
                s.close()
            except OSError:
                pass


def chmod_600(p):
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def is_dir_attr(attr):
    try:
        return statmod.S_ISDIR(attr.st_mode)
    except (AttributeError, TypeError):
        return False


SECTION_HEX_RE = re.compile("§x(?:§[0-9a-fA-F]){6}")
AMP_HEX_RE = re.compile(r"&x(?:&[0-9a-fA-F]){6}")
HEX_TAG_RE = re.compile(r"<#[0-9a-fA-F]{3,8}>")
MM_TAG_RE = re.compile(r"</?(?:color|c|gradient|rainbow|bold|b|italic|i|em|underlined|u|"
                       r"strikethrough|st|obfuscated|obf|reset|r|hover|click|font|key|lang|tr|"
                       r"newline|br|shadow_color|transition|score|selector|insert|nbt|pride)"
                       r"(?::[^<>]*)?>", re.I)
MM_NAMED_RE = re.compile(r"</?(?:black|dark_blue|dark_green|dark_aqua|dark_red|dark_purple|gold|"
                         r"gray|grey|dark_gray|dark_grey|blue|green|aqua|red|light_purple|yellow|"
                         r"white)>", re.I)
SECTION_RE = re.compile("§[0-9a-fk-orA-FK-ORx]")
AMP_RE = re.compile(r"&(?:[0-9a-fk-orA-FK-ORx]|#[0-9a-fA-F]{6})")
CODE_RES = (SECTION_HEX_RE, AMP_HEX_RE, HEX_TAG_RE, MM_TAG_RE, MM_NAMED_RE, SECTION_RE, AMP_RE)


def strip_codes(text, repl=""):
    """Drop colour markup so Discord reads cleanly: section signs, ampersand
    codes, both hex spellings, and MiniMessage tags."""
    t = text or ""
    for pat in CODE_RES:
        t = pat.sub(repl, t)
    return t


def split_log_line(line):
    """-> (thread, level, body); thread and level are empty if it is not a
    standard `[time] [thread/LEVEL]: body` line."""
    m = LINE_RE.match(line or "")
    return (m.group(1), m.group(2), m.group(3)) if m else ("", "", line or "")


def parse_chat(line, custom=None):
    """Pull (player, message) out of a log line, or return None.

    A custom pattern set on the server wins outright. Otherwise: the vanilla
    <Name> shape, then anything logged on Paper's async chat thread, whatever
    a plugin reformatted it into.
    """
    thread, _level, raw = split_log_line(line)

    if custom is not None:
        m = custom.search(strip_codes(line))
        if not m:
            return None
        gd = m.groupdict()
        who = gd.get("player") or (m.group(1) if m.lastindex else "")
        msg = gd.get("message") or (m.group(2) if (m.lastindex or 0) > 1 else "")
        return (who.strip(), msg.strip()) if who else None

    clean = strip_codes(raw).strip()
    if not clean:
        return None

    m = VANILLA_CHAT_RE.match(clean)
    if m:
        return m.group(1), m.group(2).strip()

    if not (thread and CHAT_THREAD_RE.search(thread)):
        return None
    if NOT_CHAT_RE.match(clean):
        return None

    # Colour codes double as word boundaries — a rank can be glued to the name
    # with only a code between them — so keep a copy where they became spaces.
    spaced = strip_codes(raw, " ")
    for sep in CHAT_SEPS:
        i, j = clean.find(sep), spaced.find(sep)
        if i <= 0 or j <= 0:
            continue
        message = clean[i + len(sep):].strip()
        for tok in reversed([t for t in TOKEN_SPLIT_RE.split(spaced[:j]) if t]):
            if NAME_RE.fullmatch(tok):
                return tok, message
        return None
    return None


def new_id():
    return uuid.uuid4().hex[:12]


class Conflict(Exception):
    """Raised when a file changed underneath an open editor."""

    def __init__(self, msg, disk_mtime=None):
        super().__init__(msg)
        self.disk_mtime = disk_mtime


# ---------------------------------------------------------------------------
#  RCON  (stdlib — this is what lets remote servers take commands, not just
#  file edits, and lets the panel drive a local server it did not start)
# ---------------------------------------------------------------------------

RCON_AUTH = 3
RCON_EXEC = 2


class RconError(Exception):
    pass


class Rcon:
    """Minimal synchronous RCON client. A fresh connection per burst keeps
    the state simple and survives the server restarting underneath us."""

    def __init__(self, host, port, password, timeout=6):
        self.host, self.port = host, int(port or 25575)
        self.password, self.timeout = password or "", timeout
        self.sock = None
        self._id = 0

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    def connect(self):
        try:
            self.sock = socket.create_connection((self.host, self.port), self.timeout)
            self.sock.settimeout(self.timeout)
        except OSError as e:
            raise RconError("Cannot reach RCON at %s:%s (%s)" % (self.host, self.port, e))
        rid = self._send(RCON_AUTH, self.password)
        got, _ = self._recv()
        if got == -1 or got != rid:
            self.close()
            raise RconError("RCON password rejected.")

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _send(self, kind, payload):
        self._id += 1
        body = struct.pack("<ii", self._id, kind) + payload.encode("utf-8") + b"\x00\x00"
        self.sock.sendall(struct.pack("<i", len(body)) + body)
        return self._id

    def _read_exactly(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RconError("RCON connection closed early.")
            buf += chunk
        return buf

    def _recv(self):
        (length,) = struct.unpack("<i", self._read_exactly(4))
        if length < 10 or length > 4_200_000:
            raise RconError("RCON sent a malformed packet.")
        data = self._read_exactly(length)
        rid, _kind = struct.unpack("<ii", data[:8])
        return rid, data[8:-2].decode("utf-8", "replace")

    def command(self, cmd):
        if not self.sock:
            raise RconError("Not connected.")
        rid = self._send(RCON_EXEC, cmd)
        out = []
        while True:
            try:
                got, text = self._recv()
            except (socket.timeout, OSError, RconError):
                break
            if got == rid:
                out.append(text)
            if len(text) < 4000:
                break
            self.sock.settimeout(0.35)
        return strip_codes("".join(out)).strip()

    @staticmethod
    def run(host, port, password, cmd, timeout=6):
        with Rcon(host, port, password, timeout) as r:
            return r.command(cmd)


# ---------------------------------------------------------------------------
#  Minehut — the host API, so a hosted server is as controllable as a local one
# ---------------------------------------------------------------------------

MINEHUT_BASE = "https://api.minehut.com"


class MinehutError(Exception):
    pass


class Minehut:
    """Thin client over the API Minehut's own dashboard uses.

    Unofficial and undocumented by Minehut, so treat every response as
    best-effort: failures come back as MinehutError with something readable
    rather than propagating a urllib traceback into the UI.
    """

    def __init__(self, cfg):
        self.cfg = cfg or {}

    # -- plumbing ----------------------------------------------------------
    def has_login(self):
        return bool((self.cfg.get("token") or "").strip() and (self.cfg.get("session_id") or "").strip())

    @staticmethod
    def parse_id(raw):
        """the server id from what people paste: the id itself, or the dashboard URL
        (https://dashboard.minehut.com/servers/<id>) - a URL used to go into the API path as it was"""
        raw = (raw or "").strip().rstrip("/")
        if "/" in raw:
            raw = raw.split("?")[0].split("#")[0].rstrip("/").split("/")[-1]
        return raw

    def _headers(self, public=False):
        if public:
            # Minehut's server info is public (server lists read it): no login needed
            return {"Accept": "application/json", "User-Agent": "%s/%s" % (APP, VERSION)}
        tok = (self.cfg.get("token") or "").strip()
        sid = (self.cfg.get("session_id") or "").strip()
        if not tok or not sid:
            raise MinehutError("Minehut token and session id are not set — see the Servers tab.")
        return {"authorization": tok, "x-session-id": sid,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "%s/%s" % (APP, VERSION)}

    def server_id(self):
        sid = self.parse_id(self.cfg.get("server_id"))
        if not sid:
            raise MinehutError("No Minehut server id set.")
        return sid

    def _call(self, method, path, body=None, timeout=20, public=False):
        url = MINEHUT_BASE + path
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers=self._headers(public), method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read().decode("utf-8", "replace")
            return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            if e.code in (401, 403):
                raise MinehutError("Minehut rejected the credentials (%s). They expire — grab a "
                                   "fresh token and session id from the dashboard." % e.code)
            if e.code == 404:
                raise MinehutError("Minehut has no server with that id (404). Check the server id.")
            if e.code == 429:
                raise MinehutError("Minehut is rate limiting this account. Wait a moment.")
            raise MinehutError("Minehut returned %s: %s" % (e.code, detail or e.reason))
        except urllib.error.URLError as e:
            raise MinehutError("Could not reach Minehut (%s)." % e.reason)
        except json.JSONDecodeError:
            raise MinehutError("Minehut sent something that was not JSON — the API may have changed.")

    # -- reads -------------------------------------------------------------
    def server(self, strict=False):
        """the server's info: logged in when there is a login, else Minehut's public read of the same thing (the
        dashboard's cookie login since 2026 leaves many setups without a token - status still works). strict: the
        login must work (Test connection checks the login itself)."""
        path = "/server/" + self.server_id()
        if strict or self.has_login():
            try:
                data = self._call("GET", path)
                return data.get("server") or data
            except MinehutError as e:
                if strict or "rejected the credentials" not in str(e):
                    raise
        data = self._call("GET", path, public=True)
        return data.get("server") or data

    def status(self, strict=False):
        """Normalised, so the UI does not have to know Minehut's field names."""
        srv = self.server(strict)
        online = bool(srv.get("online"))
        service = bool(srv.get("service_online") or srv.get("serviceOnline"))
        return {
            "ok": True,
            "online": online,
            "service_online": service,
            "hibernating": (not service) and (not online),
            "starting": service and not online,
            "name": srv.get("name") or "",
            "players": srv.get("playerCount", srv.get("player_count", 0)) or 0,
            "max_players": srv.get("maxPlayers", srv.get("max_players", 0)) or 0,
            "motd": srv.get("motd") or "",
            "plan": srv.get("server_plan") or srv.get("serverPlan") or "",
            "version": srv.get("server_version") or srv.get("serverVersion") or "",
            "suspended": bool(srv.get("suspended")),
            "visibility": bool(srv.get("visibility")),
            "port": srv.get("port"),
            "backup_slots": srv.get("backup_slots") or srv.get("backupSlots"),
            "last_online": srv.get("last_online") or srv.get("lastOnline"),
            "address": (srv.get("name") or "").lower() + ".minehut.gg" if srv.get("name") else "",
        }

    def backups(self):
        data = self._call("GET", "/v1/server/%s/backups" % self.server_id())
        return data.get("backups") or []

    # -- writes ------------------------------------------------------------
    def wake(self):
        return self._call("POST", "/server/%s/start_service" % self.server_id(), {})

    def start(self):
        """Minehut hibernates idle servers, so a cold start is two steps: bring
        the service up, then start the Minecraft server on it."""
        st = self.status()
        if st["online"]:
            return False, "Already online."
        if not st["service_online"]:
            self.wake()
            time.sleep(3)
        self._call("POST", "/server/%s/start" % self.server_id(), {})
        return True, "Starting on Minehut."

    def stop(self):
        self._call("POST", "/server/%s/shutdown" % self.server_id(), {})
        return True, "Stopping on Minehut."

    def restart(self):
        self._call("POST", "/server/%s/restart" % self.server_id(), {})
        return True, "Restarting on Minehut."

    def hibernate(self):
        self._call("POST", "/server/%s/destroy_service" % self.server_id(), {})
        return True, "Hibernating on Minehut."

    def save(self):
        self._call("POST", "/server/%s/save" % self.server_id(), {})
        return True, "World saved."

    def command(self, cmd):
        self._call("POST", "/server/%s/command" % self.server_id(), {"command": cmd})
        return True, ""

    def set_property(self, field, value):
        self._call("POST", "/server/%s/edit_server_properties" % self.server_id(),
                   {"field": field, "value": value})
        return True, "%s set." % field

    def test(self):
        try:
            st = self.status(strict=True)
        except MinehutError as e:
            return False, str(e)
        return True, "Connected to %s — %s, %d/%d players%s." % (
            st["name"] or "the server",
            "online" if st["online"] else ("starting" if st["starting"] else
                                           ("hibernating" if st["hibernating"] else "offline")),
            st["players"], st["max_players"],
            (", " + st["plan"]) if st["plan"] else "")


# ---------------------------------------------------------------------------
#  Discord — many webhooks, each subscribed to the events it cares about
# ---------------------------------------------------------------------------

EVENTS = [
    ("chat",        "Chat",              "Every in-game chat message"),
    ("join_leave",  "Joins & leaves",    "Players connecting and disconnecting"),
    ("first_join",  "First-time players", "Someone joining for the very first time"),
    ("death",       "Deaths",            "Death messages, with the cause"),
    ("advancement", "Advancements",      "Advancements, challenges and goals"),
    ("start_stop",  "Start & stop",      "Server coming up and going down cleanly"),
    ("crash",       "Crashes",           "Unexpected exits and auto-restarts"),
    ("lag",         "Lag warnings",      "Cannot keep up / overloaded / low TPS"),
    ("backup",      "Backups",           "Backup finished, failed or restored"),
    ("console",     "Console errors",    "ERROR / SEVERE lines from the log"),
    ("punish",      "Bans & kicks",      "Punishments seen in the server log"),
    ("ingame_cmd",  "Player commands",   "Commands players run in-game"),
    ("command",     "Panel commands",    "Commands run from this panel or RCON"),
    ("player_act",  "Panel moderation",  "Op, kick, ban and whitelist done here"),
    ("files",       "File changes",      "Edits, uploads and deletes via the panel"),
    ("schedule",    "Scheduled tasks",   "Automation firing on a timer"),
]
EVENT_KEYS = [e[0] for e in EVENTS]

COLORS = {
    "chat": 0x5865F2, "join_leave": 0x22C55E, "first_join": 0x4ADE80,
    "death": 0xEF4444, "advancement": 0xFCB83C, "start_stop": 0x22C55E,
    "crash": 0xEF4444, "lag": 0xFCB83C, "backup": 0x4CD7E0, "console": 0xFCB83C,
    "punish": 0xEF4444, "ingame_cmd": 0x8A8A98, "command": 0xB388FF,
    "player_act": 0xB388FF, "files": 0x8A8A98, "schedule": 0x4CD7E0,
}

# things worth spotting in the log that no plugin-independent event gives us
PUNISH_RE = re.compile(
    r"(?:^|\]:\s*)(?:\[?\w*\]?\s*)?(?:PUNISHMENT\s*\W\s*)?"
    r"([A-Za-z0-9_]{3,16})\s+(?:was|has been)\s+(banned|kicked|muted|warned|unbanned|unmuted)"
    r"(?:\s+by\s+([A-Za-z0-9_]{3,16}|Console))?(?:\s+for\s+(.*?))?\.?$", re.I)
VANILLA_PUNISH_RE = re.compile(r"]:\s+(Banned|Kicked|Unbanned)\s+([A-Za-z0-9_]{3,16})\s*:?\s*(.*)$")
INGAME_CMD_RE = re.compile(r"]:\s+([A-Za-z0-9_]{3,16})\s+issued server command:\s+(.*)$")
LAG_RE = re.compile(r"Can't keep up!|Running \d+ms or \d+ ticks behind|Overloaded", re.I)
FIRST_JOIN_RE = re.compile(r"]:\s+([A-Za-z0-9_]{3,16})\[/[\d.:]+\] logged in with entity id")
WHITELIST_RE = re.compile(r"]:\s+(Added|Removed)\s+([A-Za-z0-9_]{3,16})\s+(?:to|from) the whitelist")


class Discord:
    """Fan-out to any number of webhooks. Each hook picks its own events, so
    chat can go to #chat while crashes go to #staff. Posts are queued on one
    worker thread so a slow or rate-limited hook never blocks the server."""

    def __init__(self, panel):
        self.panel = panel
        self.q = deque(maxlen=500)
        self.chat_buf = {}
        self.wake = threading.Event()
        self.sent = 0
        self.failed = 0
        self.last_error = ""
        threading.Thread(target=self._worker, daemon=True).start()

    # -- config ------------------------------------------------------------
    def hooks(self):
        return self.panel.server().setdefault("discord", {}).setdefault("hooks", [])

    @staticmethod
    def blank_hook():
        return {"id": new_id(), "name": "New webhook", "url": "", "enabled": True,
                "events": ["join_leave", "start_stop", "crash"],
                "username": APP, "avatar": "", "embeds": True, "mention": "",
                "thread_id": "",          # post into an existing thread
                "chat_style": "player",   # player = post as them; compact = batched lines
                "status_msg": False,      # keep one live status message, edited in place
                "status_id": "",          # the message we are editing
                "attach_log": True,       # send a log slice with crash reports
                "sent": 0, "failed": 0, "last_error": ""}

    # -- emitting ----------------------------------------------------------
    def event(self, kind, title, desc="", player="", fields=None, force_hook=None):
        """Queue one event to every hook subscribed to `kind`."""
        targets = [force_hook] if force_hook else [
            h for h in self.hooks()
            if h.get("enabled") and h.get("url") and kind in (h.get("events") or [])
        ]
        if not targets:
            return
        for h in targets:
            self.q.append((h, kind, title, desc, player, fields or []))
        self.wake.set()

    def chat(self, player, text):
        """Chat posts as the player, with their head as the avatar, so a relayed
        channel reads like a chat log instead of a bot feed. Hooks set to
        compact style get batched lines instead."""
        clean = strip_codes(text)
        for h in self.hooks():
            if not (h.get("enabled") and h.get("url") and "chat" in (h.get("events") or [])):
                continue
            if h.get("chat_style") == "compact":
                buf = self.chat_buf.setdefault(h.get("id") or id(h),
                                               {"hook": h, "lines": [], "first": time.time()})
                buf["hook"] = h
                if not buf["lines"]:
                    buf["first"] = time.time()
                buf["lines"].append("**%s** %s" % (player, clean[:400]))
            else:
                self.q.append((h, "chat", "", clean, player, []))
        self.wake.set()

    # -- live status message ----------------------------------------------
    def status_tick(self):
        """Keep one message per subscribed hook up to date, editing it rather
        than posting again. Discord shows it like a little dashboard."""
        hooks = [h for h in self.hooks()
                 if h.get("enabled") and h.get("url") and h.get("status_msg")]
        if not hooks:
            return
        try:
            st = self.panel.status()
        except Exception:
            return
        running = st.get("running") or st.get("transport", {}).get("state") == "online"
        players = st.get("players", [])
        embed = {
            "title": st["server"]["name"],
            "color": 0x22C55E if running else 0x8A8A98,
            "fields": [
                {"name": "State", "value": ("Running" if st.get("running") else st.get("state", "-")),
                 "inline": True},
                {"name": "Players", "value": "%d" % st.get("player_count", 0), "inline": True},
                {"name": "Uptime", "value": st.get("uptime_h") or "-", "inline": True},
            ],
            "description": ("Online: " + ", ".join(players)) if players else "Nobody online.",
            "footer": {"text": "updated by %s" % APP},
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        body = {"embeds": [embed], "allowed_mentions": {"parse": []}}
        for h in hooks:
            payload = dict(body, username=h.get("username") or APP)
            if h.get("avatar"):
                payload["avatar_url"] = h["avatar"]
            mid = h.get("status_id")
            if mid:
                ok, _msg = self._post(self._url_for(h, message_id=mid), payload, method="PATCH")
                if ok:
                    continue
                h["status_id"] = ""          # message was deleted; make a new one
            got = self._post_get(self._url_for(h, wait=True), payload)
            if got:
                h["status_id"] = str(got.get("id") or "")
                self.panel.save()

    # -- url helpers -------------------------------------------------------
    @staticmethod
    def _url_for(hook, wait=False, message_id=None):
        url = (hook.get("url") or "").strip().rstrip("/")
        if not url:
            return ""
        if message_id:
            url += "/messages/" + str(message_id)
        params = []
        if hook.get("thread_id") and not message_id:
            params.append("thread_id=" + str(hook["thread_id"]).strip())
        if hook.get("thread_id") and message_id:
            params.append("thread_id=" + str(hook["thread_id"]).strip())
        if wait:
            params.append("wait=true")
        return url + ("?" + "&".join(params) if params else "")

    # -- delivery ----------------------------------------------------------
    def _payload(self, hook, kind, title, desc, player, fields):
        srv = self.panel.server().get("name") or "server"
        if kind == "chat":
            return {
                "username": "%s — %s" % (player, srv),
                "avatar_url": "https://minotar.net/helm/%s/64.png" % player,
                "content": (desc or "")[:1900] or "​",
                "allowed_mentions": {"parse": []},
            }
        body = {"username": hook.get("username") or APP,
                "allowed_mentions": {"parse": ["roles", "users"]}}
        if hook.get("avatar"):
            body["avatar_url"] = hook["avatar"]
        mention = (hook.get("mention") or "").strip()
        if mention and kind in ("crash", "console"):
            body["content"] = mention
        if not hook.get("embeds", True):
            line = title if not desc else "%s — %s" % (title, desc)
            body["content"] = ((body.get("content", "") + " ") if body.get("content") else "") + line[:1900]
            return body
        embed = {
            "title": (title or "")[:250] or None,
            "description": (strip_codes(desc) or "")[:3900] or None,
            "color": COLORS.get(kind, 0x22C55E),
            "footer": {"text": "%s · %s" % (srv, APP)},
            "timestamp": datetime.utcnow().isoformat() + "Z",
        }
        if player:
            embed["author"] = {"name": player,
                               "icon_url": "https://minotar.net/helm/%s/32.png" % player}
        if fields:
            embed["fields"] = [{"name": str(f[0])[:250], "value": str(f[1])[:1000],
                                "inline": bool(f[2]) if len(f) > 2 else True}
                               for f in fields[:12]]
        body["embeds"] = [embed]
        return body

    def _worker(self):
        while True:
            if not self.q:
                self._flush_chat()
                self.wake.wait(1.0)
                self.wake.clear()
                continue
            hook, kind, title, desc, player, fields = self.q.popleft()
            payload = self._payload(hook, kind, title, desc, player, fields)
            files = None
            if kind == "crash" and hook.get("attach_log", True):
                files = self._log_slice()
            ok, msg = self._post(self._url_for(hook), payload, files=files)
            hook["sent"] = int(hook.get("sent", 0)) + (1 if ok else 0)
            if not ok:
                hook["failed"] = int(hook.get("failed", 0)) + 1
                hook["last_error"] = msg
            # a busy queue would otherwise hammer the webhook
            if self.q:
                time.sleep(0.25)

    def _log_slice(self, lines=120):
        """The tail of the console, as a real file attachment — a crash ping is
        far more useful with the stack trace already in the channel."""
        try:
            text = "\n".join(t for _s, t in self.panel.recent(lines))
            if not text.strip():
                return None
            return [("console.log", text.encode("utf-8", "replace")[-200000:], "text/plain")]
        except Exception:
            return None

    # -- chat batching -----------------------------------------------------
    def _flush_chat(self):
        """Compact-style hooks coalesce a burst of chat into one message rather
        than one request per line, which is what actually trips the rate limit
        on a busy server."""
        now = time.time()
        for key, buf in list(self.chat_buf.items()):
            hook, lines, first = buf["hook"], buf["lines"], buf["first"]
            if not lines:
                continue
            if now - first < 2.0 and len(lines) < 12:
                continue
            self.chat_buf.pop(key, None)
            body = {"username": (hook.get("username") or APP) + " · chat",
                    "content": "\n".join(lines)[:1900],
                    "allowed_mentions": {"parse": []}}
            if hook.get("avatar"):
                body["avatar_url"] = hook["avatar"]
            ok, msg = self._post(self._url_for(hook), body)
            hook["sent"] = int(hook.get("sent", 0)) + (1 if ok else 0)
            if not ok:
                hook["failed"] = int(hook.get("failed", 0)) + 1
                hook["last_error"] = msg

    def _post(self, url, payload, _retry=0, method="POST", files=None):
        if not url:
            return False, "No webhook URL."
        try:
            if files:
                body, ctype = self._multipart(payload, files)
            else:
                body, ctype = json.dumps(payload).encode(), "application/json"
            req = urllib.request.Request(
                url, data=body, method=method,
                headers={"Content-Type": ctype,
                         "User-Agent": "%s/%s" % (APP, VERSION)})
            with urllib.request.urlopen(req, timeout=15) as r:
                r.read()
            self.sent += 1
            return True, "sent"
        except urllib.error.HTTPError as e:
            if e.code == 429 and _retry < 3:
                wait = 2.0
                try:
                    wait = min(15.0, float(json.loads(e.read().decode()).get("retry_after", 2)))
                except Exception:
                    pass
                time.sleep(wait)
                return self._post(url, payload, _retry + 1, method, files)
            self.failed += 1
            self.last_error = "Discord said %s" % e.code
            return False, self.last_error
        except Exception as e:
            self.failed += 1
            self.last_error = str(e)
            return False, self.last_error

    @staticmethod
    def _multipart(payload, files):
        """Discord takes attachments as multipart, with the JSON in a part
        called payload_json."""
        boundary = "----lodestone" + uuid.uuid4().hex
        out = []
        def part(headers, data):
            out.append(("--" + boundary + "\r\n" + headers + "\r\n\r\n").encode())
            out.append(data if isinstance(data, bytes) else str(data).encode())
            out.append(b"\r\n")
        part('Content-Disposition: form-data; name="payload_json"\r\n'
             "Content-Type: application/json", json.dumps(payload).encode())
        for i, (name, data, ctype) in enumerate(files):
            part('Content-Disposition: form-data; name="files[%d]"; filename="%s"\r\n'
                 "Content-Type: %s" % (i, name, ctype), data)
        out.append(("--" + boundary + "--\r\n").encode())
        return b"".join(out), "multipart/form-data; boundary=" + boundary

    def _post_get(self, url, payload):
        """POST with ?wait=true and hand back the created message object."""
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json",
                         "User-Agent": "%s/%s" % (APP, VERSION)})
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode() or "{}")
        except Exception as e:
            self.failed += 1
            self.last_error = str(e)
            return None

    def test(self, hook):
        """Send one probe straight through, bypassing the queue, so the button
        can report a real result instead of 'queued'."""
        payload = self._payload(
            hook, "start_stop", "Webhook connected",
            "%s is wired up to this channel. Subscribed events: %s"
            % (APP, ", ".join(hook.get("events") or []) or "none yet"), "",
            [("Server", self.panel.server().get("name") or "—", True),
             ("Panel", "%s %s" % (APP, VERSION), True)])
        return self._post(hook.get("url", ""), payload)

    def stats(self):
        return {"queued": len(self.q) + sum(len(b["lines"]) for b in self.chat_buf.values()),
                "sent": self.sent, "failed": self.failed, "last_error": self.last_error,
                "hooks": [{"id": h.get("id"), "name": h.get("name"),
                           "sent": h.get("sent", 0), "failed": h.get("failed", 0),
                           "last_error": h.get("last_error", "")}
                          for h in self.hooks()]}


# ---------------------------------------------------------------------------
#  Storage backends — the whole point of the merge. One interface, two
#  implementations: a folder on this machine, or a folder on an SFTP box.
#  Everything above this line (files, properties, backups, the whole UI)
#  works the same either way.
# ---------------------------------------------------------------------------


class Backend:
    kind = "none"

    def label(self):
        return "nothing"

    def online(self):
        return False

    def status(self):
        return {"state": "offline", "error": "No server selected."}

    def close(self):
        pass

    # Every subclass implements the rest.
    def list_dir(self, rel):
        raise NotImplementedError

    def read_file(self, rel):
        raise NotImplementedError

    def write_file(self, rel, content, expect_mtime=None):
        raise NotImplementedError

    def stat_file(self, rel):
        raise NotImplementedError

    def new_file(self, rel, name):
        raise NotImplementedError

    def mkdir(self, rel, name):
        raise NotImplementedError

    def rename(self, rel, name):
        raise NotImplementedError

    def delete(self, rel):
        raise NotImplementedError

    def copy(self, rel):
        raise NotImplementedError

    def upload(self, rel_dir, name, rfile, length):
        raise NotImplementedError

    def fetch_to_temp(self, rel):
        raise NotImplementedError

    def zip_into(self, zf, rel, prefix=""):
        """Write everything under `rel` into an open ZipFile, under `prefix`."""
        raise NotImplementedError

    def zip_folder_to_temp(self, rel, prefix=""):
        fd, tmp = tempfile.mkstemp(suffix=".zip")
        os.close(fd)
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            self.zip_into(zf, rel, prefix)
        return tmp, (rel.rstrip("/").split("/")[-1] or "server") + ".zip"

    def tail_log(self, rel=LOG_PATH, from_size=0, max_bytes=120_000, wait=True):
        raise NotImplementedError

    def walk_sizes(self, rel):
        raise NotImplementedError


class LocalBackend(Backend):
    """A server directory on this machine."""

    kind = "local"

    def __init__(self, cfg):
        self.cfg = cfg or {}
        self.down = self.up = 0

    def label(self):
        return self.cfg.get("cwd") or "(no folder set)"

    def online(self):
        return bool(self.cfg.get("cwd")) and Path(self.cfg["cwd"]).is_dir()

    def status(self):
        ok = self.online()
        return {"state": "online" if ok else "error",
                "error": "" if ok else "Set the server folder in Servers.",
                "root": self.cfg.get("cwd", ""), "kind": "local"}

    def root(self):
        return Path(self.cfg.get("cwd") or ".").resolve()

    def resolve(self, rel):
        base = self.root()
        p = (base / safe_rel(rel)).resolve()
        try:
            p.relative_to(base)
        except ValueError:
            raise RuntimeError("That path is outside the server folder.")
        return p

    # -- listing -----------------------------------------------------------
    def list_dir(self, rel):
        d = self.resolve(rel)
        if not d.is_dir():
            raise RuntimeError("Not a folder.")
        items = []
        for p in d.iterdir():
            if p.name == ".lodestone":
                continue
            try:
                st = p.stat()
                isdir = p.is_dir()
                items.append({
                    "name": p.name,
                    "rel": str(p.relative_to(self.root())).replace("\\", "/"),
                    "dir": isdir,
                    "size": "" if isdir else fmt_size(st.st_size),
                    "bytes": 0 if isdir else st.st_size,
                    "mod": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                })
            except OSError:
                continue
        items.sort(key=lambda i: (not i["dir"], i["name"].lower()))
        r = safe_rel(rel)
        return {"path": str(d), "rel": r, "at_root": r == "", "items": items}

    # -- one file ----------------------------------------------------------
    def stat_file(self, rel):
        p = self.resolve(rel)
        st = p.stat()
        return {"size": st.st_size, "mtime": int(st.st_mtime), "dir": p.is_dir()}

    def read_file(self, rel):
        p = self.resolve(rel)
        if not p.is_file():
            raise RuntimeError("Not a file.")
        if p.stat().st_size > MAX_EDIT_BYTES:
            raise RuntimeError("Too big to edit here (%s)." % fmt_size(p.stat().st_size))
        raw = p.read_bytes()
        if not looks_text(raw):
            raise RuntimeError("That looks like a binary file.")
        self.down += len(raw)
        return {"content": raw.decode("utf-8", "replace"), "mtime": int(p.stat().st_mtime)}

    def write_file(self, rel, content, expect_mtime=None):
        p = self.resolve(rel)
        if expect_mtime and p.exists():
            disk = int(p.stat().st_mtime)
            if disk != int(expect_mtime):
                raise Conflict("This file changed on disk while you had it open.", disk)
        data = (content or "").encode("utf-8")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        self.up += len(data)
        return {"mtime": int(p.stat().st_mtime), "size": len(data)}

    def new_file(self, rel, name):
        name = safe_name(name)
        if not name:
            raise RuntimeError("Give it a name.")
        p = self.resolve(rel) / name
        if p.exists():
            raise RuntimeError("Something with that name is already here.")
        p.write_text("")
        return {"rel": str(p.relative_to(self.root())).replace("\\", "/")}

    def mkdir(self, rel, name):
        name = safe_name(name)
        if not name:
            raise RuntimeError("Give it a name.")
        p = self.resolve(rel) / name
        if p.exists():
            raise RuntimeError("Something with that name is already here.")
        p.mkdir(parents=True)
        return {"rel": str(p.relative_to(self.root())).replace("\\", "/")}

    def rename(self, rel, name):
        name = safe_name(name)
        if not name:
            raise RuntimeError("Give it a name.")
        p = self.resolve(rel)
        dest = p.parent / name
        if dest.exists():
            raise RuntimeError("Something with that name is already here.")
        p.rename(dest)
        return {"rel": str(dest.relative_to(self.root())).replace("\\", "/")}

    def delete(self, rel):
        p = self.resolve(rel)
        if p == self.root():
            raise RuntimeError("That is the server folder itself.")
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()
        return {"ok": True}

    def copy(self, rel):
        p = self.resolve(rel)
        stem, suffix = (p.stem, p.suffix) if p.is_file() else (p.name, "")
        for n in range(1, 500):
            cand = p.parent / ("%s copy%s%s" % (stem, "" if n == 1 else " %d" % n, suffix))
            if not cand.exists():
                break
        else:
            raise RuntimeError("Too many copies already.")
        if p.is_dir():
            shutil.copytree(p, cand)
        else:
            shutil.copy2(p, cand)
        return {"rel": str(cand.relative_to(self.root())).replace("\\", "/")}

    def upload(self, rel_dir, name, rfile, length):
        name = safe_name(name)
        if not name:
            raise RuntimeError("Give it a name.")
        dest = self.resolve(rel_dir) / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".lodestone-part")
        written = 0
        with open(tmp, "wb") as f:
            while written < length:
                chunk = rfile.read(min(65536, length - written))
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)
        if written < length:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise RuntimeError("The upload of %s was cut off (%s of %s arrived) - nothing was written." % (
                name, fmt_size(written), fmt_size(length)))
        os.replace(tmp, dest)
        self.up += written
        return {"name": name, "size": written}

    def fetch_to_temp(self, rel):
        p = self.resolve(rel)
        if not p.is_file():
            raise RuntimeError("Not a file.")
        return str(p), p.name          # already local; no copy needed

    def zip_into(self, zf, rel, prefix=""):
        p = self.resolve(rel)
        if not p.is_dir():
            raise RuntimeError("Not a folder.")
        for dirpath, _dirs, files in os.walk(p):
            for fn in files:
                full = Path(dirpath) / fn
                arc = prefix + str(full.relative_to(p)).replace("\\", "/")
                try:
                    zf.write(full, arc)
                except OSError:
                    continue

    def read_raw(self, rel, max_bytes=8_000_000):
        p = self.resolve(rel)
        if not p.is_file():
            raise RuntimeError("Not a file.")
        with open(p, "rb") as f:
            data = f.read(max_bytes)
        self.down += len(data)
        return data

    def tail_log(self, rel=LOG_PATH, from_size=0, max_bytes=120_000, wait=True):
        try:
            p = self.resolve(rel)
        except RuntimeError:
            return {"missing": True, "text": "", "size": 0}
        if not p.is_file():
            return {"missing": True, "text": "", "size": 0}
        size = p.stat().st_size
        start = int(from_size or 0)
        skipped = False
        if start > size:
            start = 0
        if size - start > max_bytes:
            start, skipped = size - max_bytes, True
        if start >= size:
            return {"missing": False, "text": "", "size": size, "skipped": False}
        with open(p, "rb") as f:
            f.seek(start)
            data = f.read(size - start)
        return {"missing": False, "skipped": skipped, "size": size,
                "text": data.decode("utf-8", "replace")}

    def walk_sizes(self, rel):
        p = self.resolve(rel)
        total = files = 0
        for dirpath, _dirs, fns in os.walk(p):
            for fn in fns:
                try:
                    total += (Path(dirpath) / fn).stat().st_size
                    files += 1
                except OSError:
                    continue
        return total, files


class SftpBackend(Backend):
    """A server directory on a host we only reach over SFTP."""

    kind = "sftp"

    def __init__(self, cfg):
        self.cfg = cfg or {}
        self.client = self.sftp = None
        self.state = "offline"
        self.error = ""
        self.since_ts = 0
        self.down = self.up = 0
        self.note = ""
        self.lock = threading.RLock()
        # paramiko's SFTPClient is one multiplexed channel and is NOT thread safe.
        # Every operation goes through this so the log tail and the UI cannot
        # interleave packets and wedge the channel.
        self.io_lock = threading.RLock()
        self.busy = False

    def label(self):
        return "%s@%s" % (self.cfg.get("user") or "?", self.cfg.get("host") or "?")

    def online(self):
        return self.sftp is not None

    def status(self):
        return {"state": self.state, "error": self.error, "kind": "sftp",
                "busy": self.busy,
                "host": self.cfg.get("host", ""), "port": self.cfg.get("port", 22),
                "user": self.cfg.get("user", ""), "root": self.cfg.get("root", "/"),
                "uptime": int(time.time() - self.since_ts) if (self.state == "online" and self.since_ts) else 0,
                "has_paramiko": HAVE_PARAMIKO}

    # -- connection --------------------------------------------------------
    @staticmethod
    def _friendly(exc, host=""):
        s = str(exc)
        low = s.lower()
        if "authentication" in low or "auth failed" in low:
            if "minehut" in (host or "").lower():
                # learnt 2026-09-28: every start of a Minehut server makes a new SFTP password, and SFTP only
                # lets anyone in while the server is running
                return ("Host refused those credentials. Minehut makes a NEW SFTP password every time the server "
                        "starts, and only lets SFTP in while it is running - start it, then copy the current "
                        "password from the dashboard (your server > SFTP) into this server's Password. The "
                        "username is the long id from the dashboard URL.")
            return ("Host refused those credentials. Check the username and password — on Minehut "
                    "the username is the long id from your dashboard URL, and the SFTP password is "
                    "the separate one under Settings, not your account password.")
        if "getaddrinfo" in low or "name or service" in low or "not known" in low:
            return "Cannot find that hostname. Check it for typos."
        if "unable to connect to port" in low or "connection refused" in low or "10061" in low:
            return "Nothing accepted a connection on that port. Check the host and port."
        if "timed out" in low or "timeout" in low:
            return "Timed out reaching the host. Is it up, and is the port right?"
        if "banner" in low:
            return ("The host accepted the connection but never completed the SSH handshake. "
                    "That usually means the server is asleep or still starting.")
        if "no such file" in low:
            return "Signed in, but that folder is not there — try Test connection to find the right one."
        if "not a valid" in low or "incompatible" in low or "kex" in low:
            return "Could not agree on encryption with that host (%s)." % s
        return s or exc.__class__.__name__

    def _close(self):
        with self.lock:
            for obj in (self.sftp, self.client):
                try:
                    if obj:
                        obj.close()
                except Exception:
                    pass
            self.sftp = self.client = None

    def close(self):
        self._close()
        self.state = "offline"

    ROOT_HINTS = ("server.properties", "eula.txt", "world", "plugins", "logs")

    @classmethod
    def _looks_like_server(cls, sftp, path):
        try:
            names = {a.filename for a in sftp.listdir_attr(path)}
        except IOError:
            return False
        return bool(names & set(cls.ROOT_HINTS))

    def _candidate_roots(self, sftp):
        """Where a Minecraft server plausibly lives on this account."""
        out = []
        for c in (self.cfg.get("root") or "/", "/server", "/", "."):
            if c and c not in out:
                out.append(c)
        try:
            here = sftp.normalize(".")
            if here not in out:
                out.append(here)
            for a in sftp.listdir_attr(here):
                if is_dir_attr(a):
                    cand = posixpath.join(here, a.filename)
                    if cand not in out:
                        out.append(cand)
        except IOError:
            pass
        return out[:14]

    def _connect_raw(self, timeout=None):
        """Everything up to and including open_sftp, with no root check."""
        if not HAVE_PARAMIKO:
            raise RuntimeError("paramiko is not installed — the Servers tab can install it.")
        if not self.cfg.get("host") or not self.cfg.get("user"):
            raise RuntimeError("Fill in the host and username first.")
        t = int(timeout or self.cfg.get("timeout") or 20)
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        c.connect(self.cfg.get("host", ""), port=int(self.cfg.get("port") or 22),
                  username=self.cfg.get("user", ""), password=self.cfg.get("password", ""),
                  allow_agent=False, look_for_keys=False,
                  timeout=t, banner_timeout=max(t, 30), auth_timeout=t)
        return c, c.open_sftp()

    def _open(self):
        c, s = self._connect_raw()
        root = self.cfg.get("root") or "/"
        try:
            s.stat(root)
        except IOError:
            # The single commonest misconfiguration is the root box pointing at /
            # when the files are one folder down. Find it instead of just failing.
            found = next((cand for cand in self._candidate_roots(s)
                          if self._looks_like_server(s, cand)), None)
            if found:
                self.cfg["root"] = found
                self.note = "%s was not there, so the folder was switched to %s." % (root, found)
            else:
                try:
                    s.close()
                except Exception:
                    pass
                c.close()
                raise RuntimeError("Signed in, but %s is not there. Press Test connection to see "
                                   "what is on the account." % root)
        with self.lock:
            self.client, self.sftp = c, s
            self.since_ts = time.time()
        return s

    # -- staged diagnosis --------------------------------------------------
    def diagnose(self):
        """Walk the connection one step at a time and report where it stops."""
        steps = []
        host = (self.cfg.get("host") or "").strip()
        port = int(self.cfg.get("port") or 22)
        user = (self.cfg.get("user") or "").strip()

        def step(name, ok, detail=""):
            steps.append({"step": name, "ok": bool(ok), "detail": str(detail)[:400]})
            return ok

        if not host or not user:
            step("settings", False, "Host and username are both required.")
            return {"ok": False, "steps": steps, "suggest_root": ""}
        step("settings", True, "%s@%s:%d" % (user, host, port))

        if not HAVE_PARAMIKO:
            step("paramiko", False, "Not installed — Settings can install it.")
            return {"ok": False, "steps": steps, "suggest_root": ""}
        step("paramiko", True, "version %s" % getattr(paramiko, "__version__", "?"))

        try:
            infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
            step("dns", True, ", ".join(sorted({i[4][0] for i in infos})))
        except OSError as e:
            step("dns", False, "Cannot resolve %s (%s)." % (host, e))
            return {"ok": False, "steps": steps, "suggest_root": ""}

        t0 = time.time()
        try:
            sock = socket.create_connection((host, port), 15)
            step("tcp", True, "connected in %dms" % int((time.time() - t0) * 1000))
        except OSError as e:
            step("tcp", False, "Nothing accepted a connection on port %d (%s)." % (port, e))
            return {"ok": False, "steps": steps, "suggest_root": ""}

        try:
            tr = paramiko.Transport(sock)
            tr.start_client(timeout=25)
            key = tr.get_remote_server_key()
            step("ssh", True, "host key %s" % key.get_name())
            allowed = []
            try:
                tr.auth_none(user)
            except paramiko.BadAuthenticationType as e:
                allowed = list(e.allowed_types)
            except Exception:
                pass
            if allowed:
                step("auth methods", True, ", ".join(allowed))
                if "password" not in allowed:
                    step("auth methods", False,
                         "This host does not accept passwords, only: %s" % ", ".join(allowed))
            tr.close()
        except Exception as e:
            step("ssh", False, "Handshake failed: %s" % e)
            try:
                sock.close()
            except OSError:
                pass
            return {"ok": False, "steps": steps, "suggest_root": ""}

        try:
            c, sf = self._connect_raw(timeout=25)
        except Exception as e:
            step("sign in", False, self._friendly(e, self.cfg.get("host")))
            return {"ok": False, "steps": steps, "suggest_root": ""}
        step("sign in", True, "authenticated as %s" % user)

        suggest, listing = "", []
        try:
            here = sf.normalize(".")
            step("login folder", True, here)
            try:
                listing = sorted((("[D] " if is_dir_attr(a) else "") + a.filename)
                                 for a in sf.listdir_attr(here))[:40]
            except IOError as e:
                listing = ["(could not list: %s)" % e]
            root = self.cfg.get("root") or "/"
            try:
                sf.stat(root)
                if self._looks_like_server(sf, root):
                    step("server folder", True, "%s looks right — it has server files in it." % root)
                else:
                    step("server folder", False,
                         "%s exists but has no server.properties in it." % root)
            except IOError:
                step("server folder", False, "%s is not there." % root)
            if not steps[-1]["ok"]:
                suggest = next((cand for cand in self._candidate_roots(sf)
                                if self._looks_like_server(sf, cand)), "")
                if suggest:
                    steps[-1]["detail"] += "  Found the server files in %s instead." % suggest
        finally:
            try:
                sf.close()
                c.close()
            except Exception:
                pass

        ok = all(x["ok"] for x in steps)
        return {"ok": ok, "steps": steps, "suggest_root": suggest, "listing": listing}

    def connect(self):
        with self.lock:
            if self.state == "connecting":
                return True, "Already connecting."
            self._close()
            self.state = "connecting"
            self.error = ""
        try:
            self._open()
        except Exception as e:
            self.state, self.error = "error", self._friendly(e, self.cfg.get("host"))
            return False, self.error
        self.state = "online"
        msg = "Connected to %s." % self.cfg.get("host")
        if self.note:
            msg += " " + self.note
        return True, msg

    def ensure(self):
        with self.lock:
            if self.sftp is not None:
                return self.sftp
        ok, msg = self.connect()
        if not ok:
            raise RuntimeError(msg)
        return self.sftp

    @staticmethod
    def _is_link_error(e):
        s = str(e).lower()
        return isinstance(e, (EOFError, OSError, socket.error)) or \
            "closed" in s or "broken" in s or "not connected" in s or "banner" in s

    def _with_retry(self, fn, wait=True):
        """Hosted boxes drop idle SFTP sessions constantly. Reconnect once,
        silently, instead of making the user press Connect again.

        Serialised: the channel carries one request at a time. `wait=False`
        gives up rather than queueing, which is what the log tail wants — it
        must never hold the UI up behind a big transfer.
        """
        if not self.io_lock.acquire(blocking=wait, timeout=120 if wait else -1):
            return None
        self.busy = True
        try:
            s = self.ensure()
            try:
                return fn(s)
            except Exception as e:
                if not self._is_link_error(e):
                    raise
                self._close()
                s = self.ensure()
                return fn(s)
        finally:
            self.busy = False
            self.io_lock.release()

    # -- paths -------------------------------------------------------------
    def root(self):
        return (self.cfg.get("root") or "/").rstrip("/") or "/"

    def abs(self, rel):
        r = safe_rel(rel)
        return posixpath.join(self.root(), r) if r else self.root()

    def rel(self, absp):
        root = self.root()
        if absp == root:
            return ""
        return absp[len(root):].lstrip("/") if absp.startswith(root) else absp.lstrip("/")

    # -- listing -----------------------------------------------------------
    def list_dir(self, rel):
        target = self.abs(rel)
        entries = self._with_retry(lambda s: s.listdir_attr(target))
        items = []
        for a in entries:
            if a.filename in (".", "..", ".lodestone") or a.filename.endswith(".lodestone-part"):
                continue
            full = posixpath.join(target, a.filename)
            d = is_dir_attr(a)
            items.append({
                "name": a.filename, "rel": self.rel(full), "dir": d,
                "size": "" if d else fmt_size(a.st_size or 0),
                "bytes": 0 if d else (a.st_size or 0),
                "mod": datetime.fromtimestamp(a.st_mtime).strftime("%Y-%m-%d %H:%M") if a.st_mtime else "",
            })
        items.sort(key=lambda i: (not i["dir"], i["name"].lower()))
        return {"path": target, "rel": self.rel(target),
                "at_root": target.rstrip("/") == self.root().rstrip("/"), "items": items}

    def stat_file(self, rel):
        p = self.abs(rel)
        st = self._with_retry(lambda s: s.stat(p))
        return {"size": st.st_size or 0, "mtime": int(st.st_mtime or 0),
                "dir": statmod.S_ISDIR(st.st_mode)}

    def read_file(self, rel):
        p = self.abs(rel)

        def op(s):
            st = s.stat(p)
            if statmod.S_ISDIR(st.st_mode):
                raise RuntimeError("That is a folder.")
            if (st.st_size or 0) > MAX_EDIT_BYTES:
                raise RuntimeError("Too big to edit here (%s)." % fmt_size(st.st_size))
            with s.open(p, "rb") as f:
                raw = f.read()
            return raw, int(st.st_mtime or 0)

        raw, mtime = self._with_retry(op)
        if not looks_text(raw):
            raise RuntimeError("That looks like a binary file.")
        self.down += len(raw)
        return {"content": raw.decode("utf-8", "replace"), "mtime": mtime}

    def _mkdirs(self, s, path):
        parts, cur = [p for p in path.split("/") if p], ""
        for part in parts:
            cur = cur + "/" + part
            try:
                s.stat(cur)
            except IOError:
                try:
                    s.mkdir(cur)
                except IOError:
                    pass

    PART = ".lodestone-part"

    def _put(self, s, p, data):
        """Write next to the file, then swap it in with one rename. Writing over the file in place is what took a
        running Minehut server down when its plugin jar was replaced (the JVM reads the open jar lazily and found new
        bytes under old offsets); a rename leaves the running server its old file, and nobody ever sees a half-written
        one. Hosts without the posix-rename extension get remove + rename (still never a partial file at p)."""
        self._mkdirs(s, posixpath.dirname(p))
        tmp = p + self.PART
        with s.open(tmp, "wb") as f:
            f.set_pipelined(True)
            f.write(data)
        try:
            s.posix_rename(tmp, p)
        except IOError:
            try:
                s.remove(p)
            except IOError:
                pass
            s.rename(tmp, p)
        self.up += len(data)

    def write_file(self, rel, content, expect_mtime=None):
        p = self.abs(rel)

        def op(s):
            if expect_mtime:
                try:
                    disk = int(s.stat(p).st_mtime or 0)
                    if disk != int(expect_mtime):
                        raise Conflict("This file changed on the server while you had it open.", disk)
                except IOError:
                    pass
            self._put(s, p, (content or "").encode("utf-8"))
            return int(s.stat(p).st_mtime or 0)

        mtime = self._with_retry(op)
        return {"mtime": mtime, "size": len((content or "").encode("utf-8"))}

    def new_file(self, rel, name):
        name = safe_name(name)
        if not name:
            raise RuntimeError("Give it a name.")
        p = posixpath.join(self.abs(rel), name)

        def op(s):
            try:
                s.stat(p)
                raise RuntimeError("Something with that name is already here.")
            except IOError:
                pass
            self._put(s, p, b"")
            return {"rel": self.rel(p)}

        return self._with_retry(op)

    def mkdir(self, rel, name):
        name = safe_name(name)
        if not name:
            raise RuntimeError("Give it a name.")
        p = posixpath.join(self.abs(rel), name)

        def op(s):
            try:
                s.stat(p)
                raise RuntimeError("Something with that name is already here.")
            except IOError:
                pass
            s.mkdir(p)
            return {"rel": self.rel(p)}

        return self._with_retry(op)

    def rename(self, rel, name):
        name = safe_name(name)
        if not name:
            raise RuntimeError("Give it a name.")
        src = self.abs(rel)
        dest = posixpath.join(posixpath.dirname(src), name)

        def op(s):
            try:
                s.stat(dest)
                raise RuntimeError("Something with that name is already here.")
            except IOError:
                pass
            s.rename(src, dest)
            return {"rel": self.rel(dest)}

        return self._with_retry(op)

    def _rm(self, s, path):
        try:
            st = s.stat(path)
        except IOError:
            return
        if statmod.S_ISDIR(st.st_mode):
            for a in s.listdir_attr(path):
                self._rm(s, posixpath.join(path, a.filename))
            s.rmdir(path)
        else:
            s.remove(path)

    def delete(self, rel):
        p = self.abs(rel)
        if p.rstrip("/") == self.root().rstrip("/"):
            raise RuntimeError("That is the server folder itself.")
        self._with_retry(lambda s: self._rm(s, p))
        return {"ok": True}

    @staticmethod
    def _free_name(s, parent, base):
        stem, dot, ext = base.rpartition(".")
        if not dot:
            stem, ext = base, ""
        for n in range(1, 500):
            cand = "%s copy%s%s" % (stem, "" if n == 1 else " %d" % n, ("." + ext) if ext else "")
            try:
                s.stat(posixpath.join(parent, cand))
            except IOError:
                return cand
        raise RuntimeError("Too many copies already.")

    def _copy_tree(self, s, src, dest):
        st = s.stat(src)
        if not statmod.S_ISDIR(st.st_mode):
            with s.open(src, "rb") as a:
                data = a.read()
            self._put(s, dest, data)
            return
        try:
            s.mkdir(dest)
        except IOError:
            pass
        for a in s.listdir_attr(src):
            self._copy_tree(s, posixpath.join(src, a.filename), posixpath.join(dest, a.filename))

    def copy(self, rel):
        src = self.abs(rel)
        parent = posixpath.dirname(src)
        base = posixpath.basename(src)

        def op(s):
            dest = posixpath.join(parent, self._free_name(s, parent, base))
            self._copy_tree(s, src, dest)
            return {"rel": self.rel(dest)}

        return self._with_retry(op)

    def upload(self, rel_dir, name, rfile, length):
        name = safe_name(name)
        if not name:
            raise RuntimeError("Give it a name.")
        p = posixpath.join(self.abs(rel_dir), name)
        parts, got = [], 0
        while got < length:
            chunk = rfile.read(min(262144, length - got))
            if not chunk:
                break
            parts.append(chunk)
            got += len(chunk)
        if got < length:
            # the sender went away part-way: writing what arrived would leave a broken file (a cut jar)
            raise RuntimeError("The upload of %s was cut off (%s of %s arrived) - nothing was written." % (
                name, fmt_size(got), fmt_size(length)))
        data = b"".join(parts)
        self._with_retry(lambda s: self._put(s, p, data))
        return {"name": name, "size": len(data)}

    def fetch_to_temp(self, rel):
        p = self.abs(rel)
        fd, tmp = tempfile.mkstemp()
        os.close(fd)

        def op(s):
            s.get(p, tmp)
            return os.path.getsize(tmp)

        self.down += self._with_retry(op)
        return tmp, posixpath.basename(p) or "download"

    def zip_into(self, zf, rel, prefix=""):
        base = self.abs(rel)

        def op(s):
            def walk(d):
                for a in s.listdir_attr(d):
                    full = posixpath.join(d, a.filename)
                    if is_dir_attr(a):
                        walk(full)
                    else:
                        try:
                            with s.open(full, "rb") as f:
                                f.prefetch(a.st_size or 0)
                                data = f.read()
                            self.down += len(data)
                            zf.writestr(prefix + full[len(base):].lstrip("/"), data)
                        except Exception:
                            continue
            walk(base)

        self._with_retry(op)

    def read_raw(self, rel, max_bytes=8_000_000):
        target = self.abs(rel)

        def op(s):
            with s.open(target, "rb") as f:
                f.prefetch(max_bytes)
                return f.read(max_bytes)

        data = self._with_retry(op)
        self.down += len(data)
        return data

    def tail_log(self, rel=LOG_PATH, from_size=0, max_bytes=120_000, wait=True):
        p = self.abs(rel)

        def op(s):
            try:
                st = s.stat(p)
            except IOError:
                return {"missing": True, "text": "", "size": 0}
            size = st.st_size or 0
            start = int(from_size or 0)
            skipped = False
            if start > size:
                start = 0
            if size - start > max_bytes:
                start, skipped = size - max_bytes, True
            if start >= size:
                return {"missing": False, "text": "", "size": size, "skipped": False}
            with s.open(p, "rb") as f:
                f.seek(start)
                data = f.read(size - start)
            self.down += len(data)
            return {"missing": False, "skipped": skipped, "size": size,
                    "text": data.decode("utf-8", "replace")}

        return self._with_retry(op, wait=wait)

    def walk_sizes(self, rel):
        base = self.abs(rel)
        totals = [0, 0]

        def op(s):
            def walk(d):
                for a in s.listdir_attr(d):
                    if is_dir_attr(a):
                        walk(posixpath.join(d, a.filename))
                    else:
                        totals[0] += a.st_size or 0
                        totals[1] += 1
            walk(base)

        self._with_retry(op)
        return totals[0], totals[1]

    @staticmethod
    def install_paramiko():
        try:
            out = subprocess.run([sys.executable, "-m", "pip", "install", "paramiko"],
                                 capture_output=True, text=True, timeout=300)
            if out.returncode == 0:
                return True, "paramiko installed. Restart Lodestone to pick it up."
            return False, (out.stderr or out.stdout or "pip failed.").strip()[-400:]
        except Exception as e:
            return False, str(e)


# ---------------------------------------------------------------------------
#  Panel — the shelf of servers, the console, and everything that reacts to it
# ---------------------------------------------------------------------------

DATA_DIR = Path.home() / ".lodestone"
BACKUP_ROOT = DATA_DIR / "backups"
JAVA_ROOT = DATA_DIR / "java"              # runtimes the wizard downloaded
TEX_ROOT = DATA_DIR / "textures"           # item and block textures for instance icons
BUILD_ROOT = DATA_DIR / "buildtools"       # Spigot's BuildTools works here


def blank_server(kind="local", name="New server"):
    return {
        "id": new_id(), "name": name, "kind": kind,
        "created": int(time.time()),          # for "newest first"; 0 on older entries
        "local": {"java": "java", "jar": "", "cwd": "", "min_ram": "1G",
                  "max_ram": "4G", "extra": "", "server_type": "vanilla",
                  "args_file": "", "run_cmd": "",
                  # Set by the Create server wizard, which knows exactly what it
                  # installed; servers added by hand are detected instead.
                  "mc_version": "", "loader_version": ""},
        "sftp": {"host": "", "port": 22, "user": "", "password": "",
                 "save_password": True, "root": "/"},
        "rcon": {"enabled": False, "host": "", "port": 25575, "password": ""},
        "chat": {"pattern": "", "enabled": True},
        "minehut": {"enabled": False, "server_id": "", "token": "", "session_id": ""},
        "discord": {"hooks": []},
        "hosting": default_hosting({}),
        "offline_mode": default_offline({}),
        "features": default_features({}),
        # How the instance looks in the library, and the launch profile it
        # belongs to. Purely presentational until a profile is applied.
        "profile": "",
        "look": {"banner": "", "icon": ""},
        "last_started": 0,
    }


# Built-in art the desktop app ships; "" means "pick one from the server id".
LOOK_BANNERS = ("", "forest", "valley", "jungle", "ocean", "deepdark", "abyss", "battlefield")
# Icons are Minecraft items and blocks ("item/diamond", "block/grass_block"), from textures.py.
PROFILE_COLORS = ("#1BD96A", "#4F9CFF", "#C78AFF", "#FFA347", "#FF496E", "#5EE6D0", "#FFD65C")

# Aikar's flags: the G1GC tuning most Paper servers run with.
AIKAR_FLAGS = ("-XX:+UseG1GC -XX:+ParallelRefProcEnabled -XX:MaxGCPauseMillis=200 "
               "-XX:+UnlockExperimentalVMOptions -XX:+DisableExplicitGC -XX:+AlwaysPreTouch "
               "-XX:G1NewSizePercent=30 -XX:G1MaxNewSizePercent=40 -XX:G1HeapRegionSize=8M "
               "-XX:G1ReservePercent=20 -XX:G1HeapWastePercent=5 -XX:G1MixedGCCountTarget=4 "
               "-XX:InitiatingHeapOccupancyPercent=15 -XX:G1MixedGCLiveThresholdPercent=90 "
               "-XX:G1RSetUpdatingPauseIntervalMillis=100 -XX:SurvivorRatio=32 "
               "-XX:+PerfDisableSharedMem -XX:MaxTenuringThreshold=1")


def default_profile(p):
    """A launch profile: a name, a colour, and the Java settings it hands to
    every instance it is applied to."""
    base = {"id": new_id(), "name": "New profile", "color": PROFILE_COLORS[0],
            "description": "", "banner": "", "java": "", "min_ram": "", "max_ram": "",
            "extra": ""}
    for k, v in base.items():
        p.setdefault(k, v)
    return p


def default_hosting(h):
    """Per-server hosting settings.

    The default is "lan" and nothing is applied until the user picks a mode, so
    an existing install behaves exactly as before: server.properties is not
    touched, no router is contacted, no port is mapped.
    """
    base = {
        "mode": "lan",              # local | lan | internet
        "port": 25565,              # the Minecraft port; mirrors server.properties
        "external_port": 0,         # 0 == same as the internal port
        "upnp": True,
        "natpmp": True,
        "pcp": True,
        "firewall": True,           # manage a Windows Firewall rule for the port
        "public_ip_lookup": True,   # ask an outside service for our address
        "external_test": True,      # let an outside service try to connect
        "auto_enable": False,       # re-apply internet mode when the panel starts
        # Tunnel mode: a playit.gg or ngrok agent instead of a port forward,
        # for connections where forwarding cannot work at all (CGNAT, an ISP
        # router you have no password for). Which agent is "tunnel_provider";
        # the two paths are kept apart so switching back and forth does not
        # hand one provider the other's binary. Empty means "find it".
        "tunnel_provider": "playit",
        "tunnel_agent": "",
        "ngrok_agent": "",
        # Mappings this app created, written down so a crash cannot orphan an
        # open port: the next launch finds them here and can take them back down.
        "mappings": [],
    }
    for k, v in base.items():
        h.setdefault(k, v)
    return h


# ---------------------------------------------------------------------------
#  Offline / non-premium mode (testing)
# ---------------------------------------------------------------------------
# Lets non-premium accounts join a local test server. Only the keys below are
# touched, one line at a time; what they said before is kept in the server's
# config entry, so switching back puts the file back exactly as it was.

OFFLINE_PROPS_KEYS = ("online-mode", "enforce-secure-profile", "server-ip")
OFFLINE_TOML_KEYS = ("online-mode", "force-key-authentication", "bind")
OFFLINE_AUTH_PLUGINS = ("authme", "nlogin", "librelogin", "loginsecurity",
                        "openlogin", "jpremium", "fastlogin")


def default_offline(o):
    """Per-server offline-mode state. Off by default: nothing is touched until
    the user turns it on."""
    base = {
        "enabled": False,
        "allow_lan": False,         # off: bind to 127.0.0.1 while offline
        "kind": "",                 # properties | velocity, fixed while on
        "saved": {},                # key -> raw value before (None == absent)
        "changed": [],              # keys currently holding our value
        "added_eol": False,         # we added a final line ending to append
    }
    for k, v in base.items():
        o.setdefault(k, v)
    return o


def _offline_line_re(key, toml):
    # The key, separator and spacing are kept; only the value is swapped.
    if toml:
        return re.compile(r'^(\s*%s\s*=\s*)("(?:[^"\\]|\\.)*"|[^\s#]*)(\s*(?:#.*)?)$'
                          % re.escape(key))
    return re.compile(r"^(\s*%s\s*=\s*)(.*?)(\s*)$" % re.escape(key))


def offline_read(text, keys, toml=False):
    """The raw current value of each key, None when the key is absent. TOML
    keys are read from the top level only (before the first [table])."""
    out = {k: None for k in keys}
    for line in text.splitlines():
        if toml and line.lstrip().startswith("["):
            break
        for k in keys:
            if out[k] is None:
                m = _offline_line_re(k, toml).match(line)
                if m:
                    out[k] = m.group(2)
    return out


def offline_edit(text, values, toml=False, strip_final_eol=False):
    """Set keys to raw values and change nothing else.

    values maps key -> raw value, or None to drop a line this feature appended.
    Each line keeps its own line ending. A missing key is added with the file's
    line ending — at the end of server.properties, or before the first [table]
    of a TOML file so it stays top-level. Returns (text, added_final_eol).
    """
    lines = text.splitlines(True)
    eol = "\r\n" if "\r\n" in text else "\n"
    out, done, in_table, first_table = [], set(), False, None
    for raw in lines:
        body = raw.rstrip("\r\n")
        end = raw[len(body):]
        if toml and body.lstrip().startswith("["):
            if not in_table:
                first_table = len(out)
            in_table = True
        hit = None
        if not in_table:
            for k in values:
                m = _offline_line_re(k, toml).match(body)
                if m:
                    hit = (k, m)
                    break
        if hit is None:
            out.append(raw)
            continue
        k, m = hit
        if values[k] is None:
            continue
        out.append(m.group(1) + values[k] + m.group(3) + end)
        done.add(k)
    added_eol = False
    extra = ["%s%s%s%s" % (k, " = " if toml else "=", v, eol)
             for k, v in values.items() if v is not None and k not in done]
    if extra:
        if first_table is not None:
            out[first_table:first_table] = extra
        else:
            if out and not out[-1].endswith(("\n", "\r")):
                out[-1] += eol
                added_eol = True
            out.extend(extra)
    new = "".join(out)
    if strip_final_eol and new.endswith(eol):
        new = new[:-len(eol)]
    return new, added_eol


def _offline_targets(kind, saved, allow_lan):
    """What the keys must say while offline mode is on."""
    if kind == "velocity":
        want = {"online-mode": "false", "force-key-authentication": "false"}
        if not allow_lan:
            port = 25565
            m = re.search(r":(\d+)\"?$", saved.get("bind") or "")
            if m:
                port = int(m.group(1))
            want["bind"] = '"127.0.0.1:%d"' % port
        return want
    want = {"online-mode": "false", "enforce-secure-profile": "false"}
    if not allow_lan:
        want["server-ip"] = "127.0.0.1"
    return want


def _yaml_value(text, path):
    """A scalar from a plain block-style YAML file, by key path. Enough for
    paper-global.yml and spigot.yml; not a YAML parser."""
    stack = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = re.match(r"^(\s*)([\w.-]+):\s*(.*?)\s*$", line)
        if not m:
            continue
        ind = len(m.group(1))
        while stack and stack[-1][0] >= ind:
            stack.pop()
        stack.append((ind, m.group(2)))
        if [k for _, k in stack] == list(path):
            return m.group(3).split(" #")[0].strip().strip("'\"")
    return None


def default_features(f):
    base = {
        "auto_restart": False,
        "daily_restart": {"enabled": False, "time": "04:00"},
        "auto_backup": {"enabled": False, "hours": 6, "keep": 10},
        "auto_save": {"enabled": False, "minutes": 15},
        "broadcast": {"enabled": False, "minutes": 30, "message": "Running on Lodestone"},
        "clearlag": {"enabled": False, "minutes": 10, "warn": True},
        "welcome": {"enabled": False, "message": "Welcome {player}!"},
        "watchdog": {"enabled": False, "minutes": 5},
        "tasks": [],
        "macros": [
            {"name": "Clear dropped items", "commands": "kill @e[type=item]"},
            {"name": "Bright day", "commands": "time set day\nweather clear"},
            {"name": "Save everything", "commands": "save-all flush"},
        ],
    }
    for k, v in base.items():
        if k not in f:
            f[k] = v
        elif isinstance(v, dict) and isinstance(f.get(k), dict):
            for kk, vv in v.items():
                f[k].setdefault(kk, vv)
    return f


class ServerRuntime:
    """What is true of a *running* server, as opposed to a configured one.

    One of these per server id. Splitting it out is what lets two local servers
    be up at the same time: console, players, uptime and the child process are
    per server rather than per panel, so switching which server you are looking
    at no longer disturbs the others.
    """

    __slots__ = ("proc", "buf", "seq", "log_lock", "started_at",
                 "intentional_stop", "done_ms", "players", "player_seen",
                 "known_players", "last_line_at", "chat_seen", "last_chat",
                 "_chat_re_cache", "tps")

    def __init__(self):
        self.proc = None                  # local server process
        self.buf = deque(maxlen=6000)
        self.seq = 0
        self.log_lock = threading.Lock()
        self.started_at = None
        self.intentional_stop = False
        self.done_ms = ""
        self.players = set()
        self.player_seen = {}
        self.known_players = set()
        self.last_line_at = 0
        self.chat_seen = 0
        self.last_chat = ""
        self._chat_re_cache = (None, None)
        self.tps = None                   # the last TPS/MSPT the server reported

    def running(self):
        return bool(self.proc and self.proc.poll() is None)


def _runtime_prop(name):
    """A Panel attribute that really lives on the active server's runtime.

    Keeps the old single-server spelling (self.proc, self.players, ...) working
    everywhere it is already used, so this change stays confined to the places
    that genuinely have to name a server.
    """

    def getter(self):
        return getattr(self.runtime(), name)

    def setter(self, value):
        setattr(self.runtime(), name, value)

    return property(getter, setter)


class AgentBridge:
    """What the development assistant can do with one server: read its state
    and console, type a console command, and start or stop it - the same
    things the Console page's buttons do, through the same code."""

    def __init__(self, panel, sid):
        self.panel, self.sid = panel, sid

    def _srv(self):
        s = self.panel.find(self.sid)
        if not s:
            raise RuntimeError("That server is no longer in Lodestone.")
        return s

    def info(self):
        s = self._srv()
        lc = s.get("local") or {}
        st = self.panel.status_for(self.sid)
        local = s.get("kind") == "local"
        return {"name": s.get("name") or "server", "id": self.sid, "kind": s.get("kind"), "local": local,
                "software": lc.get("server_type", ""), "mc": lc.get("mc_version", ""), "running": st["running"],
                "ready": bool(st["running"] and self.panel.runtime(self.sid).done_ms),
                "can_command": st["can_command"], "can_start": local or bool((s.get("minehut") or {}).get("enabled")),
                "players": st["players"], "uptime": st["uptime_h"], "folder": lc.get("cwd", "") if local else "",
                "port": lc.get("port") or ""}

    def start(self):
        s = self._srv()
        if s.get("kind") == "local":
            return self.panel.start(self.sid)
        if (s.get("minehut") or {}).get("enabled"):
            return self.panel.minehut_action("start", self.sid)
        return False, "This is a remote server - start it from its host's panel."

    def stop(self):
        s = self._srv()
        if s.get("kind") == "local":
            return self.panel.stop(self.sid)
        if (s.get("minehut") or {}).get("enabled"):
            return self.panel.minehut_action("stop", self.sid)
        return False, "This is a remote server - stop it from its host's panel."

    def kill(self):
        """Only the server process Lodestone started - nothing else."""
        if self._srv().get("kind") != "local":
            return False, "Only a server running on this computer can be force-stopped."
        return self.panel.kill(self.sid)

    def console(self, n):
        return [t for _s, t in self.panel.recent(n, sid=self.sid)]

    def last_seq(self):
        rt = self.panel.runtime(self.sid)
        with rt.log_lock:
            return rt.seq

    def since(self, seq):
        return self.panel.since(seq, sid=self.sid)

    def command(self, cmd):
        ok, out = self.panel.command(cmd, quiet=True, who="assistant", sid=self.sid)
        if ok:
            self.panel.emit("> %s   (assistant)" % cmd, self.sid)
        return ok, out


class Panel:

    def __init__(self):
        self.lock = threading.RLock()
        self.cfg = self._load()
        # server id -> ServerRuntime. Console, players and the child process all
        # live in here now; the attributes that used to hold them are properties
        # onto whichever server is currently selected.
        self.runtimes = {}
        self._via_busy = set()      # server ids with an install in flight
        self.jobs = {}              # long jobs (creating a server, fetching Java), by id
        # Self-update: what the last check found, and whether new files are
        # waiting for a restart. In memory only - a restart is the point.
        self.upd = {"state": "idle", "info": None, "error": "", "pct": 0, "applied": None,
                    "checked_at": 0, "log": deque(maxlen=200)}
        self._upd_lock = threading.Lock()
        self._jobs_lock = threading.Lock()

        self.backends = {}
        self.hosts = {}                   # server id -> HostingManager
        self._offline_restart = set()     # server ids toggled while running
        self.remote_size = 0
        self.remote_thread = None
        self.remote_stop = threading.Event()

        self.backup = {"busy": False, "msg": "", "pct": 0}
        self.discord = Discord(self)
        self.ai_history = []
        self.ai_usage_total = {"in": 0, "out": 0}
        self.agent = None                 # the development assistant, made on first use
        self._agent_lock = threading.Lock()
        self._perf_sampler = perfmod.Sampler() if HAVE_PERF else None
        self._ollama_seen = {"at": 0.0, "state": None}
        self._automod_q = deque(maxlen=200)
        self._automod_last = 0.0
        self.watch = {"cmds": 0, "events": 0, "api_calls": 0}

    # Old single-server attribute names, now views onto the active runtime.
    proc = _runtime_prop("proc")
    buf = _runtime_prop("buf")
    seq = _runtime_prop("seq")
    log_lock = _runtime_prop("log_lock")
    started_at = _runtime_prop("started_at")
    intentional_stop = _runtime_prop("intentional_stop")
    done_ms = _runtime_prop("done_ms")
    players = _runtime_prop("players")
    player_seen = _runtime_prop("player_seen")
    known_players = _runtime_prop("known_players")
    last_line_at = _runtime_prop("last_line_at")
    chat_seen = _runtime_prop("chat_seen")
    last_chat = _runtime_prop("last_chat")
    _chat_re_cache = _runtime_prop("_chat_re_cache")

    def runtime(self, sid=None):
        """The runtime for a server, created on first use.

        Falls back to the active server, which is what every caller that does
        not name one means.
        """
        key = sid or self.cfg.get("active")
        if not key:
            servers = self.cfg.get("servers") or []
            key = servers[0]["id"] if servers else "__none__"
        rt = self.runtimes.get(key)
        if rt is None:
            rt = self.runtimes[key] = ServerRuntime()
        return rt

    def running_ids(self):
        """Ids of every local server with a live process."""
        return [sid for sid, rt in self.runtimes.items() if rt.running()]

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        BACKUP_ROOT.mkdir(parents=True, exist_ok=True)

        try:
            self.known_players = set(self.cfg.get("known_players", []))
        except Exception:
            self.known_players = set()

        if self.cfg.pop("_fresh", False):
            try:
                got = self.import_legacy()
                if got:
                    self.emit(">>> adopted from LocalHost/RemoteHost: " + ", ".join(got))
            except Exception as e:
                self.emit("!!! could not read the old configs: %s" % e)

        threading.Thread(target=self._feature_loop, daemon=True).start()
        threading.Thread(target=self._automod_loop, daemon=True).start()

    # -- config ------------------------------------------------------------
    @staticmethod
    def _load():
        cfg = {}
        if CONFIG_PATH.exists():
            try:
                cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                cfg = {}
        cfg.setdefault("servers", [])
        cfg.setdefault("active", "")
        cfg.setdefault("panel_host", "127.0.0.1")
        cfg.setdefault("port", DEFAULT_PORT)
        cfg.setdefault("theme", "lodestone")
        cfg.setdefault("ui", {})
        cfg["ui"].setdefault("mode", "")          # "" until the user picks one
        cfg.setdefault("ai", {})
        for k, v in {"enabled": False, "provider": "openai", "api_key": "", "model": "",
                     "base_url": "", "allow_commands": True, "allow_files": False,
                     "automod": False, "automod_auto": False,
                     "rules": "Be respectful. No spam, slurs, or advertising other servers."}.items():
            cfg["ai"].setdefault(k, v)
        cfg.setdefault("dev", {})
        for k, v in DEV_DEFAULTS.items():
            cfg["dev"].setdefault(k, json.loads(json.dumps(v)))
        cfg.setdefault("api", {"enabled": False, "tokens": []})
        cfg["api"].setdefault("enabled", False)
        cfg["api"].setdefault("tokens", [])
        cfg.setdefault("profiles", [])
        cfg["profiles"] = [default_profile(p) for p in cfg["profiles"] if isinstance(p, dict)]
        for s in cfg["servers"]:
            s.setdefault("id", new_id())
            s.setdefault("kind", "local")
            s.setdefault("rcon", {"enabled": False, "host": "", "port": 25575, "password": ""})
            s.setdefault("minehut", {"enabled": False, "server_id": "", "token": "", "session_id": ""})
            for k, v in (("enabled", False), ("server_id", ""), ("token", ""), ("session_id", "")):
                s["minehut"].setdefault(k, v)
            s.setdefault("chat", {"pattern": "", "enabled": True})
            s["chat"].setdefault("pattern", "")
            s["chat"].setdefault("enabled", True)
            s.setdefault("discord", {"hooks": []})
            s["discord"].setdefault("hooks", [])
            s["hosting"] = default_hosting(s.get("hosting", {}))
            s["offline_mode"] = default_offline(s.get("offline_mode") or {})
            s["features"] = default_features(s.get("features", {}))
            s.setdefault("profile", "")
            s.setdefault("look", {})
            s["look"].setdefault("banner", "")
            s["look"].setdefault("icon", "")
            s.setdefault("last_started", 0)
            for key, blank in (("local", blank_server()["local"]), ("sftp", blank_server()["sftp"])):
                s.setdefault(key, {})
                for k, v in blank.items():
                    s[key].setdefault(k, v)
        if not cfg["servers"]:
            first = blank_server("local", "My server")
            cfg["servers"].append(first)
            cfg["active"] = first["id"]
            cfg["_fresh"] = True
        if cfg["active"] not in [s["id"] for s in cfg["servers"]]:
            cfg["active"] = cfg["servers"][0]["id"]
        return cfg

    def save(self):
        with self.lock:
            try:
                CONFIG_PATH.write_text(json.dumps(self.cfg, indent=2), encoding="utf-8")
                chmod_600(CONFIG_PATH)
            except OSError as e:
                self.emit("!!! could not save config: %s" % e)
        return True

    # -- the shelf ---------------------------------------------------------
    def servers(self):
        return self.cfg["servers"]

    def server(self, sid=None):
        sid = sid or self.cfg.get("active")
        for s in self.servers():
            if s["id"] == sid:
                return s
        return self.servers()[0] if self.servers() else blank_server()

    def find(self, sid):
        for s in self.servers():
            if s["id"] == sid:
                return s
        return None

    def icon_of(self, s):
        """The instance icon: the item or block picked for it, or the one its software suggests."""
        lc = s.get("local") or {}
        software = lc.get("server_type", "") if lc.get("mc_version") else ""
        chosen = (s.get("look") or {}).get("icon", "")
        return texmod.resolve(chosen, software, s.get("kind", "local")) if HAVE_TEX else chosen

    def listing(self):
        out = []
        for s in self.servers():
            be = self.backends.get(s["id"])
            out.append({
                "id": s["id"], "name": s.get("name") or "server", "kind": s.get("kind"),
                "active": s["id"] == self.cfg.get("active"),
                "where": (s["local"].get("cwd") or "no folder yet") if s["kind"] == "local"
                         else ("%s@%s" % (s["sftp"].get("user") or "?", s["sftp"].get("host") or "?")),
                "rcon": bool(s.get("rcon", {}).get("enabled")),
                "hooks": len([h for h in s.get("discord", {}).get("hooks", []) if h.get("enabled")]),
                "state": (be.status().get("state") if be else "offline"),
                "running": self.is_running(s["id"]),
                "via": (self.via_state(s["id"]) if HAVE_VIA else
                        {"available": False, "reason": VIA_ERROR}),
                "software": s["local"].get("server_type", "") if s["local"].get("mc_version") else "",
                "mc": s["local"].get("mc_version", ""),
                "profile": s.get("profile") or "",
                "look": s.get("look") or {"banner": "", "icon": ""},
                "icon": self.icon_of(s),
                "icon_auto": self.icon_of(dict(s, look={})),
                "last_started": int(s.get("last_started") or 0),
                "max_ram": s["local"].get("max_ram", "") if s["kind"] == "local" else "",
                "port": (self._local_port(s) if s["kind"] == "local" else None),
                "offline_mode": bool((s.get("offline_mode") or {}).get("enabled")),
                "created": s.get("created", 0),
                # Only a local folder can be looked at without a round trip.
                "has_icon": bool(s["kind"] == "local" and self.local_icon_path(s)),
            })
        return out

    # -- profiles ------------------------------------------------------------
    #
    # A profile groups instances in the library and carries Java settings. It
    # never changes a server behind your back: its settings reach an instance
    # only when it is applied (profile_apply), and then they are written into
    # that server's own "local" block where the Servers editor shows them.

    def profiles(self):
        out = []
        for p in self.cfg.get("profiles", []):
            members = [s for s in self.servers() if s.get("profile") == p["id"]]
            out.append(dict(p, count=len(members),
                            members=[{"id": s["id"], "name": s.get("name") or "server",
                                      "icon": self.icon_of(s),
                                      "running": self.is_running(s["id"])} for s in members]))
        return out

    def profile_save(self, body):
        pid = (body or {}).get("id")
        prof = next((p for p in self.cfg["profiles"] if p["id"] == pid), None) if pid else None
        if prof is None:
            prof = default_profile({})
            self.cfg["profiles"].append(prof)
        for k in ("name", "description", "java", "min_ram", "max_ram", "extra"):
            if k in body:
                prof[k] = str(body.get(k) or "").strip()
        if not prof["name"]:
            prof["name"] = "Profile"
        if body.get("color") in PROFILE_COLORS:
            prof["color"] = body["color"]
        if body.get("banner", None) in LOOK_BANNERS:
            prof["banner"] = body["banner"]
        for k in ("min_ram", "max_ram"):
            if prof[k] and not re.fullmatch(r"\d+[MmGg]", prof[k]):
                raise RuntimeError("%s must look like 512M or 4G." % k.replace("_", " ").title())
        self.save()
        return prof

    def profile_delete(self, pid):
        before = len(self.cfg["profiles"])
        self.cfg["profiles"] = [p for p in self.cfg["profiles"] if p["id"] != pid]
        if len(self.cfg["profiles"]) == before:
            raise RuntimeError("No such profile.")
        for s in self.servers():
            if s.get("profile") == pid:
                s["profile"] = ""
        self.save()
        return True

    def profile_apply(self, pid, sid=None):
        """Copy the profile's Java settings into its local instances (or one).
        Blank profile fields leave that setting alone."""
        prof = next((p for p in self.cfg["profiles"] if p["id"] == pid), None)
        if not prof:
            raise RuntimeError("No such profile.")
        changed, running = [], []
        for s in self.servers():
            if s.get("profile") != pid or s.get("kind") != "local" or (sid and s["id"] != sid):
                continue
            for k in ("java", "min_ram", "max_ram", "extra"):
                if prof.get(k):
                    s["local"][k] = prof[k]
            changed.append(s.get("name") or "server")
            if self.is_running(s["id"]):
                running.append(s.get("name") or "server")
        self.save()
        if changed:
            self.emit(">>> profile %s applied to %s." % (prof["name"], ", ".join(changed)))
        return {"ok": True, "applied": changed, "restart": running}

    def upsert(self, body):
        sid = body.get("id")
        s = self.find(sid) if sid else None
        _via_before = ((s or {}).get("local", {}) or {}).get("cwd") or ""
        if not s:
            s = blank_server(body.get("kind") or "local", body.get("name") or "New server")
            self.servers().append(s)
        for k in ("name", "kind"):
            if k in body:
                s[k] = body[k]
        self._apply_look(s, body)
        for group in ("local", "sftp", "rcon", "chat"):
            if isinstance(body.get(group), dict):
                s.setdefault(group, {}).update(body[group])
        if isinstance(body.get("minehut"), dict):
            mh = s.setdefault("minehut", {})
            incoming = dict(body["minehut"])
            # blank means "leave what is stored alone", not "clear it"
            for secret in ("token", "session_id"):
                if not (incoming.get(secret) or "").strip():
                    incoming.pop(secret, None)
            incoming.pop("has_token", None)
            incoming.pop("has_session", None)
            mh.update(incoming)
        if s.get("kind") == "sftp" and not s["sftp"].get("save_password"):
            s["sftp"]["password"] = ""
        self.backends.pop(s["id"], None)
        self.save()
        # A local server that has just been given its folder is the moment to
        # put the Via plugins in: any later and someone has already started it
        # and wondered why old clients cannot join. Only on a folder that
        # changed, so re-saving a server does not re-run this every time, and
        # off the request thread so saving stays instant.
        # Created or updated, both count: the plugins are checked whenever a
        # local server is saved with a folder set. When everything is already
        # current this costs three index lookups and no downloads, which is
        # cheap enough to be worth the guarantee.
        after = (s.get("local", {}) or {}).get("cwd") or ""
        if (s.get("kind") == "local" and after
                and self.cfg.get("auto_via", True)):
            self.install_via_async(s["id"])
        _ = _via_before
        return s

    def _apply_look(self, s, body):
        """Profile and library look from a request body; unknown values are refused."""
        if "profile" in body:
            pid = body.get("profile") or ""
            if pid and not any(p["id"] == pid for p in self.cfg.get("profiles", [])):
                raise RuntimeError("No such profile.")
            s["profile"] = pid
        if isinstance(body.get("look"), dict):
            look = s.setdefault("look", {"banner": "", "icon": ""})
            if body["look"].get("banner", None) in LOOK_BANNERS:
                look["banner"] = body["look"]["banner"]
            icon = body["look"].get("icon", None)
            # "" is automatic; otherwise an item or block the textures can draw
            if icon == "" or (HAVE_TEX and isinstance(icon, str) and texmod.KEY_RE.match(icon)
                              and (not texmod.ready(str(TEX_ROOT)) or texmod.known(str(TEX_ROOT), icon))):
                look["icon"] = icon

    def set_look(self, body):
        """Change only the profile or look of a server. Unlike upsert this
        touches nothing else, so it never sets off the Via install."""
        s = self.find((body or {}).get("id"))
        if not s:
            raise RuntimeError("No such server.")
        self._apply_look(s, body)
        self.save()
        return s

    def delete_server(self, sid):
        if len(self.servers()) <= 1:
            raise RuntimeError("Keep at least one server on the shelf.")
        s = self.find(sid)
        if not s:
            raise RuntimeError("No such server.")
        if self.is_running(sid):
            raise RuntimeError("Stop it before removing it.")
        be = self.backends.pop(sid, None)
        if be:
            be.close()
        # Removing the server must also close the hole we opened for it,
        # otherwise the record of that mapping disappears with the config entry
        # and nothing is left that knows to take it down.
        mgr = self.hosts.pop(sid, None)
        if mgr:
            try:
                mgr.shutdown()
            except Exception:
                pass
        self.cfg["servers"] = [x for x in self.servers() if x["id"] != sid]
        if self.cfg.get("active") == sid:
            self.cfg["active"] = self.servers()[0]["id"]
        self.save()
        return True

    def duplicate(self, sid):
        s = self.find(sid)
        if not s:
            raise RuntimeError("No such server.")
        copy = json.loads(json.dumps(s))
        copy["id"] = new_id()
        copy["name"] = (s.get("name") or "server") + " copy"
        copy["created"] = int(time.time())
        for h in copy.get("discord", {}).get("hooks", []):
            h["id"] = new_id()
        # The mapping register names one hole in the router. Two servers both
        # claiming it would each try to remove it, and the second removal would
        # be aimed at whatever had taken the port over by then.
        hosting = default_hosting(copy.setdefault("hosting", {}))
        hosting["mappings"] = []
        hosting["mode"] = "lan" if hosting.get("mode") == "internet" else hosting.get("mode")
        hosting["auto_enable"] = False
        self.servers().append(copy)
        self.save()
        return copy

    def activate(self, sid):
        s = self.find(sid)
        if not s:
            raise RuntimeError("No such server.")
        if self.cfg.get("active") == sid:
            return s
        self._stop_remote_tail()
        self.cfg["active"] = sid
        self.save()
        # No console or player clearing here any more: the server being switched
        # away from may still be running, and its runtime is its own.
        self.remote_size = 0
        self.emit(">>> switched to %s" % (s.get("name") or "server"))
        if s.get("kind") == "sftp":
            threading.Thread(target=self._start_remote_tail, daemon=True).start()
        return s

    # -- backends ----------------------------------------------------------
    def backend(self, sid=None):
        s = self.server(sid)
        be = self.backends.get(s["id"])
        want = s.get("kind", "local")
        if be is None or be.kind != want:
            if be:
                be.close()
            be = LocalBackend(s["local"]) if want == "local" else SftpBackend(s["sftp"])
            self.backends[s["id"]] = be
        else:
            be.cfg = s["local"] if want == "local" else s["sftp"]
        return be

    def connect(self, sid=None):
        be = self.backend(sid)
        if be.kind == "local":
            ok = be.online()
            self.emit(">>> %s" % ("server folder ready" if ok else "set the server folder first"))
            return ok, "Ready." if ok else "Set the server folder in Servers."
        self.emit(">>> connecting to %s …" % be.label())
        ok, msg = be.connect()
        if ok and getattr(be, "note", ""):
            # the backend corrected the folder; keep the fix
            self.server(sid)["sftp"]["root"] = be.cfg.get("root", "")
            be.note = ""
            self.save()
        self.emit((">>> " if ok else "!!! ") + msg)
        if ok:
            self.remote_size = 0
            threading.Thread(target=self._start_remote_tail, daemon=True).start()
        return ok, msg

    def disconnect(self, sid=None):
        self._stop_remote_tail()
        be = self.backend(sid)
        be.close()
        self.emit(">>> disconnected.")
        return True, "Disconnected."

    # -- console buffer ----------------------------------------------------
    def emit(self, text, sid=None):
        rt = self.runtime(sid)
        with rt.log_lock:
            rt.seq += 1
            rt.buf.append((rt.seq, text))
        rt.last_line_at = time.time()

    def since(self, last_seq, sid=None):
        rt = self.runtime(sid)
        with rt.log_lock:
            return [(s, t) for s, t in rt.buf if s > last_seq]

    def recent(self, n=200, sid=None):
        rt = self.runtime(sid)
        with rt.log_lock:
            return list(rt.buf)[-n:]

    # -- log ingestion: one place, both transports -------------------------
    def ingest(self, line, sid=None):
        """Parse a server log line, fire the events it implies, then store it."""
        srv = (self.find(sid) if sid else None) or self.server()
        rt = self.runtime(sid)
        f = srv.get("features", {})
        m = JOIN_RE.search(line)
        if m:
            who = m.group(1)
            rt.players.add(who)
            rt.player_seen[who] = time.time()
            w = f.get("welcome", {})
            if w.get("enabled") and (w.get("message") or "").strip():
                self.command(w["message"].replace("{player}", who).strip()
                             if w["message"].startswith("/")
                             else "say " + w["message"].replace("{player}", who).strip(),
                             quiet=True)
            self.discord.event("join_leave", "%s joined" % who, "", who,
                               [("Online", str(len(rt.players)), True)])
        elif LEAVE_RE.search(line):
            who = LEAVE_RE.search(line).group(1)
            rt.players.discard(who)
            self.discord.event("join_leave", "%s left" % who, "", who,
                               [("Online", str(len(rt.players)), True)])
        else:
            cm = parse_chat(line, self.chat_pattern())
            if cm:
                who, said = cm
                rt.chat_seen += 1
                rt.last_chat = "%s: %s" % (who, said[:60])
                self.discord.chat(who, said)
                _aicfg = self.cfg.get("ai", {})
                if _aicfg.get("automod") and (_aicfg.get("api_key")
                                              or _aicfg.get("provider") in self.LOCAL_PROVIDERS):
                    self._automod_q.append((who, said))
            else:
                am = ADVANCE_RE.search(line)
                dm = DEATH_RE.search(line)
                if am:
                    self.discord.event("advancement", "%s earned an advancement" % am.group(1),
                                       am.group(2), am.group(1))
                elif dm and dm.group(1) in rt.players:
                    self.discord.event("death", "%s died" % dm.group(1),
                                       "%s %s" % (dm.group(1), dm.group(2)), dm.group(1))
        if DONE_RE.search(line):
            rt.done_ms = DONE_RE.search(line).group(1)
            self.discord.event("start_stop", "Server is up",
                               "Ready in %ss" % rt.done_ms, "",
                               [("Server", srv.get("name") or "—", True)])
        if ERROR_RE.search(line):
            self.discord.event("console", "Console error", line[-1500:])
        elif LAG_RE.search(line):
            self.discord.event("lag", "Server is lagging", strip_codes(line)[-1500:])

        clean_line = strip_codes(line)
        pm = VANILLA_PUNISH_RE.search(clean_line)
        if pm:
            self.discord.event("punish", "%s %s" % (pm.group(1), pm.group(2)),
                               pm.group(3) or "", pm.group(2))
        else:
            pm = PUNISH_RE.search(clean_line)
            if pm and pm.group(2):
                who, what, by, why = pm.group(1), pm.group(2).lower(), pm.group(3), pm.group(4)
                self.discord.event("punish", "%s was %s" % (who, what), why or "", who,
                                   [("By", by or "unknown", True)])
        cm2 = INGAME_CMD_RE.search(clean_line)
        if cm2:
            self.discord.event("ingame_cmd", "%s ran a command" % cm2.group(1),
                               "`%s`" % cm2.group(2)[:400], cm2.group(1))
        wm = WHITELIST_RE.search(clean_line)
        if wm:
            self.discord.event("player_act", "Whitelist: %s %s" % (wm.group(1).lower(), wm.group(2)),
                               "", wm.group(2))
        fj = FIRST_JOIN_RE.search(clean_line)
        if fj and fj.group(1) not in rt.known_players:
            rt.known_players.add(fj.group(1))
            self.discord.event("first_join", "%s joined for the first time" % fj.group(1),
                               "", fj.group(1))
        self.emit(line, sid)

    # -- memory ------------------------------------------------------------
    def _ram_protected(self):
        """PIDs this panel must never offer to close: itself, and its servers.

        Java is refused by name inside ramtools as well, so a server survives
        even if its pid is not known here - belt and braces, because the cost
        of getting this wrong is somebody's world.
        """
        mine = {os.getpid()}
        try:
            mine.add(os.getppid())
        except (AttributeError, OSError):
            pass
        servers = set()
        for rt in self.runtimes.values():
            proc = getattr(rt, "proc", None)
            if proc is not None and proc.poll() is None:
                servers.add(proc.pid)
        return mine, servers

    def ram_state(self):
        if not HAVE_RAM:
            return {"supported": False, "error": RAM_ERROR}
        mine, servers = self._ram_protected()
        state = rammod.snapshot(protected_pids=mine, server_pids=servers)
        state["error"] = ""
        return state

    def ram_close(self, name, force=False):
        """Close one application by name. Never automatic, never implicit."""
        if not HAVE_RAM:
            raise RuntimeError(RAM_ERROR)
        mine, servers = self._ram_protected()
        before = rammod.memory().get("free_mb", 0)
        res = rammod.close(name, protected_pids=mine, server_pids=servers,
                           force=force)
        after = rammod.memory().get("free_mb", 0)
        res["freed_mb"] = max(0, after - before)
        res["free_mb"] = after
        self.emit(">>> memory: %s (%+d MB free, now %d MB)"
                  % (res.get("msg", name), res["freed_mb"], after))
        return res

    # -- Via plugins -------------------------------------------------------
    def via_auto(self, on=None):
        """Whether a newly-pointed server gets the Via plugins by itself."""
        if on is not None:
            self.cfg["auto_via"] = bool(on)
            self.save()
        return bool(self.cfg.get("auto_via", True))

    def via_state(self, sid=None):
        """What the button should say before anything is downloaded."""
        if not HAVE_VIA:
            return {"available": False, "reason": VIA_ERROR}
        srv = self.server(sid)
        if srv.get("kind") != "local":
            return {"available": False, "compatible": False,
                    "reason": "This server runs on someone else's machine, so "
                              "its plugins folder is not ours to write to."}
        lc = srv.get("local", {}) or {}
        cwd = lc.get("cwd") or ""
        if not cwd:
            return {"available": False, "compatible": False,
                    "reason": "Set the server folder first."}
        info = viamod.detect(cwd, lc.get("jar") or "")
        info["available"] = True
        info["installed"] = self._via_installed(cwd)
        return info

    @staticmethod
    def _via_installed(cwd):
        """Which Via jars are already sitting in plugins/, by name."""
        out = []
        pdir = os.path.join(cwd or "", "plugins")
        try:
            entries = os.listdir(pdir)
        except OSError:
            return out
        for _, pretty in (viamod.VIA_PLUGINS if HAVE_VIA else ()):
            for entry in entries:
                if entry.lower().endswith(".jar") and \
                        entry.lower().startswith(pretty.lower()):
                    out.append(entry)
                    break
        return out

    def install_via(self, sid=None):
        """Install or update the Via plugins for one server.

        Everything it does is echoed into that server's console, so the record
        of what was downloaded, skipped or replaced lives where the user is
        already looking.
        """
        if not HAVE_VIA:
            raise RuntimeError(VIA_ERROR)
        srv = self.server(sid)
        key = srv["id"]
        if srv.get("kind") != "local":
            raise RuntimeError("Only local servers have a plugins folder here.")
        lc = srv.get("local", {}) or {}
        cwd = lc.get("cwd") or ""
        if not cwd or not Path(cwd).is_dir():
            raise RuntimeError("Set the server folder first.")
        report = viamod.install(cwd, lc.get("jar") or "",
                                log=lambda m: self.emit(m, key))
        report["installed_files"] = self._via_installed(cwd)
        return report

    def install_pack(self, sid=None):
        """Install the recommended set for whatever this server turns out to be.

        Bukkit servers get Via plus the permissions/placeholder staples; proxies
        get the proxy-side equivalents; Fabric gets performance mods into mods/.
        """
        if not HAVE_VIA:
            raise RuntimeError(VIA_ERROR)
        srv = self.server(sid)
        key = srv["id"]
        if srv.get("kind") != "local":
            raise RuntimeError("Only local servers have a folder to install into.")
        lc = srv.get("local", {}) or {}
        cwd = lc.get("cwd") or ""
        if not cwd or not Path(cwd).is_dir():
            raise RuntimeError("Set the server folder first.")
        report = viamod.install_preset(cwd, lc.get("jar") or "",
                                       log=lambda m: self.emit(m, key))
        report["installed_files"] = self._via_installed(cwd)
        return report

    def install_via_async(self, sid=None, quiet=True):
        """Fire and forget, for the paths that must not block a save."""
        if not HAVE_VIA:
            return

        key = self.server(sid)["id"]
        if key in self._via_busy:
            return

        def run():
            try:
                self.install_pack(key)
            except Exception as e:                              # pragma: no cover
                if not quiet:
                    self.emit(">>> Package installer: %s" % e, key)
            finally:
                self._via_busy.discard(key)

        self._via_busy.add(key)
        threading.Thread(target=run, daemon=True).start()

    # -- updates from GitHub -----------------------------------------------
    # Checking and installing happen here, on a thread, whenever the settings
    # say so. Restarting does not: it closes the window and stops servers, so
    # it is the user's button (or, if they asked for it, done only while no
    # server is running).
    def update_cfg(self):
        raw = self.cfg.setdefault("updates", {})
        return updmod.settings(raw) if HAVE_UPDATER else {}

    def update_supported(self):
        if not HAVE_UPDATER:
            return False, UPDATER_ERROR
        if getattr(sys, "frozen", False):
            return False, ("This is a packaged Lodestone-Engine.exe; it cannot replace itself. "
                           "Run lodestone.py from the source folder to get automatic updates.")
        if not os.access(APP_ROOT, os.W_OK):
            return False, "Lodestone's folder (%s) is not writable." % APP_ROOT
        return True, ""

    def _upd_log(self, msg):
        self.upd["log"].append("%s  %s" % (datetime.now().strftime("%H:%M:%S"), msg))

    def update_state(self):
        ok, why = self.update_supported()
        st = self.update_cfg()
        safe = {k: v for k, v in st.items() if k != "token"}
        safe["has_token"] = bool(st.get("token"))
        stamp = updmod.stamp(APP_ROOT) if HAVE_UPDATER else {}
        nxt = (self.upd["checked_at"] + st.get("hours", 6) * 3600) if st.get("enabled") else 0
        return {"supported": ok, "reason": why, "settings": safe, "root": APP_ROOT,
                "state": self.upd["state"], "info": self.upd["info"], "error": self.upd["error"],
                "pct": self.upd["pct"], "applied": self.upd["applied"],
                "installed": {k: v for k, v in stamp.items() if k != "files"},
                "checked_at": self.upd["checked_at"], "next_check": nxt,
                "can_rollback": bool(HAVE_UPDATER and updmod.latest_backup(APP_ROOT)),
                "running_servers": len(self.running_ids()),
                "log": list(self.upd["log"])[-40:]}

    def update_settings(self, data):
        raw = self.cfg.setdefault("updates", {})
        for k in ("enabled", "auto_install", "auto_restart", "use_git_login"):
            if k in data:
                raw[k] = bool(data[k])
        for k in ("repo", "branch", "path"):
            if k in data:
                raw[k] = str(data[k] or "").strip()
        if "hours" in data:
            try:
                raw["hours"] = max(1, min(168, int(data["hours"])))
            except (TypeError, ValueError):
                raise RuntimeError("Check every N hours: N must be a number.")
        # Blank means "leave the saved token alone"; "-" clears it.
        tok = (data.get("token") or "").strip()
        if tok == "-":
            raw["token"] = ""
        elif tok:
            raw["token"] = tok
        if HAVE_UPDATER:
            updmod.forget_login()
        self.save()
        return self.update_state()

    def update_check(self):
        ok, why = self.update_supported()
        if not ok:
            raise RuntimeError(why)
        if not self._upd_lock.acquire(blocking=False):
            raise RuntimeError("An update check is already running.")
        try:
            self.upd["state"] = "checking"
            self.upd["error"] = ""
            info = updmod.check(APP_ROOT, self.cfg.get("updates"))
            self.upd["info"] = info
            self.upd["checked_at"] = time.time()
            if self.upd["applied"] and not info["available"]:
                self.upd["state"] = "ready"
            else:
                self.upd["state"] = "available" if info["available"] else "current"
            self._upd_log("checked %s@%s: %s" % (info["repo"], info["branch"],
                          "update available" if info["available"] else "up to date"))
            return self.update_state()
        except updmod.UpdateError as e:
            self.upd["state"] = "error"
            self.upd["error"] = str(e)
            self.upd["checked_at"] = time.time()
            self._upd_log("check failed: %s" % e)
            raise RuntimeError(str(e))
        finally:
            self._upd_lock.release()

    def update_install(self):
        """Download and put the new files in place. Takes effect on restart."""
        ok, why = self.update_supported()
        if not ok:
            raise RuntimeError(why)
        info = self.upd["info"]
        if not info or not info.get("available"):
            info = updmod.check(APP_ROOT, self.cfg.get("updates"))
            self.upd["info"] = info
            if not info["available"]:
                self.upd["state"] = "ready" if self.upd["applied"] else "current"
                return self.update_state()
        if not self._upd_lock.acquire(blocking=False):
            raise RuntimeError("An update is already being installed.")
        try:
            self.upd["state"] = "installing"
            self.upd["pct"] = 0
            log = self._upd_log
            if info["mode"] == "git":
                rep = updmod.apply_git(APP_ROOT, self.cfg.get("updates"), log=log)
            else:
                stage = updmod.download(APP_ROOT, self.cfg.get("updates"), info["latest"]["sha"], log=log,
                                        progress=lambda d, t: self.upd.__setitem__("pct", int(d * 100 / t) if t else 50))
                rep = updmod.apply(APP_ROOT, stage, info["latest"], self.cfg.get("updates"), log=log)
            rep["title"] = (info.get("latest") or {}).get("title", "")
            rep["count"] = len(info.get("commits") or [])
            self.upd["pct"] = 100
            self.upd["info"] = dict(info, available=False, current=info.get("latest"), behind=0, commits=[])
            if not (rep["changed"] or rep["added"] or rep["removed"]):
                # A copy put here by hand that already matched: now it is
                # stamped, and there is nothing to restart for.
                self.upd["state"] = "current"
                self._upd_log("already had %s - stamped it, nothing to restart for" % rep["sha"][:7])
                return self.update_state()
            self.upd["applied"] = rep
            self.upd["state"] = "ready"
            self.emit(">>> Lodestone update %s installed - restart Lodestone to use it." % rep["sha"][:7])
            return self.update_state()
        except updmod.UpdateError as e:
            self.upd["state"] = "error"
            self.upd["error"] = str(e)
            self._upd_log("install failed: %s" % e)
            raise RuntimeError(str(e))
        finally:
            self._upd_lock.release()

    def update_rollback(self):
        backup = updmod.latest_backup(APP_ROOT) if HAVE_UPDATER else ""
        if not backup:
            raise RuntimeError("There is no earlier version to go back to.")
        undo = updmod.rollback(APP_ROOT, backup)
        shutil.rmtree(backup, ignore_errors=True)
        self.upd["applied"] = {"sha": undo.get("from") or "", "rolled_back": True, "desktop": True,
                               "backup": "", "changed": undo.get("restore") or [], "added": [], "removed": []}
        os.makedirs(os.path.join(APP_ROOT, updmod.WORK), exist_ok=True)
        open(os.path.join(APP_ROOT, updmod.WORK, updmod.REBUILD_FLAG), "w").close()
        self.upd["state"] = "ready"
        self._upd_log("went back to %s" % ((undo.get("from") or "the version before")[:7]))
        return self.update_state()

    def update_restart(self, force=False, app_pid=0):
        """Quit, and leave updater.py --relaunch to start the new version."""
        ok, why = self.update_supported()
        if not ok:
            raise RuntimeError(why)
        running = self.running_ids()
        if running and not force:
            names = ", ".join((self.find(sid) or {}).get("name") or "a server" for sid in running)
            return {"ok": False, "needs_force": True,
                    "error": "%s %s running. Restarting Lodestone stops %s first."
                             % (names, "is" if len(running) == 1 else "are",
                                "it" if len(running) == 1 else "them")}
        port = SERVER.server_address[1] if SERVER else int(self.cfg.get("port") or DEFAULT_PORT)
        backup = (self.upd["applied"] or {}).get("backup") or ""
        pids = [os.getpid()] + ([int(app_pid)] if app_pid else [])
        updmod.spawn_relaunch(APP_ROOT, pids, port, backup, "desktop" if app_pid else "browser")
        self._upd_log("restarting into the new version")

        def bye():
            time.sleep(0.4)
            for sid in running:
                try:
                    self.stop(sid)
                except Exception:
                    pass
            for _ in range(80):
                if not self.running_ids():
                    break
                time.sleep(0.5)
            try:
                self.hosting_shutdown()
            except Exception:
                pass
            if SERVER:
                SERVER.shutdown()

        threading.Thread(target=bye, daemon=True).start()
        return {"ok": True, "msg": "Restarting Lodestone - it will be back in a moment."}

    def _update_loop(self):
        time.sleep(25)                      # let the panel come up first
        while True:
            try:
                st = self.update_cfg()
                ok, _ = self.update_supported()
                due = time.time() - self.upd["checked_at"] >= st.get("hours", 6) * 3600
                due = due and not self.perf_mode()      # Development Performance Mode defers it
                if ok and st.get("enabled") and due and self.upd["state"] not in ("checking", "installing"):
                    try:
                        self.update_check()
                    except RuntimeError:
                        pass
                    if self.upd["state"] == "available" and st.get("auto_install"):
                        try:
                            self.update_install()
                        except RuntimeError:
                            pass
                if (ok and st.get("auto_restart") and self.upd["state"] == "ready"
                        and self.upd["applied"] and not self.running_ids()):
                    self._upd_log("no server running - restarting into the update by itself")
                    self.update_restart(False, 0)
                    return
            except Exception as e:                              # pragma: no cover
                self._upd_log("update loop: %s" % e)
            time.sleep(60)

    # -- long jobs -------------------------------------------------------
    # Creating a server or fetching a Java runtime takes minutes, so it runs on
    # a thread and the UI polls it. A job keeps its own log as well as echoing
    # into a console, because the server it is building does not exist yet.
    def _job_new(self, kind, title, steps=()):
        job = {"id": new_id(), "kind": kind, "title": title, "state": "running",
               "steps": [{"key": k, "label": label, "state": "pending", "detail": ""}
                         for k, label in steps],
               "pct": 0, "log": [], "error": "", "warnings": [], "result": None,
               "started": time.time(), "finished": 0}
        with self._jobs_lock:
            done = sorted((j for j in self.jobs.values() if j["state"] != "running"),
                          key=lambda j: j["finished"])
            for old in done[:-12]:
                self.jobs.pop(old["id"], None)
            self.jobs[job["id"]] = job
        return job

    @staticmethod
    def _job_log(job, text):
        job["log"].append(text)
        if len(job["log"]) > 1500:
            del job["log"][:300]
            job["log_base"] = job.get("log_base", 0) + 300

    @staticmethod
    def _job_step(job, key, state, detail=""):
        for st in job["steps"]:
            if st["key"] == key:
                st["state"] = state
                if detail:
                    st["detail"] = detail
            elif state == "running" and st["state"] == "running":
                st["state"] = "done"

    def job_get(self, jid, since=0):
        job = self.jobs.get(jid)
        if not job:
            raise RuntimeError("No such job - it may have finished before the panel restarted.")
        base = job.get("log_base", 0)
        start = max(0, int(since or 0) - base)
        out = {k: v for k, v in job.items() if k != "log" and not k.startswith("_")}
        out["log"] = job["log"][start:]
        out["next"] = base + len(job["log"])
        return out

    def jobs_list(self):
        return [{k: v for k, v in j.items() if k != "log" and not k.startswith("_")}
                for j in sorted(self.jobs.values(), key=lambda j: -j["started"])]

    # -- creating servers --------------------------------------------------
    def _need_create(self):
        if not HAVE_CREATE:
            raise RuntimeError(CREATE_ERROR)

    def servers_root(self):
        """Where a new server goes by default: beside the existing local ones.

        Most people keep their servers in one folder, so the parent most of
        them share is the natural place. With none, a folder in the home
        directory, named so it is obvious what made it.
        """
        parents = {}
        for s in self.servers():
            cwd = (s.get("local") or {}).get("cwd") or ""
            if s.get("kind") == "local" and cwd:
                parent = str(Path(cwd).parent)
                parents[parent] = parents.get(parent, 0) + 1
        if parents:
            best = max(parents.items(), key=lambda kv: kv[1])[0]
            if Path(best).is_dir():
                return best
        return str(Path.home() / "Lodestone Servers")

    def create_catalog(self, snapshots=False):
        self._need_create()
        try:
            cat = createmod.catalog(include_snapshots=bool(snapshots))
        except createmod.CreateError as e:
            raise RuntimeError(str(e))
        cat["default_dir"] = self.servers_root()
        cat["used_ports"] = sorted({p for p in (self._local_port(s) for s in self.servers()
                                                 if s.get("kind") == "local") if p})
        cat["ram_mb"] = self._ram_total_mb()
        # The Via installer's recommended sets, offered as one click in the
        # wizard. Modrinth slugs; the browser resolves them per version.
        cat["presets"] = {}
        if HAVE_VIA:
            for sw in createmod.SOFTWARE_IDS:
                plat = viamod.platform_of(sw)
                if plat in viamod.PRESETS:
                    cat["presets"][sw] = [{"source": "modrinth", "project_id": slug,
                                           "name": viamod.package_name(slug)}
                                          for slug in viamod.PRESETS[plat]]
        return cat

    def create_builds(self, software, mc):
        self._need_create()
        try:
            return {"items": createmod.builds(software, mc),
                    "java": createmod.java_required(mc)}
        except createmod.CreateError as e:
            raise RuntimeError(str(e))

    @staticmethod
    def _ram_total_mb():
        if HAVE_RAM:
            try:
                return int(rammod.memory().get("total_mb") or 0)
            except Exception:
                return 0
        return 0

    def java_list(self, mc="", software=""):
        """Installed Javas, what this Minecraft needs, and the one to use."""
        self._need_create()
        extra = []
        for s in self.servers():
            j = (s.get("local") or {}).get("java") or ""
            if os.sep in j or "/" in j:
                extra.append(j)
        javas = createmod.find_javas(str(JAVA_ROOT), extra)
        need = createmod.java_required(mc) if mc else 0
        for j in javas:
            ok, note = createmod.java_fits(j["major"], need, software, mc) if need else (True, "")
            j["fits"], j["note"] = ok, note
        pick = createmod.pick_java(javas, need, software, mc) if need else (javas[0] if javas else None)
        return {"items": javas, "need": need, "pick": (pick or {}).get("path", ""),
                "managed_root": str(JAVA_ROOT)}

    # -- instance icons: Minecraft's items and blocks --------------------------
    def art_icons(self):
        """What can be an icon, grouped, and whether the textures are here yet."""
        if not HAVE_TEX:
            return {"ready": False, "busy": False, "error": "The texture module is missing.", "groups": [], "items": []}
        root = str(TEX_ROOT)
        items = texmod.catalog(root)
        counts = {}
        for it in items:
            counts[it["group"]] = counts.get(it["group"], 0) + 1
        groups = [{"key": g, "label": g, "count": counts[g]} for g in texmod.GROUPS if g in counts]
        return dict(texmod.status(root), groups=groups, items=items)

    def art_fetch(self, force=False):
        if not HAVE_TEX:
            raise RuntimeError("The texture module is missing.")
        started = texmod.ensure_async(str(TEX_ROOT), force=bool(force))
        return dict(texmod.status(str(TEX_ROOT)), started=started)

    # -- adding a server that already exists --------------------------------
    def detect_folder(self, path):
        """Look at a server folder and say what it is, for "Add a server I have".

        The fields come back ready to save: the jar (or Forge args file), the
        software and Minecraft version, the port, a Java that fits and a RAM
        figure for this PC. A folder already in the library says which entry.
        """
        self._need_create()
        r = createmod.detect_folder(path)
        if r.get("path"):
            want = os.path.normcase(os.path.realpath(r["path"]))
            for s in self.servers():
                cwd = (s.get("local") or {}).get("cwd") or ""
                if s.get("kind") == "local" and cwd and os.path.normcase(os.path.realpath(cwd)) == want:
                    r["existing"] = {"id": s["id"], "name": s.get("name") or "server"}
                    break
        ram = self._ram_total_mb()
        r["ram_mb"] = ram
        if not r.get("max_ram"):
            r["max_ram"] = "6G" if ram >= 16000 else "4G" if ram >= 8000 or not ram else "2G"
        r["java"], r["java_need"], r["java_note"] = "", 0, ""
        if r.get("ok") and r.get("mc"):
            need = createmod.java_guess(r["mc"])
            r["java_need"] = need
            try:
                javas = createmod.find_javas(str(JAVA_ROOT))
            except Exception:
                javas = []
            pick = createmod.pick_java(javas, need, r.get("software") or "", r["mc"])
            if pick:
                r["java"] = pick["path"]
                r["java_note"] = "Java %d found." % pick["major"]
            else:
                r["java_note"] = ("Minecraft %s wants Java %d and none that fits is installed. "
                                  "Lodestone can download it for you." % (r["mc"], need))
        return r

    def add_local(self, body):
        """Put an existing server folder in the library, from detect_folder's fields."""
        self._need_create()
        path = os.path.abspath(os.path.expanduser(str(body.get("path") or "").strip()))
        if not path or not os.path.isdir(path):
            raise RuntimeError("That folder does not exist.")
        want = os.path.normcase(os.path.realpath(path))
        for s in self.servers():
            cwd = (s.get("local") or {}).get("cwd") or ""
            if s.get("kind") == "local" and cwd and os.path.normcase(os.path.realpath(cwd)) == want:
                raise RuntimeError("%s is already in your library." % (s.get("name") or "That folder"))
        jar = str(body.get("jar") or "").strip()
        if jar and not os.path.isabs(jar):
            jar = os.path.join(path, jar)
        args = str(body.get("args_file") or "").strip()
        if jar and not os.path.isfile(jar):
            raise RuntimeError("The server jar %s is not there." % os.path.basename(jar))
        if args and not os.path.isfile(os.path.join(path, args)):
            raise RuntimeError("The Forge args file %s is not there." % args)
        if not jar and not args:
            raise RuntimeError("Pick the server jar - this folder has none Lodestone recognises.")
        software = str(body.get("software") or "").strip().lower()
        for k in ("min_ram", "max_ram"):
            v = str(body.get(k) or "").strip()
            if v and not re.fullmatch(r"\d+[MmGg]", v):
                raise RuntimeError("%s must look like 512M or 4G." % k.replace("_", " ").title())
        local = {"cwd": path, "jar": jar if not args else "", "args_file": args,
                 "server_type": software or "vanilla",
                 "mc_version": str(body.get("mc") or "").strip(),
                 "loader_version": str(body.get("loader_version") or "").strip(),
                 "java": str(body.get("java") or "").strip() or "java",
                 "max_ram": str(body.get("max_ram") or "").strip() or "4G",
                 "min_ram": str(body.get("min_ram") or "").strip() or "1G"}
        name = str(body.get("name") or "").strip() or os.path.basename(path.rstrip("\\/")) or "My server"
        s = self.upsert({"kind": "local", "name": name, "local": local})
        self.emit(">>> added %s from %s" % (name, path))
        return s

    def java_install(self, major):
        """Fetch a Temurin runtime on a job; the UI polls it."""
        self._need_create()
        try:
            major = int(major)
        except (TypeError, ValueError):
            raise RuntimeError("Which Java version?")
        if major < 8 or major > 40:
            raise RuntimeError("Java %d is not a version Lodestone can fetch." % major)
        job = self._job_new("java", "Java %d" % major, [("java", "Download Java %d" % major)])

        def run():
            self._job_step(job, "java", "running")
            try:
                path = createmod.install_java(
                    major, str(JAVA_ROOT), log=lambda m: self._job_log(job, m),
                    progress=lambda d, t: job.__setitem__("pct", int(d * 100 / t) if t else 0))
                job["result"] = {"path": path, "major": major}
                self._job_step(job, "java", "done", path)
                job["state"] = "done"
                job["pct"] = 100
            except Exception as e:
                job["error"] = str(e)
                job["state"] = "failed"
                self._job_step(job, "java", "failed", str(e))
                self._job_log(job, "!!! %s" % e)
            job["finished"] = time.time()

        threading.Thread(target=run, daemon=True).start()
        return job["id"]

    RAM_RE = re.compile(r"^\s*(\d{1,6})\s*([MmGg])\s*$")

    def _ram_mb(self, text, what):
        m = self.RAM_RE.match(str(text or ""))
        if not m:
            raise RuntimeError("%s must look like 2G or 2048M." % what)
        n = int(m.group(1))
        return n * 1024 if m.group(2).lower() == "g" else n

    def create_validate(self, b):
        """Everything that can be checked before anything is written.

        Raises with a sentence the wizard can show next to the field.
        """
        self._need_create()
        spec = {}
        name = (b.get("name") or "").strip()
        if not name:
            raise RuntimeError("Give the server a name.")
        if len(name) > 60:
            raise RuntimeError("Keep the name under 60 characters.")
        spec["name"] = name
        sw = (b.get("software") or "").strip().lower()
        if sw not in createmod.SOFTWARE_IDS:
            raise RuntimeError("Pick the server software.")
        mc = (b.get("mc") or "").strip()
        if not mc:
            raise RuntimeError("Pick a Minecraft version.")
        spec.update(software=sw, mc=mc, build=str(b.get("build") or "").strip())

        folder = (b.get("dir") or "").strip()
        if not folder:
            slug = re.sub(r"[^A-Za-z0-9 ._-]+", "", name).strip().replace(" ", "-") or "server"
            folder = str(Path(self.servers_root()) / slug)
        folder = os.path.expanduser(folder)
        if not os.path.isabs(folder):
            raise RuntimeError("The install folder must be a full path, like %s."
                               % str(Path(self.servers_root()) / "my-server"))
        folder = os.path.abspath(folder)
        if os.path.exists(folder):
            if not os.path.isdir(folder):
                raise RuntimeError("%s is a file, not a folder." % folder)
            if any(n for n in os.listdir(folder) if n not in (".DS_Store", "desktop.ini", "Thumbs.db")):
                raise RuntimeError("%s already has files in it. Pick an empty or new folder so "
                                   "nothing of yours gets overwritten." % folder)
        for s in self.servers():
            cwd = (s.get("local") or {}).get("cwd") or ""
            if s.get("kind") == "local" and cwd and os.path.normcase(os.path.abspath(cwd)) == \
                    os.path.normcase(folder):
                raise RuntimeError("%s already uses that folder." % (s.get("name") or "Another server"))
        spec["dir"] = folder

        min_mb = self._ram_mb(b.get("min_ram") or "1G", "Minimum RAM")
        max_mb = self._ram_mb(b.get("max_ram") or "4G", "Maximum RAM")
        if max_mb < 512:
            raise RuntimeError("Give it at least 512M - Minecraft will not start with less.")
        if min_mb > max_mb:
            raise RuntimeError("Minimum RAM is more than maximum RAM.")
        total = self._ram_total_mb()
        if total and max_mb > total:
            raise RuntimeError("This PC has %d MB of memory; %d MB is more than it has." % (total, max_mb))
        spec["min_ram"] = (b.get("min_ram") or "1G").strip().upper()
        spec["max_ram"] = (b.get("max_ram") or "4G").strip().upper()

        try:
            port = int(b.get("port") or 25565)
        except (TypeError, ValueError):
            raise RuntimeError("The port must be a number.")
        if not 1024 <= port <= 65535:
            raise RuntimeError("Pick a port between 1024 and 65535 (25565 is Minecraft's own).")
        for s in self.servers():
            if s.get("kind") == "local" and self._local_port(s) == port and \
                    (s.get("local") or {}).get("cwd"):
                spec.setdefault("warnings", []).append(
                    "%s also uses port %d - they cannot run at the same time." % (s.get("name"), port))
        spec["port"] = port

        java = (b.get("java") or "auto").strip()
        spec["java"] = java
        spec["extra"] = (b.get("extra") or "").strip()
        spec["eula"] = bool(b.get("eula"))
        props = dict(b.get("properties") or {})
        props["server-port"] = port
        spec["properties"] = {k: v for k, v in props.items() if k in createmod.PROPERTY_KEYS}
        content = []
        for c in b.get("content") or []:
            if isinstance(c, dict) and (c.get("project_id") or c.get("id")):
                content.append({"source": c.get("source") or "modrinth",
                                "project_id": c.get("project_id") or c.get("id"),
                                "version_id": c.get("version_id") or None,
                                "name": c.get("name") or ""})
        if content and HAVE_CONTENT:
            prof = contentmod.profile(sw, mc)
            if not prof["kind"]:
                raise RuntimeError(prof["reason"])
            for c in content:
                if c["source"] not in prof["sources"]:
                    raise RuntimeError("%s comes from %s, which has nothing for %s servers."
                                       % (c["name"] or c["project_id"],
                                          contentmod.SOURCE_NAMES.get(c["source"], c["source"]),
                                          prof["label"]))
        spec["content"] = content
        # The UIs switch to the new server themselves (that is what restarts
        # their console stream); scripts can ask for it here.
        spec["activate"] = bool(b.get("activate"))
        return spec

    def create_start(self, body):
        spec = self.create_validate(body or {})
        steps = [("folder", "Create the folder"), ("java", "Find Java"),
                 ("software", "Install %s" % createmod.software_info(spec["software"])["name"]),
                 ("config", "Write the settings")]
        if spec["content"]:
            steps.append(("content", "Install mods & plugins"))
        steps.append(("register", "Add it to Lodestone"))
        job = self._job_new("create", spec["name"], steps)
        job["spec"] = {k: v for k, v in spec.items() if k != "properties"}
        job["warnings"].extend(spec.get("warnings", []))
        threading.Thread(target=self._create_worker, args=(job, spec), daemon=True).start()
        return job["id"]

    def _create_worker(self, job, spec):
        log = lambda m: self._job_log(job, m)
        dest = spec["dir"]
        made = False
        step = "folder"
        weights = {"folder": (0, 2), "java": (2, 15), "software": (15, 75),
                   "config": (75, 78), "content": (78, 96), "register": (96, 100)}

        def at(key, frac=0.0):
            lo, hi = weights[key]
            job["pct"] = int(lo + (hi - lo) * max(0.0, min(1.0, frac)))

        def prog(key):
            return lambda d, t: at(key, (d / t) if t else 0.3)

        try:
            # 1. the folder
            self._job_step(job, "folder", "running")
            at("folder")
            if not os.path.isdir(dest):
                os.makedirs(dest)
                made = True
            log(">>> creating %s in %s" % (spec["name"], dest))
            self._job_step(job, "folder", "done", dest)

            # 2. Java - installers and the server itself both need the right one
            step = "java"
            self._job_step(job, "java", "running")
            at("java")
            need = createmod.java_required(spec["mc"])
            java = spec["java"]
            if java in ("", "auto", "download"):
                pick = None
                if java != "download":
                    pick = createmod.pick_java(createmod.find_javas(str(JAVA_ROOT)), need,
                                               spec["software"], spec["mc"])
                if pick:
                    java = pick["path"]
                    log(">>> using Java %d at %s" % (pick["major"], java))
                else:
                    log(">>> Minecraft %s needs Java %d and none is installed - fetching one"
                        % (spec["mc"], need))
                    java = createmod.install_java(need, str(JAVA_ROOT), log=log, progress=prog("java"))
            else:
                info = createmod.probe_java(java)
                if not info:
                    raise RuntimeError("%s is not a working Java." % java)
                ok, note = createmod.java_fits(info["major"], need, spec["software"], spec["mc"])
                if not ok:
                    raise RuntimeError(note)
                if note:
                    job["warnings"].append(note)
                log(">>> using Java %d at %s" % (info["major"], java))
            self._job_step(job, "java", "done", java)

            # 3. the server software
            step = "software"
            self._job_step(job, "software", "running")
            at("software")
            res = createmod.install_server(spec["software"], spec["mc"], spec["build"], dest,
                                           java=java, log=log, progress=prog("software"),
                                           work_root=str(BUILD_ROOT))
            self._job_step(job, "software", "done", os.path.basename(res["jar"] or res["args_file"]))

            # 4. first-run files
            step = "config"
            self._job_step(job, "config", "running")
            at("config")
            createmod.write_properties(dest, spec["properties"])
            if spec["eula"]:
                createmod.write_eula(dest)
                log(">>> eula.txt accepted (you ticked the box)")
            else:
                job["warnings"].append("The Minecraft EULA is not accepted yet - the server will "
                                       "not start until you accept it.")
            self._job_step(job, "config", "done")
        except Exception as e:
            msg = str(e)
            job["error"] = msg
            job["state"] = "failed"
            self._job_step(job, step, "failed", msg)
            log("!!! %s" % msg)
            # Only take down what this job made: a folder that already existed
            # (empty, as validated) is left exactly where it was.
            if made and os.path.isdir(dest):
                shutil.rmtree(dest, ignore_errors=True)
                log(">>> removed the half-made folder %s" % dest)
            elif os.path.isdir(dest):
                for n in os.listdir(dest):
                    p = os.path.join(dest, n)
                    try:
                        if os.path.isdir(p):
                            shutil.rmtree(p, ignore_errors=True)
                        else:
                            os.remove(p)
                    except OSError:
                        pass
                log(">>> emptied %s again" % dest)
            job["finished"] = time.time()
            return

        # 5. mods and plugins - a failure here leaves a working server, so it is
        # reported, not rolled back.
        if spec["content"]:
            self._job_step(job, "content", "running")
            at("content")
            try:
                prof = contentmod.profile(spec["software"], spec["mc"])
                pl = contentmod.plan(dest, prof, spec["content"])
                for blk in pl["blocked"]:
                    job["warnings"].append(blk["reason"])
                    log("!!! skipped: %s" % blk["reason"])
                job["warnings"].extend(pl["warnings"])
                rep = contentmod.apply(dest, prof, pl, log=log)
                for f in rep["failed"]:
                    job["warnings"].append("%s did not install: %s" % (f["name"], f["error"]))
                n = len(rep["installed"]) + len(rep["updated"])
                state = "done" if not (rep["failed"] or pl["blocked"]) else "warn"
                self._job_step(job, "content", state, "%d installed" % n)
            except Exception as e:
                job["warnings"].append("Mods and plugins were not installed: %s" % e)
                self._job_step(job, "content", "warn", str(e))
                log("!!! mods and plugins: %s" % e)

        # 6. on the shelf
        self._job_step(job, "register", "running")
        at("register")
        srv = blank_server("local", spec["name"])
        lc = srv["local"]
        lc.update({"cwd": dest, "jar": res["jar"], "args_file": res["args_file"],
                   "java": java, "min_ram": spec["min_ram"], "max_ram": spec["max_ram"],
                   "extra": spec["extra"], "server_type": spec["software"],
                   "mc_version": spec["mc"], "loader_version": res.get("loader_version") or ""})
        srv["hosting"]["port"] = spec["port"]
        with self.lock:
            self.servers().append(srv)
            self.save()
        if spec["activate"]:
            try:
                self.activate(srv["id"])
            except Exception:
                pass
        self.emit(">>> created %s (%s %s) in %s"
                  % (spec["name"], createmod.software_info(spec["software"])["name"],
                     spec["mc"], dest), srv["id"])
        for line in job["log"][-60:]:
            self.emit(line, srv["id"])
        self._job_step(job, "register", "done")
        job["result"] = {"server_id": srv["id"], "dir": dest, "java": java,
                         "jar": res["jar"], "args_file": res["args_file"]}
        job["pct"] = 100
        job["state"] = "done"
        job["finished"] = time.time()
        self.discord.event("start_stop", "Server created", "%s - %s %s"
                           % (spec["name"], createmod.software_info(spec["software"])["name"], spec["mc"]))

    # -- mods and plugins --------------------------------------------------
    def _need_content(self):
        if not HAVE_CONTENT:
            raise RuntimeError(CONTENT_ERROR)

    def content_profile(self, sid=None, software=None, mc=None):
        """What this server is, as far as mods and plugins are concerned.

        A server the wizard made says exactly what it is. One added by hand is
        detected from its folder, the same way the Via installer does it.
        """
        self._need_content()
        if software:
            prof = contentmod.profile(software, mc or "")
            prof.update(cwd="", server="", running=False, detected="wizard")
            return prof
        srv = self.server(sid)
        if srv.get("kind") != "local":
            prof = contentmod.profile("", "")
            prof["reason"] = ("This server runs on another machine. Use Files to upload jars "
                              "to its mods/ or plugins/ folder.")
            prof.update(cwd="", server=srv.get("name") or "", running=False, detected="")
            return prof
        lc = srv.get("local") or {}
        cwd = lc.get("cwd") or (str(Path(lc["jar"]).parent) if lc.get("jar") else "")
        sw, mc, how = "", lc.get("mc_version") or "", ""
        if lc.get("mc_version") and lc.get("server_type") in contentmod.PROFILES.keys() | {"vanilla"}:
            sw, how = lc["server_type"], "created by Lodestone"
        elif HAVE_VIA and cwd:
            info = viamod.detect(cwd, lc.get("jar") or "")
            sw, how = info["flavour"], info["how"]
            mc = mc or info.get("mc_version") or ""
        prof = contentmod.profile(sw, mc)
        if not cwd:
            prof["kind"] = ""
            prof["reason"] = "Set the server folder first (Servers tab)."
        prof.update(cwd=cwd, server=srv.get("name") or "", running=self.is_running(srv["id"]),
                    detected=how, sid=srv["id"])
        return prof

    def _content_ready(self, sid):
        prof = self.content_profile(sid)
        if not prof["kind"]:
            raise RuntimeError(prof["reason"] or "This server takes no mods or plugins.")
        if not prof["cwd"] or not os.path.isdir(prof["cwd"]):
            raise RuntimeError("The server folder does not exist any more.")
        return prof

    def content_search(self, source, query="", offset=0, sort="relevance", all_versions=False,
                       sid=None, software=None, mc=None, category=""):
        prof = self.content_profile(sid, software, mc)
        try:
            out = contentmod.search(source or "modrinth", prof, query, offset, 20, sort,
                                    compatible_only=not all_versions, category=category)
        except contentmod.ContentError as e:
            raise RuntimeError(str(e))
        out["profile"] = prof
        return out

    def content_project(self, source, pid, sid=None, software=None, mc=None):
        prof = self.content_profile(sid, software, mc)
        try:
            return {"project": contentmod.project(source, pid, prof), "profile": prof}
        except contentmod.ContentError as e:
            raise RuntimeError(str(e))

    def content_plan(self, items, sid=None, software=None, mc=None):
        prof = self.content_profile(sid, software, mc)
        try:
            return contentmod.plan(prof.get("cwd") or "", prof, items or [])
        except contentmod.ContentError as e:
            raise RuntimeError(str(e))

    def content_categories(self, source, sid=None, software=None, mc=None):
        prof = self.content_profile(sid, software, mc)
        try:
            return {"items": contentmod.categories(source or "modrinth", prof)}
        except contentmod.ContentError as e:
            raise RuntimeError(str(e))

    def content_icon(self, fname, sid=None):
        """The icon a mod jar carries inside itself, for jars nobody indexed."""
        prof = self._content_ready(sid)
        name = os.path.basename(fname or "")
        path = os.path.join(prof["cwd"], prof["dir"], name)
        if not name.lower().endswith((".jar", ".jar.disabled")) or not os.path.isfile(path):
            raise RuntimeError("No such jar.")
        data = contentmod.jar_icon(path)
        if not data:
            raise RuntimeError("That jar has no icon.")
        return data

    def content_installed(self, sid=None):
        prof = self.content_profile(sid)
        if not prof["kind"] or not prof["cwd"]:
            return {"items": [], "profile": prof}
        out = contentmod.installed(prof["cwd"], prof)
        out["profile"] = prof
        return out

    def content_install(self, items, sid=None):
        prof = self._content_ready(sid)
        key = prof["sid"]
        try:
            pl = contentmod.plan(prof["cwd"], prof, items or [])
        except contentmod.ContentError as e:
            raise RuntimeError(str(e))
        if not pl["install"]:
            raise RuntimeError("; ".join(b["reason"] for b in pl["blocked"])
                               or "Nothing to install - it is already there.")
        rep = contentmod.apply(prof["cwd"], prof, pl, log=lambda m: self.emit(m, key))
        rep["warnings"] = pl["warnings"] + [b["reason"] for b in pl["blocked"]]
        rep["restart"] = prof["running"]
        names = ", ".join(i["name"] for i in rep["installed"] + rep["updated"])
        if names:
            self.discord.event("files", "Installed %s" % names, prof["server"])
        return rep

    def content_updates(self, sid=None):
        prof = self._content_ready(sid)
        try:
            return contentmod.updates(prof["cwd"], prof)
        except contentmod.ContentError as e:
            raise RuntimeError(str(e))

    def content_update(self, files=None, sid=None):
        """Update the named files (or everything with an update)."""
        prof = self._content_ready(sid)
        found = contentmod.updates(prof["cwd"], prof)["items"]
        if files:
            want = set(files)
            found = [u for u in found if u["file"] in want]
        if not found:
            return {"ok": True, "installed": [], "updated": [], "failed": [], "skipped": [],
                    "warnings": [], "msg": "Everything is up to date."}
        items = [{"source": u["source"], "project_id": u["project_id"],
                  "version_id": u["version_id"], "update": True} for u in found]
        return self.content_install(items, prof["sid"])

    def content_remove(self, fname, sid=None):
        prof = self._content_ready(sid)
        try:
            res = contentmod.remove(prof["cwd"], prof, fname)
        except contentmod.ContentError as e:
            raise RuntimeError(str(e))
        self.emit(">>> removed %s from %s/" % (", ".join(res["removed"]), prof["dir"]), prof["sid"])
        self.discord.event("files", "Removed %s" % res["name"], prof["server"])
        res["restart"] = prof["running"]
        return res

    def content_toggle(self, fname, sid=None):
        prof = self._content_ready(sid)
        try:
            res = contentmod.toggle(prof["cwd"], prof, fname)
        except contentmod.ContentError as e:
            raise RuntimeError(str(e))
        self.emit(">>> %s %s" % (res["file"], "enabled" if res["enabled"] else "disabled"), prof["sid"])
        res["restart"] = prof["running"]
        return res

    # -- which port will this server bind? ---------------------------------
    def _local_port(self, srv):
        """The port a local server will actually bind, from its own folder.

        server.properties is the truth: the hosting config can lag behind an
        edit made in the Properties tab, and starting two servers on one port
        fails in a way that looks like the second one simply crashed.
        """
        lc = srv.get("local", {}) or {}
        cwd = lc.get("cwd") or (str(Path(lc["jar"]).parent) if lc.get("jar") else "")
        if cwd:
            try:
                text = (Path(cwd) / "server.properties").read_text(
                    encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            for line in text.splitlines():
                line = line.strip()
                if line.startswith("server-port="):
                    try:
                        return int(line.split("=", 1)[1].strip()) or None
                    except ValueError:
                        break
        try:
            return int((srv.get("hosting") or {}).get("port") or 0) or None
        except (TypeError, ValueError):
            return None

    def _port_clash(self, srv):
        """Name the running server already holding this port, or "" if free.

        Two servers on one port is the obvious way to get this wrong now that
        more than one can run, and the failure is otherwise silent - the JVM
        exits with a bind error buried in its log.
        """
        port = self._local_port(srv)
        if not port:
            return ""
        for sid in self.running_ids():
            if sid == srv.get("id"):
                continue
            other = self.find(sid)
            if not other or self._local_port(other) != port:
                continue
            name = other.get("name") or "Another server"
            return ("%s is already running on port %d. Change this server's "
                    "server-port in the Properties tab, or stop %s first."
                    % (name, port, name))
        return ""

    # -- local process control --------------------------------------------
    def is_running(self, sid=None):
        s = self.server(sid)
        if s.get("kind") != "local":
            return False
        # Deliberately not restricted to the active server any more - that
        # restriction was the whole reason only one could run.
        return self.runtime(s["id"]).running()

    def _build_cmd(self, lc):
        if lc.get("server_type") == "script" and lc.get("run_cmd"):
            return None, lc["run_cmd"]
        java = lc.get("java") or "java"
        mem = []
        if lc.get("min_ram"):
            mem.append("-Xms" + lc["min_ram"])
        if lc.get("max_ram"):
            mem.append("-Xmx" + lc["max_ram"])
        extra = [a for a in (lc.get("extra") or "").split() if a]
        if lc.get("server_type") in ("forge", "neoforge") and lc.get("args_file"):
            # The way Forge's own run.bat does it: user_jvm_args.txt (the file
            # its README tells people to edit) after Lodestone's memory flags,
            # so a -Xmx someone uncommented there wins, as they expect.
            user = []
            cwd = lc.get("cwd") or ""
            if cwd and os.path.isfile(os.path.join(cwd, "user_jvm_args.txt")):
                user = ["@user_jvm_args.txt"]
            return [java] + mem + extra + user + ["@" + lc["args_file"], "nogui"], None
        if not lc.get("jar"):
            raise RuntimeError("Pick the server JAR first (Servers tab).")
        return [java] + mem + extra + ["-jar", lc["jar"], "nogui"], None

    def start(self, sid=None):
        s = self.server(sid)
        if s.get("kind") != "local":
            if self.minehut(s["id"]):
                return self.minehut_action("start", s["id"])
            raise RuntimeError("This is a remote server — turn on Minehut for it, or start it "
                               "from your host's panel.")
        if self.is_running(s["id"]):
            return False, "Already running."
        lc = s["local"]
        cwd = lc.get("cwd") or (str(Path(lc["jar"]).parent) if lc.get("jar") else "")
        if not cwd or not Path(cwd).is_dir():
            return False, "Set the server folder first."
        if not self._eula_ok(cwd):
            return False, "eula.txt has not been accepted yet."
        clash = self._port_clash(s)
        if clash:
            return False, clash
        try:
            argv, shell_cmd = self._build_cmd(lc)
        except RuntimeError as e:
            return False, str(e)
        key = s["id"]
        self._offline_restart.discard(key)
        s["last_started"] = int(time.time())
        self.save()
        rt = self.runtime(key)
        self.emit(">>> starting %s …" % (s.get("name") or "server"), key)
        # No console window on Windows: the server talks to us through the pipes, and a console host that goes
        # down (Windows Terminal crashed twice on 2026-09-30) takes every server attached to it down with it.
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            rt.proc = subprocess.Popen(
                shell_cmd if shell_cmd else argv,
                shell=bool(shell_cmd), cwd=cwd,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
                encoding="utf-8", errors="replace", creationflags=flags)
        except (OSError, ValueError) as e:
            self.emit("!!! could not start: %s" % e, key)
            return False, str(e)
        rt.started_at = time.time()
        rt.done_ms = ""
        rt.intentional_stop = False
        self.discord.event("start_stop", "Server starting", "", "",
                           [("Server", s.get("name") or "—", True)])
        threading.Thread(target=self._reader, args=(key,), daemon=True).start()
        return True, "Starting."

    def _reader(self, sid=None):
        rt = self.runtime(sid)
        try:
            for line in rt.proc.stdout:
                self.ingest(line.rstrip("\n"), sid)
        except (ValueError, OSError):
            pass
        code = rt.proc.poll()
        rt.started_at = None
        rt.players.clear()
        self.emit(">>> server process exited (code %s)." % code, sid)
        crashed = not rt.intentional_stop
        srv = (self.find(sid) if sid else None) or self.server()
        if crashed:
            self.discord.event("crash", "Server stopped unexpectedly",
                               "Exit code %s" % code, "",
                               [("Auto-restart",
                                 "on" if srv.get("features", {}).get("auto_restart") else "off", True)])
        else:
            self.discord.event("start_stop", "Server stopped",
                               "Exit code %s" % code)
        rt.intentional_stop = False
        if crashed and srv.get("features", {}).get("auto_restart"):
            self.emit(">>> auto-restart: relaunching in 3s …", sid)
            time.sleep(3)
            ok, msg = self.start(sid)
            if not ok:
                self.emit(">>> auto-restart failed: %s" % msg, sid)

    def stop(self, sid=None):
        s = self.server(sid)
        key = s["id"]
        if not self.is_running(key):
            if self.minehut(key):
                return self.minehut_action("stop", key)
            return False, "Not running."
        self.runtime(key).intentional_stop = True
        self.emit(">>> stopping …", key)
        self._stdin("stop", key)
        return True, "Stopping."

    def kill(self, sid=None):
        key = self.server(sid)["id"]
        rt = self.runtime(key)
        if not rt.proc:
            return False, "Not running."
        rt.intentional_stop = True
        try:
            rt.proc.kill()
        except OSError:
            pass
        self.emit(">>> killed.", key)
        return True, "Killed."

    def restart(self, sid=None):
        key = self.server(sid)["id"]
        if self.is_running(key):
            self.stop(key)
            threading.Thread(target=self._await_restart, args=(key,),
                             daemon=True).start()
            return True, "Restarting."
        if self.minehut(key):
            return self.minehut_action("restart", key)
        return self.start(key)

    def _await_restart(self, sid=None):
        for _ in range(60):
            if not self.is_running(sid):
                break
            time.sleep(0.5)
        time.sleep(1.5)
        self.start(sid)

    def _stdin(self, cmd, sid=None):
        try:
            proc = self.runtime(sid).proc
            proc.stdin.write(cmd + "\n")
            proc.stdin.flush()
            return True
        except (OSError, ValueError, AttributeError):
            return False

    # -- one command, whichever pipe is available --------------------------
    def command(self, cmd, quiet=False, who="panel", sid=None):
        """stdin if we own the process, otherwise RCON. Remote servers with
        RCON turned on are just as controllable as local ones."""
        cmd = (cmd or "").strip().lstrip("/")
        if not cmd:
            return False, "Nothing to send."
        srv = self.server(sid)
        key = srv["id"]
        self.watch["cmds"] += 1
        if not quiet:
            self.discord.event("command", "Command run", "`%s`" % cmd[:400], "",
                               [("By", who, True)])
        if self.is_running(key):
            if self._stdin(cmd, key):
                if not quiet:
                    self.emit("> " + cmd, key)
                return True, ""
            return False, "The server stopped listening on stdin."
        rc = srv.get("rcon", {})
        if rc.get("enabled"):
            host = rc.get("host") or (srv["sftp"].get("host") if srv["kind"] == "sftp" else "127.0.0.1")
            try:
                out = Rcon.run(host, rc.get("port") or 25575, rc.get("password") or "", cmd)
            except RconError as e:
                self.emit("!!! rcon: %s" % e, key)
                return False, str(e)
            self.emit("> " + cmd, key)
            for ln in (out or "").splitlines():
                self.emit("  " + ln, key)
            return True, out or ""
        # Minehut can run a console command over its API, which is the only way
        # in for a hosted server with no RCON port.
        mh = self.minehut(key)
        if mh:
            try:
                mh.command(cmd)
            except MinehutError as e:
                self.emit("!!! minehut: %s" % e, key)
                return False, str(e)
            self.emit("> " + cmd + "   (via Minehut)", key)
            return True, ""
        return False, ("Server is not running." if srv["kind"] == "local"
                       else "Turn on RCON or Minehut for this server to send commands.")

    def rcon_test(self, sid=None):
        srv = self.server(sid)
        rc = srv.get("rcon", {})
        host = rc.get("host") or (srv["sftp"].get("host") if srv["kind"] == "sftp" else "127.0.0.1")
        try:
            out = Rcon.run(host, rc.get("port") or 25575, rc.get("password") or "", "list", timeout=6)
            return True, out or "Connected — server had nothing to say."
        except RconError as e:
            return False, str(e)

    # -- hosting: local / LAN / internet -----------------------------------
    #
    # All of the protocol work lives in the `networking` package; this section
    # is only the bridge between it and the panel. Two things it owns that the
    # package deliberately does not:
    #
    #   * writing server.properties, because only the panel knows how to reach
    #     a server's files (local folder or SFTP);
    #   * remembering the mappings we made, in ~/.lodestone.json, so a crash
    #     mid-session cannot leave a port open on the router with nothing left
    #     that knows about it.

    def hosting_available(self):
        return HAVE_NET

    def hosting_cfg(self, sid=None):
        srv = self.server(sid)
        srv["hosting"] = default_hosting(srv.get("hosting", {}))
        return srv["hosting"]

    def hosting_manager(self, sid=None, create=True):
        """The HostingManager for a server, built on first use.

        One per server, because port, mode and mapping are all per-server. Only
        one may be in internet mode at a time — see hosting_apply().
        """
        if not HAVE_NET:
            raise RuntimeError(NET_ERROR or "Networking support is not available.")
        srv = self.server(sid)
        key = srv["id"]
        with self.lock:
            mgr = self.hosts.get(key)
            if mgr is not None or not create:
                return mgr
            hc = self.hosting_cfg(key)
            mgr = netmod.HostingManager(
                log=self.emit,
                on_change=lambda k=key: self._hosting_persist(k))
            mgr.server_name = srv.get("name") or "server"
            mgr.mode = hc.get("mode") if hc.get("mode") in netmod.MODES else "lan"
            try:
                mgr.port = netmod.validate_port(hc.get("port") or netmod.DEFAULT_MC_PORT)
            except netmod.PortInvalid:
                mgr.port = netmod.DEFAULT_MC_PORT
            mgr.external_port = int(hc.get("external_port") or 0)
            mgr.public_ip.enabled = bool(hc.get("public_ip_lookup", True))
            mgr.tester.allow_external = bool(hc.get("external_test", True))
            mgr.mapping.description = ("Lodestone %s" % (srv.get("name") or "Minecraft"))[:60]
            which = (hc.get("tunnel_provider") or "playit").strip().lower()
            agent = (hc.get("ngrok_agent") if which == "ngrok"
                     else hc.get("tunnel_agent")) or ""
            mgr.tunnel.set_provider(which, agent_path=agent, port=mgr.port)
            mgr.tunnel.provider.agent_path = agent
            mgr.tunnel.provider.port = mgr.port
            self.hosts[key] = mgr
        # Outside the lock: adopting can log, and logging takes the lock again.
        taken = mgr.mapping.adopt(hc.get("mappings") or [])
        if taken:
            self.emit(">>> hosting: found %d port mapping(s) from a previous "
                      "session — they will be removed when internet hosting is "
                      "switched off." % len(taken))
        return mgr

    def _hosting_persist(self, sid):
        """Write the mapping register and current mode back to the config."""
        mgr = self.hosts.get(sid)
        if not mgr:
            return
        srv = self.find(sid)
        if not srv:
            return
        hc = default_hosting(srv.setdefault("hosting", {}))
        hc["mode"] = mgr.mode
        hc["port"] = mgr.port
        hc["external_port"] = mgr.external_port
        hc["mappings"] = mgr.mapping.export()
        self.save()

    def _hosting_props_writer(self, sid=None):
        """Give the HostingManager a way to edit server.properties without it
        knowing anything about backends. Returns True when it changed something."""
        def write(values):
            current = {}
            got = self.props_get()
            if not got.get("ok"):
                raise RuntimeError(got.get("error") or "server.properties is unreadable.")
            for item in got["items"]:
                current[item["key"]] = item["value"]
            changes = {k: v for k, v in values.items()
                       if str(current.get(k, "")).strip() != str(v)}
            if not changes:
                return False
            self.props_save(changes)
            self.emit(">>> hosting: %s" % ", ".join(
                "%s=%s" % (k, v or "(all interfaces)") for k, v in sorted(changes.items())))
            return True
        return write

    def hosting_ports_in_use(self, sid=None, props=None):
        """The ports this server must never publish.

        Two places can turn RCON on and they do not have to agree: the panel's
        own RCON client settings, and enable-rcon in server.properties. A server
        with RCON on in the file but not configured in the panel is still
        running a remote console, so the file is consulted too — missing that
        would leave a real hole in "management ports are never published".
        """
        srv = self.server(sid)
        rc = srv.get("rcon", {}) or {}
        rcon_port = int(rc.get("port") or 25575) if rc.get("enabled") else None
        if rcon_port is None:
            if props is None:
                props = self._hosting_props_snapshot() or {}
            if str(props.get("enable-rcon", "")).strip().lower() == "true":
                try:
                    rcon_port = int(str(props.get("rcon.port") or 25575).strip())
                except (TypeError, ValueError):
                    rcon_port = 25575
        return rcon_port, int(self.cfg.get("port") or DEFAULT_PORT)

    def hosting_sync_port(self, sid=None):
        """Adopt the port the server is actually configured with.

        server.properties is the source of truth for the Minecraft port, so the
        dashboard reads it rather than inventing a number. Failures are silent:
        a server with no folder set simply keeps the stored port.
        """
        try:
            mgr = self.hosting_manager(sid)
        except RuntimeError:
            return None
        try:
            got = self.props_get()
            if not got.get("ok"):
                return mgr.port
            for item in got["items"]:
                if item["key"] == "server-port" and item["value"].strip():
                    port = int(item["value"].strip())
                    if port != mgr.port and 1 <= port <= 65535:
                        mgr.port = port
                        # The reachability verdict was about the old port. Not
                        # dropping it here would let an edit in the Properties
                        # tab silently transfer a green light to a port nothing
                        # has ever tested.
                        mgr.invalidate_verdict(
                            "server-port changed to %d since the last check." % port)
                        self._hosting_persist(self.server(sid)["id"])
                    return mgr.port
        except (ValueError, TypeError, OSError, RuntimeError):
            pass
        return mgr.port

    def hosting_state(self, sid=None, probe=False, sync=True):
        """Everything the Network tab renders."""
        if not HAVE_NET:
            return {"ok": False, "available": False, "error": NET_ERROR,
                    "modes": ["local", "lan", "internet"]}
        srv = self.server(sid)
        mgr = self.hosting_manager(srv["id"])
        if sync:
            self.hosting_sync_port(srv["id"])
        state = mgr.snapshot(include_probe=probe)
        hc = self.hosting_cfg(srv["id"])
        # Read the properties once; both the reserved-port list and the
        # conflict check want them, and on an SFTP server that is a round trip.
        try:
            props = self._hosting_props_snapshot()
        except Exception:
            props = None
        rcon_port, panel_port = self.hosting_ports_in_use(srv["id"], props or {})
        state.update({
            "ok": True, "available": True,
            "server": {"id": srv["id"], "name": srv.get("name") or "server",
                       "kind": srv.get("kind")},
            "can_host": srv.get("kind") == "local",
            "why_not": ("" if srv.get("kind") == "local" else
                        "This server runs on someone else's machine, so its port "
                        "is not yours to forward. Hosting modes apply to local "
                        "servers only."),
            "settings": {k: hc.get(k) for k in
                         ("upnp", "natpmp", "pcp", "firewall", "public_ip_lookup",
                          "external_test", "auto_enable", "tunnel_provider",
                          "tunnel_agent", "ngrok_agent")},
            "reserved": {"rcon": rcon_port, "panel": panel_port,
                         "sftp": 22},
            "running": self.is_running(srv["id"]),
            "windows": sys.platform.startswith("win"),
        })
        # No properties file is not a conflict — it is a server with no folder
        # set yet. Reporting "server-port is wrong" there would be nonsense.
        state["property_conflicts"] = []
        state["property_error"] = ""
        if props is None:
            state["property_error"] = (
                "server.properties could not be read, so the panel cannot "
                "confirm the server is set up for this mode.")
        else:
            try:
                state["property_conflicts"] = mgr.properties_conflicts(props)
            except Exception as e:
                state["property_error"] = str(e)
        return state

    def _hosting_props_snapshot(self):
        """The live server.properties as a dict, or None if it is unreadable."""
        try:
            got = self.props_get()
        except Exception:
            return None
        if not got.get("ok"):
            return None
        return {i["key"]: i["value"] for i in got["items"]}

    def hosting_security(self, sid=None, check_router=False):
        if not HAVE_NET:
            return []
        srv = self.server(sid)
        mgr = self.hosting_manager(srv["id"])
        return mgr.security_audit(
            properties=self._hosting_props_snapshot(),
            rcon=srv.get("rcon", {}),
            panel_host=self.cfg.get("panel_host", "127.0.0.1"),
            panel_port=self.cfg.get("port", DEFAULT_PORT),
            check_router=check_router)

    def hosting_apply(self, mode, port=None, external_port=None, sid=None):
        if not HAVE_NET:
            raise RuntimeError(NET_ERROR or "Networking support is not available.")
        srv = self.server(sid)
        if srv.get("kind") != "local":
            raise RuntimeError("Hosting modes apply to local servers only — a "
                               "remote server's port belongs to its host.")
        if mode not in netmod.MODES:
            raise RuntimeError("Unknown hosting mode %r." % mode)
        why = self.offline_exposure_block(srv["id"], mode)
        if why:
            self.emit("!!! hosting: %s" % why)
            raise RuntimeError(why)
        mgr = self.hosting_manager(srv["id"])
        rcon_port, panel_port = self.hosting_ports_in_use(srv["id"])

        if port is not None:
            mgr.set_port(port, rcon_port, panel_port,
                         check_free=not self.is_running(srv["id"]))
        if external_port is not None:
            mgr.set_external_port(external_port, rcon_port, panel_port)

        if mode == "internet":
            # One public host at a time: two servers fighting over the same
            # external port would silently steal it from each other.
            for other_id, other in list(self.hosts.items()):
                if other_id != srv["id"] and other.mode == "internet":
                    self.emit(">>> hosting: switching %s off internet mode first."
                              % (self.find(other_id) or {}).get("name", other_id))
                    other.disable(remove_firewall_rule=False, blocking=True)
                    self._hosting_persist(other_id)

        hc = self.hosting_cfg(srv["id"])
        want = tuple(p for p in ("upnp", "natpmp", "pcp") if hc.get(p, True))
        if mode == "internet" and not want:
            raise RuntimeError("Every automatic port-mapping protocol is turned "
                               "off, so there is nothing left to try.")
        self.emit(">>> hosting: switching %s to %s mode."
                  % (srv.get("name") or "server", netmod.MODE_LABEL[mode]))
        mgr.apply(mode, blocking=False, want_providers=want,
                  manage_firewall=bool(hc.get("firewall", True)),
                  write_properties=self._hosting_props_writer(srv["id"]),
                  rcon_port=rcon_port, panel_port=panel_port)
        hc["mode"] = mode
        hc["port"] = mgr.port
        hc["external_port"] = mgr.external_port
        self.save()
        self.discord.event("files", "Hosting mode changed",
                           "%s is now in %s mode"
                           % (srv.get("name") or "server", netmod.MODE_LABEL[mode]))
        return mgr.snapshot()

    def hosting_disable(self, sid=None, remove_firewall=False):
        if not HAVE_NET:
            return {"ok": True, "results": []}
        srv = self.server(sid)
        mgr = self.hosting_manager(srv["id"], create=False)
        if not mgr:
            return {"ok": True, "results": []}
        self.emit(">>> hosting: turning internet hosting off.")
        results = mgr.disable(remove_firewall_rule=remove_firewall, blocking=True,
                              write_properties=self._hosting_props_writer(srv["id"]))
        self._hosting_persist(srv["id"])
        return {"ok": True, "results": results, "state": mgr.snapshot()}

    def hosting_test(self, sid=None, deep_firewall=False):
        if not HAVE_NET:
            raise RuntimeError(NET_ERROR or "Networking support is not available.")
        mgr = self.hosting_manager(sid)
        mgr.recheck(blocking=False, deep_firewall=deep_firewall)
        return {"ok": True, "state": mgr.snapshot()}

    def hosting_autofix(self, sid=None):
        """Fix what can be fixed from this machine, then report what cannot.

        The panel supplies the two things the networking layer has no business
        knowing about: how to write server.properties, and how to start the
        Minecraft server.
        """
        if not HAVE_NET:
            raise RuntimeError(NET_ERROR or "Networking support is not available.")
        srv = self.server(sid)
        if srv.get("kind") != "local":
            raise RuntimeError("Auto fix applies to local servers only.")
        # Auto fix maps ports and rewrites server-ip, so treat it as public.
        why = self.offline_exposure_block(srv["id"], "internet")
        if why:
            self.emit("!!! hosting: %s" % why)
            raise RuntimeError(why)
        mgr = self.hosting_manager(srv["id"])
        hc = self.hosting_cfg(srv["id"])
        want = tuple(p for p in ("upnp", "natpmp", "pcp") if hc.get(p, True))
        self.emit(">>> hosting: running auto fix …")
        mgr.autofix(write_properties=self._hosting_props_writer(srv["id"]),
                    start_server=self.start,
                    is_running=lambda: self.is_running(srv["id"]),
                    want_providers=want,
                    manage_firewall=bool(hc.get("firewall", True)),
                    blocking=False)
        return {"ok": True, "state": mgr.snapshot()}

    def hosting_probe(self, sid=None):
        """Which mapping protocols this network supports. Creates nothing."""
        if not HAVE_NET:
            raise RuntimeError(NET_ERROR or "Networking support is not available.")
        mgr = self.hosting_manager(sid)
        return {"ok": True, "providers": mgr.mapping.probe(),
                "gateway": mgr.interfaces.gateway_ipv4(),
                "router_table": mgr.mapping.router_table()}

    def hosting_firewall(self, action, sid=None):
        if not HAVE_NET:
            raise RuntimeError(NET_ERROR or "Networking support is not available.")
        mgr = self.hosting_manager(sid)
        if not mgr.firewall.supported:
            raise RuntimeError("Host firewall management is Windows-only. On this "
                               "system, allow inbound TCP %d with your own firewall "
                               "tool." % mgr.port)
        if action == "add":
            res = mgr.firewall.add_rule(mgr.port, mgr.protocol)
            self.emit(">>> hosting: %s" % res.get("detail", "firewall rule added"))
        elif action == "remove":
            res = mgr.firewall.remove_rule(mgr.port, mgr.protocol)
            self.emit(">>> hosting: %s" % res.get("detail", "firewall rule removed"))
        elif action == "check":
            res = mgr.firewall.snapshot(mgr.port, mgr.protocol, deep=True)
        else:
            raise RuntimeError("Unknown firewall action %r." % action)
        mgr.firewall_state = mgr.firewall.snapshot(mgr.port, mgr.protocol, deep=False)
        return {"ok": True, "result": res, "firewall": mgr.firewall_state}

    def hosting_settings(self, data, sid=None):
        srv = self.server(sid)
        hc = self.hosting_cfg(srv["id"])
        for key in ("upnp", "natpmp", "pcp", "firewall", "public_ip_lookup",
                    "external_test", "auto_enable"):
            if key in data:
                hc[key] = bool(data[key])
        for key in ("tunnel_agent", "ngrok_agent"):
            if key in data:
                path = (data.get(key) or "").strip()
                if path and not os.path.isfile(path):
                    raise RuntimeError("There is no file at %s." % path)
                hc[key] = path
        if "tunnel_provider" in data:
            which = (data.get("tunnel_provider") or "playit").strip().lower()
            if which not in netmod.PROVIDERS:
                raise RuntimeError("Unknown tunnel provider %r." % which)
            hc["tunnel_provider"] = which
        # One re-sync covers a provider swap, a path edit, or both. Doing it
        # here rather than per-key means the manager never sees a half-applied
        # pairing of "ngrok selected" with "playit path".
        mgr_now = self.hosting_manager(srv["id"], create=False) if HAVE_NET else None
        if mgr_now:
            which = (hc.get("tunnel_provider") or "playit").strip().lower()
            agent = (hc.get("ngrok_agent") if which == "ngrok"
                     else hc.get("tunnel_agent")) or ""
            mgr_now.tunnel.set_provider(which, agent_path=agent,
                                        port=mgr_now.port)
            mgr_now.tunnel.provider.agent_path = agent
        mgr = self.hosting_manager(srv["id"], create=False) if HAVE_NET else None
        rcon_port, panel_port = self.hosting_ports_in_use(srv["id"])
        if "port" in data and mgr:
            hc["port"] = mgr.set_port(data["port"], rcon_port, panel_port,
                                      check_free=not self.is_running(srv["id"]))
        if "external_port" in data and mgr:
            hc["external_port"] = mgr.set_external_port(
                data["external_port"] or 0, rcon_port, panel_port)
        if mgr:
            mgr.public_ip.enabled = bool(hc.get("public_ip_lookup", True))
            mgr.tester.allow_external = bool(hc.get("external_test", True))
            if not hc.get("public_ip_lookup", True):
                mgr.public_ip.invalidate()
        self.save()
        return {"ok": True, "settings": hc}

    def hosting_apply_properties(self, sid=None):
        """Write just the server-ip / server-port the current mode needs."""
        if not HAVE_NET:
            raise RuntimeError(NET_ERROR or "Networking support is not available.")
        mgr = self.hosting_manager(sid)
        changed = self._hosting_props_writer(sid)(mgr.desired_properties())
        return {"ok": True, "changed": bool(changed),
                "values": mgr.desired_properties()}

    def hosting_brief(self, sid=None):
        """Cheap enough for the status poll."""
        if not HAVE_NET:
            return {"available": False, "mode": "lan", "online": False}
        mgr = self.hosting_manager(sid, create=False)
        if not mgr:
            hc = self.hosting_cfg(sid)
            return {"available": True, "mode": hc.get("mode", "lan"),
                    "mode_label": netmod.MODE_LABEL.get(hc.get("mode", "lan"), "LAN"),
                    "port": hc.get("port", netmod.DEFAULT_MC_PORT),
                    "online": False, "busy": False,
                    "connectivity_state": "unknown", "mapping_active": False}
        out = mgr.brief()
        out["available"] = True
        return out

    # -- offline / non-premium mode (testing) ------------------------------
    #
    # Local servers only. The keys are edited in place (see offline_edit) and
    # their old values kept under "offline_mode" in the server's config entry,
    # so turning it off restores the file byte for byte. While it is on, the
    # hosting code refuses to publish the server unless an auth plugin is in.

    def offline_cfg(self, sid=None):
        srv = self.server(sid)
        srv["offline_mode"] = default_offline(srv.get("offline_mode") or {})
        return srv["offline_mode"]

    @staticmethod
    def _server_folder(srv):
        lc = srv.get("local") or {}
        return lc.get("cwd") or (str(Path(lc["jar"]).parent) if lc.get("jar") else "")

    @staticmethod
    def _offline_kind(folder):
        if (Path(folder) / "velocity.toml").is_file():
            return "velocity"
        if (Path(folder) / "server.properties").is_file():
            return "properties"
        return ""

    @staticmethod
    def _offline_file(folder, kind):
        return Path(folder) / ("velocity.toml" if kind == "velocity" else "server.properties")

    @staticmethod
    def _offline_auth_plugins(folder):
        try:
            jars = [p.name for p in (Path(folder) / "plugins").glob("*.jar")]
        except OSError:
            return []
        return sorted(j for j in jars if any(a in j.lower() for a in OFFLINE_AUTH_PLUGINS))

    @staticmethod
    def _offline_proxied(folder):
        """"Velocity" / "BungeeCord" when this backend expects a proxy in front."""
        def read(*parts):
            try:
                return Path(folder, *parts).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""
        if (_yaml_value(read("config", "paper-global.yml"),
                        ("proxies", "velocity", "enabled")) or "").lower() == "true":
            return "Velocity"
        if (_yaml_value(read("spigot.yml"), ("settings", "bungeecord")) or "").lower() == "true":
            return "BungeeCord"
        return ""

    def _offline_find_proxy(self, srv, folder):
        """The Lodestone entry whose velocity.toml [servers] points at this
        backend's port, or None."""
        try:
            props = offline_read((Path(folder) / "server.properties").read_text(
                encoding="utf-8", errors="replace"), ("server-port", "server-ip"))
        except OSError:
            return None
        port = (props.get("server-port") or "25565").strip()
        hosts = {"127.0.0.1", "localhost", "0.0.0.0", (props.get("server-ip") or "").strip()}
        for other in self.servers():
            if other["id"] == srv["id"] or other.get("kind") != "local":
                continue
            ofolder = self._server_folder(other)
            if not ofolder:
                continue
            try:
                text = (Path(ofolder) / "velocity.toml").read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            in_servers = False
            for line in text.splitlines():
                s = line.strip()
                if s.startswith("["):
                    in_servers = s == "[servers]"
                    continue
                m = re.match(r'^([\w.-]+)\s*=\s*"([^":]+):(\d+)"', s) if in_servers else None
                if m and m.group(3) == port and m.group(2) in hosts:
                    return {"id": other["id"], "name": other.get("name") or "proxy",
                            "entry": m.group(1)}
        return None

    def offline_state(self, sid=None):
        """Everything the offline-mode toggle renders."""
        srv = self.server(sid)
        oc = self.offline_cfg(srv["id"])
        out = {"ok": True, "server": {"id": srv["id"], "name": srv.get("name") or "server"},
               "enabled": bool(oc["enabled"]), "allow_lan": bool(oc["allow_lan"]),
               "available": False, "why_not": "", "kind": oc.get("kind") or "",
               "behind": None, "proxy": "", "auth_plugins": [], "current": {},
               "running": self.is_running(srv["id"]),
               "restart_needed": srv["id"] in self._offline_restart}
        if srv.get("kind") != "local":
            out["why_not"] = "Not available for remote servers."
            return out
        folder = self._server_folder(srv)
        if not folder or not Path(folder).is_dir():
            out["why_not"] = "Set the server folder first."
            return out
        kind = oc["kind"] if oc["enabled"] and oc["kind"] else self._offline_kind(folder)
        if not kind:
            out["why_not"] = ("There is no server.properties or velocity.toml in the server "
                              "folder yet. Start the server once so it writes one.")
            return out
        out["kind"] = kind
        if kind == "properties" and not oc["enabled"]:
            proxy = self._offline_proxied(folder)
            if proxy:
                out["proxy"] = proxy
                out["behind"] = self._offline_find_proxy(srv, folder)
                out["why_not"] = ("Behind %s. Toggle offline mode on the proxy instead."
                                  % ((out["behind"] or {}).get("name") or proxy))
                return out
        try:
            text = self._offline_file(folder, kind).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            out["why_not"] = "Could not read %s: %s" % (self._offline_file(folder, kind).name, e)
            return out
        keys = OFFLINE_TOML_KEYS if kind == "velocity" else OFFLINE_PROPS_KEYS
        out["current"] = offline_read(text, keys, kind == "velocity")
        out["auth_plugins"] = self._offline_auth_plugins(folder)
        out["available"] = True
        return out

    def offline_set(self, enabled, allow_lan=None, sid=None):
        """Turn offline mode on or off, or change Allow LAN while it is on."""
        srv = self.server(sid)
        st = self.offline_state(srv["id"])
        if not st["available"]:
            raise RuntimeError(st["why_not"])
        oc = self.offline_cfg(srv["id"])
        enabled = bool(enabled)
        lan = oc["allow_lan"] if allow_lan is None else bool(allow_lan)
        if not enabled and not oc["enabled"]:
            oc["allow_lan"] = lan
            self.save()
            return self.offline_state(srv["id"])
        name = srv.get("name") or "server"
        folder = self._server_folder(srv)
        if enabled and not oc["enabled"]:
            mode = self.hosting_cfg(srv["id"]).get("mode")
            if mode in ("internet", "tunnel") and not st["auth_plugins"]:
                raise RuntimeError(
                    "%s is published in %s mode. Switch hosting to LAN or Local first: "
                    "an offline server open to the internet lets anyone join as anyone, "
                    "including you with OP." % (name, mode))
            if mode == "lan" and not lan:
                self.emit(">>> offline mode: %s binds to 127.0.0.1 now; tick Allow LAN "
                          "to keep it reachable on your home network." % name)
        kind = oc["kind"] if oc["enabled"] else st["kind"]
        toml = kind == "velocity"
        keys = OFFLINE_TOML_KEYS if toml else OFFLINE_PROPS_KEYS
        path = self._offline_file(folder, kind)
        raw = path.read_bytes().decode("utf-8", "surrogateescape")
        if not oc["enabled"]:
            oc["saved"] = offline_read(raw, keys, toml)
            oc["kind"] = kind
            oc["changed"] = []
            oc["added_eol"] = False
        saved = oc["saved"]
        targets = _offline_targets(kind, saved, lan) if enabled else {}
        values = {k: targets[k] if k in targets else saved.get(k)
                  for k in set(oc["changed"]) | set(targets)}
        new, added = offline_edit(raw, values, toml,
                                  strip_final_eol=not enabled and oc.get("added_eol"))
        if new != raw:
            bak = path.with_name(path.name + ".bak-online")
            if not bak.exists():
                shutil.copy2(str(path), str(bak))
            tmp = path.with_name(path.name + ".lodestone-tmp")
            tmp.write_bytes(new.encode("utf-8", "surrogateescape"))
            os.replace(str(tmp), str(path))
        oc["added_eol"] = bool(oc.get("added_eol") or added) if enabled else False
        oc["changed"] = sorted(targets)
        oc["enabled"] = enabled
        oc["allow_lan"] = lan
        if not enabled:
            oc["saved"], oc["kind"] = {}, ""
        self.save()
        if self.is_running(srv["id"]):
            self._offline_restart.add(srv["id"])
        if enabled:
            self.emit(">>> offline mode ON for %s (%s): %s" % (
                name, path.name, ", ".join("%s=%s" % kv for kv in sorted(targets.items()))))
            if st["auth_plugins"]:
                self.emit(">>> offline mode: auth plugin found (%s), so every player has to "
                          "register / log in." % ", ".join(st["auth_plugins"]))
        else:
            self.emit(">>> offline mode OFF for %s: %s restored." % (name, path.name))
        self.discord.event("files", "Offline mode %s" % ("on" if enabled else "off"),
                           "%s: %s" % (name, path.name))
        return self.offline_state(srv["id"])

    def offline_exposure_block(self, sid, mode):
        """Why this server may not be exposed in this hosting mode, or ""."""
        srv = self.server(sid)
        oc = srv.get("offline_mode") or {}
        if not oc.get("enabled") or mode == "local":
            return ""
        name = srv.get("name") or "server"
        if mode == "lan":
            if oc.get("allow_lan"):
                return ""
            return ("Offline mode is on for %s without Allow LAN, so it stays on "
                    "127.0.0.1. Tick Allow LAN in the offline-mode card first." % name)
        plugins = self._offline_auth_plugins(self._server_folder(srv))
        if plugins:
            self.emit("!!! hosting: offline mode is on for %s; publishing it anyway because "
                      "an auth plugin is installed (%s). Every player must register."
                      % (name, ", ".join(plugins)))
            return ""
        return ("Offline mode is on for %s, so anyone could join with any username, "
                "including yours with OP. Lodestone will not open a tunnel, UPnP, NAT-PMP "
                "or PCP mapping for it. Turn offline mode off, or install an auth plugin "
                "(AuthMe, nLogin, LibreLogin, LoginSecurity, OpeNLogin, JPremium, FastLogin)."
                % name)

    def hosting_shutdown(self):
        """Take down every mapping we made. Called on exit; never raises."""
        for sid, mgr in list(self.hosts.items()):
            try:
                if mgr.mapping.owned():
                    self.emit(">>> hosting: removing port mapping(s) on shutdown.")
                mgr.shutdown()
                self._hosting_persist(sid)
            except Exception:
                pass

    def hosting_autostart(self):
        """Re-apply internet mode for any server that asked for it."""
        if not HAVE_NET:
            return
        for srv in list(self.servers()):
            hc = default_hosting(srv.setdefault("hosting", {}))
            stale = hc.get("mappings") or []
            if hc.get("auto_enable") and hc.get("mode") == "internet" \
                    and srv.get("kind") == "local":
                try:
                    self.hosting_apply("internet", sid=srv["id"])
                except Exception as e:
                    self.emit("!!! hosting: could not re-enable internet mode for "
                              "%s: %s" % (srv.get("name") or "server", e))
            elif stale:
                # Left over from a crash: adopt them, then take them down, so a
                # port is never left open by a session that no longer exists.
                try:
                    mgr = self.hosting_manager(srv["id"])
                    if mgr.mapping.owned():
                        self.emit(">>> hosting: cleaning up %d port mapping(s) left "
                                  "by a previous session." % len(mgr.mapping.owned()))
                        mgr.mapping.release()
                        self._hosting_persist(srv["id"])
                except Exception as e:
                    self.emit("!!! hosting: could not clean up old mappings: %s" % e)

    # -- remote console tail ----------------------------------------------
    def _start_remote_tail(self):
        self._stop_remote_tail()
        self.remote_stop.clear()
        self.remote_thread = threading.Thread(target=self._remote_tail_loop, daemon=True)
        self.remote_thread.start()

    def _stop_remote_tail(self):
        self.remote_stop.set()
        t = self.remote_thread
        if t and t.is_alive():
            t.join(timeout=2)
        self.remote_thread = None

    def _remote_tail_loop(self):
        sid = self.cfg.get("active")
        carry = ""
        misses = 0
        while not self.remote_stop.is_set():
            if self.cfg.get("active") != sid:
                return
            be = self.backends.get(sid)
            if not be or not be.online():
                time.sleep(3)
                continue
            try:
                # wait=False: if the UI is mid-transfer, skip this tick rather
                # than queue behind it
                res = be.tail_log(LOG_PATH, self.remote_size, wait=False)
            except Exception:
                misses += 1
                time.sleep(min(20, 3 * misses))
                continue
            misses = 0
            if res is None:
                time.sleep(1.5)
                continue
            if res.get("missing"):
                time.sleep(6)
                continue
            self.remote_size = res.get("size", self.remote_size)
            text = carry + (res.get("text") or "")
            lines = text.split("\n")
            carry = lines.pop() if lines and not text.endswith("\n") else ""
            for ln in lines:
                if ln.strip():
                    self.ingest(ln.rstrip("\r"))
            time.sleep(2)

    # -- status ------------------------------------------------------------
    def status_for(self, sid):
        """A compact status of ANY server by id - not only the one selected in the UI. Scripts that drive one server
        while the user has another open need it (the full status() is always the selected server's). Cache-only,
        like status(): nothing here touches a socket."""
        s = self.find(sid)
        if not s:
            raise KeyError(sid)
        rt = self.runtime(sid)
        be = self.backends.get(sid)
        running = self.is_running(sid)
        up = int(time.time() - rt.started_at) if rt.started_at else 0
        st = be.status() if be else {"state": "offline"}
        return {
            "server": {"id": sid, "name": s.get("name") or "server", "kind": s.get("kind")},
            "selected": sid == self.cfg.get("active"),
            "running": running,
            "state": "running" if running else st.get("state", "offline"),
            "error": st.get("error", ""),
            "uptime": up,
            "uptime_h": fmt_dur(up),
            "players": sorted(rt.players),
            "player_count": len(rt.players),
            "can_command": (running or bool(s.get("rcon", {}).get("enabled"))
                            or bool(s.get("minehut", {}).get("enabled"))),
        }

    def status(self):
        s = self.server()
        be = self.backend()
        st = be.status()
        running = self.is_running()
        return {
            "server": {"id": s["id"], "name": s.get("name") or "server", "kind": s.get("kind"),
                       # What the Create server wizard installed; blank for servers added by hand.
                       "software": (s.get("local") or {}).get("server_type", "") if (s.get("local") or {}).get("mc_version") else "",
                       "mc": (s.get("local") or {}).get("mc_version", ""),
                       "icon": self.icon_of(s)},
            "transport": st,
            "running": running,
            "state": ("running" if running else
                      ("online" if st.get("state") == "online" else st.get("state", "offline"))),
            "uptime": int(time.time() - self.started_at) if self.started_at else st.get("uptime", 0),
            "uptime_h": fmt_dur(int(time.time() - self.started_at) if self.started_at else st.get("uptime", 0)),
            "players": sorted(self.players),
            "player_count": len(self.players),
            "done_ms": self.done_ms,
            "can_command": (running or bool(s.get("rcon", {}).get("enabled"))
                            or bool(s.get("minehut", {}).get("enabled"))),
            "can_start": s.get("kind") == "local" or bool(s.get("minehut", {}).get("enabled")),
            "minehut": bool(s.get("minehut", {}).get("enabled")),
            # a Minehut server even without a login: the header shows its public status
            "minehut_id": self.minehut_id(s["id"]),
            "eula": self._eula_ok(s["local"].get("cwd")) if s.get("kind") == "local" else True,
            "backup": self.backup,
            "discord": self.discord.stats(),
            "hooks": len([h for h in s.get("discord", {}).get("hooks", []) if h.get("enabled")]),
            "api": {"enabled": bool(self.cfg["api"].get("enabled")),
                    "tokens": len(self.cfg["api"].get("tokens", []))},
            "chat": {"seen": self.chat_seen, "last": self.last_chat,
                     "custom": bool((s.get("chat", {}) or {}).get("pattern"))},
            "ai": {"ready": bool(self.cfg.get("ai", {}).get("api_key")),
                   "automod": bool(self.cfg.get("ai", {}).get("automod")),
                   "queued": len(self._automod_q)},
            "counters": self.watch,
            "down": fmt_size(getattr(be, "down", 0)),
            "up": fmt_size(getattr(be, "up", 0)),
            # Cache-only: status() is polled twice a second, so nothing in here
            # may touch a socket or shell out.
            "hosting": self.hosting_brief(s["id"]),
            "offline_mode": bool((s.get("offline_mode") or {}).get("enabled")),
            "perf_mode": self.perf_mode(),
            "version": VERSION,
            "update": {"state": self.upd["state"],
                       "behind": ((self.upd["info"] or {}).get("behind")),
                       "ready": bool(self.upd["applied"])},
        }

    # -- minehut -----------------------------------------------------------
    def minehut(self, sid=None):
        srv = self.server(sid)
        mh = srv.get("minehut", {}) or {}
        return Minehut(mh) if mh.get("enabled") else None

    def minehut_id(self, sid=None):
        """this server's Minehut id if it is a Minehut server - from its Minehut settings, or (an SFTP server on
        ftp.minehut.com) its SFTP username, which Minehut makes the same id. "" otherwise. Config only, no network."""
        srv = self.server(sid)
        got = Minehut.parse_id((srv.get("minehut", {}) or {}).get("server_id"))
        sf = srv.get("sftp", {}) or {}
        if not got and srv.get("kind") == "sftp" and "minehut" in (sf.get("host") or "").lower():
            got = (sf.get("user") or "").strip()
        return got

    def minehut_status(self, sid=None):
        mh = self.minehut(sid)
        if not mh:
            mid = self.minehut_id(sid)
            if not mid:
                return {"ok": False, "error": "Minehut is not turned on for this server."}
            # no login set up: Minehut's public info still says online / players (start / stop need the login)
            mh = Minehut({"server_id": mid})
        try:
            return mh.status()
        except MinehutError as e:
            return {"ok": False, "error": str(e)}

    def minehut_action(self, what, sid=None):
        mh = self.minehut(sid)
        if not mh:
            return False, "Turn on Minehut for this server first."
        try:
            if what == "start":
                ok, msg = mh.start()
            elif what == "stop":
                ok, msg = mh.stop()
            elif what == "restart":
                ok, msg = mh.restart()
            elif what == "hibernate":
                ok, msg = mh.hibernate()
            elif what == "wake":
                mh.wake()
                ok, msg = True, "Waking the service."
            elif what == "save":
                ok, msg = mh.save()
            else:
                return False, "Unknown action."
        except MinehutError as e:
            self.emit("!!! minehut: %s" % e)
            return False, str(e)
        self.emit(">>> minehut: %s" % msg)
        self.discord.event("command", "Minehut: %s" % what, msg)
        return ok, msg

    # -- chat shape --------------------------------------------------------
    def chat_pattern(self):
        """Compile the server's custom chat regex, if it set one. Cached, and a
        bad pattern degrades to the built-in detection rather than throwing."""
        raw = (self.server().get("chat", {}) or {}).get("pattern", "") or ""
        if not raw:
            return None
        if self._chat_re_cache[0] == raw:
            return self._chat_re_cache[1]
        try:
            compiled = re.compile(raw)
        except re.error as e:
            self.emit("!!! chat pattern is not valid regex (%s) - using auto-detect" % e)
            compiled = None
        self._chat_re_cache = (raw, compiled)
        return compiled

    def chat_sniff(self, limit=1500):
        """Scan the recent log for chat and report what was found, so the user
        can see whether detection works without waiting for someone to talk."""
        lines = [t for _s, t in self.recent(limit)]
        if len(lines) < 40:
            try:
                res = self.backend().tail_log(LOG_PATH, 0)
                lines = (res.get("text") or "").splitlines() + lines
            except Exception:
                pass
        seen, uniq = set(), []          # the buffer and the log overlap
        for l in lines:
            k = l.strip()
            if k and k not in seen:
                seen.add(k)
                uniq.append(l)
        lines = uniq
        custom = self.chat_pattern()
        hits, threads, unmatched = [], {}, []
        for l in lines[-4000:]:
            thread, _lv, _b = split_log_line(l)
            got = parse_chat(l, custom)
            if got:
                hits.append({"line": l[:300], "player": got[0], "message": got[1][:200]})
                if thread:
                    threads[thread] = threads.get(thread, 0) + 1
            elif thread and CHAT_THREAD_RE.search(thread):
                unmatched.append(l[:300])
        return {"ok": True, "hits": hits[-40:], "count": len(hits),
                "threads": sorted(threads.items(), key=lambda x: -x[1])[:6],
                "unmatched": unmatched[-10:], "custom": bool(custom),
                "scanned": len(lines[-4000:])}

    def chat_test(self, line):
        """Try one pasted line, so a custom pattern can be checked instantly."""
        got = parse_chat(line or "", self.chat_pattern())
        auto = parse_chat(line or "", None)
        return {"ok": True, "matched": bool(got),
                "player": got[0] if got else "", "message": got[1] if got else "",
                "auto_matched": bool(auto),
                "auto_player": auto[0] if auto else "", "auto_message": auto[1] if auto else ""}

    # -- log search --------------------------------------------------------
    def log_files(self):
        """latest.log plus the rotated archives, newest first."""
        out = []
        try:
            listing = self.backend().list_dir("logs")
        except Exception:
            return out
        for i in listing.get("items", []):
            if i["dir"]:
                continue
            n = i["name"]
            if n == "latest.log" or n.endswith(".log") or n.endswith(".log.gz"):
                out.append({"name": n, "rel": i["rel"], "size": i["size"],
                            "mod": i["mod"], "bytes": i.get("bytes", 0)})
        out.sort(key=lambda x: (x["name"] != "latest.log", x["mod"]), reverse=True)
        out.sort(key=lambda x: x["name"] != "latest.log")
        return out

    def log_search(self, query, files=None, limit=400, regex=False, case=False):
        """Grep the logs. Rotated logs are gzipped, and on a remote server they
        have to come down over SFTP, so this is capped rather than exhaustive."""
        query = (query or "").strip()
        if not query:
            return {"ok": False, "error": "Type something to search for."}
        try:
            pat = re.compile(query if regex else re.escape(query),
                             0 if case else re.I)
        except re.error as e:
            return {"ok": False, "error": "Bad regex: %s" % e}
        be = self.backend()
        avail = self.log_files()
        wanted = [f for f in avail if (not files or f["name"] in files)][:12]
        hits, scanned, truncated = [], [], False
        for f in wanted:
            try:
                raw = be.read_raw(f["rel"])
                if f["name"].endswith(".gz"):
                    raw = gzip.decompress(raw)
                text = raw.decode("utf-8", "replace")
            except Exception as e:
                scanned.append({"name": f["name"], "error": str(e)[:120]})
                continue
            n = 0
            for ln, line in enumerate(text.splitlines(), 1):
                if pat.search(line):
                    hits.append({"file": f["name"], "line": ln, "text": line[:400]})
                    n += 1
                    if len(hits) >= limit:
                        truncated = True
                        break
            scanned.append({"name": f["name"], "hits": n})
            if truncated:
                break
        return {"ok": True, "hits": hits, "scanned": scanned, "truncated": truncated,
                "files": [f["name"] for f in avail]}

    # -- worlds ------------------------------------------------------------
    WORLD_MARKERS = ("level.dat", "region", "session.lock")

    def worlds_list(self):
        """A world is a folder holding level.dat (or at least a region dir).
        The live one is whatever level-name points at."""
        be = self.backend()
        try:
            listing = be.list_dir("")
        except Exception as e:
            return {"ok": False, "error": str(e), "items": []}
        active = ""
        try:
            for i in self.props_get().get("items", []):
                if i["key"] == "level-name":
                    active = i["value"]
                    break
        except Exception:
            pass
        out = []
        for entry in listing.get("items", []):
            if not entry["dir"]:
                continue
            name = entry["name"]
            if name.startswith(".") or name in ("plugins", "logs", "libraries", "cache",
                                                "versions", "config", "backups", "mods"):
                continue
            try:
                inner = {x["name"] for x in be.list_dir(entry["rel"]).get("items", [])}
            except Exception:
                continue
            if not (inner & set(self.WORLD_MARKERS)):
                continue
            dims = [d for d in ("DIM-1", "DIM1", "region", "entities", "poi") if d in inner]
            out.append({"name": name, "rel": entry["rel"], "active": name == active,
                        "mod": entry.get("mod", ""), "parts": dims,
                        "nether": name.endswith("_nether"), "end": name.endswith("_the_end")})
        out.sort(key=lambda w: (not w["active"], w["name"].lower()))
        return {"ok": True, "items": out, "active": active}

    def world_activate(self, name):
        name = safe_name(name)
        if not name:
            raise RuntimeError("Pick a world.")
        self.props_save({"level-name": name})
        mh = self.minehut()
        if mh:
            try:
                mh.set_property("level-name", name)
            except MinehutError as e:
                self.emit("!!! minehut: could not mirror level-name (%s)" % e)
        self.emit(">>> level-name set to %s — restart for it to take effect." % name)
        return {"ok": True, "note": "Restart the server for it to load %s." % name}

    def world_reset(self, name, mode="contents"):
        """`contents` clears the chunks so the server regenerates them but keeps
        the folder and its level.dat; `all` removes the world outright."""
        name = safe_name(name)
        if not name:
            raise RuntimeError("Pick a world.")
        be = self.backend()
        if self.is_running():
            raise RuntimeError("Stop the server before resetting a world.")
        if mode == "all":
            be.delete(name)
            self.emit(">>> world %s deleted." % name)
            self.discord.event("files", "World deleted", name)
            return {"ok": True, "note": "%s is gone." % name}
        removed = []
        for part in ("region", "entities", "poi", "DIM-1", "DIM1", "level.dat", "level.dat_old"):
            try:
                be.delete("%s/%s" % (name, part))
                removed.append(part)
            except Exception:
                continue
        if not removed:
            raise RuntimeError("Nothing to clear in %s." % name)
        self.emit(">>> world %s reset (%s)" % (name, ", ".join(removed)))
        self.discord.event("files", "World contents reset", "%s — %s" % (name, ", ".join(removed)))
        return {"ok": True, "note": "Cleared %s. It regenerates on next start." % ", ".join(removed)}

    # -- appearance --------------------------------------------------------
    def motd_get(self):
        vals = {}
        for i in self.props_get().get("items", []):
            if i["key"] in ("motd", "resource-pack", "resource-pack-sha1",
                            "resource-pack-prompt", "require-resource-pack",
                            "max-players", "level-name"):
                vals[i["key"]] = i["value"]
        return {"ok": True, "values": vals, "icon": self.icon_info()}

    def motd_save(self, values):
        allowed = {k: v for k, v in (values or {}).items()
                   if k in ("motd", "resource-pack", "resource-pack-sha1",
                            "resource-pack-prompt", "require-resource-pack", "max-players")}
        if not allowed:
            raise RuntimeError("Nothing to save.")
        self.props_save(allowed)
        mh = self.minehut()
        if mh:
            for k, v in allowed.items():
                try:
                    mh.set_property(k, v)
                except MinehutError as e:
                    self.emit("!!! minehut: could not mirror %s (%s)" % (k, e))
        return {"ok": True, "changed": len(allowed)}

    ICON_NAME = "server-icon.png"

    def icon_info(self):
        try:
            st = self.backend().stat_file(self.ICON_NAME)
        except Exception:
            return {"present": False}
        return {"present": True, "size": fmt_size(st.get("size", 0)),
                "bytes": st.get("size", 0)}

    def icon_bytes(self):
        return self.backend().read_raw(self.ICON_NAME, 200_000)

    def local_icon_path(self, srv):
        """server-icon.png of a local server, whether or not it is selected."""
        lc = srv.get("local") or {}
        cwd = lc.get("cwd") or (str(Path(lc["jar"]).parent) if lc.get("jar") else "")
        p = Path(cwd) / self.ICON_NAME if cwd else None
        return str(p) if p and p.is_file() else ""

    def icon_bytes_for(self, sid):
        """Any server's icon by id: read from disk for local ones, through the
        backend for the selected remote one."""
        srv = self.find(sid)
        if not srv:
            raise RuntimeError("No such server.")
        if srv.get("kind") == "local":
            path = self.local_icon_path(srv)
            if not path or os.path.getsize(path) > 200_000:
                raise RuntimeError("No server icon set.")
            return Path(path).read_bytes()
        if sid != self.cfg.get("active"):
            raise RuntimeError("Remote icons are read when the server is open.")
        return self.icon_bytes()

    @staticmethod
    def png_size(data):
        """Width/height straight out of the IHDR chunk — no image library."""
        if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
            return None
        return struct.unpack(">II", data[16:24])

    def icon_set(self, data):
        dims = self.png_size(data)
        if not dims:
            raise RuntimeError("That is not a PNG. Minecraft only accepts a PNG server icon.")
        if dims != (64, 64):
            raise RuntimeError("A server icon must be exactly 64x64 — that one is %dx%d." % dims)
        if len(data) > 64 * 1024:
            raise RuntimeError("That file is %s; keep the icon under 64 KB." % fmt_size(len(data)))
        be = self.backend()
        if be.kind == "local":
            target = be.resolve(self.ICON_NAME)
            target.write_bytes(data)
        else:
            be._with_retry(lambda sf: be._put(sf, be.abs(self.ICON_NAME), data))
        self.emit(">>> server icon updated (%dx%d, %s)" % (dims[0], dims[1], fmt_size(len(data))))
        self.discord.event("files", "Server icon updated", "%dx%d" % dims)
        return {"ok": True, "size": fmt_size(len(data))}

    def icon_clear(self):
        self.backend().delete(self.ICON_NAME)
        self.emit(">>> server icon removed.")
        return {"ok": True}

    # -- plugins -----------------------------------------------------------
    def plugins_list(self):
        try:
            listing = self.backend().list_dir("plugins")
        except Exception as e:
            return {"ok": False, "error": "No plugins folder here (%s)." % e, "items": []}
        items = []
        for i in listing.get("items", []):
            if i["dir"]:
                continue
            n = i["name"]
            low = n.lower()
            if not (low.endswith(".jar") or low.endswith(".jar.disabled")):
                continue
            off = low.endswith(".disabled")
            items.append({"name": n, "rel": i["rel"], "size": i["size"],
                          "mod": i["mod"], "enabled": not off,
                          "title": n[:-9] if off else n})
        items.sort(key=lambda x: x["title"].lower())
        return {"ok": True, "items": items, "count": len(items)}

    def plugin_toggle(self, rel):
        """Disabling is a rename, which is how server owners already do it and
        means nothing is destroyed."""
        be = self.backend()
        name = (rel or "").replace("\\", "/").split("/")[-1]
        if not name.lower().endswith((".jar", ".jar.disabled")):
            raise RuntimeError("That is not a plugin jar.")
        new = name[:-9] if name.lower().endswith(".disabled") else name + ".disabled"
        be.rename(rel, new)
        self.emit(">>> plugin %s %s" % (new, "enabled" if new.lower().endswith(".jar") else "disabled"))
        self.discord.event("files", "Plugin %s" % ("enabled" if new.lower().endswith(".jar") else "disabled"), name)
        return {"ok": True, "name": new,
                "note": "Restart the server for this to take effect."}

    # -- ops / whitelist / bans -------------------------------------------
    def list_edit(self, which, player, add):
        verbs = {
            "ops": ("op", "deop"),
            "whitelist": ("whitelist add", "whitelist remove"),
            "banned-players": ("ban", "pardon"),
            "banned-ips": ("ban-ip", "pardon-ip"),
        }
        if which not in verbs:
            raise RuntimeError("Unknown list.")
        player = (player or "").strip()
        if which == "banned-ips":
            if not re.fullmatch(r"[0-9a-fA-F:.]{3,45}", player):
                raise RuntimeError("That is not an IP address.")
        elif not re.fullmatch(r"[A-Za-z0-9_]{2,16}", player):
            raise RuntimeError("That is not a player name.")
        cmd = "%s %s" % (verbs[which][0 if add else 1], player)
        ok, out = self.command(cmd, quiet=True, who="panel")
        if ok:
            self.discord.event("player_act", "%s: %s %s" % (which, "add" if add else "remove", player),
                               "", player if which != "banned-ips" else "")
        return ok, out or ("Sent." if ok else "Could not send — the server has to be reachable.")

    # -- eula --------------------------------------------------------------
    @staticmethod
    def _eula_ok(cwd):
        if not cwd:
            return False
        p = Path(cwd) / "eula.txt"
        if not p.exists():
            return False
        try:
            return "eula=true" in p.read_text(encoding="utf-8", errors="replace").lower()
        except OSError:
            return False

    def accept_eula(self):
        s = self.server()
        if s.get("kind") != "local":
            raise RuntimeError("Accept the EULA on the host for remote servers.")
        cwd = s["local"].get("cwd")
        if not cwd:
            raise RuntimeError("Set the server folder first.")
        p = Path(cwd) / "eula.txt"
        p.write_text("# Accepted through Lodestone on %s\neula=true\n"
                     % datetime.now().strftime("%Y-%m-%d %H:%M"), encoding="utf-8")
        self.emit(">>> eula.txt accepted.")
        return True

    # -- server.properties -------------------------------------------------
    def props_get(self):
        be = self.backend()
        try:
            res = be.read_file("server.properties")
        except Exception as e:
            return {"ok": False, "error": str(e), "items": []}
        items = []
        for raw in res["content"].splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            kind = "bool" if k in BOOL_KEYS else ("enum" if k in ENUM_KEYS else "text")
            items.append({"key": k, "value": v, "type": kind,
                          "options": ENUM_KEYS.get(k, [])})
        items.sort(key=lambda i: i["key"])
        return {"ok": True, "items": items, "mtime": res.get("mtime", 0)}

    def props_save(self, values):
        be = self.backend()
        res = be.read_file("server.properties")
        out, seen = [], set()
        for raw in res["content"].splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                out.append(raw)
                continue
            k = line.split("=", 1)[0].strip()
            if k in values:
                out.append("%s=%s" % (k, values[k]))
                seen.add(k)
            else:
                out.append(raw)
        for k, v in values.items():
            if k not in seen:
                out.append("%s=%s" % (k, v))
        be.write_file("server.properties", "\n".join(out) + "\n")
        self.emit(">>> server.properties saved (%d changed)." % len(values))
        self.discord.event("files", "server.properties updated",
                           ", ".join(sorted(values)[:20]) or "—")
        return {"ok": True, "changed": len(values)}

    # -- json lists: ops / whitelist / bans --------------------------------
    LISTS = {"ops": "ops.json", "whitelist": "whitelist.json",
             "banned-players": "banned-players.json", "banned-ips": "banned-ips.json"}

    def list_get(self, which):
        fn = self.LISTS.get(which)
        if not fn:
            raise RuntimeError("Unknown list.")
        be = self.backend()
        try:
            data = json.loads(be.read_file(fn)["content"] or "[]")
        except Exception:
            data = []
        return {"ok": True, "which": which, "items": data if isinstance(data, list) else []}

    # -- players -----------------------------------------------------------
    def refresh_players(self):
        """Ask the server who is on, for cases where we joined mid-session."""
        ok, out = self.command("list", quiet=True)
        if not ok:
            return sorted(self.players)
        m = re.search(r":\s*(.*)$", (out or "").strip())
        names = [n.strip() for n in (m.group(1) if m else "").split(",") if n.strip()]
        if names:
            self.players = set(n for n in names if re.fullmatch(r"[A-Za-z0-9_]{2,16}", n))
        return sorted(self.players)

    def player_action(self, player, action, reason=""):
        player = safe_name(player)
        if not re.fullmatch(r"[A-Za-z0-9_]{2,16}", player or ""):
            raise RuntimeError("That is not a player name.")
        reason = (reason or "").replace("\n", " ")[:120]
        verbs = {
            "op": "op %s" % player,
            "deop": "deop %s" % player,
            "kick": ("kick %s %s" % (player, reason)).strip(),
            "ban": ("ban %s %s" % (player, reason)).strip(),
            "pardon": "pardon %s" % player,
            "whitelist_add": "whitelist add %s" % player,
            "whitelist_remove": "whitelist remove %s" % player,
        }
        if action not in verbs:
            raise RuntimeError("Unknown action.")
        ok, out = self.command(verbs[action], quiet=True)
        if ok:
            self.discord.event("player_act", "%s: %s" % (action.replace("_", " "), player),
                               reason, player)
        return ok, out or ("Sent." if ok else "Could not send.")

    # -- backups -----------------------------------------------------------
    def backup_dir(self):
        d = BACKUP_ROOT / self.server()["id"]
        d.mkdir(parents=True, exist_ok=True)
        return d

    def backups_list(self):
        out = []
        for p in sorted(self.backup_dir().glob("*.zip"), reverse=True):
            try:
                st = p.stat()
            except OSError:
                continue
            out.append({"name": p.name, "size": fmt_size(st.st_size),
                        "bytes": st.st_size,
                        "when": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M")})
        return {"ok": True, "items": out, "dir": str(self.backup_dir()),
                "busy": self.backup["busy"], "msg": self.backup["msg"]}

    def backup_create(self, label="", what="world"):
        if self.backup["busy"]:
            return False, "A backup is already running."
        label = re.sub(r"[^A-Za-z0-9 _-]", "", label or "").strip()[:40]
        name = "%s%s.zip" % (datetime.now().strftime("%Y-%m-%d_%H-%M"),
                             ("_" + label.replace(" ", "-")) if label else "")
        self.backup.update({"busy": True, "msg": "starting…", "pct": 0})
        threading.Thread(target=self._backup_worker, args=(name, what), daemon=True).start()
        return True, "Backup started."

    def _backup_worker(self, name, what):
        be = self.backend()
        dest = self.backup_dir() / name
        started = time.time()
        try:
            if self.is_running():
                self.command("save-off", quiet=True)
                self.command("save-all flush", quiet=True)
                time.sleep(2)
            targets = []
            if what == "all":
                targets = [""]
            else:
                listing = be.list_dir("")
                targets = [i["rel"] for i in listing["items"]
                           if i["dir"] and (i["name"] == "world" or i["name"].startswith("world"))]
                if not targets:
                    targets = [""]
            self.backup["msg"] = "packing %s…" % (", ".join(t or "everything" for t in targets))
            self.emit(">>> backup: packing %s" % ", ".join(t or "everything" for t in targets))
            # One archive, every path stored relative to the server root, so a
            # restore drops each file back exactly where it came from.
            part = dest.with_suffix(".part")
            with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                for t in targets:
                    self.backup["msg"] = "packing %s…" % (t or "everything")
                    be.zip_into(zf, t, (t.rstrip("/") + "/") if t else "")
            part.replace(dest)
            size = dest.stat().st_size
            took = int(time.time() - started)
            self.backup.update({"busy": False, "msg": "done — %s" % fmt_size(size), "pct": 100})
            self.emit(">>> backup finished: %s (%s in %ss)" % (name, fmt_size(size), took))
            self.discord.event("backup", "Backup finished", name, "",
                               [("Size", fmt_size(size), True), ("Took", "%ss" % took, True)])
            self._prune_backups()
        except Exception as e:
            self.backup.update({"busy": False, "msg": "failed: %s" % e, "pct": 0})
            self.emit("!!! backup failed: %s" % e)
            self.discord.event("backup", "Backup failed", str(e)[:1000])
        finally:
            if self.is_running():
                self.command("save-on", quiet=True)

    def _prune_backups(self):
        keep = int(self.server().get("features", {}).get("auto_backup", {}).get("keep", 10) or 10)
        files = sorted(self.backup_dir().glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[keep:]:
            try:
                p.unlink()
                self.emit(">>> backup: pruned %s" % p.name)
            except OSError:
                pass

    def backup_delete(self, name):
        p = self.backup_dir() / safe_name(name)
        if p.suffix != ".zip" or not p.is_file():
            raise RuntimeError("No such backup.")
        p.unlink()
        return True

    def backup_restore(self, name):
        if self.backup["busy"]:
            raise RuntimeError("Wait for the running backup to finish.")
        p = self.backup_dir() / safe_name(name)
        if p.suffix != ".zip" or not p.is_file():
            raise RuntimeError("No such backup.")
        if self.is_running():
            raise RuntimeError("Stop the server before restoring.")
        self.backup.update({"busy": True, "msg": "restoring…", "pct": 0})
        threading.Thread(target=self._restore_worker, args=(p,), daemon=True).start()
        return True

    def _restore_worker(self, path):
        be = self.backend()
        try:
            self.emit(">>> restore: unpacking %s" % path.name)
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
                total = len(names)
                for i, member in enumerate(names):
                    if member.endswith("/"):
                        continue
                    rel = safe_rel(member)
                    if not rel:
                        continue
                    data = z.read(member)
                    if be.kind == "local":
                        target = be.resolve(rel)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(data)
                    else:
                        be._with_retry(lambda s, r=rel, d=data: be._put(s, be.abs(r), d))
                    if i % 25 == 0:
                        self.backup["msg"] = "restoring %d/%d" % (i, total)
                        self.backup["pct"] = int(i * 100 / max(1, total))
            self.backup.update({"busy": False, "msg": "restored", "pct": 100})
            self.emit(">>> restore finished.")
            self.discord.event("backup", "Backup restored", path.name)
        except Exception as e:
            self.backup.update({"busy": False, "msg": "restore failed: %s" % e, "pct": 0})
            self.emit("!!! restore failed: %s" % e)
            self.discord.event("backup", "Restore failed", str(e)[:1000])

    # -- automation --------------------------------------------------------
    def features_get(self):
        return default_features(self.server().setdefault("features", {}))

    def features_save(self, data):
        f = self.features_get()
        for k, v in (data or {}).items():
            if k in f:
                if isinstance(f[k], dict) and isinstance(v, dict):
                    f[k].update(v)
                else:
                    f[k] = v
        self.server()["features"] = f
        self.save()
        return f

    def macro_run(self, name):
        for m in self.features_get().get("macros", []):
            if m.get("name") == name:
                cmds = [c.strip() for c in (m.get("commands") or "").splitlines() if c.strip()]
                if not cmds:
                    return False, "That macro is empty."
                threading.Thread(target=self._run_seq, args=(cmds, name), daemon=True).start()
                return True, "Running %d command%s." % (len(cmds), "" if len(cmds) == 1 else "s")
        return False, "No macro by that name."

    def _run_seq(self, cmds, label=""):
        for c in cmds:
            self.command(c, quiet=True, who="macro:%s" % label if label else "macro")
            time.sleep(0.35)
        self.discord.event("schedule", "Macro run", "%s — %d commands" % (label or "macro", len(cmds)))

    def _feature_loop(self):
        last = {}
        while True:
            time.sleep(10)
            try:
                self._feature_tick(last)
            except Exception:
                pass

    def _feature_tick(self, last):
        try:
            self.discord.status_tick()
        except Exception:
            pass
        f = self.features_get()
        now = time.time()
        clock = datetime.now().strftime("%H:%M")
        alive = self.is_running() or bool(self.server().get("rcon", {}).get("enabled"))

        def due(key, minutes):
            if minutes <= 0:
                return False
            if key not in last:
                last[key] = now
                return False
            if now - last[key] >= minutes * 60:
                last[key] = now
                return True
            return False

        if alive:
            b = f.get("broadcast", {})
            if b.get("enabled") and (b.get("message") or "").strip() and due("bc", int(b.get("minutes") or 30)):
                self.command("say " + b["message"].strip(), quiet=True)
            cl = f.get("clearlag", {})
            if cl.get("enabled") and due("cl", int(cl.get("minutes") or 10)):
                if cl.get("warn"):
                    self.command("say Clearing dropped items in 10 seconds…", quiet=True)
                    time.sleep(10)
                self.command("kill @e[type=item]", quiet=True)
                self.discord.event("schedule", "Clear-lag ran", "Dropped items cleared")
            sa = f.get("auto_save", {})
            if sa.get("enabled") and due("save", int(sa.get("minutes") or 15)):
                self.command("save-all flush", quiet=True)

        ab = f.get("auto_backup", {})
        if ab.get("enabled") and due("bk", int(ab.get("hours") or 6) * 60):
            if not self.backup["busy"]:
                self.backup_create("auto")

        dr = f.get("daily_restart", {})
        if dr.get("enabled") and clock == (dr.get("time") or "04:00") and last.get("dr") != clock:
            last["dr"] = clock
            if self.is_running():
                self.discord.event("schedule", "Daily restart", "Restarting at %s" % clock)
                self.command("say Server restarting in 30 seconds…", quiet=True)
                threading.Thread(target=self._delayed_restart, args=(30,), daemon=True).start()
        elif clock != (dr.get("time") or "04:00"):
            last.pop("dr", None)

        for i, t in enumerate(f.get("tasks", [])):
            key = "task%d" % i
            if t.get("time") == clock and last.get(key) != clock:
                last[key] = clock
                cmds = [c.strip() for c in (t.get("command") or "").splitlines() if c.strip()]
                if cmds:
                    self._run_seq(cmds, "task %s" % t.get("time"))
            elif t.get("time") != clock:
                last.pop(key, None)

        wd = f.get("watchdog", {})
        if wd.get("enabled") and self.is_running() and self.last_line_at:
            quiet_for = now - self.last_line_at
            if quiet_for > int(wd.get("minutes") or 5) * 60 and last.get("wd") != "fired":
                last["wd"] = "fired"
                self.emit(">>> watchdog: no output for %s — restarting." % fmt_dur(quiet_for))
                self.discord.event("crash", "Watchdog fired",
                                   "No console output for %s" % fmt_dur(quiet_for))
                self.restart()
            elif quiet_for < 60:
                last.pop("wd", None)

    def _delayed_restart(self, secs):
        time.sleep(secs)
        self.restart()

    # -- API tokens --------------------------------------------------------
    SCOPES = ["read", "control", "files", "admin"]

    def api_tokens(self):
        return [{"id": t["id"], "name": t.get("name") or "token",
                 "scopes": t.get("scopes", []), "prefix": t.get("prefix", ""),
                 "created": t.get("created", ""), "last_used": t.get("last_used", "never"),
                 "calls": t.get("calls", 0)}
                for t in self.cfg["api"].get("tokens", [])]

    def api_token_create(self, name, scopes):
        raw = "lds_" + secrets.token_urlsafe(32)
        rec = {"id": new_id(), "name": (name or "token")[:40],
               "scopes": [s for s in (scopes or ["read"]) if s in self.SCOPES] or ["read"],
               "hash": hashlib.sha256(raw.encode()).hexdigest(),
               "prefix": raw[:12], "created": datetime.now().strftime("%Y-%m-%d %H:%M"),
               "last_used": "never", "calls": 0}
        self.cfg["api"]["tokens"].append(rec)
        self.save()
        # Returned once and never again — only the hash is kept.
        return {"token": raw, "record": rec}

    def api_token_delete(self, tid):
        before = len(self.cfg["api"]["tokens"])
        self.cfg["api"]["tokens"] = [t for t in self.cfg["api"]["tokens"] if t["id"] != tid]
        self.save()
        return len(self.cfg["api"]["tokens"]) < before

    def api_auth(self, header):
        """Constant-time check of a bearer token; returns the record or None."""
        if not self.cfg["api"].get("enabled"):
            return None
        token = ""
        if header and header.lower().startswith("bearer "):
            token = header[7:].strip()
        if not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        for t in self.cfg["api"].get("tokens", []):
            if hmac.compare_digest(t.get("hash", ""), digest):
                t["last_used"] = datetime.now().strftime("%Y-%m-%d %H:%M")
                t["calls"] = int(t.get("calls", 0)) + 1
                self.watch["api_calls"] += 1
                return t
        return None


    # -- adopting the old apps ---------------------------------------------
    LEGACY = {"local": Path.home() / ".mc_control_panel.json",
              "sftp": Path.home() / ".mc_sftp_panel.json",
              "profiles": Path.home() / ".remotehost_profiles.json"}

    @staticmethod
    def _read_legacy(path):
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        except (json.JSONDecodeError, OSError):
            return None

    def _legacy_remotes(self):
        out = []
        prof = self._read_legacy(self.LEGACY["profiles"]) or {}
        for pr in prof.get("profiles", []):
            if pr.get("host"):
                out.append(pr)
        cur = self._read_legacy(self.LEGACY["sftp"])
        if cur and cur.get("host"):
            out.append(dict(cur, name=cur.get("display_name") or cur.get("host")))
        return out

    def _have_local(self, cwd):
        cwd = (cwd or "").replace("\\", "/").rstrip("/").lower()
        if not cwd:
            return False
        return any(x.get("kind") == "local" and
                   (x["local"].get("cwd") or "").replace("\\", "/").rstrip("/").lower() == cwd
                   for x in self.servers())

    def _have_remote(self, rec):
        return any(x.get("kind") == "sftp" and x["sftp"].get("host") == rec.get("host")
                   and x["sftp"].get("user") == rec.get("user")
                   and (x["sftp"].get("root") or "/") == (rec.get("root") or "/")
                   for x in self.servers())

    def legacy_available(self):
        """What LocalHost / RemoteHost left behind that is not on the shelf yet."""
        found = []
        loc = self._read_legacy(self.LEGACY["local"])
        if loc and (loc.get("cwd") or loc.get("jar")):
            where = loc.get("cwd") or loc.get("jar")
            found.append({"kind": "local", "name": Path(where).name or "LocalHost server",
                          "where": where, "new": not self._have_local(loc.get("cwd", "")),
                          "missing": not Path(loc.get("cwd") or ".").is_dir()})
        seen = set()
        for rec in self._legacy_remotes():
            key = (rec.get("host"), rec.get("user"), rec.get("root"))
            if key in seen:
                continue
            seen.add(key)
            found.append({"kind": "sftp",
                          "name": rec.get("name") or rec.get("host") or "RemoteHost server",
                          "where": "%s@%s%s" % (rec.get("user") or "?", rec.get("host") or "?",
                                                rec.get("root") or ""),
                          "new": not self._have_remote(rec), "missing": False})
        return found

    def import_legacy(self):
        """Pull LocalHost and RemoteHost setups onto the shelf. Anything already
        here is skipped, so pressing it twice is harmless."""
        added = []
        loc = self._read_legacy(self.LEGACY["local"])
        if loc and (loc.get("cwd") or loc.get("jar")) and not self._have_local(loc.get("cwd", "")):
            srv = blank_server("local", Path(loc.get("cwd") or loc.get("jar")).name or "LocalHost server")
            for k in ("java", "jar", "cwd", "min_ram", "max_ram", "extra",
                      "server_type", "args_file", "run_cmd"):
                if loc.get(k):
                    srv["local"][k] = loc[k]
            if isinstance(loc.get("features"), dict):
                srv["features"] = default_features(dict(loc["features"]))
                hook = loc.get("features", {}).get("webhook") or {}
                if hook.get("url"):        # LocalHost only ever had the one webhook
                    events = ["crash"]
                    if hook.get("join_leave", True):
                        events.append("join_leave")
                    if hook.get("start_stop", True):
                        events.append("start_stop")
                    srv["discord"]["hooks"] = [dict(Discord.blank_hook(), name="Imported webhook",
                                                    url=hook["url"],
                                                    enabled=bool(hook.get("enabled")),
                                                    events=events)]
            self.servers().append(srv)
            added.append(srv["name"])

        for rec in self._legacy_remotes():
            if self._have_remote(rec):
                continue
            srv = blank_server("sftp", rec.get("name") or rec.get("host") or "RemoteHost server")
            srv["sftp"].update({"host": rec.get("host", ""), "port": rec.get("port") or 22,
                                "user": rec.get("user", ""), "password": rec.get("password") or "",
                                "save_password": bool(rec.get("save_password", True)),
                                "root": rec.get("root") or "/"})
            self.servers().append(srv)
            added.append(srv["name"])

        # the assistant key and rules were global in LocalHost
        if loc and isinstance(loc.get("ai"), dict) and not self.cfg["ai"].get("api_key"):
            for k, v in loc["ai"].items():
                self.cfg["ai"][k] = v
            added.append("assistant settings")

        if added:
            # drop the untouched starter row now that real servers exist
            keep = [x for x in self.servers()
                    if x.get("name") != "My server" or x["local"].get("cwd") or x["sftp"].get("host")]
            if keep:
                self.cfg["servers"] = keep
            if self.cfg.get("active") not in [x["id"] for x in self.servers()]:
                self.cfg["active"] = self.servers()[0]["id"]
            self.save()
        return added

    # -- assistant ---------------------------------------------------------
    # Ported from LocalHost, but routed through the backend, so it now drives
    # remote servers over SFTP/RCON exactly as it drives local ones.

    def ai_get(self):
        a = dict(self.cfg.get("ai", {}))
        a["has_key"] = bool(a.get("api_key"))
        a["local"] = a.get("provider") in self.LOCAL_PROVIDERS
        a["needs_key"] = not a["local"]
        a["base_default"] = self.PROVIDERS.get(a.get("provider", "openai"),
                                               self.PROVIDERS["openai"])[0]
        a.pop("api_key", None)
        a["usage"] = self.ai_usage_total
        a["history"] = len(self.ai_history)
        return a

    def ai_save(self, data):
        a = self.cfg.setdefault("ai", {})
        for k in ("enabled", "allow_commands", "allow_files", "automod", "automod_auto"):
            if k in data:
                a[k] = bool(data[k])
        for k in ("provider", "model", "base_url", "rules"):
            if k in data:
                a[k] = str(data[k])
        if "temperature" in data:
            try:
                a["temperature"] = max(0.0, min(2.0, float(data["temperature"])))
            except (TypeError, ValueError):
                pass
        if data.get("api_key"):          # only overwrite when a new key is supplied
            a["api_key"] = str(data["api_key"]).strip()
        self.save()
        return True, "Assistant settings saved."

    def ai_clear(self):
        self.ai_history = []
        return True, "Chat cleared."

    # name -> (default base url, default model, is it an openai-compatible shape)
    PROVIDERS = {
        "openai":    ("https://api.openai.com/v1", "gpt-4o-mini", True),
        "anthropic": ("https://api.anthropic.com", "claude-sonnet-5", False),
        "gemini":    ("https://generativelanguage.googleapis.com/v1beta", "gemini-2.5-flash", False),
        "ollama":    ("http://localhost:11434", "llama3.1:8b", False),
        "lmstudio":  ("http://localhost:1234/v1", "", True),
        "llamacpp":  ("http://localhost:8080/v1", "", True),
        "custom":    ("http://localhost:8000/v1", "", True),
    }
    LOCAL_PROVIDERS = ("ollama", "lmstudio", "llamacpp", "custom")

    @classmethod
    def _default_model(cls, provider):
        return cls.PROVIDERS.get(provider, cls.PROVIDERS["openai"])[1] or "gpt-4o-mini"

    @classmethod
    def _base_for(cls, a):
        return (a.get("base_url") or "").rstrip("/") or \
            cls.PROVIDERS.get(a.get("provider", "openai"), cls.PROVIDERS["openai"])[0]

    def ai_models(self):
        """Ask the configured server what models it actually has. Local servers
        answer without a key, which makes this the fastest way to tell whether
        Ollama is even running."""
        a = self.cfg.get("ai", {})
        prov = a.get("provider", "openai")
        base = self._base_for(a)
        try:
            if prov == "ollama":
                data = self._http_get(base + "/api/tags", {}, "Ollama")
                names = [m.get("name") or m.get("model") for m in data.get("models", [])]
                return True, [n for n in names if n], base
            if prov in ("lmstudio", "llamacpp", "custom", "openai"):
                headers = {}
                if a.get("api_key"):
                    headers["Authorization"] = "Bearer " + a["api_key"]
                data = self._http_get(base + "/models", headers, prov)
                names = [m.get("id") for m in data.get("data", [])]
                return True, [n for n in names if n], base
            return False, [], base
        except Exception as e:
            return False, str(e), base

    @staticmethod
    def _http_get(url, headers, who="API"):
        req = urllib.request.Request(url, headers=headers or {}, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=12) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            raise RuntimeError("%s error %s at %s" % (who, e.code, url))
        except urllib.error.URLError as e:
            raise RuntimeError("Could not reach %s at %s (%s). Is it running?"
                               % (who, url, e.reason))

    @staticmethod
    def _http_json(url, body, headers, who="API", timeout=60):
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                eb = json.loads(e.read().decode())
                err = eb.get("error", eb)
                detail = err.get("message") if isinstance(err, dict) else str(err)
            except Exception:
                detail = ""
            hint = {401: " (the key looks wrong or lacks access)",
                    403: " (the key looks wrong or lacks access)",
                    404: " (the model name is probably wrong - press Detect to list what is installed)",
                    429: " (rate limited or out of credit)"}.get(e.code, "")
            raise RuntimeError("%s error %s: %s%s" % (who, e.code, detail or e.reason, hint))
        except urllib.error.URLError as e:
            raise RuntimeError("Could not reach %s (%s). If it is a local model server, check it is "
                               "running and the base URL is right." % (who, e.reason))

    def _ai_chat(self, system, messages, force_json=False):
        """One completion. Returns (text, {'in':n,'out':n})."""
        a = self.cfg.get("ai", {})
        provider = a.get("provider", "openai")
        key = a.get("api_key", "")
        model = (a.get("model") or "").strip() or self._default_model(provider)
        base = self._base_for(a)
        if not key and provider not in self.LOCAL_PROVIDERS:
            raise RuntimeError("No API key set — add one in the Assistant tab.")

        if provider == "ollama":
            # Native Ollama. stream must be off, and format:"json" is what keeps
            # a small local model from wrapping its reply in prose.
            body = {"model": model, "stream": False,
                    "messages": [{"role": "system", "content": system}] + messages,
                    "options": {"temperature": float(a.get("temperature", 0.4) or 0.4)}}
            if force_json:
                body["format"] = "json"
            if a.get("num_ctx"):
                body["options"]["num_ctx"] = int(a["num_ctx"])
            data = self._http_json(base + "/api/chat", body,
                                   {"Content-Type": "application/json"}, "Ollama", timeout=300)
            text = (data.get("message") or {}).get("content", "")
            if not text and data.get("response"):
                text = data["response"]
            return text, {"in": data.get("prompt_eval_count", 0) or 0,
                          "out": data.get("eval_count", 0) or 0}

        if provider == "anthropic":
            data = self._http_json(
                base.rstrip("/") + "/v1/messages" if not base.endswith("/v1")
                else base + "/messages",
                {"model": model, "max_tokens": 2048, "system": system, "messages": messages},
                {"Content-Type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"}, "Anthropic")
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            u = data.get("usage", {})
            return text, {"in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0)}

        if provider == "gemini":
            url = "%s/models/%s:generateContent?key=%s" % (base, model, key)
            contents = [{"role": "user" if msg["role"] == "user" else "model",
                         "parts": [{"text": msg["content"]}]} for msg in messages]
            data = self._http_json(url, {"contents": contents,
                                         "systemInstruction": {"parts": [{"text": system}]}},
                                   {"Content-Type": "application/json"}, "Gemini")
            cands = data.get("candidates", [])
            if not cands:
                raise RuntimeError("Gemini returned nothing — check the model name and key.")
            text = "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", []))
            u = data.get("usageMetadata", {})
            return text, {"in": u.get("promptTokenCount", 0), "out": u.get("candidatesTokenCount", 0)}

        # OpenAI and everything that speaks its shape: LM Studio, llama.cpp,
        # vLLM, text-generation-webui, OpenRouter, Together, and so on.
        body = {"model": model, "messages": [{"role": "system", "content": system}] + messages}
        if force_json and provider in ("openai", "custom", "lmstudio", "vllm"):
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = "Bearer " + key
        data = self._http_json(base + "/chat/completions", body, headers,
                               provider or "OpenAI",
                               timeout=300 if provider in self.LOCAL_PROVIDERS else 60)
        ch = data.get("choices", [])
        if not ch:
            raise RuntimeError("No response from the model — check the model name.")
        u = data.get("usage", {})
        return ch[0].get("message", {}).get("content", ""), \
            {"in": u.get("prompt_tokens", 0), "out": u.get("completion_tokens", 0)}

    def ai_test(self):
        a = self.cfg.get("ai", {})
        if not a.get("api_key") and a.get("provider") not in self.LOCAL_PROVIDERS:
            return False, "No API key set. Paste one and save first."
        try:
            txt, _u = self._ai_chat("You are a connection test. Reply with exactly: OK",
                                    [{"role": "user", "content": "ping"}])
        except Exception as e:
            return False, str(e)
        prov = a.get("provider", "openai")
        model = (a.get("model") or "").strip() or self._default_model(prov)
        return True, "Connected to %s (%s). Reply: %s" % (prov, model, txt.strip()[:40] or "(empty)")

    @staticmethod
    def _extract_json(text):
        text = (text or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("\n") + 1:] if "\n" in text else text
        i, j = text.find("{"), text.rfind("}")
        if i >= 0 and j > i:
            try:
                return json.loads(text[i:j + 1])
            except json.JSONDecodeError:
                return None
        return None

    AI_SYSTEM = (
        "You are the built-in admin assistant for a Minecraft Java server, running inside a control "
        "panel. You can actually operate the server through the actions below. Work step by step, ONE "
        "action per reply, and reply with STRICT JSON only — no prose, no markdown, no code fences "
        "outside the JSON object.\n\n"
        "Available actions:\n"
        '  {"action":"run","command":"CMD"}          run a console command (no leading slash)\n'
        '  {"action":"say","message":"TEXT"}          broadcast a chat message to players\n'
        '  {"action":"read_console","lines":80}       read recent console/log output\n'
        '  {"action":"list_dir","path":"REL"}         list a folder (empty path = server root)\n'
        '  {"action":"read_file","path":"REL"}        read a text file (read before editing!)\n'
        '  {"action":"write_file","path":"REL","content":"FULL NEW CONTENTS"}   overwrite/create a file\n'
        '  {"action":"properties"}                    read server.properties as key/value pairs\n'
        '  {"action":"set_property","key":"K","value":"V"}   change one server.properties key\n'
        '  {"action":"moderate","player":"NAME","act":"op|deop|kick|ban|pardon|whitelist_add|'
        'whitelist_remove","reason":"TEXT"}\n'
        '  {"action":"notify","title":"TEXT","description":"TEXT"}   post to the Discord webhooks\n'
        '  {"action":"backup"}                        make a world backup\n'
        '  {"action":"done","reply":"SUMMARY"}        finish and tell the human what you did\n\n'
        "Rules:\n"
        "- ALWAYS finish with a 'done' action containing a clear, friendly reply. Never end without it.\n"
        '- To "check the logs" or diagnose a crash, use read_console, and/or read_file on "logs/latest.log".\n'
        "- When editing config, read_file first, change only what is needed, then write_file the whole "
        "file back.\n"
        "- Paths are confined to the server folder. Keep changes minimal and safe. Never delete files.\n"
        "- The server may be remote: commands go over RCON, files over SFTP. If an action is denied or "
        "fails, adapt and explain it in your final reply.\n"
        "- If the task is just a question, answer it in 'done' without changing anything.\n\n"
        "SKRIPT EXPERTISE — this server likely uses Skript. You are an expert Skript developer:\n"
        "- Skript files end in .sk and live under plugins/Skript/scripts/. Reload with: sk reload <name>.\n"
        "- Syntax is indentation-based (nesting matters). Structures: 'command /name:', 'on <event>:', "
        "'every X', 'function f(a: type) :: type:'. Common events: on join, on quit, on death, on right "
        "click, on chat.\n"
        "- Use options (options: / {@opt}), local vars {_x}, global vars {x}, list vars {list::*}. Text "
        "uses &-colour codes or hex <#rrggbb>. Prefer vanilla/Skript built-ins; only assume addons "
        "(skript-reflect, SkBee, skript-gui) if the script already uses them.\n"
        "- Produce complete, working, correctly-indented scripts with NO comments unless asked, and keep "
        "the existing style.\n"
        "- After creating or editing a .sk file, run 'sk reload <filename-without-extension>' and report "
        "any parse errors from read_console.")

    def ai_run(self, instruction, attachments=None, on_step=None):
        a = self.cfg.get("ai", {})
        if not a.get("api_key") and a.get("provider") not in self.LOCAL_PROVIDERS:
            return False, "No API key yet — open Assistant, paste your key and save.", [], {"in": 0, "out": 0}
        srv = self.server()
        be = self.backend()
        ctx = ("[server] %s (%s); state: %s; players: %s; folder: %s; commands: %s; "
               "permissions: run=%s, files=%s."
               % (srv.get("name"), srv.get("kind"),
                  "running" if self.is_running() else be.status().get("state", "offline"),
                  ", ".join(sorted(self.players)) or "none", be.label(),
                  "stdin" if self.is_running() else ("rcon" if srv.get("rcon", {}).get("enabled") else "unavailable"),
                  "on" if a.get("allow_commands", True) else "OFF",
                  "on" if a.get("allow_files") else "OFF"))
        att = ""
        for f in (attachments or []):
            att += "\n\n[attached file: %s]\n%s\n[end of %s]" % (
                str(f.get("name", "attachment")), str(f.get("content", ""))[:12000],
                str(f.get("name", "attachment")))
        messages = [dict(msg) for msg in self.ai_history] + \
                   [{"role": "user", "content": "%s\n\nTask: %s%s" % (ctx, instruction, att)}]
        log, usage, reply = [], {"in": 0, "out": 0}, None
        for _ in range(12):
            try:
                raw, u = self._ai_chat(self.AI_SYSTEM, messages, force_json=True)
            except Exception as e:
                return False, str(e), log, usage
            usage["in"] += u.get("in", 0)
            usage["out"] += u.get("out", 0)
            obj = self._extract_json(raw)
            if not obj:
                reply = raw.strip()[:1500] or "(the model returned an empty response)"
                break
            act = obj.get("action")
            messages.append({"role": "assistant", "content": json.dumps(obj)})
            if act == "done":
                reply = obj.get("reply", "Done.")
                break
            result, note = self._ai_do(act, obj)
            log.append({"action": act, "detail": note})
            if on_step:
                on_step(act, note)
            messages.append({"role": "user", "content": "RESULT: " + result})
        if reply is None:
            reply = "Stopped after 12 steps — try breaking the task into smaller asks."
        self.ai_history = (self.ai_history +
                           [{"role": "user", "content": instruction},
                            {"role": "assistant", "content": reply}])[-8:]
        self.ai_usage_total = {"in": self.ai_usage_total["in"] + usage["in"],
                               "out": self.ai_usage_total["out"] + usage["out"]}
        usage["total_in"] = self.ai_usage_total["in"]
        usage["total_out"] = self.ai_usage_total["out"]
        return True, reply, log, usage

    def _ai_do(self, act, obj):
        a = self.cfg.get("ai", {})
        be = self.backend()
        try:
            if act == "run":
                if not a.get("allow_commands", True):
                    return "denied: running commands is turned off in Assistant settings", "run (denied)"
                cmd = (obj.get("command") or "").strip()
                ok, out = self.command(cmd, quiet=True, who="assistant")
                return "ran '%s': %s" % (cmd, out or ("sent" if ok else "failed")), "run: " + cmd
            if act == "say":
                ok, out = self.command("say " + (obj.get("message") or ""), quiet=True, who="assistant")
                return "say: %s" % (out or ("sent" if ok else "failed")), \
                       "say: " + (obj.get("message") or "")[:40]
            if act == "read_console":
                try:
                    n = max(10, min(300, int(obj.get("lines", 80))))
                except (TypeError, ValueError):
                    n = 80
                return "\n".join(t for _s, t in self.recent(n)) or "(console is empty)", \
                       "read_console (%d lines)" % n
            if act == "list_dir":
                d = be.list_dir(obj.get("path", ""))
                return "entries: " + ", ".join((("[D] " if i["dir"] else "") + i["name"])
                                               for i in d["items"][:200]), \
                       "list_dir " + (obj.get("path") or "/")
            if act == "read_file":
                return be.read_file(obj.get("path", ""))["content"][:6000], \
                       "read_file " + (obj.get("path") or "")
            if act == "write_file":
                if not a.get("allow_files", False):
                    return "denied: file editing is turned off in Assistant settings", "write_file (denied)"
                be.write_file(obj.get("path", ""), obj.get("content", ""))
                self.discord.event("files", "Assistant edited a file", obj.get("path", ""))
                return "wrote " + (obj.get("path") or ""), "write_file " + (obj.get("path") or "")
            if act == "properties":
                p = self.props_get()
                if not p.get("ok"):
                    return "could not read server.properties: " + p.get("error", ""), "properties"
                return "\n".join("%s=%s" % (i["key"], i["value"]) for i in p["items"]), "properties"
            if act == "set_property":
                if not a.get("allow_files", False):
                    return "denied: file editing is turned off in Assistant settings", "set_property (denied)"
                key, val = obj.get("key", ""), str(obj.get("value", ""))
                self.props_save({key: val})
                return "set %s=%s" % (key, val), "set_property %s" % key
            if act == "moderate":
                if not a.get("allow_commands", True):
                    return "denied: running commands is turned off in Assistant settings", "moderate (denied)"
                ok, out = self.player_action(obj.get("player", ""), obj.get("act", ""),
                                             obj.get("reason", ""))
                return "%s %s: %s" % (obj.get("act"), obj.get("player"), out), \
                       "moderate %s %s" % (obj.get("act"), obj.get("player"))
            if act == "notify":
                self.discord.event("command", obj.get("title", "Assistant"),
                                   obj.get("description", ""))
                return "posted to the subscribed webhooks", "notify"
            if act == "backup":
                ok, msg = self.backup_create("assistant")
                return msg, "backup"
        except Exception as e:
            return "action failed: %s" % e, "%s (failed)" % act
        return "unknown action", str(act)

    def ai_attach(self):
        """Pick a local text file and hand its contents to the assistant."""
        src, err = native_pick("anyfile")
        if err:
            return False, err, "", ""
        if not src:
            return False, "Nothing selected.", "", ""
        p = Path(src)
        try:
            if p.stat().st_size > 200 * 1024:
                return False, "Too big to attach (200 KB max).", "", ""
            content = p.read_text(errors="replace")
        except (OSError, UnicodeDecodeError) as e:
            return False, "Could not read that file: %s" % e, "", ""
        return True, "attached", p.name, content

    # -- development assistant ---------------------------------------------
    # A local model working inside a project: see agent.py. Every local
    # server is a workspace, and folders (a plugin or mod being written) can
    # be added as more. The panel's part is the settings, the server bridge
    # and the routes.

    def dev_cfg(self):
        d = self.cfg.setdefault("dev", {})
        if "approval_mode" not in d:
            # settings from before approval modes: the one switch there was
            d["approval_mode"] = "edits" if d.get("auto_approve_edits") else "ask"
        for k, v in DEV_DEFAULTS.items():
            d.setdefault(k, json.loads(json.dumps(v)))
        return d

    def perf_mode(self):
        return bool(self.cfg.get("dev", {}).get("perf_mode"))

    def dev_agent(self):
        if not HAVE_DEV:
            raise RuntimeError(DEV_ERROR)
        with self._agent_lock:
            if self.agent is None:
                self.agent = agentmod.Agent(DATA_DIR / "agent", self.dev_cfg, bridge_for=self._agent_bridge,
                                            save=self._dev_save_quiet, props_meta=(BOOL_KEYS, ENUM_KEYS),
                                            servers=self._agent_servers)
            return self.agent

    def _agent_bridge(self, sid):
        if not self.find(sid):
            return None
        return AgentBridge(self, sid)

    def _agent_servers(self):
        """Every server, as the assistant sees them."""
        out = []
        for s in self.servers():
            lc = s.get("local") or {}
            try:
                running = self.status_for(s["id"])["running"]
            except KeyError:
                running = False
            out.append({"id": s["id"], "name": s.get("name") or "server", "kind": s.get("kind"),
                        "software": lc.get("server_type", "") if s.get("kind") == "local" else s.get("kind"),
                        "mc": lc.get("mc_version", ""), "running": running,
                        "folder": lc.get("cwd", "") if s.get("kind") == "local" else ""})
        return out

    def _dev_save_quiet(self, changes):
        self.dev_cfg().update(changes or {})
        self.save()

    def dev_workspaces(self):
        """Every local server with a folder, then the added project folders -
        and, when the assistant may reach the whole computer, the computer."""
        out = []
        if self.dev_cfg().get("access") == "computer":
            home = os.path.expanduser("~")
            out.append({"id": "pc", "name": "This computer", "root": home, "kind": "computer",
                        "server_id": "", "exists": os.path.isdir(home)})
        for s in self.servers():
            cwd = (s.get("local") or {}).get("cwd") or ""
            if s.get("kind") != "local" or not cwd:
                continue
            out.append({"id": "srv-" + s["id"], "name": s.get("name") or "server", "root": cwd,
                        "kind": "server", "server_id": s["id"], "exists": os.path.isdir(cwd),
                        "software": (s.get("local") or {}).get("server_type", ""),
                        "mc": (s.get("local") or {}).get("mc_version", "")})
        if HAVE_DEV:
            for w in self.dev_agent().project_workspaces():
                out.append(dict(w, exists=os.path.isdir(w["root"])))
        return out

    def dev_workspace(self, wsid):
        for w in self.dev_workspaces():
            if w["id"] == wsid:
                if not w["exists"]:
                    raise RuntimeError("The folder %s is not there any more." % w["root"])
                return w
        raise RuntimeError("No such workspace.")

    DEV_NUMBERS = {"num_ctx": (512, 1048576), "num_thread": (1, 512), "num_gpu": (0, 999),
                   "max_parallel": (1, 8), "max_steps": (2, 60), "command_timeout": (30, 7200)}

    def dev_settings_save(self, data):
        d = self.dev_cfg()
        for k in ("base_url", "model", "keep_alive"):
            if k in data:
                d[k] = str(data[k] or "").strip()
        if "base_url" in data and d["base_url"] and not re.match(r"^https?://", d["base_url"]):
            raise RuntimeError("The Ollama address must start with http:// or https://")
        for k, (lo, hi) in self.DEV_NUMBERS.items():
            if k in data:
                v = data[k]
                if v in ("", None) and k in ("num_thread", "num_gpu"):
                    d[k] = ""
                    continue
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    raise RuntimeError("%s must be a whole number." % k)
                if not lo <= v <= hi:
                    raise RuntimeError("%s must be between %d and %d." % (k, lo, hi))
                d[k] = v
        if "temperature" in data:
            try:
                d["temperature"] = max(0.0, min(2.0, float(data["temperature"])))
            except (TypeError, ValueError):
                raise RuntimeError("Temperature must be a number.")
        for k in ("think", "auto_approve_edits", "index_enabled", "perf_mode", "shell_enabled", "web_enabled"):
            if k in data:
                d[k] = bool(data[k])
        if "auto_approve_edits" in data and "approval_mode" not in data:
            d["approval_mode"] = "edits" if d["auto_approve_edits"] else "ask"
        for k, allowed in (("access", ("workspace", "computer")), ("approval_mode", ("ask", "edits", "all")),
                           ("web_provider", ("none", "ollama", "searxng"))):
            if k in data:
                v = str(data[k] or "").strip()
                if v not in allowed:
                    raise RuntimeError("%s must be one of: %s." % (k, ", ".join(allowed)))
                d[k] = v
        d["auto_approve_edits"] = d.get("approval_mode") in ("edits", "all")
        if "searxng_url" in data:
            u = str(data["searxng_url"] or "").strip().rstrip("/")
            if u and not re.match(r"^https?://\S+$", u):
                raise RuntimeError("The SearXNG address must start with http:// or https://")
            d["searxng_url"] = u
        # a key is only replaced by a new one, or cleared on purpose - the
        # settings page never gets it back, so an empty box means "unchanged"
        if str(data.get("ollama_api_key") or "").strip():
            d["ollama_api_key"] = str(data["ollama_api_key"]).strip()
        if data.get("ollama_api_key_clear"):
            d["ollama_api_key"] = ""
        for k in ("allowed_commands", "trusted_commands"):
            if k in data:
                items = data[k] if isinstance(data[k], list) else str(data[k] or "").splitlines()
                d[k] = [str(x).strip() for x in items if str(x).strip()][:100]
        if isinstance(data.get("tool_mode"), dict):
            d["tool_mode"] = {str(m): v for m, v in data["tool_mode"].items() if v in ("auto", "native", "json")}
        self.save()
        return self.dev_state()

    def dev_state(self):
        d = dict(self.dev_cfg())
        for k in DEV_SECRETS:
            d["has_" + k] = bool(d.pop(k, ""))
        out = {"available": HAVE_DEV, "error": DEV_ERROR, "settings": d, "perf_mode": bool(d.get("perf_mode")),
               "workspaces": self.dev_workspaces(), "active": [], "defaults": DEV_DEFAULTS}
        if HAVE_DEV and self.agent is not None:
            out["active"] = self.agent.active()
        return out

    def dev_ollama(self, fresh=False):
        """Is Ollama there, what does it have, what is loaded. Cached for a
        few seconds because the UI asks often."""
        if not HAVE_DEV:
            return {"running": False, "error": DEV_ERROR}
        if not fresh and self._ollama_seen["state"] and time.time() - self._ollama_seen["at"] < 4:
            return self._ollama_seen["state"]
        base = self.dev_cfg().get("base_url") or ollamamod.DEFAULT_BASE
        st = {"base": base, "running": False, "version": "", "models": [], "loaded": [], "error": ""}
        try:
            ag = self.dev_agent()
            c = ag.client()
            st["version"] = c.version()
            st["running"] = True
            st["models"] = ag.models()
            st["loaded"] = c.ps()
        except Exception as e:
            st["error"] = str(e)
        self._ollama_seen = {"at": time.time(), "state": st}
        return st

    def dev_ollama_cached(self):
        """The last Ollama status, refreshed in the background when stale -
        so a page that polls never waits on an Ollama that does not answer."""
        seen = self._ollama_seen
        if seen["state"] is None or time.time() - seen["at"] > 6:
            if not getattr(self, "_ollama_refreshing", False):
                self._ollama_refreshing = True

                def refresh():
                    try:
                        self.dev_ollama(fresh=True)
                    finally:
                        self._ollama_refreshing = False
                threading.Thread(target=refresh, daemon=True).start()
        return seen["state"] or {"running": None, "error": "checking…", "loaded": [], "version": "", "base": ""}

    def dev_model_action(self, action, model):
        if not HAVE_DEV:
            raise RuntimeError(DEV_ERROR)
        model = (model or self.dev_cfg().get("model") or "").strip()
        if not model:
            raise RuntimeError("Choose a model first.")
        ag = self.dev_agent()
        if action == "unload":
            ag.client().unload(model)
            msg = "Unloaded %s." % model
        elif action == "preload":
            ag.client().preload(model, ag.options(model))
            msg = "%s is loaded with the assistant's settings." % model
        else:
            raise RuntimeError("Unknown action.")
        self._ollama_seen["at"] = 0
        return {"msg": msg}

    def dev_recommend(self, model):
        """A context size for this model on this machine, with the numbers behind it."""
        if not HAVE_DEV:
            raise RuntimeError(DEV_ERROR)
        desc = self.dev_agent().model_info(model)
        host = perfmod.host() if HAVE_PERF else {}
        free = host.get("memory_available")
        g = perfmod.gpus() if HAVE_PERF else {"available": False, "items": []}
        if g.get("available"):
            vram_free = max((x.get("memory_total") or 0) - (x.get("memory_used") or 0) for x in g["items"])
            free = vram_free or free
        rec = ollamamod.recommend_ctx(desc, free)
        rows = []
        for n in (4096, 8192, 16384, 32768, 65536):
            if desc.get("context_length") and n > desc["context_length"]:
                break
            kv = ollamamod.kv_cache_bytes(desc, n)
            rows.append({"num_ctx": n, "kv_bytes": kv, "kv": ollamamod.fmt_bytes(kv) if kv else "unknown"})
        return {"model": model, "recommended": rec, "context_length": desc.get("context_length"),
                "free_bytes": free, "on_gpu": bool(g.get("available")), "table": rows,
                "note": "KV cache at f16 for one request. Ollama multiplies it by OLLAMA_NUM_PARALLEL."}

    # -- files of a workspace, for the code editor --------------------------
    def _ws_box(self, wsid):
        return agentmod.Sandbox(self.dev_workspace(wsid)["root"])

    def dev_list(self, wsid, rel):
        box = self._ws_box(wsid)
        d = box.resolve(rel or ".", must_exist=True)
        if not d.is_dir():
            raise RuntimeError("Not a folder.")
        items = []
        for e in sorted(os.scandir(d), key=lambda e: (not e.is_dir(), e.name.lower())):
            if e.name in (".git", ".lodestone", "__pycache__"):
                continue
            try:
                st = e.stat()
            except OSError:
                continue
            items.append({"name": e.name, "path": box.rel(e.path), "dir": e.is_dir(),
                          "size": 0 if e.is_dir() else st.st_size, "mtime": st.st_mtime})
        return {"path": box.rel(d), "items": items[:2000]}

    def dev_read(self, wsid, rel):
        box = self._ws_box(wsid)
        p = box.resolve(rel, must_exist=True)
        if p.is_dir() or not agentmod._is_text_file(p, 4_000_000):
            raise RuntimeError("That is not a text file the editor can open.")
        text = p.read_text(encoding="utf-8", errors="replace")
        return {"path": box.rel(p), "content": text, "mtime": p.stat().st_mtime,
                "format": devmod.format_for(p.name) or ("java" if p.suffix == ".java" else "")}

    def dev_save(self, wsid, rel, content, mtime=None):
        """Save from the editor: refused if the file changed underneath, backed
        up first so it can be put back, and checked after."""
        ws = self.dev_workspace(wsid)
        box = agentmod.Sandbox(ws["root"])
        p = box.resolve(rel)
        exists = p.exists()
        if exists and mtime is not None and abs(p.stat().st_mtime - float(mtime)) > 0.001:
            raise Conflict("The file changed on disk since it was opened.", p.stat().st_mtime)
        store = self.dev_agent().store(ws)
        chid = new_id()
        backup = ""
        if exists:
            bp = store.backup_path(chid)
            shutil.copy2(str(p), str(bp))
            backup = str(bp)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(content or "")
        store.record_change({"id": chid, "run": "", "path": box.rel(p), "created": not exists, "backup": backup,
                             "added": 0, "removed": 0, "t": time.time(), "by": "editor"})
        check = agentmod.check_text(box.rel(p), content or "", (BOOL_KEYS, ENUM_KEYS))
        return {"path": box.rel(p), "mtime": p.stat().st_mtime, "check": check, "change": chid}

    def dev_check(self, wsid=None, rel=None, name=None, text=None):
        if text is None:
            data = self.dev_read(wsid, rel)
            name, text = data["path"], data["content"]
        return agentmod.check_text(name or "file.txt", text, (BOOL_KEYS, ENUM_KEYS))

    def dev_skript(self, wsid):
        ws = self.dev_workspace(wsid)
        env = skmod.environment(ws["root"])
        sd = Path(ws["root"]) / "plugins" / "Skript" / "scripts"
        scripts = {}
        for sc in env["scripts"][:400]:
            try:
                scripts[sc["name"]] = (sd / sc["name"]).read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        rep = skmod.workspace_report(scripts, env)
        return {"environment": env, "cross": rep["cross"], "missing_addons": rep["missing_addons"],
                "commands": rep["commands"], "functions": sorted(rep["functions"]),
                "files": {k: {"problems": v["problems"], "info": v["info"]} for k, v in rep["files"].items()}}

    def dev_skript_reload(self, wsid, script):
        ws = self.dev_workspace(wsid)
        if not ws.get("server_id"):
            raise RuntimeError("Reloading needs a server - open the server's own workspace.")
        st = self.status_for(ws["server_id"])
        if not st["can_command"]:
            raise RuntimeError("The server is not running, so Skript cannot check the script.")
        name = (script or "").replace("\\", "/").split("plugins/Skript/scripts/")[-1]
        stem = name[:-3] if name.endswith(".sk") else name
        if not stem or ".." in stem.split("/"):
            raise RuntimeError("Name a script under plugins/Skript/scripts.")
        text, summary, data = agentmod.skript_reload(AgentBridge(self, ws["server_id"]), stem)
        return {"summary": summary, "text": text, **(data or {})}

    def dev_java(self, wsid):
        ws = self.dev_workspace(wsid)
        jp = devmod.java_project(ws["root"])
        lint = []
        idx = self.dev_agent().index(ws)
        if not idx.data.get("files"):
            idx.build(budget_s=10)
        for rel, f in list(idx.data.get("files", {}).items())[:4000]:
            if f.get("lang") != "Java" or len(lint) >= 200:
                continue
            try:
                text = (Path(ws["root"]) / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for pr in devmod.java_lint(rel, text)["problems"]:
                lint.append(dict(pr, file=rel))
        return {"project": jp, "lint": lint[:200]}

    def dev_build(self, wsid, task="build"):
        """Run the project's own Gradle/Maven as a job the UI can follow."""
        ws = self.dev_workspace(wsid)
        jp = devmod.java_project(ws["root"])
        if not jp["kind"]:
            raise RuntimeError("There is no build.gradle(.kts) or pom.xml in %s." % ws["root"])
        argv = jp["test_command"] if task == "test" else jp["build_command"]
        real = agentmod.resolve_argv(argv, ws["root"])
        job = self._job_new("build", "%s %s" % (ws["name"], task), [("run", " ".join(argv)), ("read", "Read the result")])

        def work():
            self._job_step(job, "run", "running")
            self._job_log(job, "$ " + " ".join(argv))
            runner = agentmod.ProcessRunner(real, ws["root"], lambda ln: self._job_log(job, ln),
                                            timeout=int(self.dev_cfg().get("command_timeout") or 900))
            job["_runner"] = runner
            try:
                code, tail = runner.run()
            except Exception as e:
                job.update(state="failed", error=str(e), finished=time.time())
                self._job_step(job, "run", "failed", str(e))
                return
            self._job_step(job, "read", "running")
            parsed = devmod.parse_build_output(tail, ws["root"])
            job["result"] = {"exit": code, "build": parsed, "killed": runner.killed}
            ok = code == 0 and not parsed["failed"]
            self._job_step(job, "read", "done" if ok else "failed", parsed["summary"])
            job.update(state="done" if ok else "failed", pct=100, finished=time.time(),
                       error="" if ok else (runner.killed or parsed["summary"] or "exit code %d" % code))

        threading.Thread(target=work, daemon=True).start()
        return {k: v for k, v in job.items() if k not in ("log", "_runner")}

    def dev_job_cancel(self, jid):
        job = self.jobs.get(jid)
        if not job or job["state"] != "running":
            raise RuntimeError("That job is not running.")
        r = job.get("_runner")
        if r is None:
            raise RuntimeError("That job cannot be stopped.")
        r.kill("stopped by you")
        return True

    def dev_trace(self, text, wsid=None):
        root, packages = None, None
        if wsid:
            ws = self.dev_workspace(wsid)
            root = ws["root"]
            packages = self.dev_agent().index(ws).packages()
        if not (text or "").strip() and wsid:
            ws = self.dev_workspace(wsid)
            if ws.get("server_id"):
                text = "\n".join(t for _s, t in self.recent(1500, sid=ws["server_id"]))
        return {"traces": devmod.parse_stacktraces(text or "", root, packages),
                "build": devmod.parse_build_output(text or "", root)}

    # -- performance -------------------------------------------------------
    def perf_state(self, sid=None):
        s = self.server(sid)
        key = s["id"]
        rt = self.runtime(key)
        lc = s.get("local") or {}
        running = self.is_running(key)
        out = {"supported": HAVE_PERF and perfmod.supported(), "perf_mode": self.perf_mode(),
               "host": perfmod.host(self._perf_sampler) if HAVE_PERF else {},
               "gpu": perfmod.gpus() if HAVE_PERF else {"available": False, "why": "not available", "items": []}}
        srv = {"id": key, "name": s.get("name") or "server", "kind": s.get("kind"), "running": running,
               "uptime": int(time.time() - rt.started_at) if rt.started_at else 0,
               "players": sorted(rt.players), "player_count": len(rt.players),
               "player_source": "join and leave lines in the console since it started",
               "software": lc.get("server_type", ""), "mc": lc.get("mc_version", ""),
               "allocated_mb": perfmod.ram_to_mb(lc.get("max_ram", "")) if HAVE_PERF else None,
               "process": None, "process_why": ""}
        if s.get("kind") != "local":
            srv["process_why"] = "A remote server's process is on another machine."
        elif not running:
            srv["process_why"] = "The server is not running."
        elif not out["supported"]:
            srv["process_why"] = "Process measurement is not available on this operating system."
        else:
            pids = perfmod.process_tree(rt.proc.pid)
            use = perfmod.process_usage(pids)
            if use:
                cores = (out["host"].get("cores") or 1)
                pct = self._perf_sampler.percent(key, use["cpu_s"], 1)
                srv["process"] = {"pids": len(pids), "rss": use["rss"],
                                  "cpu_core_percent": round(pct, 1) if pct is not None else None,
                                  "cpu_percent": round(pct / cores, 1) if pct is not None else None}
            else:
                srv["process_why"] = "The operating system did not report the process."
        cmds = perfmod.tps_commands(srv["software"], srv["mc"]) if HAVE_PERF else []
        srv["tps_commands"] = cmds
        srv["tps_why"] = "" if cmds else (
            "This server's software has no command that reports its tick rate%s." %
            (" (vanilla and Fabric gained /tick query in 1.20.3)" if srv["software"] in ("vanilla", "fabric", "quilt")
             else "" if srv["software"] else " - or Lodestone does not know which software it runs"))
        if rt.tps:
            srv["tps"] = dict(rt.tps, age=int(time.time() - rt.tps["at"]))
        out["server"] = srv
        ol_state = self.dev_ollama_cached() if HAVE_DEV else {"running": False}
        out["ollama"] = {k: ol_state.get(k) for k in ("running", "version", "loaded", "error", "base")}
        out["assistant"] = {"active": len(self.agent.active()) if self.agent else 0}
        return out

    def perf_measure(self, sid=None):
        s = self.server(sid)
        key = s["id"]
        lc = s.get("local") or {}
        if not HAVE_PERF:
            raise RuntimeError("Performance measurement could not be loaded.")
        cmds = perfmod.tps_commands(lc.get("server_type", ""), lc.get("mc_version", ""))
        if not cmds:
            raise RuntimeError("This server's software has no command that reports its tick rate.")
        if not self.status_for(key)["can_command"]:
            raise RuntimeError("The server is not running.")
        rt = self.runtime(key)
        with rt.log_lock:
            seq0 = rt.seq
        direct = []
        for c in cmds:
            ok, out = self.command(c, quiet=True, who="panel", sid=key)
            if not ok:
                raise RuntimeError(out or "The server did not take '%s'." % c)
            if out:
                direct += out.splitlines()
        res = {}
        end = time.time() + 5
        while time.time() < end:
            lines = direct + [t for _s, t in self.since(seq0, sid=key)]
            res = perfmod.parse_tps(lines)
            if res.get("tps") and (res.get("mspt") or "mspt" not in cmds):
                break
            time.sleep(0.25)
        if not res:
            raise RuntimeError("The server did not answer '%s' in time." % "', '".join(cmds))
        rt.tps = dict(res, at=time.time(), commands=cmds)
        return dict(rt.tps, age=0)

    # -- automod -----------------------------------------------------------
    def _automod_loop(self):
        while True:
            time.sleep(6)
            try:
                self._automod_tick()
            except Exception:
                pass

    def _automod_tick(self):
        a = self.cfg.get("ai", {})
        if not (a.get("automod") and (a.get("api_key") or a.get("provider") in self.LOCAL_PROVIDERS)):
            self._automod_q.clear()
            return
        if not self._automod_q or (time.time() - self._automod_last) < 8:
            return
        if self.perf_mode():
            return              # Development Performance Mode: the model is busy coding; chat waits in the queue
        batch = list(self._automod_q)
        self._automod_q.clear()
        self._automod_last = time.time()
        chat = "\n".join("%s: %s" % (p, t) for p, t in batch[-25:])
        system = ('You are an automatic moderator for a Minecraft server. Given the rules and recent '
                  'chat, return STRICT JSON only: {"violations":[{"player":str,"reason":str,'
                  '"action":"warn|kick|mute|none"}]}. Return an empty list if nothing breaks the rules. '
                  'Be conservative — only flag clear violations.')
        rules = a.get("rules") or "Be respectful. No spam, slurs, or advertising other servers."
        try:
            raw, _u = self._ai_chat(system, [{"role": "user",
                                              "content": "Rules:\n%s\n\nRecent chat:\n%s" % (rules, chat)}],
                                    force_json=True)
        except Exception as e:
            self.emit(">>> automod error: %s" % e)
            return
        hits = [v for v in (self._extract_json(raw) or {}).get("violations", [])
                if v.get("action", "none") != "none"]
        for v in hits:
            player, reason, action = v.get("player", ""), v.get("reason", ""), v.get("action")
            self.emit(">>> automod flag: %s — %s -> %s" % (player, reason, action))
            self.discord.event("player_act", "Automod flagged %s" % player, reason, player,
                               [("Suggested", action, True),
                                ("Applied", "yes" if a.get("automod_auto") else "no (review only)", True)])
            if not a.get("automod_auto"):
                continue
            if action == "kick":
                self.player_action(player, "kick", reason[:80] or "Automod")
            elif action == "mute":
                self.command("mute %s" % player, quiet=True, who="automod")
            elif action == "warn":
                self.command("tell %s %s" % (player, ("Warning: " + reason)[:100]),
                             quiet=True, who="automod")

# ---------------------------------------------------------------------------
#  UI
# ---------------------------------------------------------------------------

PANEL = Panel()

COMMON_CSS = r"""
@font-face{font-family:'Monocraft';font-display:swap;
  src:url('https://cdn.jsdelivr.net/gh/IdreesInc/Monocraft@main/dist/Monocraft.ttf') format('truetype')}
:root{
  --ink:#0b0b10; --panel:#14141b; --panel2:#1b1b24; --raise:#23232e;
  --line:#282833; --line2:#343442; --text:#e8e8ec; --dim:#a2a2ad; --faint:#676775;
  --gold:#22c55e; --gold-hi:#4ade80; --gold-soft:#123524;
  --green:#4ade80; --green-soft:#123524; --red:#ff5b5b; --red-soft:#3a1a1a;
  --sky:#4cd7e0; --warn:#fcb83c; --mag:#b388ff; --blurple:#5865f2;
  --f-display:'Monocraft','JetBrains Mono',ui-monospace,monospace;
  --f-ui:'Inter',ui-sans-serif,system-ui,-apple-system,'Segoe UI',sans-serif;
  --f-mono:'JetBrains Mono',ui-monospace,'SFMono-Regular',Consolas,monospace;
  --mc-chunk:4px;
  --fs-mono:13px;      /* editor, console, code blocks - the reading surfaces */
  --fs-ui:14px;        /* everything else, scaled from this */
  --lh-mono:1.6;
  --bevel:inset 0 2px 0 rgba(255,255,255,.09),inset 0 -3px 0 rgba(0,0,0,.34);
  --bevel-in:inset 0 2px 0 rgba(0,0,0,.4),inset 0 -2px 0 rgba(255,255,255,.05);
  --notch:polygon(var(--mc-chunk) 0,calc(100% - var(--mc-chunk)) 0,calc(100% - var(--mc-chunk)) var(--mc-chunk),100% var(--mc-chunk),100% calc(100% - var(--mc-chunk)),calc(100% - var(--mc-chunk)) calc(100% - var(--mc-chunk)),calc(100% - var(--mc-chunk)) 100%,var(--mc-chunk) 100%,var(--mc-chunk) calc(100% - var(--mc-chunk)),0 calc(100% - var(--mc-chunk)),0 var(--mc-chunk),var(--mc-chunk) var(--mc-chunk));
}
*{box-sizing:border-box}
html,body{height:100%}
body{margin:0;background:var(--ink);color:var(--text);font-family:var(--f-ui);
  font-size:var(--fs-ui);overflow:hidden}
button{font-family:inherit;cursor:pointer;border:none;background:none;color:inherit}
input,select,textarea{font-family:inherit}
a{color:var(--green);text-decoration:none}
::selection{background:var(--green);color:#04140a}
h1,h2,h3{margin:0;font-family:var(--f-display);font-weight:400}

.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;padding:9px 15px;
  font-size:12px;font-weight:700;letter-spacing:.3px;transition:.1s;white-space:nowrap;
  border:2px solid rgba(0,0,0,.5);color:var(--text);background:var(--panel2);
  box-shadow:var(--bevel);clip-path:var(--notch)}
.btn:hover{background:var(--raise)}
.btn:active{transform:translate(2px,2px);box-shadow:var(--bevel-in)}
.btn:disabled{opacity:.35;cursor:not-allowed;transform:none;box-shadow:var(--bevel)}
.btn svg{width:14px;height:14px;flex:none}
.btn.gold{background:var(--gold);border-color:#0f3d20;color:#04140a}
.btn.gold:hover{background:var(--gold-hi)}
.btn.danger{background:#241416;color:#ff8b8b;border-color:#5a2626}
.btn.danger:hover{background:var(--red-soft);color:var(--red)}
.btn.blurple{background:var(--blurple);border-color:#2b33a8;color:#fff}
.btn.sm{padding:6px 11px;font-size:11px}
.btn.xs{padding:5px 9px;font-size:calc(var(--fs-ui) - 3px)}
.inp{background:#0e0e15;border:2px solid var(--line2);color:var(--text);padding:9px 12px;
  font-family:var(--f-mono);font-size:calc(var(--fs-ui) - 1px);outline:none;width:100%;transition:.1s}
.inp:focus{border-color:var(--green)}
.inp::placeholder{color:var(--faint)}
select.inp{cursor:pointer}
textarea.inp{resize:vertical;line-height:1.5}
label.fld{display:block;margin-bottom:12px}
label.fld > span{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.6px;
  color:var(--dim);margin-bottom:5px;font-weight:700}
.hint{font-size:calc(var(--fs-ui) - 2px);color:var(--dim);margin-top:5px;line-height:1.55}
.row{display:flex;gap:10px;align-items:center}
.row.wrap{flex-wrap:wrap}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:0 14px}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:0 14px}
.spacer{flex:1}

/* toggles */
.tog{display:inline-flex;align-items:center;gap:9px;cursor:pointer;user-select:none}
.tog input{display:none}
.tog .box{width:38px;height:20px;background:#0e0e15;border:2px solid var(--line2);
  position:relative;transition:.12s;flex:none}
.tog .box::after{content:'';position:absolute;left:2px;top:2px;width:12px;height:12px;
  background:var(--faint);transition:.12s}
.tog input:checked + .box{border-color:var(--green);background:var(--green-soft)}
.tog input:checked + .box::after{left:20px;background:var(--green)}
.tog .lbl{font-size:13px}

/* layout */
.top{height:58px;display:flex;align-items:center;gap:14px;padding:0 16px;
  background:var(--panel);border-bottom:2px solid var(--line);flex:none;position:relative;z-index:5}
.brand{display:flex;align-items:center;gap:10px;font-family:var(--f-display);font-size:17px}
.brand .mark{width:26px;height:26px;flex:none}
.brand .ver{font-size:10px;color:var(--faint);font-family:var(--f-mono)}
.srvpick{display:flex;align-items:center;gap:8px;padding-left:6px;border-left:2px solid var(--line);margin-left:4px}
.srvpick select{min-width:190px}
.pill{font-family:var(--f-mono);font-size:11px;padding:4px 9px;border:2px solid var(--line2);
  background:var(--panel2);color:var(--dim);white-space:nowrap;clip-path:var(--notch)}
.pill.on{color:var(--green);border-color:#1e5136;background:var(--green-soft)}
.pill.bad{color:var(--red);border-color:#5a2626;background:var(--red-soft)}
.pill.warn{color:var(--warn);border-color:#5a4620;background:#2a2010}
.controls{display:flex;gap:8px;margin-left:auto}

.body{display:flex;height:calc(100% - 58px)}
.rail{width:172px;flex:none;background:var(--panel);border-right:2px solid var(--line);
  display:flex;flex-direction:column;padding:10px 0;overflow-y:auto}
.nav{display:flex;align-items:center;gap:10px;padding:10px 16px;cursor:pointer;
  color:var(--dim);font-size:13px;border-left:3px solid transparent;transition:.1s}
.nav:hover{color:var(--text);background:var(--panel2)}
.nav.active{color:var(--green);border-left-color:var(--green);background:var(--green-soft)}
.nav svg{width:16px;height:16px;flex:none}
.nav .badge{margin-left:auto;font-family:var(--f-mono);font-size:10px;background:var(--raise);
  color:var(--dim);padding:1px 5px}
.railgrp{padding:14px 16px 5px;font-size:10px;letter-spacing:.12em;text-transform:uppercase;color:var(--faint);
  font-weight:700}
.sicon{width:40px;height:40px;flex:none;border-radius:8px;image-rendering:pixelated;background:var(--panel);
  object-fit:cover}
.sicon.smooth{image-rendering:auto}
.sicon.off{opacity:.5}
.statrow{display:flex;gap:9px;flex-wrap:wrap;flex:none}
.stat{background:var(--panel2);border:2px solid var(--line2);padding:9px 14px;min-width:120px}
.stat b{display:block;font-size:18px}
.stat span{font-size:11px;color:var(--dim)}
.grphead{font-size:10.5px;letter-spacing:.1em;text-transform:uppercase;color:var(--dim);font-weight:700;
  margin:10px 2px 2px}
.shots{display:flex;gap:8px;margin:8px 0}
.shots img{width:32%;aspect-ratio:16/9;object-fit:cover;border:2px solid var(--line2);cursor:pointer}
.ev .art{width:34px;height:34px;image-rendering:pixelated;flex:none}
.nav.hasupd::after{content:'update';margin-left:auto;font-size:9.5px;padding:2px 6px;background:var(--green-soft);
  color:var(--green);border:1px solid var(--green)}
.railfoot{margin-top:auto;padding:12px 16px;font-size:10.5px;color:var(--faint);
  font-family:var(--f-mono);line-height:1.6;border-top:2px solid var(--line)}
.railfoot b{color:var(--dim);font-weight:400}

.main{flex:1;overflow:hidden;display:flex;flex-direction:column;background:var(--ink)}
.viewwrap{flex:1;overflow:hidden;position:relative}
.view{position:absolute;inset:0;padding:16px;overflow-y:auto;display:none;flex-direction:column;gap:14px}
.view.show{display:flex}

.card{background:var(--panel);border:2px solid var(--line);display:flex;flex-direction:column;
  min-height:0;box-shadow:var(--bevel)}
.card-h{display:flex;align-items:center;gap:10px;padding:11px 14px;border-bottom:2px solid var(--line);flex:none}
.card-h h2{font-size:14px;letter-spacing:.4px}
.card-b{padding:14px;overflow-y:auto}
.card .path{font-family:var(--f-mono);font-size:11px;color:var(--faint)}

/* console */
.console-wrap{display:flex;gap:14px;flex:1;min-height:0}
.console-main{flex:1;display:flex;flex-direction:column;gap:10px;min-width:0}
.card.console{flex:1;min-height:0}
.log{flex:1;overflow-y:auto;padding:10px 13px;font-family:var(--f-mono);font-size:var(--fs-mono);
  line-height:var(--lh-mono);white-space:pre-wrap;word-break:break-word;background:#08080d}
.log .l{display:block}
.log .l.err{color:#ff8b8b} .log .l.warn{color:var(--warn)} .log .l.info{color:var(--sky)}
.log .l.chat{color:#cfcfe4} .log .l.me{color:var(--mag)} .log .l.sys{color:var(--green)}
.log .emptylog{color:var(--faint)}
.cmdbar{display:flex;gap:8px;flex:none}
.cmdbar .inp{flex:1}
.side-panel{width:270px;flex:none;overflow-y:auto}
.klist{padding:6px 0}
.krow{display:flex;justify-content:space-between;gap:10px;padding:6px 14px;font-size:12px}
.krow .k{color:var(--faint)} .krow .v{font-family:var(--f-mono);color:var(--dim);text-align:right;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:150px}
.plist{padding:8px 10px;display:flex;flex-direction:column;gap:5px;border-top:2px solid var(--line)}
.prow{display:flex;align-items:center;gap:8px;padding:5px 6px;background:var(--panel2);font-size:12.5px}
.prow img{width:20px;height:20px;image-rendering:pixelated;flex:none}
.prow .nm{flex:1;font-family:var(--f-mono);overflow:hidden;text-overflow:ellipsis}
.prow .acts{display:flex;gap:3px;opacity:0;transition:.1s}
.prow:hover .acts{opacity:1}
.chips{display:flex;gap:7px;flex-wrap:wrap;flex:none}
.chip{font-size:11px;padding:5px 10px;background:var(--panel2);border:2px solid var(--line2);
  cursor:pointer;font-family:var(--f-mono);clip-path:var(--notch)}
.chip:hover{border-color:var(--green);color:var(--green)}
.stream-ind{display:flex;align-items:center;gap:6px;font-size:10.5px;color:var(--faint);font-family:var(--f-mono)}
.stream-ind .d{width:7px;height:7px;background:var(--faint)}
.stream-ind.live .d{background:var(--green)}
.confilter{width:160px;padding:5px 9px;font-size:calc(var(--fs-ui) - 2px)}

/* files */
.fm{display:flex;gap:14px;flex:1;min-height:0}
.fm-list{flex:1;min-width:0;display:flex;flex-direction:column}
.crumbs{display:flex;align-items:center;gap:4px;flex-wrap:wrap;font-family:var(--f-mono);
  font-size:calc(var(--fs-ui) - 1px)}
.crumbs a{cursor:pointer;color:var(--dim)} .crumbs a:hover{color:var(--green)}
.crumbs .sep{color:var(--faint)}
table.ft{width:100%;border-collapse:collapse;font-size:calc(var(--fs-ui) - 1px)}
table.ft th{text-align:left;font-size:calc(var(--fs-ui) - 3px);text-transform:uppercase;letter-spacing:.6px;
  color:var(--faint);padding:8px 10px;border-bottom:2px solid var(--line);position:sticky;top:0;
  background:var(--panel);font-weight:700}
table.ft td{padding:7px 10px;border-bottom:1px solid var(--line);vertical-align:middle}
table.ft tr:hover td{background:var(--panel2)}
table.ft .nm{display:flex;align-items:center;gap:9px;cursor:pointer}
table.ft .nm svg{width:15px;height:15px;flex:none;color:var(--faint)}
table.ft .nm.dir svg{color:var(--gold)}
table.ft .sz,table.ft .md{font-family:var(--f-mono);font-size:calc(var(--fs-ui) - 2px);
  color:var(--dim);white-space:nowrap}
table.ft .ops{text-align:right;white-space:nowrap;opacity:0;transition:.1s}
table.ft tr:hover .ops{opacity:1}
.editor{width:clamp(420px,46vw,900px);flex:none;display:flex;flex-direction:column;min-width:0}
.fm.edmax .fm-list{display:none}
.fm.edmax .editor{width:auto;flex:1}
.editor textarea{flex:1;background:#08080d;border:none;color:var(--text);padding:12px;
  font-family:var(--f-mono);font-size:var(--fs-mono);line-height:var(--lh-mono);
  outline:none;resize:none;tab-size:4;white-space:pre;overflow-wrap:normal;overflow:auto}
.editor textarea.wrap{white-space:pre-wrap;overflow-wrap:break-word}
.drop{position:absolute;inset:0;background:rgba(34,197,94,.12);border:3px dashed var(--green);
  display:none;align-items:center;justify-content:center;font-family:var(--f-display);
  font-size:18px;color:var(--green);z-index:20;pointer-events:none}
.drop.on{display:flex}

/* generic lists */
.items{display:flex;flex-direction:column;gap:9px}
.item{background:var(--panel2);border:2px solid var(--line2);padding:12px 14px;
  display:flex;align-items:center;gap:12px}
.item .ttl{font-family:var(--f-mono);font-size:13px}
.item .sub{font-size:11.5px;color:var(--faint);margin-top:3px}
.item.active{border-color:var(--green);background:var(--green-soft)}
.empty{color:var(--faint);font-size:13px;padding:22px;text-align:center;line-height:1.7}

/* props */
.props{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px}
.prop{background:var(--panel2);border:2px solid var(--line2);padding:10px 12px}
.prop .pk{font-family:var(--f-mono);font-size:calc(var(--fs-ui) - 2px);color:var(--text);
  margin-bottom:7px;overflow-wrap:anywhere}
.prop.changed{border-color:var(--warn)}

/* discord */
.hook{background:var(--panel2);border:2px solid var(--line2);padding:0;overflow:hidden}
.hook-h{display:flex;align-items:center;gap:10px;padding:11px 13px;cursor:pointer}
.hook-h .nm{font-family:var(--f-mono);font-size:13px;flex:1}
.hook-b{padding:0 13px 13px;border-top:2px solid var(--line);display:none}
.hook.open .hook-b{display:block}
.evgrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:7px;margin-top:6px}
.ev{display:flex;gap:8px;align-items:flex-start;padding:8px 10px;background:var(--panel);
  border:2px solid var(--line);cursor:pointer;transition:.1s}
.ev:hover{border-color:var(--line2)}
.ev.on{border-color:var(--green);background:var(--green-soft)}
.ev .evn{font-size:12px;font-weight:600}
.ev .evd{font-size:calc(var(--fs-ui) - 3px);color:var(--dim);margin-top:2px;line-height:1.45}
.ev .tick{width:14px;height:14px;border:2px solid var(--line2);flex:none;margin-top:1px;position:relative}
.ev.on .tick{border-color:var(--green);background:var(--green)}
.ev.on .tick::after{content:'';position:absolute;left:3px;top:0;width:4px;height:8px;
  border:solid #04140a;border-width:0 2px 2px 0;transform:rotate(45deg)}

/* api */
.code{background:#08080d;border:2px solid var(--line);padding:11px 13px;font-family:var(--f-mono);
  font-size:calc(var(--fs-mono) - 0.5px);line-height:1.7;overflow-x:auto;white-space:pre;color:var(--dim)}
.code .m{color:var(--green)} .code .u{color:var(--sky)} .code .c{color:var(--faint)}
.ep{display:flex;gap:10px;align-items:baseline;padding:7px 0;border-bottom:1px solid var(--line);font-size:12.5px}
.ep .verb{font-family:var(--f-mono);font-size:10.5px;font-weight:700;padding:2px 7px;flex:none;min-width:52px;text-align:center}
.ep .verb.get{background:var(--green-soft);color:var(--green)}
.ep .verb.post{background:#1a2340;color:var(--sky)}
.ep .verb.del{background:var(--red-soft);color:var(--red)}
.ep .u{font-family:var(--f-mono);color:var(--text)}
.ep .d{color:var(--faint);font-size:11.5px;margin-left:auto;text-align:right}
.tokrow{display:flex;align-items:center;gap:12px;padding:10px 12px;background:var(--panel2);
  border:2px solid var(--line2)}
.secret{background:var(--green-soft);border:2px solid var(--green);padding:13px;margin-bottom:12px}
.secret .t{font-size:12px;color:var(--green);margin-bottom:7px;font-weight:700}
.secret code{font-family:var(--f-mono);font-size:var(--fs-mono);word-break:break-all;display:block;
  background:#08080d;padding:9px;color:var(--text)}

/* modal + toasts */
.modal{position:fixed;inset:0;background:rgba(0,0,0,.72);display:none;align-items:center;
  justify-content:center;z-index:50;padding:20px}
.modal.on{display:flex}
.modal .box{background:var(--panel);border:2px solid var(--line2);max-width:560px;width:100%;
  max-height:86vh;overflow-y:auto;box-shadow:0 10px 0 rgba(0,0,0,.4)}
.modal .box h3{padding:14px;border-bottom:2px solid var(--line);font-size:15px}
.modal .box .mb{padding:14px}
.modal .box .mf{padding:12px 14px;border-top:2px solid var(--line);display:flex;gap:9px;justify-content:flex-end}
#toasts{position:fixed;bottom:20px;right:20px;display:flex;flex-direction:column;gap:9px;z-index:60}
.toast{background:var(--panel2);border:2px solid var(--line2);border-left:4px solid var(--green);
  padding:11px 15px;font-size:13px;filter:drop-shadow(3px 3px 0 rgba(0,0,0,.5));
  animation:slidein .18s ease;max-width:380px;clip-path:var(--notch)}
.toast.bad{border-left-color:var(--red)} .toast.good{border-left-color:var(--green)}
@keyframes slidein{from{transform:translateX(20px);opacity:0}to{transform:none;opacity:1}}
*::-webkit-scrollbar{width:12px;height:12px}
*::-webkit-scrollbar-thumb{background:var(--line2);border:3px solid transparent;background-clip:padding-box}
*::-webkit-scrollbar-thumb:hover{background:var(--faint);background-clip:padding-box}
*::-webkit-scrollbar-track{background:transparent}
/* assistant */
.chatwrap{display:flex;gap:14px;flex:1;min-height:0}
.chat{flex:1;display:flex;flex-direction:column;min-width:0}
.msgs{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:11px;background:#08080d}
.msg{max-width:82%;padding:10px 13px;font-size:calc(var(--fs-ui) - 1px);line-height:1.65;white-space:pre-wrap;
  word-break:break-word;border:2px solid var(--line2);background:var(--panel2)}
.msg.me{align-self:flex-end;border-color:#1e5136;background:var(--green-soft)}
.msg.bot{align-self:flex-start}
.msg.step{align-self:flex-start;background:transparent;border:none;border-left:3px solid var(--line2);
  padding:3px 11px;font-family:var(--f-mono);font-size:calc(var(--fs-ui) - 2px);color:var(--dim);max-width:100%}
.msg.err{align-self:flex-start;border-color:#5a2626;background:var(--red-soft);color:#ff8b8b}
.msg .who{font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:var(--faint);
  margin-bottom:5px;font-weight:700}
.aiside{width:320px;flex:none;overflow-y:auto}
.atts{display:flex;gap:6px;flex-wrap:wrap}
.att{font-size:11px;font-family:var(--f-mono);background:var(--panel2);border:2px solid var(--line2);
  padding:3px 8px;display:flex;gap:6px;align-items:center}
.att b{cursor:pointer;color:var(--red)}
/* motd preview - a Minecraft server-list row */
.motdbox{background:#101018;border:2px solid var(--line2);padding:11px 13px;display:flex;gap:11px;align-items:flex-start}
.motdbox .ico{width:64px;height:64px;flex:none;image-rendering:pixelated;background:#08080d;
  border:2px solid var(--line);display:flex;align-items:center;justify-content:center;
  font-size:9px;color:var(--faint);text-align:center}
.motdline{font-family:var(--f-mono);font-size:calc(var(--fs-ui) - 1px);line-height:1.5;
  white-space:pre-wrap;word-break:break-word;flex:1}
.mc-k{animation:mcflicker .4s steps(2) infinite}
@keyframes mcflicker{50%{opacity:.35}}
.world{display:flex;align-items:center;gap:12px;padding:11px 13px;background:var(--panel2);
  border:2px solid var(--line2)}
.world.live{border-color:var(--green);background:var(--green-soft)}
.world .wn{font-family:var(--f-mono);font-size:calc(var(--fs-ui) - 1px);flex:1}
.mhrow{display:flex;gap:9px;flex-wrap:wrap;align-items:center}

/* the size control itself */
.sizer{display:flex;align-items:center;gap:1px}
.sizer button{width:24px;height:24px;display:flex;align-items:center;justify-content:center;
  border:2px solid var(--line2);background:var(--panel2);font-size:13px;font-weight:700;line-height:1}
.sizer button:hover{border-color:var(--green);color:var(--green)}
.sizer .val{font-family:var(--f-mono);font-size:11px;color:var(--faint);min-width:30px;text-align:center}
@media (max-width:1100px){.side-panel{display:none}.editor{width:clamp(300px,50vw,520px)}.aiside{width:270px}}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""

ICON_SVG = r"""<svg class="mark" viewBox="0 0 32 32" shape-rendering="crispEdges"><rect x="2" y="2" width="28" height="28" fill="#2f3140"/><rect x="4" y="4" width="24" height="24" fill="#4a4d63"/><rect x="6" y="6" width="20" height="20" fill="#23242f"/><rect x="4" y="4" width="24" height="2" fill="#6b6f8a"/><rect x="4" y="26" width="24" height="2" fill="#1b1c25"/><rect x="8" y="8" width="2" height="2" fill="#3a3c4d"/><rect x="22" y="8" width="2" height="2" fill="#3a3c4d"/><rect x="8" y="22" width="2" height="2" fill="#3a3c4d"/><rect x="22" y="22" width="2" height="2" fill="#3a3c4d"/><rect x="14" y="9" width="4" height="14" fill="#123524"/><rect x="9" y="14" width="14" height="4" fill="#123524"/><rect x="15" y="10" width="2" height="12" fill="#22c55e"/><rect x="10" y="15" width="12" height="2" fill="#22c55e"/><rect x="15" y="10" width="2" height="4" fill="#e8fff2"/><rect x="14" y="14" width="4" height="4" fill="#4ade80"/><rect x="15" y="15" width="2" height="2" fill="#eafff3"/></svg>"""

APP_HTML = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Lodestone</title>
<link rel="icon" href="/icon.svg" type="image/svg+xml">
<style>__CSS__</style></head><body>
<header class="top">
  <div class="brand">__ICON__ <span>Lodestone</span>
    <span class="ver" id="modeTag" onclick="chooseMode()" title="Switch between Simple and Advanced"
      style="cursor:pointer">__VER__</span></div>
  <div class="srvpick">
    <select class="inp" id="srvSel" onchange="switchServer(this.value)"></select>
    <span class="pill" id="statePill">…</span>
    <span class="pill bad" id="offPill" style="display:none;cursor:pointer" onclick="showView('network')"
      title="Offline / non-premium mode is on. Turn it off in the Network tab.">OFFLINE MODE: testing</span>
  </div>
  <div class="controls" id="controls"></div>
</header>
<div class="body">
  <nav class="rail">
    <div class="railgrp">Server</div>
    <a class="nav active" data-view="console" onclick="showView('console')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M7 9l3 3-3 3M13 15h4"/></svg><span>Console</span><span class="badge" id="bPlayers">0</span></a>
    <a class="nav" data-view="files" onclick="showView('files')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z"/></svg><span>Files</span></a>
    <a class="nav" data-view="props" onclick="showView('props')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M4 8h10M18 8h2M4 16h4M12 16h8"/><circle cx="16" cy="8" r="2"/><circle cx="9" cy="16" r="2"/></svg><span>Properties</span></a>
    <a class="nav" data-view="backups" onclick="showView('backups')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><ellipse cx="12" cy="6" rx="8" ry="3"/><path d="M4 6v6c0 1.7 3.6 3 8 3s8-1.3 8-3V6M4 12v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6"/></svg><span>Backups</span></a>
    <div class="railgrp">Manage</div>
    <a class="nav" data-view="content" onclick="showView('content')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M10 3h4v3a2 2 0 104 0V3h3v7h-3a2 2 0 100 4h3v7h-7v-3a2 2 0 10-4 0v3H3v-7h3a2 2 0 100-4H3V3h7z"/></svg><span>Mods &amp; plugins</span></a>
    <a class="nav" data-adv="1" data-view="tools" onclick="showView('tools')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg><span>Tools</span></a>
    <div class="railgrp" data-adv="1">Automate</div>
    <a class="nav" data-adv="1" data-view="auto" onclick="showView('auto')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></svg><span>Automation</span></a>
    <a class="nav" data-adv="1" data-view="ai" onclick="showView('ai')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 3l1.9 4.6L18.5 9l-4.6 1.9L12 15.5l-1.9-4.6L5.5 9l4.6-1.4z"/><path d="M18 15l.9 2.1 2.1.9-2.1.9L18 21l-.9-2.1-2.1-.9 2.1-.9z"/></svg><span>Assistant</span><span class="badge" id="bAi">off</span></a>
    <a class="nav" data-adv="1" data-view="discord" onclick="showView('discord')"><svg viewBox="0 0 24 24" fill="currentColor"><path d="M19.3 5.4A16.7 16.7 0 0015.1 4l-.2.4a12.6 12.6 0 013.7 1.9 11.8 11.8 0 00-4.3-1.1 12.4 12.4 0 00-4.5 0A11.8 11.8 0 005.4 6.3 12.6 12.6 0 019.1 4.4L8.9 4a16.7 16.7 0 00-4.2 1.4C2.3 9 1.7 12.5 2 15.9a16.8 16.8 0 005.1 2.6l.6-1a11 11 0 01-1.7-.8l.4-.3a11.9 11.9 0 0011.2 0l.4.3a11 11 0 01-1.7.8l.6 1a16.8 16.8 0 005.1-2.6c.4-4-.6-7.4-2.7-10.5zM9 14c-.8 0-1.5-.8-1.5-1.7S8.2 10.6 9 10.6s1.5.8 1.5 1.7S9.8 14 9 14zm6 0c-.8 0-1.5-.8-1.5-1.7s.7-1.7 1.5-1.7 1.5.8 1.5 1.7S15.8 14 15 14z"/></svg><span>Discord</span><span class="badge" id="bHooks">0</span></a>
    <div class="railgrp">Lodestone</div>
    <a class="nav" data-view="servers" onclick="showView('servers')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="3" y="4" width="18" height="7" rx="1"/><rect x="3" y="13" width="18" height="7" rx="1"/><path d="M7 7.5h.01M7 16.5h.01"/></svg><span>Servers</span></a>
    <a class="nav" data-view="network" onclick="showView('network')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.7 3.8 5.7 3.8 9s-1.3 6.3-3.8 9c-2.5-2.7-3.8-5.7-3.8-9S9.5 5.7 12 3z"/></svg><span>Network</span><span class="badge" id="bNet">lan</span></a>
    <a class="nav" data-adv="1" data-view="api" onclick="showView('api')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 15l6-6"/><path d="M11 6l1.5-1.5a4.2 4.2 0 016 6L17 12"/><path d="M13 18l-1.5 1.5a4.2 4.2 0 01-6-6L7 12"/></svg><span>API</span></a>
    <a class="nav" data-view="settings" onclick="showView('settings')"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="3"/><path d="M19 12a7 7 0 00-.1-1l2-1.6-2-3.4-2.4 1a7 7 0 00-1.7-1l-.4-2.5h-4l-.4 2.5a7 7 0 00-1.7 1l-2.4-1-2 3.4 2 1.6a7 7 0 000 2l-2 1.6 2 3.4 2.4-1a7 7 0 001.7 1l.4 2.5h4l.4-2.5a7 7 0 001.7-1l2.4 1 2-3.4-2-1.6a7 7 0 00.1-1z"/></svg><span>Settings</span></a>
    <div class="railfoot"><b id="fName">—</b><br><span id="fWhere">no server</span></div>
  </nav>
  <main class="main"><div class="viewwrap">

  <!-- ===================== CONSOLE ===================== -->
  <section class="view show" id="view-console">
    <div class="console-wrap">
      <div class="console-main">
        <div class="card console"><div class="card-h"><h2>Console</h2>
          <div class="path" id="conSrc">—</div><div class="spacer"></div>
          <input class="inp confilter" id="conFilter" placeholder="filter…" oninput="renderLog()" autocomplete="off">
          <div class="sizer" title="Text size">
            <button onclick="bumpMono(-1)">&minus;</button>
            <span class="val" id="monoVal2">13</span>
            <button onclick="bumpMono(1)">+</button></div>
          <div class="stream-ind" id="streamInd"><span class="d"></span>connecting</div>
          <button class="btn sm" onclick="clearLog()">Clear</button></div>
          <div class="log" id="log"><span class="emptylog">Waiting for the server to say something…</span></div></div>
        <div class="cmdbar">
          <input class="inp" id="cmdInput" placeholder="say hello   —   commands go over stdin, or RCON when the panel does not own the process" autocomplete="off" onkeydown="cmdKey(event)">
          <button class="btn gold" onclick="sendCmd()">Send</button>
        </div>
        <div class="chips" id="macroChips"></div>
      </div>
      <aside class="card side-panel"><div class="card-h"><h2>Session</h2><div class="spacer"></div>
        <span class="pill" id="sidePill">offline</span></div>
        <div class="klist">
          <div class="krow"><span class="k">Server</span><span class="v" id="kName">—</span></div>
          <div class="krow"><span class="k">Kind</span><span class="v" id="kKind">—</span></div>
          <div class="krow"><span class="k">Where</span><span class="v" id="kWhere">—</span></div>
          <div class="krow"><span class="k">Uptime</span><span class="v" id="kUp">—</span></div>
          <div class="krow"><span class="k">Started in</span><span class="v" id="kDone">—</span></div>
          <div class="krow"><span class="k">Players</span><span class="v" id="kPlayers">0</span></div>
          <div class="krow"><span class="k">Commands</span><span class="v" id="kCmds">0</span></div>
          <div class="krow"><span class="k">Webhooks</span><span class="v" id="kHooks">0</span></div>
          <div class="krow"><span class="k">Discord sent</span><span class="v" id="kSent">0</span></div>
          <div class="krow"><span class="k">Transferred</span><span class="v" id="kBytes">—</span></div>
          <div class="krow"><span class="k">Backup</span><span class="v" id="kBk">idle</span></div>
        </div>
        <div class="plist" id="players"></div></aside>
    </div>
  </section>

  <!-- ===================== FILES ===================== -->
  <section class="view" id="view-files">
    <div class="card" style="flex:none"><div class="card-h">
      <div class="crumbs" id="crumbs"></div><div class="spacer"></div>
      <button class="btn sm" onclick="mkNew('file')">New file</button>
      <button class="btn sm" onclick="mkNew('folder')">New folder</button>
      <button class="btn sm" onclick="document.getElementById('upl').click()">Upload</button>
      <button class="btn sm" onclick="loadFiles(CWD)">Refresh</button>
      <input type="file" id="upl" style="display:none" multiple onchange="doUpload(this.files)">
    </div></div>
    <div class="fm">
      <div class="card fm-list" style="position:relative">
        <div class="drop" id="dropZone">drop to upload here</div>
        <div style="overflow-y:auto;flex:1"><table class="ft"><thead><tr>
          <th>Name</th><th style="width:90px">Size</th><th style="width:130px">Modified</th><th style="width:190px"></th>
        </tr></thead><tbody id="ftBody"></tbody></table></div>
      </div>
      <div class="card editor" id="editor" style="display:none">
        <div class="card-h"><h2 id="edName">file</h2><div class="spacer"></div>
          <div class="sizer" title="Text size">
            <button onclick="bumpMono(-1)">&minus;</button>
            <span class="val" id="monoVal">13</span>
            <button onclick="bumpMono(1)">+</button></div>
          <button class="btn xs" id="wrapBtn" onclick="toggleWrap()">Wrap</button>
          <button class="btn xs" id="maxBtn" onclick="toggleEdMax()">Expand</button>
          <span class="pill" id="edState">saved</span>
          <button class="btn sm gold" onclick="saveEdit()">Save</button>
          <button class="btn sm" onclick="closeEdit()">Close</button></div>
        <textarea id="edText" spellcheck="false" oninput="markDirty()" onkeydown="edKey(event)"></textarea>
      </div>
    </div>
  </section>

  <!-- ===================== PROPERTIES ===================== -->
  <section class="view" id="view-props">
    <div class="card" style="flex:none"><div class="card-h"><h2>How the server looks</h2>
      <div class="path">MOTD, icon and resource pack</div><div class="spacer"></div>
      <button class="btn sm gold" onclick="saveAppearance()">Save</button></div>
      <div class="card-b">
        <div class="motdbox" style="margin-bottom:13px">
          <div class="ico" id="motdIcon">no icon</div>
          <div><div style="font-family:var(--f-mono);font-size:calc(var(--fs-ui) - 1px);color:var(--text)"
                 id="motdTitle">A Minecraft Server</div>
            <div class="motdline" id="motdPrev"></div></div></div>
        <label class="fld"><span>MOTD</span>
          <textarea class="inp" id="apMotd" rows="2" oninput="renderMotd()"
            placeholder="&amp;aMy server &amp;7| &amp;bnow with colours"></textarea>
          <div class="hint">Two lines max in the server list. <code>&amp;a</code> or <code>§a</code> for
            colours, <code>&amp;#ff8800</code> or <code>&lt;#ff8800&gt;</code> for hex, <code>\n</code>
            for the second line.</div></label>
        <div class="grid2">
          <label class="fld"><span>Server icon</span>
            <div class="row"><input type="file" id="iconFile" accept="image/png" style="display:none"
              onchange="uploadIcon(this.files[0])">
              <button class="btn sm" onclick="document.getElementById('iconFile').click()">Upload PNG</button>
              <button class="btn sm danger" onclick="clearIcon()">Remove</button></div>
            <div class="hint">Exactly 64&times;64, PNG, under 64 KB — Minecraft rejects anything else.</div></label>
          <label class="fld"><span>Max players</span>
            <input class="inp" id="apMax"></label></div>
        <div class="grid2">
          <label class="fld"><span>Resource pack URL</span><input class="inp" id="apRp"></label>
          <label class="fld"><span>Resource pack SHA-1</span><input class="inp" id="apRpSha"></label></div>
        <div class="grid2">
          <label class="fld"><span>Resource pack prompt</span><input class="inp" id="apRpPrompt"></label>
          <label class="fld"><span>Require it</span>
            <select class="inp" id="apRpReq"><option value="false">Optional</option>
            <option value="true">Required</option></select></label></div>
      </div></div>
    <div class="card"><div class="card-h"><h2>server.properties</h2>
      <div class="path">restart the server for most of these to take effect</div><div class="spacer"></div>
      <input class="inp confilter" id="propFilter" placeholder="filter…" oninput="renderProps()">
      <button class="btn sm" onclick="loadProps()">Reload</button>
      <button class="btn sm gold" onclick="saveProps()">Save changes</button></div>
      <div class="card-b"><div class="props" id="propGrid"></div></div></div>
  </section>

  <!-- ===================== BACKUPS ===================== -->
  <section class="view" id="view-backups">
    <div class="card" style="flex:none"><div class="card-h"><h2>Backups</h2>
      <div class="path" id="bkDir">—</div><div class="spacer"></div>
      <input class="inp" id="bkLabel" placeholder="label (optional)" style="width:180px">
      <select class="inp" id="bkWhat" style="width:150px">
        <option value="world">Worlds only</option><option value="all">Everything</option></select>
      <button class="btn sm gold" onclick="makeBackup()">Back up now</button></div>
      <div class="card-b" style="padding-top:10px"><div class="hint" id="bkMsg">
        Backups are zipped and kept on this machine, under your home folder — remote servers get
        pulled down over SFTP first. Automation can run these on a timer.</div></div></div>
    <div class="card"><div class="card-b"><div class="items" id="bkList"></div></div></div>
  </section>

  <!-- ===================== TOOLS ===================== -->
  <section class="view" id="view-content">
    <div class="card" style="flex:none"><div class="card-h"><h2 id="ctHead">Mods &amp; plugins</h2>
      <div class="path" id="ctAbout">—</div><div class="spacer"></div>
      <button class="btn sm" id="ctTabInst" onclick="ctTab('installed')">Installed</button>
      <button class="btn sm" id="ctTabBrowse" onclick="ctTab('browse')">Browse</button></div></div>
    <div class="card" id="ctNone" style="display:none"><div class="card-b"><div class="empty" id="ctNoneMsg"></div>
      <div class="row" style="justify-content:center"><button class="btn sm" onclick="showView('servers');wzOpen()">Create a modded or plugin server</button></div></div></div>
    <div class="statrow" id="ctStats"></div>
    <div class="card" id="ctInstCard"><div class="card-h"><h2>Installed</h2><div class="path" id="ctCount">—</div>
      <div class="spacer"></div>
      <button class="btn sm gold" id="ctUpdAll" style="display:none" onclick="ctUpdateAll()">Update all</button>
      <button class="btn sm" onclick="ctCheckUpdates()">Check for updates</button>
      <button class="btn sm" onclick="ctLoad()">Refresh</button></div>
      <div class="card-b"><div class="row"><input class="inp" id="ctFind" placeholder="Find by name, file or source" oninput="ctRender()">
        <select class="inp" id="ctSort" style="width:220px" onchange="ctSortSet(this.value)">
          <option value="name">Sort: name</option><option value="recent">Sort: recently installed</option>
          <option value="size">Sort: size</option><option value="source">Sort: source</option></select></div>
        <div class="chips" id="ctChips" style="margin:9px 0"></div>
        <div class="items" id="ctList"></div>
        <div class="hint" style="margin-top:10px">Disabling renames the jar to <code>.jar.disabled</code>, so
          nothing is lost. Removing deletes the jar but keeps its config folder. Changes take effect the next time
          the server starts.</div></div></div>
    <div class="card" id="ctBrowseCard" style="display:none"><div class="card-b" id="ctBrowse"></div></div>
  </section>

  <section class="view" id="view-tools">
    <div class="card" style="flex:none"><div class="card-h"><h2>Search the logs</h2>
      <div class="path">including the gzipped rotated ones</div><div class="spacer"></div>
      <label class="tog"><input type="checkbox" id="lsRegex"><span class="box"></span>
        <span class="lbl">regex</span></label>
      <label class="tog"><input type="checkbox" id="lsCase"><span class="box"></span>
        <span class="lbl">match case</span></label></div>
      <div class="card-b">
        <div class="row"><input class="inp" id="lsQ" placeholder="Exception, or a player name, or Can&#39;t keep up"
          onkeydown="if(event.key===&quot;Enter&quot;)doLogSearch()">
          <button class="btn gold" onclick="doLogSearch()">Search</button></div>
        <div class="hint" style="margin-top:8px">Remote servers pull each log down over SFTP, so this
          is capped at the twelve most recent files and 400 hits.</div>
        <div id="lsOut"></div></div></div>

    <div class="card"><div class="card-h"><h2>Free memory</h2>
      <div class="path" id="ramSummary">—</div><div class="spacer"></div>
      <button class="btn sm" onclick="loadRam()">Refresh</button></div>
      <div class="card-b"><div id="ramOut"></div>
        <div class="hint" style="margin-top:10px">Closing is a normal close request, the
          kind the application can refuse if it has unsaved work — not a kill. Windows
          components, Lodestone and every Java process are never offered, because a Java
          process here is either a Minecraft server or the game. Stop servers with their
          own Stop button so the world saves first.</div></div></div>

    <div class="card"><div class="card-h"><h2>Worlds</h2>
      <div class="path" id="wCount">—</div><div class="spacer"></div>
      <button class="btn sm" onclick="loadWorlds()">Refresh</button></div>
      <div class="card-b"><div class="items" id="wList"></div>
        <div class="hint" style="margin-top:10px">The live world is whatever <code>level-name</code>
          points at. Switching takes effect on the next restart. Resetting clears the chunks so the
          server regenerates them; the server has to be stopped first.</div></div></div>

    <div class="card"><div class="card-h"><h2>Plugins</h2>
      <div class="path" id="plCount">—</div><div class="spacer"></div>
      <button class="btn sm" onclick="loadPlugins()">Refresh</button></div>
      <div class="card-b"><div class="items" id="plList"></div></div></div>

    <div class="card"><div class="card-h"><h2>Ops, whitelist and bans</h2><div class="spacer"></div>
      <select class="inp" id="lsWhich" style="width:170px" onchange="loadList()">
        <option value="ops">Operators</option>
        <option value="whitelist">Whitelist</option>
        <option value="banned-players">Banned players</option>
        <option value="banned-ips">Banned IPs</option></select>
      <input class="inp" id="lsName" placeholder="name" style="width:160px">
      <button class="btn sm gold" onclick="listEdit(true)">Add</button>
      <button class="btn sm danger" onclick="listEdit(false)">Remove</button></div>
      <div class="card-b"><div class="items" id="listOut"></div>
        <div class="hint" style="margin-top:10px">These are read from the server JSON files, and
          changed by sending the real command — so the server has to be running, or reachable
          over RCON.</div></div></div>
  </section>

  <!-- ===================== AUTOMATION ===================== -->
  <section class="view" id="view-auto">
    <div class="card"><div class="card-h"><h2>Automation</h2><div class="spacer"></div>
      <span class="path">runs while the panel is open</span>
      <button class="btn sm gold" onclick="saveFeatures()">Save</button></div>
      <div class="card-b" id="autoBody"></div></div>
    <div class="card"><div class="card-h"><h2>Timed commands</h2><div class="spacer"></div>
      <button class="btn sm" onclick="addTask()">Add</button></div>
      <div class="card-b"><div class="items" id="taskList"></div></div></div>
    <div class="card"><div class="card-h"><h2>Macros</h2><div class="spacer"></div>
      <button class="btn sm" onclick="addMacro()">Add</button></div>
      <div class="card-b"><div class="items" id="macroList"></div></div></div>
  </section>

  <!-- ===================== ASSISTANT ===================== -->
  <section class="view" id="view-ai">
    <div class="chatwrap">
      <div class="card chat"><div class="card-h"><h2>Assistant</h2>
        <div class="path" id="aiModel">—</div><div class="spacer"></div>
        <span class="path" id="aiUsage"></span>
        <button class="btn sm" onclick="aiClear()">Clear chat</button></div>
        <div class="msgs" id="aiMsgs"><div class="empty">Ask it to change a setting, read the logs,
          write a Skript file, or diagnose a crash. It works the server through the same console and
          files this panel uses — so it drives remote servers over RCON and SFTP too.</div></div>
        <div style="padding:10px;border-top:2px solid var(--line);display:flex;flex-direction:column;gap:8px">
          <div class="atts" id="aiAtts"></div>
          <div class="cmdbar">
            <input class="inp" id="aiInput" placeholder="set the difficulty to hard and tell everyone why"
              autocomplete="off" onkeydown="if(event.key===&quot;Enter&quot;)aiSend()">
            <button class="btn sm" onclick="aiAttach()">Attach</button>
            <button class="btn gold" id="aiGo" onclick="aiSend()">Send</button>
          </div></div></div>
      <aside class="card aiside"><div class="card-h"><h2>Settings</h2><div class="spacer"></div>
        <button class="btn sm" onclick="aiTest()">Test</button>
        <button class="btn sm gold" onclick="aiSaveSettings()">Save</button></div>
        <div class="card-b" id="aiSettings"></div></aside>
    </div>
  </section>

  <!-- ===================== DISCORD ===================== -->
  <section class="view" id="view-discord">
    <div class="card" style="flex:none"><div class="card-h"><h2>Discord webhooks</h2><div class="spacer"></div>
      <span class="path" id="dcStats">—</span>
      <button class="btn sm blurple" onclick="addHook()">Add webhook</button></div>
      <div class="card-b" style="padding-top:11px"><div class="hint">
        Every webhook picks its own events, so chat can land in one channel while crashes ping staff
        in another. Chat is relayed as the player, with their skin as the avatar. Get a URL from
        Discord under <b>Server settings → Integrations → Webhooks</b>.<br>
        This is one-way — Discord messages do not come back into the game, which would need a bot.</div></div></div>
    <div class="items" id="hookList"></div>
    <div class="card"><div class="card-h"><h2>Chat detection</h2>
      <div class="path" id="chatStat">—</div><div class="spacer"></div>
      <button class="btn sm" onclick="sniffChat()">Scan the log</button>
      <button class="btn sm gold" onclick="saveChat()">Save pattern</button></div>
      <div class="card-b">
        <div class="hint" style="margin-bottom:11px">
          Vanilla logs chat as <code>&lt;Name&gt; message</code>, but Paper logs it from an
          <b>Async Chat Thread</b>, and any chat plugin — or a Skript that cancels the chat event and
          re-broadcasts — rewrites the line completely. Lodestone reads both, strips colour codes and
          rank prefixes, and takes the last name before the separator. If your format is unusual,
          give it a regex with <code>(?P&lt;player&gt;…)</code> and <code>(?P&lt;message&gt;…)</code> groups.</div>
        <label class="fld"><span>Custom pattern (optional)</span>
          <input class="inp" id="chatPat" placeholder="leave blank to auto-detect"></label>
        <div class="row" style="margin-bottom:11px">
          <input class="inp" id="chatTestLine" placeholder="paste a chat line from your log to test it">
          <button class="btn sm" onclick="testChatLine()">Test</button></div>
        <div id="chatTestOut"></div>
        <div id="chatHits"></div>
      </div></div>
  </section>

  <!-- ===================== CHAT SETUP (inside Discord view) ===================== -->

  <!-- ===================== SERVERS ===================== -->
  <section class="view" id="view-servers">
    <div class="card" style="flex:none" id="srvHead"><div class="card-h"><h2>Servers</h2><div class="spacer"></div>
      <button class="btn sm gold" onclick="wzOpen()" title="Download and install a new server - Vanilla, Paper, Purpur, Spigot, Fabric, Quilt, Forge or NeoForge">Create server</button>
      <button class="btn sm" onclick="addFolder()" title="A server that is already on this PC - pick its folder and Lodestone works out the rest">Add existing folder</button>
      <button class="btn sm" onclick="newServer('sftp')">Add remote</button>
      <button class="btn sm" onclick="doImport()" title="Pull in servers from LocalHost or RemoteHost">Import old</button></div>
      <div class="card-b" style="padding-top:11px"><div class="hint" id="srvHint">
        Pick a server and press <b>Open</b> to work inside it - console, files, mods and the rest. Local servers
        are a folder on this machine; remote ones a folder on a box you reach over SFTP.</div></div></div>
    <div class="card" style="flex:none" id="srvTools"><div class="card-b"><div class="row wrap">
      <input class="inp" id="srvFind" placeholder="Find a server by name, folder or software" style="flex:1;min-width:220px" oninput="srvRender()">
      <div class="chips" id="srvChips"></div>
      <select class="inp" id="srvSort" style="width:220px" onchange="srvSortSet(this.value)">
        <option value="status">Group: running first</option><option value="software">Group: by software</option>
        <option value="kind">Group: local / remote</option><option value="name">Sort: name</option>
        <option value="newest">Sort: newest first</option><option value="port">Sort: port</option></select>
    </div></div></div>
    <div class="items" id="srvList"></div>
    <div class="card" id="srvEditCard" style="display:none"><div class="card-h">
      <h2 id="srvEditTitle">Edit server</h2><div class="spacer"></div>
      <button class="btn sm" onclick="closeSrvEdit()">Close</button>
      <button class="btn sm gold" onclick="saveServer()">Save</button></div>
      <div class="card-b" id="srvEditBody"></div></div>
    <div id="wizard" style="display:none"></div>
  </section>

  <!-- ===================== NETWORK ===================== -->
  <section class="view" id="view-network">
    <div class="card" style="flex:none"><div class="card-h"><h2>Hosting</h2>
      <div class="spacer"></div>
      <span class="pill" id="netPhase">…</span>
      <button class="btn sm gold" onclick="netAutofix()" title="Fix everything that can be fixed from this PC, then say what cannot">Auto fix</button>
      <button class="btn sm" onclick="netProbe()" title="Which port-mapping protocols your router supports">Check router</button>
      <button class="btn sm" onclick="netTest()">Test connection</button>
      <button class="btn sm" onclick="loadNetwork()">Refresh</button></div>
      <div class="card-b" id="netModes"></div></div>

    <div class="card" style="flex:none"><div class="card-h"><h2>Offline / non-premium mode (testing)</h2>
      <div class="spacer"></div><span class="pill" id="offPillNet">off</span></div>
      <div class="card-b" id="netOffline"></div></div>

    <div class="card" id="netFixCard" style="flex:none;display:none">
      <div class="card-h"><h2>Auto fix</h2><div class="spacer"></div>
        <button class="btn sm" onclick="$('netFixCard').style.display='none'">Hide</button></div>
      <div class="card-b" id="netFix"></div></div>

    <div class="card" style="flex:none"><div class="card-h"><h2>Minecraft Server</h2>
      <div class="spacer"></div><span class="path" id="netStamp">—</span></div>
      <div class="card-b" id="netDash"></div></div>

    <div class="card" id="netStepsCard" style="flex:none;display:none">
      <div class="card-h"><h2>What happened</h2><div class="spacer"></div>
        <button class="btn sm" onclick="netHideSteps()">Hide</button></div>
      <div class="card-b" id="netSteps"></div></div>

    <div class="card" id="netWarnCard" style="flex:none;display:none">
      <div class="card-h"><h2>Before you share the address</h2><div class="spacer"></div>
        <button class="btn sm" onclick="netAudit(true)" title="Also read the router's forwarding table">Deep check</button></div>
      <div class="card-b" id="netWarn"></div></div>

    <!-- flex:none on every card, so the view scrolls as one page. Without it the
         last card is the only flexible child, collapses to a sliver, and scrolls
         its own body instead — which reads as "the settings are cut off". -->
    <div class="card" style="flex:none"><div class="card-h"><h2>Settings</h2><div class="spacer"></div>
      <button class="btn sm gold" onclick="netSaveSettings()">Save</button></div>
      <div class="card-b" id="netSettings"></div></div>
  </section>

  <!-- ===================== API ===================== -->
  <section class="view" id="view-api">
    <div class="card" style="flex:none"><div class="card-h"><h2>REST API</h2><div class="spacer"></div>
      <label class="tog"><input type="checkbox" id="apiOn" onchange="toggleApi()"><span class="box"></span>
        <span class="lbl">Enabled</span></label>
      <button class="btn sm gold" onclick="newToken()">New token</button></div>
      <div class="card-b"><div id="tokSecret"></div>
        <div class="hint" style="margin-bottom:12px">
          Send <code>Authorization: Bearer &lt;token&gt;</code>. Tokens are shown once and stored as a
          SHA-256 hash — if you lose one, make another. Scopes: <b>read</b> status and files,
          <b>control</b> start/stop/commands, <b>files</b> writes and uploads, <b>admin</b> everything
          including server records.</div>
        <div class="items" id="tokList"></div></div></div>
    <div class="card"><div class="card-h"><h2>Endpoints</h2><div class="spacer"></div>
      <span class="path">base: <span id="apiBase">/api/v1</span></span></div>
      <div class="card-b" id="epList"></div></div>
  </section>

  <!-- ===================== SETTINGS ===================== -->
  <section class="view" id="view-settings">
    <div class="card" style="flex:none"><div class="card-h"><h2>Updates</h2><div class="path" id="updFrom">—</div><div class="spacer"></div>
      <label class="tog"><input type="checkbox" id="updEnabled" onchange="updSave({enabled:this.checked})"><span class="box"></span>
      <span class="lbl">check by itself</span></label></div>
      <div class="card-b" id="updBody"><div class="hint">…</div></div></div>
    <div class="card"><div class="card-h"><h2>Panel</h2></div>
      <div class="card-b" id="setBody"></div></div>
    <div class="card"><div class="card-h"><h2>About</h2></div>
      <div class="card-b"><div class="hint" style="line-height:1.8">
        <b>Lodestone __VER__</b> — one panel for local and remote Minecraft servers.<br>
        Built by merging LocalHost (local process control) and RemoteHost (SFTP file management)
        into a single transport-agnostic app, then adding RCON, a Discord event router and a
        token-authenticated REST API.<br><br>
        The UI has no login. Keep it on 127.0.0.1 unless you trust every device on your network.
        The API is separate and does require a token.</div></div></div>
  </section>

  </div></main>
</div>
<div class="modal" id="modal"><div class="box">
  <h3 id="mTitle">…</h3><div class="mb" id="mBody"></div>
  <div class="mf" id="mFoot"></div></div></div>
<div id="toasts"></div>
<script>
const $ = (id) => document.getElementById(id);
let STATE = {}, LINES = [], LASTSEQ = 0, CWD = "", EDIT = null, PROPS = [], DIRTY = {};
let FEATURES = {}, HOOKS = [], SERVERS = [], EDITING = null, HIST = [], HPOS = -1;

function toast(msg, kind) {
  const t = document.createElement("div");
  t.className = "toast " + (kind || "");
  t.textContent = msg;
  $("toasts").appendChild(t);
  setTimeout(() => t.remove(), 4200);
}
function esc(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
async function api(path, body, method) {
  const opt = { method: method || (body ? "POST" : "GET"), headers: {} };
  if (body) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  let j = {};
  try { j = await r.json(); } catch (e) { j = { ok: false, error: "Bad reply from the panel." }; }
  if (!r.ok && j.error) throw new Error(j.error);
  if (j && j.ok === false && j.error) throw new Error(j.error);
  return j;
}
async function act(path, body, okMsg) {
  try {
    const j = await api(path, body || {});
    if (okMsg !== null) toast(j.msg || okMsg || "Done.", "good");
    return j;
  } catch (e) { toast(e.message, "bad"); throw e; }
}

/* ---------------- simple vs advanced ---------------- */
const SIMPLE_VIEWS = ["console", "files", "props", "backups", "content", "servers", "network", "settings"];
let MODE = "advanced";
function applyMode(mode) {
  MODE = mode === "simple" ? "simple" : "advanced";
  document.querySelectorAll(".nav[data-adv], .railgrp[data-adv]").forEach(n => {
    n.style.display = MODE === "simple" ? "none" : "";
  });
  const tag = $("modeTag");
  if (tag) tag.textContent = MODE === "simple" ? "simple" : "advanced";
  const open = document.querySelector(".view.show");
  if (MODE === "simple" && open && !SIMPLE_VIEWS.includes(open.id.replace("view-", ""))) {
    showView("console");
  }
}
function chooseMode() {
  modal("How much do you want to see?",
    '<div class="items">' +
    '<div class="item" style="cursor:pointer" onclick="setMode(\'simple\')">' +
      '<div style="flex:1"><div class="ttl">Simple</div><div class="sub">' +
      "Console, files, properties, backups, mods & plugins and your servers. Everything you need to run a server, " +
      "nothing else.</div></div>" +
      '<button class="btn sm gold">Use simple</button></div>' +
    '<div class="item" style="cursor:pointer" onclick="setMode(\'advanced\')">' +
      '<div style="flex:1"><div class="ttl">Advanced</div><div class="sub">' +
      "Everything: log search, plugins and worlds, automation, the AI assistant, Discord webhooks " +
      "and the REST API.</div></div>" +
      '<button class="btn sm">Use advanced</button></div></div>' +
    '<div class="hint" style="margin-top:11px">You can switch any time — click the word next to the ' +
    "Lodestone logo, or change it in Settings. Nothing is deleted either way; Simple only hides " +
    "things.</div>",
    [["Later", "closeModal()"]]);
}
async function setMode(mode) {
  closeModal();
  applyMode(mode);
  try { await api("/api/ui/mode", { mode }); } catch (e) { toast(e.message, "bad"); }
  toast(mode === "simple" ? "Simple mode — the extra tabs are hidden." : "Advanced mode — everything is on.", "good");
}

/* ---------------- text size ---------------- */
const SZ = { mono: 13, ui: 14, wrap: false };
function applySize() {
  const r = document.documentElement.style;
  r.setProperty("--fs-mono", SZ.mono + "px");
  r.setProperty("--fs-ui", SZ.ui + "px");
  const a = $("monoVal"), b = $("monoVal2");
  if (a) a.textContent = SZ.mono;
  if (b) b.textContent = SZ.mono;
  const t = $("edText");
  if (t) t.classList.toggle("wrap", SZ.wrap);
  const wb = $("wrapBtn");
  if (wb) wb.classList.toggle("gold", SZ.wrap);
  try { localStorage.setItem("lodestone.size", JSON.stringify(SZ)); } catch (e) {}
}
function bumpMono(d) {
  SZ.mono = Math.max(10, Math.min(28, SZ.mono + d));
  applySize();
}
function setUiSize(v) {
  SZ.ui = Math.max(12, Math.min(22, parseInt(v, 10) || 14));
  applySize();
}
function toggleWrap() { SZ.wrap = !SZ.wrap; applySize(); }
function toggleEdMax() {
  const on = document.querySelector(".fm").classList.toggle("edmax");
  $("maxBtn").textContent = on ? "Shrink" : "Expand";
  $("maxBtn").classList.toggle("gold", on);
}
function edKey(e) {
  // a real editor indents with Tab instead of leaving the field
  if (e.key === "Tab") {
    e.preventDefault();
    const t = e.target, a = t.selectionStart, b = t.selectionEnd;
    if (a === b && !e.shiftKey) {
      t.value = t.value.slice(0, a) + "\t" + t.value.slice(b);
      t.selectionStart = t.selectionEnd = a + 1;
    } else {
      const s0 = t.value.lastIndexOf("\n", a - 1) + 1;
      const block = t.value.slice(s0, b);
      const out = e.shiftKey ? block.replace(/^[\t ]/gm, "") : block.replace(/^/gm, "\t");
      t.value = t.value.slice(0, s0) + out + t.value.slice(b);
      t.selectionStart = s0; t.selectionEnd = s0 + out.length;
    }
    markDirty();
  }
  if ((e.ctrlKey || e.metaKey) && e.key === "s") { e.preventDefault(); saveEdit(); }
  if ((e.ctrlKey || e.metaKey) && (e.key === "=" || e.key === "+")) { e.preventDefault(); bumpMono(1); }
  if ((e.ctrlKey || e.metaKey) && e.key === "-") { e.preventDefault(); bumpMono(-1); }
}
(function () {
  try {
    const saved = JSON.parse(localStorage.getItem("lodestone.size") || "null");
    if (saved) Object.assign(SZ, saved);
  } catch (e) {}
})();

/* ---------------- views ---------------- */
function showView(name) {
  document.querySelectorAll(".view").forEach(v => v.classList.remove("show"));
  document.querySelectorAll(".nav").forEach(n => n.classList.toggle("active", n.dataset.view === name));
  $("view-" + name).classList.add("show");
  if (name === "files") loadFiles(CWD);
  if (name === "props") { loadProps(); loadAppearance(); }
  if (name === "backups") loadBackups();
  if (name === "content") ctLoad();
  if (name === "tools") { loadWorlds(); loadPlugins(); loadList(); loadRam(); }
  if (name === "auto") loadFeatures();
  if (name === "ai") loadAi();
  if (name === "discord") { loadHooks(); loadChatCfg(); }
  if (name === "servers") loadServers();
  if (name === "network") loadNetwork();
  if (name === "api") loadApi();
  if (name === "settings") loadSettings();
}

/* ---------------- console ---------------- */
function lineClass(t) {
  if (/^>>>/.test(t)) return "sys";
  if (/^!!!/.test(t)) return "err";
  if (/^>/.test(t)) return "me";
  if (/[\/\[](ERROR|SEVERE|FATAL)]/i.test(t)) return "err";
  if (/[\/\[]WARN(ING)?]/i.test(t)) return "warn";
  if (/]:\s+<[A-Za-z0-9_]+>/.test(t)) return "chat";
  if (/joined the game|left the game|Done \(/.test(t)) return "info";
  return "";
}
function renderLog() {
  const box = $("log"), f = ($("conFilter").value || "").toLowerCase();
  const at = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
  const use = f ? LINES.filter(l => l.toLowerCase().includes(f)) : LINES;
  if (!use.length) {
    box.innerHTML = '<span class="emptylog">' +
      (f ? "Nothing matches that filter." : "Waiting for the server to say something…") + "</span>";
    return;
  }
  box.innerHTML = use.slice(-2000).map(l =>
    '<span class="l ' + lineClass(l) + '">' + esc(l) + "</span>").join("");
  if (at) box.scrollTop = box.scrollHeight;
}
function clearLog() { LINES = []; renderLog(); }
function cmdKey(e) {
  if (e.key === "Enter") { sendCmd(); return; }
  if (e.key === "ArrowUp" && HIST.length) {
    HPOS = Math.min(HPOS + 1, HIST.length - 1);
    $("cmdInput").value = HIST[HPOS]; e.preventDefault();
  }
  if (e.key === "ArrowDown") {
    HPOS = Math.max(HPOS - 1, -1);
    $("cmdInput").value = HPOS < 0 ? "" : HIST[HPOS]; e.preventDefault();
  }
}
async function sendCmd() {
  const el = $("cmdInput"), cmd = el.value.trim();
  if (!cmd) return;
  HIST.unshift(cmd); HPOS = -1; el.value = "";
  try { await api("/api/command", { command: cmd }); }
  catch (e) { toast(e.message, "bad"); }
}

/* ---------------- status ---------------- */
function paintStatus(s) {
  STATE = s;
  paintUpdate(s.update);
  const st = s.state || "offline", running = s.running;
  const pill = $("statePill"), side = $("sidePill");
  const label = running ? "running" : st;
  const cls = "pill " + (running || st === "online" ? "on" : (st === "error" ? "bad" : ""));
  pill.className = cls; pill.textContent = label;
  side.className = cls; side.textContent = label;
  $("kName").textContent = s.server.name;
  $("kKind").textContent = s.server.kind === "local" ? "local process" : "remote (sftp)";
  $("kWhere").textContent = s.transport.root || s.transport.host || "—";
  $("kUp").textContent = s.uptime ? s.uptime_h : "—";
  $("kDone").textContent = s.done_ms ? s.done_ms + "s" : "—";
  $("kPlayers").textContent = s.player_count;
  $("kCmds").textContent = s.counters.cmds;
  $("kHooks").textContent = s.hooks;
  $("kSent").textContent = s.discord.sent + (s.discord.failed ? " (" + s.discord.failed + " failed)" : "");
  $("kBytes").textContent = "↓" + s.down + "  ↑" + s.up;
  $("kBk").textContent = s.backup.busy ? (s.backup.msg || "working…") : (s.backup.msg || "idle");
  $("bPlayers").textContent = s.player_count;
  $("bHooks").textContent = s.hooks;
  if (s.ai) $("bAi").textContent = s.ai.ready ? (s.ai.automod ? "automod" : "ready") : "off";
  paintNetBadge(s.hosting);
  $("offPill").style.display = s.offline_mode ? "" : "none";
  $("fName").textContent = s.server.name;
  $("fWhere").textContent = s.server.kind === "local" ? "local" : (s.transport.host || "remote");
  $("conSrc").textContent = s.server.kind === "local"
    ? (running ? "live process output" : "not running") : "tailing " + "logs/latest.log";
  $("cmdInput").disabled = !s.can_command;
  renderPlayers(s.players);
  renderControls(s);
  if (s.transport.error) $("srvHint").textContent = s.transport.error;
}
function renderControls(s) {
  const c = $("controls");
  let h = "";
  if (s.can_start) {
    if (!s.eula) h += '<button class="btn warn" onclick="acceptEula()">Accept EULA</button>';
    h += s.running
      ? '<button class="btn danger" onclick="act(\'/api/stop\',{},\'Stopping.\')">Stop</button>' +
        '<button class="btn" onclick="act(\'/api/restart\',{},\'Restarting.\')">Restart</button>' +
        '<button class="btn danger" onclick="killSrv()">Kill</button>'
      : '<button class="btn gold" onclick="act(\'/api/start\',{},\'Starting.\')">Start</button>';
  } else {
    h += s.transport.state === "online"
      ? '<button class="btn" onclick="act(\'/api/connect\',{},\'Reconnected.\')">Reconnect</button>' +
        '<button class="btn danger" onclick="act(\'/api/disconnect\',{},\'Disconnected.\')">Disconnect</button>'
      : '<button class="btn gold" onclick="act(\'/api/connect\',{},\'Connected.\')">Connect</button>';
  }
  if (s.minehut_id && !s.minehut) {
    h = '<span class="pill" id="mhPill" title="Minehut">minehut</span>' + h;
  }
  if (s.minehut) {
    h = '<span class="pill" id="mhPill" title="Minehut">minehut</span>' +
        '<button class="btn gold" onclick="mhDo(\'start\')">Start</button>' +
        '<button class="btn" onclick="mhDo(\'restart\')">Restart</button>' +
        '<button class="btn danger" onclick="mhDo(\'stop\')">Stop</button>' +
        '<button class="btn" onclick="mhDo(\'hibernate\')" title="Force hibernate">Sleep</button>' + h;
  }
  c.innerHTML = h;
}
async function mhDo(what) {
  if (what === "hibernate") {
    confirmBox("Hibernate the server?", "Minehut stops it immediately and puts it to sleep. " +
      "Players are disconnected.", async () => {
        await act("/api/minehut/action", { what }, null);
        setTimeout(refreshMinehut, 1500);
      });
    return;
  }
  await act("/api/minehut/action", { what }, null);
  setTimeout(refreshMinehut, 1500);
}
async function refreshMinehut() {
  if (!STATE.minehut && !STATE.minehut_id) return;
  try {
    const j = await api("/api/minehut/status", {});
    const el = $("mhPill");
    if (!el) return;
    if (!j.ok) { el.className = "pill bad"; el.textContent = "minehut: error"; el.title = j.error; return; }
    const state = j.online ? "online" : (j.starting ? "starting" : (j.hibernating ? "asleep" : "offline"));
    el.className = "pill " + (j.online ? "on" : (j.hibernating ? "" : "warn"));
    el.textContent = "minehut " + state + " · " + j.players + "/" + j.max_players;
    el.title = [j.name, j.plan, j.version, j.address].filter(Boolean).join(" · ");
  } catch (e) { /* header badge only */ }
}
setInterval(refreshMinehut, 15000);

function killSrv() {
  confirmBox("Kill the server?", "This stops the process immediately — anything unsaved in the "
    + "world is lost. Use Stop unless it has hung.", () => act("/api/kill", {}, "Killed."));
}
function acceptEula() {
  confirmBox("Accept the Minecraft EULA?", "This writes eula=true into eula.txt. Only do this if "
    + "you accept Mojang's EULA at aka.ms/MinecraftEULA.", () => act("/api/eula", {}, "EULA accepted."));
}
function renderPlayers(list) {
  const box = $("players");
  if (!list.length) { box.innerHTML = '<div class="empty" style="padding:12px">Nobody online.</div>'; return; }
  box.innerHTML = list.map(p =>
    '<div class="prow"><img src="https://minotar.net/helm/' + encodeURIComponent(p) + '/20.png" alt="">' +
    '<span class="nm">' + esc(p) + "</span>" +
    '<span class="acts">' +
    '<button class="btn xs" onclick="pAct(\'' + p + '\',\'op\')" title="Op">OP</button>' +
    '<button class="btn xs" onclick="pAct(\'' + p + '\',\'kick\')" title="Kick">Kick</button>' +
    '<button class="btn xs danger" onclick="pAct(\'' + p + '\',\'ban\')" title="Ban">Ban</button>' +
    "</span></div>").join("");
}
async function pAct(player, action) {
  if (action === "ban" || action === "kick") {
    promptBox((action === "ban" ? "Ban " : "Kick ") + player, "Reason (optional)", "", async r => {
      const j = await act("/api/player", { player, action, reason: r });
      if (j.out) toast(j.out, "good");
    });
    return;
  }
  await act("/api/player", { player, action });
}

/* ---------------- stream ---------------- */
function startStream() {
  const es = new EventSource("/api/stream?since=" + LASTSEQ);
  es.addEventListener("line", e => {
    const d = JSON.parse(e.data);
    LASTSEQ = d.seq;
    LINES.push(d.text);
    if (LINES.length > 4000) LINES = LINES.slice(-3000);
    renderLog();
  });
  es.addEventListener("status", e => paintStatus(JSON.parse(e.data)));
  es.onopen = () => { $("streamInd").classList.add("live"); $("streamInd").lastChild.nodeValue = "live"; };
  es.onerror = () => {
    $("streamInd").classList.remove("live");
    $("streamInd").lastChild.nodeValue = "reconnecting";
    es.close(); setTimeout(startStream, 2500);
  };
}

/* ---------------- server switcher ---------------- */
async function loadShelf() {
  SERVERS = (await api("/api/servers")).items;
  const sel = $("srvSel");
  sel.innerHTML = SERVERS.map(s =>
    '<option value="' + s.id + '"' + (s.active ? " selected" : "") + ">" +
    esc(s.name) + " · " + (s.kind === "local" ? "local" : "remote") + "</option>").join("");
}
async function switchServer(id) {
  await act("/api/servers/activate", { id }, null);
  LINES = []; LASTSEQ = 0; renderLog();
  await loadShelf();
  toast("Switched server.", "good");
}

/* ---------------- files ---------------- */
const ICON_DIR = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 7a2 2 0 012-2h4l2 2h8a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2z"/></svg>';
const ICON_FILE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 3H7a2 2 0 00-2 2v14a2 2 0 002 2h10a2 2 0 002-2V8z"/><path d="M14 3v5h5"/></svg>';

async function loadFiles(rel) {
  let d;
  try { d = await api("/api/files?path=" + encodeURIComponent(rel || "")); }
  catch (e) { $("ftBody").innerHTML = '<tr><td colspan="4"><div class="empty">' + esc(e.message) + "</div></td></tr>"; return; }
  CWD = d.rel || "";
  const parts = CWD ? CWD.split("/") : [];
  let acc = "", crumbs = '<a onclick="loadFiles(\'\')">server root</a>';
  parts.forEach(p => {
    acc = acc ? acc + "/" + p : p;
    crumbs += '<span class="sep">/</span><a onclick="loadFiles(\'' + esc(acc) + '\')">' + esc(p) + "</a>";
  });
  $("crumbs").innerHTML = crumbs;
  const rows = [];
  if (!d.at_root) {
    const up = parts.slice(0, -1).join("/");
    rows.push('<tr><td colspan="4"><span class="nm dir" onclick="loadFiles(\'' + esc(up) +
      '\')">' + ICON_DIR + "<span>..</span></span></td></tr>");
  }
  d.items.forEach(i => {
    const open = i.dir ? "loadFiles('" + esc(i.rel) + "')" : "openFile('" + esc(i.rel) + "')";
    rows.push("<tr><td><span class=\"nm" + (i.dir ? " dir" : "") + '" onclick="' + open + '">' +
      (i.dir ? ICON_DIR : ICON_FILE) + "<span>" + esc(i.name) + "</span></span></td>" +
      '<td class="sz">' + esc(i.size) + '</td><td class="md">' + esc(i.mod) + "</td>" +
      '<td class="ops">' +
      '<button class="btn xs" onclick="dl(\'' + esc(i.rel) + "'," + i.dir + ')">Download</button> ' +
      '<button class="btn xs" onclick="renameIt(\'' + esc(i.rel) + "','" + esc(i.name) + '\')">Rename</button> ' +
      '<button class="btn xs" onclick="copyIt(\'' + esc(i.rel) + '\')">Copy</button> ' +
      '<button class="btn xs danger" onclick="delIt(\'' + esc(i.rel) + "','" + esc(i.name) + '\')">Delete</button>' +
      "</td></tr>");
  });
  $("ftBody").innerHTML = rows.join("") ||
    '<tr><td colspan="4"><div class="empty">This folder is empty.</div></td></tr>';
}
async function openFile(rel) {
  try {
    const j = await api("/api/file?path=" + encodeURIComponent(rel));
    EDIT = { rel, mtime: j.mtime };
    $("editor").style.display = "flex";
    $("edName").textContent = rel.split("/").pop();
    $("edText").value = j.content;
    $("edState").textContent = "saved"; $("edState").className = "pill";
  } catch (e) { toast(e.message, "bad"); }
}
function markDirty() { $("edState").textContent = "unsaved"; $("edState").className = "pill warn"; }
function closeEdit() { EDIT = null; $("editor").style.display = "none"; }
async function saveEdit() {
  if (!EDIT) return;
  try {
    const j = await api("/api/file/save", { path: EDIT.rel, content: $("edText").value, mtime: EDIT.mtime });
    EDIT.mtime = j.mtime;
    $("edState").textContent = "saved"; $("edState").className = "pill on";
    toast("Saved.", "good");
  } catch (e) {
    if (/changed on/.test(e.message)) {
      confirmBox("File changed underneath you", e.message +
        " Overwrite it with what is in the editor?", async () => {
          const j = await api("/api/file/save", { path: EDIT.rel, content: $("edText").value });
          EDIT.mtime = j.mtime; toast("Overwritten.", "good");
          $("edState").textContent = "saved"; $("edState").className = "pill on";
        });
    } else toast(e.message, "bad");
  }
}
function dl(rel, isDir) {
  window.location = (isDir ? "/api/download/folder?path=" : "/api/download?path=") + encodeURIComponent(rel);
}
function mkNew(kind) {
  promptBox("New " + kind, "Name", "", async name => {
    if (!name) return;
    await act("/api/file/" + (kind === "folder" ? "mkdir" : "new"), { path: CWD, name }, kind + " created.");
    loadFiles(CWD);
  });
}
function renameIt(rel, cur) {
  promptBox("Rename", "New name", cur, async name => {
    if (!name || name === cur) return;
    await act("/api/file/rename", { path: rel, name }, "Renamed.");
    loadFiles(CWD);
  });
}
async function copyIt(rel) { await act("/api/file/copy", { path: rel }, "Copied."); loadFiles(CWD); }
function delIt(rel, name) {
  confirmBox("Delete " + name + "?", "This cannot be undone from the panel.", async () => {
    await act("/api/file/delete", { path: rel }, "Deleted.");
    if (EDIT && EDIT.rel === rel) closeEdit();
    loadFiles(CWD);
  });
}
async function doUpload(files) {
  for (const f of files) {
    try {
      const r = await fetch("/api/upload?path=" + encodeURIComponent(CWD) +
        "&name=" + encodeURIComponent(f.name), { method: "POST", body: f });
      const j = await r.json();
      if (j.ok === false) throw new Error(j.error);
      toast("Uploaded " + f.name, "good");
    } catch (e) { toast(f.name + ": " + e.message, "bad"); }
  }
  $("upl").value = "";
  loadFiles(CWD);
}

/* ---------------- properties ---------------- */
async function loadProps() {
  DIRTY = {};
  try {
    const j = await api("/api/properties");
    PROPS = j.items || [];
  } catch (e) { PROPS = []; $("propGrid").innerHTML = '<div class="empty">' + esc(e.message) + "</div>"; return; }
  renderProps();
}
function renderProps() {
  const f = ($("propFilter").value || "").toLowerCase();
  const use = PROPS.filter(p => !f || p.key.includes(f));
  if (!use.length) { $("propGrid").innerHTML = '<div class="empty">Nothing matches.</div>'; return; }
  $("propGrid").innerHTML = use.map(p => {
    let ctl;
    if (p.type === "bool") {
      ctl = '<label class="tog"><input type="checkbox" ' + (p.value === "true" ? "checked" : "") +
        ' onchange="setProp(this,\'' + p.key + '\',this.checked?\'true\':\'false\')"><span class="box"></span>' +
        '<span class="lbl">' + (p.value === "true" ? "on" : "off") + "</span></label>";
    } else if (p.type === "enum") {
      ctl = '<select class="inp" onchange="setProp(this,\'' + p.key + '\',this.value)">' +
        p.options.map(o => '<option' + (o === p.value ? " selected" : "") + ">" + esc(o) + "</option>").join("") +
        "</select>";
    } else {
      ctl = '<input class="inp" value="' + esc(p.value) + '" oninput="setProp(this,\'' + p.key + '\',this.value)">';
    }
    return '<div class="prop" id="prop-' + esc(p.key) + '"><div class="pk">' + esc(p.key) + "</div>" + ctl + "</div>";
  }).join("");
}
function setProp(el, key, val) {
  DIRTY[key] = val;
  const card = el.closest(".prop");
  if (card) card.classList.add("changed");
  const lbl = card && card.querySelector(".tog .lbl");
  if (lbl) lbl.textContent = val === "true" ? "on" : "off";
}
async function saveProps() {
  if (!Object.keys(DIRTY).length) { toast("Nothing changed.", ""); return; }
  await act("/api/properties", { values: DIRTY }, "Saved.");
  loadProps();
}

/* ---------------- appearance ---------------- */
const MC_COLORS = { "0":"#000000","1":"#0000AA","2":"#00AA00","3":"#00AAAA","4":"#AA0000",
  "5":"#AA00AA","6":"#FFAA00","7":"#AAAAAA","8":"#555555","9":"#5555FF","a":"#55FF55",
  "b":"#55FFFF","c":"#FF5555","d":"#FF55FF","e":"#FFFF55","f":"#FFFFFF" };
function motdToHtml(text) {
  // Renders the way the Minecraft server list does: codes are stateful until reset.
  let out = "", color = "#AAAAAA", bold = false, italic = false, under = false, strike = false, obf = false;
  const open = () => {
    let st = "color:" + color;
    if (bold) st += ";font-weight:700";
    if (italic) st += ";font-style:italic";
    if (under || strike) st += ";text-decoration:" + [under ? "underline" : "", strike ? "line-through" : ""].filter(Boolean).join(" ");
    return '<span class="' + (obf ? "mc-k" : "") + '" style="' + st + '">';
  };
  let buf = "";
  const flush = () => { if (buf) { out += open() + esc(buf) + "</span>"; buf = ""; } };
  const src = String(text || "").replace(/\\n/g, "\n");
  for (let i = 0; i < src.length; i++) {
    const ch = src[i];
    if ((ch === "&" || ch === "\u00a7") && i + 1 < src.length) {
      const nxt = src[i + 1].toLowerCase();
      if (nxt === "#" && /^[0-9a-f]{6}$/i.test(src.substr(i + 2, 6))) {
        flush(); color = "#" + src.substr(i + 2, 6); i += 7; continue;
      }
      if (MC_COLORS[nxt]) { flush(); color = MC_COLORS[nxt]; bold = italic = under = strike = obf = false; i++; continue; }
      if (nxt === "l") { flush(); bold = true; i++; continue; }
      if (nxt === "o") { flush(); italic = true; i++; continue; }
      if (nxt === "n") { flush(); under = true; i++; continue; }
      if (nxt === "m") { flush(); strike = true; i++; continue; }
      if (nxt === "k") { flush(); obf = true; i++; continue; }
      if (nxt === "r") { flush(); color = "#AAAAAA"; bold = italic = under = strike = obf = false; i++; continue; }
    }
    if (ch === "<" && /^<#[0-9a-fA-F]{6}>/.test(src.slice(i))) {
      flush(); color = src.substr(i + 1, 7); i += 8; continue;
    }
    buf += ch;
  }
  flush();
  return out || '<span style="color:#AAAAAA">A Minecraft Server</span>';
}
function renderMotd() { $("motdPrev").innerHTML = motdToHtml($("apMotd").value); }
async function loadAppearance() {
  try {
    const j = await api("/api/appearance");
    const v = j.values || {};
    $("apMotd").value = v.motd || "";
    $("apMax").value = v["max-players"] || "";
    $("apRp").value = v["resource-pack"] || "";
    $("apRpSha").value = v["resource-pack-sha1"] || "";
    $("apRpPrompt").value = v["resource-pack-prompt"] || "";
    $("apRpReq").value = (v["require-resource-pack"] === "true") ? "true" : "false";
    $("motdTitle").textContent = (STATE.server && STATE.server.name) || "A Minecraft Server";
    renderMotd();
    const ic = $("motdIcon");
    if (j.icon && j.icon.present) {
      ic.innerHTML = '<img src="/api/icon?t=' + Date.now() + '" width="64" height="64" alt="icon">';
    } else { ic.textContent = "no icon"; }
  } catch (e) { /* the properties grid reports the real error */ }
}
async function saveAppearance() {
  const values = {
    motd: $("apMotd").value,
    "max-players": $("apMax").value,
    "resource-pack": $("apRp").value,
    "resource-pack-sha1": $("apRpSha").value,
    "resource-pack-prompt": $("apRpPrompt").value,
    "require-resource-pack": $("apRpReq").value,
  };
  await act("/api/appearance", { values }, "Saved — restart for the MOTD to show.");
  loadProps();
}
async function uploadIcon(file) {
  if (!file) return;
  try {
    const r = await fetch("/api/icon", { method: "POST", body: file });
    const j = await r.json();
    if (j.ok === false) throw new Error(j.error);
    toast("Icon set (" + j.size + ").", "good");
    loadAppearance();
  } catch (e) { toast(e.message, "bad"); }
  $("iconFile").value = "";
}
function clearIcon() {
  confirmBox("Remove the server icon?", "server-icon.png is deleted from the server folder.",
    async () => { await act("/api/icon/clear", {}, "Removed."); loadAppearance(); });
}

/* ---------------- backups ---------------- */
async function loadBackups() {
  const j = await api("/api/backups");
  $("bkDir").textContent = j.dir || "";
  const box = $("bkList");
  if (!j.items.length) { box.innerHTML = '<div class="empty">No backups yet.</div>'; return; }
  box.innerHTML = j.items.map(b =>
    '<div class="item"><div style="flex:1"><div class="ttl">' + esc(b.name) + "</div>" +
    '<div class="sub">' + esc(b.when) + " · " + esc(b.size) + "</div></div>" +
    '<button class="btn xs" onclick="window.location=\'/api/backup/download?name=' +
      encodeURIComponent(b.name) + '\'">Download</button>' +
    '<button class="btn xs" onclick="restoreBk(\'' + esc(b.name) + '\')">Restore</button>' +
    '<button class="btn xs danger" onclick="delBk(\'' + esc(b.name) + '\')">Delete</button></div>').join("");
}
async function makeBackup() {
  await act("/api/backup/create", { label: $("bkLabel").value, what: $("bkWhat").value }, "Backup started.");
  $("bkLabel").value = "";
  setTimeout(loadBackups, 1500);
}
function restoreBk(name) {
  confirmBox("Restore " + name + "?", "Files in the backup overwrite what is on the server. " +
    "Stop the server first — the panel will refuse otherwise.", async () => {
      await act("/api/backup/restore", { name }, "Restore started.");
    });
}
function delBk(name) {
  confirmBox("Delete " + name + "?", "The zip is removed from this machine.", async () => {
    await act("/api/backup/delete", { name }, "Deleted."); loadBackups();
  });
}

/* ---------------- tools ---------------- */
async function loadWorlds() {
  try {
    const j = await api("/api/worlds");
    if (!j.ok) { $("wList").innerHTML = '<div class="empty">' + esc(j.error) + "</div>"; return; }
    $("wCount").textContent = j.items.length + " world(s)" + (j.active ? " · live: " + j.active : "");
    $("wList").innerHTML = j.items.length ? j.items.map(w =>
      '<div class="world' + (w.active ? " live" : "") + '"><span class="wn">' + esc(w.name) +
      (w.active ? '  <span class="pill on">live</span>' : "") +
      (w.nether ? '  <span class="pill">nether</span>' : "") +
      (w.end ? '  <span class="pill">end</span>' : "") + "</span>" +
      (w.active ? "" : '<button class="btn xs" onclick="useWorld(\'' + esc(w.name) + '\')">Make live</button>') +
      '<button class="btn xs" onclick="dl(\'' + esc(w.rel) + '\',true)">Download</button>' +
      '<button class="btn xs danger" onclick="resetWorld(\'' + esc(w.name) + "','contents')\">Reset</button>" +
      '<button class="btn xs danger" onclick="resetWorld(\'' + esc(w.name) + "','all')\">Delete</button></div>").join("")
      : '<div class="empty">No worlds found. A world is a folder holding level.dat.</div>';
  } catch (e) { $("wList").innerHTML = '<div class="empty">' + esc(e.message) + "</div>"; }
}
async function useWorld(name) {
  const j = await act("/api/worlds/activate", { name }, null);
  toast(j.note || "Set.", "good");
  loadWorlds();
}
function resetWorld(name, mode) {
  const all = mode === "all";
  confirmBox(all ? ("Delete the world " + name + "?") : ("Reset " + name + "?"),
    all ? "The entire folder is removed. Everything built there is gone, and this cannot be undone from the panel."
        : "Clears region, entities, poi and level.dat so the server regenerates the world. " +
          "Builds in it are gone. Back up first if you care about it.",
    async () => {
      try {
        const j = await api("/api/worlds/reset", { name, mode });
        toast(j.note || "Done.", "good");
        loadWorlds();
      } catch (e) { toast(e.message, "bad"); }
    });
}
async function doLogSearch() {
  const q = $("lsQ").value.trim();
  if (!q) return;
  $("lsOut").innerHTML = '<div class="empty">searching…</div>';
  try {
    const j = await api("/api/logs/search", { query: q, regex: $("lsRegex").checked, case: $("lsCase").checked });
    if (!j.ok) { $("lsOut").innerHTML = '<div class="empty">' + esc(j.error) + "</div>"; return; }
    const per = j.scanned.map(x => esc(x.name) + (x.error ? " (" + esc(x.error) + ")" : " · " + x.hits)).join("  |  ");
    let h = '<div class="hint" style="margin:12px 0 8px">' + j.hits.length + " hit(s)" +
      (j.truncated ? " (capped)" : "") + " across " + j.scanned.length + " file(s): " + per + "</div>";
    h += j.hits.length
      ? '<div class="code">' + j.hits.map(x =>
          '<span class="c">' + esc(x.file) + ":" + x.line + "</span>  " + esc(x.text)).join("\n") + "</div>"
      : '<div class="empty">Nothing matched.</div>';
    $("lsOut").innerHTML = h;
  } catch (e) { $("lsOut").innerHTML = '<div class="empty">' + esc(e.message) + "</div>"; }
}
async function loadPlugins() {
  try {
    const j = await api("/api/plugins");
    if (!j.ok) { $("plList").innerHTML = '<div class="empty">' + esc(j.error) + "</div>"; $("plCount").textContent = ""; return; }
    $("plCount").textContent = j.count + " jar(s)";
    $("plList").innerHTML = j.items.length ? j.items.map(x =>
      '<div class="item"><div style="flex:1"><div class="ttl">' + esc(x.title) +
      (x.enabled ? "" : '  <span class="pill">disabled</span>') + "</div>" +
      '<div class="sub">' + esc(x.size) + " · " + esc(x.mod) + "</div></div>" +
      '<button class="btn xs" onclick="togglePlugin(\'' + esc(x.rel) + '\')">' +
        (x.enabled ? "Disable" : "Enable") + "</button>" +
      '<button class="btn xs" onclick="dl(\'' + esc(x.rel) + '\',false)">Download</button>' +
      '<button class="btn xs danger" onclick="delPlugin(\'' + esc(x.rel) + "','" + esc(x.title) + '\')">Delete</button></div>').join("")
      : '<div class="empty">No plugin jars here.</div>';
  } catch (e) { $("plList").innerHTML = '<div class="empty">' + esc(e.message) + "</div>"; }
}
async function togglePlugin(rel) {
  const j = await act("/api/plugins/toggle", { path: rel }, null);
  toast(j.note || "Done.", "good");
  loadPlugins();
}
function delPlugin(rel, name) {
  confirmBox("Delete " + name + "?", "The jar is removed from the server. Its config folder is left alone.",
    async () => { await act("/api/file/delete", { path: rel }, "Deleted."); loadPlugins(); });
}
async function loadList() {
  const which = $("lsWhich").value;
  try {
    const j = await api("/api/list?which=" + encodeURIComponent(which));
    const rows = (j.items || []).map(x => {
      const n = x.name || x.uuid || x.ip || JSON.stringify(x);
      const extra = [x.level ? "level " + x.level : "", x.reason ? "reason: " + x.reason : "",
                     x.source ? "by " + x.source : "", x.expires && x.expires !== "forever" ? "until " + x.expires : ""]
                    .filter(Boolean).join(" · ");
      return '<div class="item"><div style="flex:1"><div class="ttl">' + esc(n) + "</div>" +
        (extra ? '<div class="sub">' + esc(extra) + "</div>" : "") + "</div>" +
        '<button class="btn xs danger" onclick="quickRemove(\'' + esc(n) + '\')">Remove</button></div>';
    });
    $("listOut").innerHTML = rows.length ? rows.join("")
      : '<div class="empty">Empty. If the server has never written this file it will not exist yet.</div>';
  } catch (e) { $("listOut").innerHTML = '<div class="empty">' + esc(e.message) + "</div>"; }
}
async function quickRemove(name) {
  $("lsName").value = name;
  await listEdit(false);
}
async function listEdit(add) {
  const player = $("lsName").value.trim();
  if (!player) { toast("Type a name first.", "bad"); return; }
  try {
    const j = await api("/api/list/edit", { which: $("lsWhich").value, player, add });
    toast(j.out || "Done.", "good");
    $("lsName").value = "";
    setTimeout(loadList, 900);
  } catch (e) { toast(e.message, "bad"); }
}

/* ---------------- automation ---------------- */
async function loadFeatures() {
  FEATURES = await api("/api/features");
  const f = FEATURES;
  const tog = (path, label, hint) =>
    '<label class="tog" style="margin-bottom:9px"><input type="checkbox" data-f="' + path + '"' +
    (get(f, path) ? " checked" : "") + '><span class="box"></span><span class="lbl">' + label +
    "</span></label>" + (hint ? '<div class="hint" style="margin:-4px 0 12px 47px">' + hint + "</div>" : "");
  const num = (path, label, w) =>
    '<label class="fld" style="width:' + (w || 110) + 'px"><span>' + label + '</span>' +
    '<input class="inp" data-f="' + path + '" value="' + esc(get(f, path)) + '"></label>';
  const txt = (path, label) =>
    '<label class="fld"><span>' + label + '</span><input class="inp" data-f="' + path +
    '" value="' + esc(get(f, path)) + '"></label>';
  $("autoBody").innerHTML =
    '<div class="grid2"><div>' +
      tog("auto_restart", "Restart after a crash", "Relaunches the server if it exits on its own.") +
      tog("daily_restart.enabled", "Daily restart") +
      '<div class="row">' + num("daily_restart.time", "At (HH:MM)") + "</div>" +
      tog("watchdog.enabled", "Watchdog", "Restart if the console goes silent — catches a hung server.") +
      '<div class="row">' + num("watchdog.minutes", "Silent for (min)") + "</div>" +
      tog("auto_backup.enabled", "Scheduled backups") +
      '<div class="row">' + num("auto_backup.hours", "Every (hours)") + num("auto_backup.keep", "Keep last") + "</div>" +
    "</div><div>" +
      tog("auto_save.enabled", "Periodic save-all") +
      '<div class="row">' + num("auto_save.minutes", "Every (min)") + "</div>" +
      tog("broadcast.enabled", "Repeating broadcast") +
      '<div class="row">' + num("broadcast.minutes", "Every (min)") + "</div>" + txt("broadcast.message", "Message") +
      tog("clearlag.enabled", "Clear dropped items") +
      '<div class="row">' + num("clearlag.minutes", "Every (min)") + "</div>" +
      tog("clearlag.warn", "Warn 10s first") +
      tog("welcome.enabled", "Welcome message") + txt("welcome.message", "Says (use {player})") +
    "</div></div>";
  renderTasks(); renderMacros(); renderChips();
}
function get(o, path) {
  return path.split(".").reduce((a, k) => (a == null ? undefined : a[k]), o);
}
function setPath(o, path, v) {
  const ks = path.split("."); const last = ks.pop();
  let cur = o; ks.forEach(k => { cur[k] = cur[k] || {}; cur = cur[k]; });
  cur[last] = v;
}
function collectFeatures() {
  document.querySelectorAll("#autoBody [data-f]").forEach(el => {
    const p = el.dataset.f;
    let v = el.type === "checkbox" ? el.checked : el.value;
    if (el.type !== "checkbox" && /^\d+$/.test(v)) v = parseInt(v, 10);
    setPath(FEATURES, p, v);
  });
  FEATURES.tasks = [...document.querySelectorAll("#taskList .item")].map(r => ({
    time: r.querySelector(".t-time").value, command: r.querySelector(".t-cmd").value }))
    .filter(t => t.time && t.command);
  FEATURES.macros = [...document.querySelectorAll("#macroList .item")].map(r => ({
    name: r.querySelector(".m-name").value, commands: r.querySelector(".m-cmds").value }))
    .filter(m => m.name);
}
async function saveFeatures() {
  collectFeatures();
  await act("/api/features", FEATURES, "Automation saved.");
  renderChips();
}
function renderTasks() {
  $("taskList").innerHTML = (FEATURES.tasks || []).map(t =>
    '<div class="item"><input class="inp t-time" style="width:96px" value="' + esc(t.time) + '" placeholder="HH:MM">' +
    '<input class="inp t-cmd" value="' + esc(t.command) + '" placeholder="say something / backup / restart">' +
    '<button class="btn xs danger" onclick="this.closest(\'.item\').remove()">Remove</button></div>').join("") ||
    '<div class="empty">Nothing scheduled. Times are on this machine’s clock, checked every 10 seconds.</div>';
}
function addTask() {
  FEATURES.tasks = FEATURES.tasks || []; collectFeatures();
  FEATURES.tasks.push({ time: "04:00", command: "say scheduled task" }); renderTasks();
}
function renderMacros() {
  $("macroList").innerHTML = (FEATURES.macros || []).map(m =>
    '<div class="item" style="align-items:flex-start"><div style="flex:1">' +
    '<input class="inp m-name" value="' + esc(m.name) + '" placeholder="name" style="margin-bottom:7px">' +
    '<textarea class="inp m-cmds" rows="3" placeholder="one command per line">' + esc(m.commands) + "</textarea></div>" +
    '<button class="btn xs danger" onclick="this.closest(\'.item\').remove()">Remove</button></div>').join("") ||
    '<div class="empty">No macros yet.</div>';
}
function addMacro() {
  FEATURES.macros = FEATURES.macros || []; collectFeatures();
  FEATURES.macros.push({ name: "New macro", commands: "" }); renderMacros();
}
function renderChips() {
  $("macroChips").innerHTML = (FEATURES.macros || []).filter(m => m.name).map(m =>
    '<span class="chip" onclick="runMacro(\'' + esc(m.name) + '\')">' + esc(m.name) + "</span>").join("");
}
async function runMacro(name) { await act("/api/macro", { name }); }

/* ---------------- assistant ---------------- */
let AI = {}, ATTS = [];
async function loadAi() {
  AI = await api("/api/ai");
  $("aiModel").textContent = AI.has_key
    ? (AI.provider || "openai") + " · " + (AI.model || "default model")
    : "no API key yet";
  $("aiUsage").textContent = AI.usage && (AI.usage.in || AI.usage.out)
    ? "tokens " + AI.usage.in + " in / " + AI.usage.out + " out" : "";
  const sel = (v, opts) => opts.map(o =>
    '<option value="' + o[0] + '"' + (v === o[0] ? " selected" : "") + ">" + o[1] + "</option>").join("");
  $("aiSettings").innerHTML =
    '<label class="fld"><span>Provider</span><select class="inp" id="aiProv" onchange="provChanged()">' +
      "<optgroup label=\"Cloud\">" +
      sel(AI.provider || "openai", [["openai","OpenAI / compatible"],["anthropic","Anthropic"],
                                    ["gemini","Google Gemini"]]) + "</optgroup>" +
      "<optgroup label=\"Local - no key needed\">" +
      sel(AI.provider || "openai", [["ollama","Ollama"],["lmstudio","LM Studio"],
                                    ["llamacpp","llama.cpp server"],["custom","Other OpenAI-compatible"]]) +
      "</optgroup></select>" +
      (AI.local ? '<div class="hint">Runs on your own machine. Nothing leaves it, and no key is needed.</div>' : "") +
      "</label>" +
    '<label class="fld"><span>Model</span><div class="row">' +
      '<input class="inp" id="aiModelIn" list="aiModelList" value="' + esc(AI.model || "") +
      '" placeholder="' + (AI.local ? "press Detect" : "leave blank for the default") + '">' +
      '<button class="btn sm" onclick="detectModels()">Detect</button></div>' +
      '<datalist id="aiModelList"></datalist>' +
      '<div class="hint" id="aiModelHint"></div></label>' +
    (AI.needs_key
      ? '<label class="fld"><span>API key</span><input class="inp" id="aiKey" type="password" placeholder="' +
        (AI.has_key ? "saved — type to replace" : "paste your key") + '"><div class="hint">Your own key, ' +
        "stored in ~/.lodestone.json on this machine.</div></label>"
      : '<input type="hidden" id="aiKey" value="">') +
    '<label class="fld"><span>Base URL</span><input class="inp" id="aiBase" value="' +
      esc(AI.base_url || "") + '" placeholder="' + esc(AI.base_default || "") + '"></label>' +
    '<label class="fld"><span>Temperature</span><div class="row">' +
      '<input type="range" min="0" max="14" value="' + Math.round((AI.temperature != null ? AI.temperature : 0.4) * 10) +
      '" oninput="$(\'aiTempV\').textContent=(this.value/10).toFixed(1)" id="aiTemp" style="flex:1">' +
      '<span class="pill" id="aiTempV">' + (AI.temperature != null ? AI.temperature : 0.4).toFixed(1) + "</span></div>" +
      '<div class="hint">Lower is steadier. The assistant has to emit strict JSON, so keep it low.</div></label>' +
    '<div style="border-top:2px solid var(--line);margin:6px 0 12px;padding-top:12px">' +
    '<label class="tog" style="margin-bottom:9px"><input type="checkbox" id="aiCmds"' +
      (AI.allow_commands !== false ? " checked" : "") + '><span class="box"></span>' +
      '<span class="lbl">May run commands</span></label><br>' +
    '<label class="tog" style="margin-bottom:9px"><input type="checkbox" id="aiFiles"' +
      (AI.allow_files ? " checked" : "") + '><span class="box"></span>' +
      '<span class="lbl">May edit files</span></label>' +
    '<div class="hint" style="margin-bottom:12px">Off means it can still read and explain, ' +
      "but not change anything.</div></div>" +
    '<div style="border-top:2px solid var(--line);padding-top:12px">' +
    '<label class="tog" style="margin-bottom:9px"><input type="checkbox" id="aiMod"' +
      (AI.automod ? " checked" : "") + '><span class="box"></span>' +
      '<span class="lbl">Watch chat for rule breaks</span></label><br>' +
    '<label class="tog" style="margin-bottom:9px"><input type="checkbox" id="aiModAuto"' +
      (AI.automod_auto ? " checked" : "") + '><span class="box"></span>' +
      '<span class="lbl">Act on it automatically</span></label>' +
    '<div class="hint" style="margin-bottom:10px">With this off, flags are logged and sent to Discord ' +
      "for a human to judge.</div>" +
    '<label class="fld"><span>Rules</span><textarea class="inp" id="aiRules" rows="4">' +
      esc(AI.rules || "") + "</textarea></label></div>";
  $("bAi").textContent = AI.has_key ? (AI.automod ? "automod" : "ready") : "off";
}
async function aiSaveSettings() {
  const body = {
    provider: $("aiProv").value, model: $("aiModelIn").value, base_url: $("aiBase").value,
    allow_commands: $("aiCmds").checked, allow_files: $("aiFiles").checked,
    automod: $("aiMod").checked, automod_auto: $("aiModAuto").checked, rules: $("aiRules").value,
  };
  body.temperature = (parseInt($("aiTemp").value, 10) || 4) / 10;
  if ($("aiKey") && $("aiKey").value) body.api_key = $("aiKey").value;
  await act("/api/ai/save", body, "Assistant settings saved.");
  loadAi();
}
async function provChanged() {
  // save first so the server knows which provider to probe
  await aiSaveSettings();
}
async function detectModels() {
  $("aiModelHint").textContent = "asking the server…";
  await aiSaveSettings();
  try {
    const j = await api("/api/ai/models", {});
    if (!j.ok) { $("aiModelHint").textContent = j.error || "could not list models"; return; }
    $("aiModelList").innerHTML = j.models.map(mm => '<option value="' + esc(mm) + '">').join("");
    $("aiModelHint").textContent = j.models.length
      ? j.models.length + " model(s) at " + j.base + " — click the box to pick one"
      : "reachable, but no models installed at " + j.base;
    if (j.models.length && !$("aiModelIn").value) {
      $("aiModelIn").value = j.models[0];
    }
  } catch (e) { $("aiModelHint").textContent = e.message; }
}
async function aiTest() {
  aiPush("step", "testing the connection…");
  try {
    const j = await api("/api/ai/test", {});
    aiPush(j.ok ? "step" : "err", j.msg);
  } catch (e) { aiPush("err", e.message); }
}
function aiPush(kind, text, who) {
  const box = $("aiMsgs");
  if (box.querySelector(".empty")) box.innerHTML = "";
  const d = document.createElement("div");
  d.className = "msg " + kind;
  d.innerHTML = (who ? '<div class="who">' + esc(who) + "</div>" : "") + esc(text);
  box.appendChild(d);
  box.scrollTop = box.scrollHeight;
  return d;
}
async function aiSend() {
  const el = $("aiInput"), text = el.value.trim();
  if (!text) return;
  el.value = "";
  aiPush("me", text, "you");
  const atts = ATTS.slice(); ATTS = []; renderAtts();
  $("aiGo").disabled = true;
  const thinking = aiPush("step", "working…");
  try {
    const j = await api("/api/ai/run", { instruction: text, attachments: atts });
    thinking.remove();
    (j.log || []).forEach(l => aiPush("step", l.action + " — " + l.detail));
    aiPush("bot", j.reply, "assistant");
    if (j.usage) $("aiUsage").textContent = "tokens " + j.usage.total_in + " in / " + j.usage.total_out + " out";
  } catch (e) {
    thinking.remove();
    aiPush("err", e.message);
  }
  $("aiGo").disabled = false;
  el.focus();
}
async function aiClear() {
  await act("/api/ai/clear", {}, "Chat cleared.");
  $("aiMsgs").innerHTML = '<div class="empty">Chat cleared.</div>';
}
async function aiAttach() {
  try {
    const j = await api("/api/ai/attach", {});
    ATTS.push({ name: j.name, content: j.content });
    renderAtts();
  } catch (e) { toast(e.message, "bad"); }
}
function renderAtts() {
  $("aiAtts").innerHTML = ATTS.map((a, i) =>
    '<span class="att">' + esc(a.name) + ' <b onclick="ATTS.splice(' + i + ',1);renderAtts()">x</b></span>').join("");
}

/* ---------------- discord ---------------- */
const EVENTS = __EVENTS__;
async function loadHooks() {
  const j = await api("/api/discord");
  HOOKS = j.hooks || [];
  $("dcStats").textContent = j.stats.sent + " sent · " + j.stats.failed + " failed" +
    (j.stats.queued ? " · " + j.stats.queued + " queued" : "") +
    (j.stats.last_error ? " · last error: " + j.stats.last_error : "");
  const box = $("hookList");
  if (!HOOKS.length) {
    box.innerHTML = '<div class="empty">No webhooks yet.<br>Add one and pick which events it should carry.</div>';
    return;
  }
  box.innerHTML = HOOKS.map((h, i) =>
    '<div class="hook" id="hook-' + i + '"><div class="hook-h" onclick="toggleHook(' + i + ')">' +
    '<span class="pill' + (h.enabled ? " on" : "") + '">' + (h.enabled ? "on" : "off") + "</span>" +
    '<span class="nm">' + esc(h.name || "webhook") + "</span>" +
    '<span class="path">' + (h.events || []).length + " events" +
      (h.url ? "" : " · no URL yet") +
      (h.sent ? " · " + h.sent + " sent" : "") +
      (h.failed ? " · " + h.failed + " failed" : "") +
      (h.status_msg ? " · live status" : "") + "</span></div>" +
    '<div class="hook-b">' +
      '<div class="grid2" style="margin-top:12px">' +
      '<label class="fld"><span>Name</span><input class="inp" data-h="' + i + '" data-k="name" value="' + esc(h.name) + '"></label>' +
      '<label class="fld"><span>Post as</span><input class="inp" data-h="' + i + '" data-k="username" value="' + esc(h.username) + '"></label>' +
      "</div>" +
      '<label class="fld"><span>Webhook URL</span><input class="inp" data-h="' + i + '" data-k="url" value="' + esc(h.url) +
        '" placeholder="https://discord.com/api/webhooks/…"></label>' +
      '<div class="grid2">' +
      '<label class="fld"><span>Avatar URL (optional)</span><input class="inp" data-h="' + i + '" data-k="avatar" value="' + esc(h.avatar) + '"></label>' +
      '<label class="fld"><span>Ping on crashes/errors</span><input class="inp" data-h="' + i + '" data-k="mention" value="' + esc(h.mention) +
        '" placeholder="&lt;@&amp;roleid&gt; or @here"></label>' +
      "</div>" +
      '<div class="grid2">' +
      '<label class="fld"><span>Thread ID (optional)</span><input class="inp" data-h="' + i + '" data-k="thread_id" value="' +
        esc(h.thread_id || "") + '" placeholder="post into a thread instead of the channel"></label>' +
      '<label class="fld"><span>Chat style</span><select class="inp" data-h="' + i + '" data-k="chat_style">' +
        '<option value="player"' + (h.chat_style !== "compact" ? " selected" : "") + ">As the player (skin avatar)</option>" +
        '<option value="compact"' + (h.chat_style === "compact" ? " selected" : "") + ">Batched lines (kinder to rate limits)</option>" +
        "</select></label></div>" +
      '<div class="row wrap" style="margin:4px 0 12px">' +
      '<label class="tog"><input type="checkbox" data-h="' + i + '" data-k="enabled"' + (h.enabled ? " checked" : "") +
        '><span class="box"></span><span class="lbl">Enabled</span></label>' +
      '<label class="tog"><input type="checkbox" data-h="' + i + '" data-k="embeds"' + (h.embeds ? " checked" : "") +
        '><span class="box"></span><span class="lbl">Rich embeds</span></label>' +
      '<label class="tog" title="Keeps one message updated instead of posting again"><input type="checkbox" data-h="' + i +
        '" data-k="status_msg"' + (h.status_msg ? " checked" : "") +
        '><span class="box"></span><span class="lbl">Live status message</span></label>' +
      '<label class="tog" title="Attach the tail of the console to crash reports"><input type="checkbox" data-h="' + i +
        '" data-k="attach_log"' + (h.attach_log !== false ? " checked" : "") +
        '><span class="box"></span><span class="lbl">Attach log on crash</span></label>' +
      '<div class="spacer"></div>' +
      '<button class="btn sm" onclick="testHook(' + i + ')">Send test</button>' +
      '<button class="btn sm" onclick="testEvent(' + i + ')">Test an event</button>' +
      '<button class="btn sm gold" onclick="saveHooks()">Save</button>' +
      '<button class="btn sm danger" onclick="delHook(' + i + ')">Remove</button>' +
      "</div>" +
      "<span style=\"font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:var(--dim);font-weight:700\">Events</span>" +
      '<div class="evgrid">' + EVENTS.map(e =>
        '<div class="ev' + ((h.events || []).includes(e[0]) ? " on" : "") + '" onclick="toggleEv(' + i + ",'" + e[0] + '\')">' +
        '<span class="tick"></span><div><div class="evn">' + esc(e[1]) + '</div><div class="evd">' + esc(e[2]) + "</div></div></div>").join("") +
      "</div></div></div>").join("");
}
function toggleHook(i) { $("hook-" + i).classList.toggle("open"); }
function toggleEv(i, key) {
  collectHooks();
  const ev = HOOKS[i].events || [];
  HOOKS[i].events = ev.includes(key) ? ev.filter(k => k !== key) : ev.concat([key]);
  const open = [...document.querySelectorAll(".hook.open")].map(el => el.id);
  loadHooksKeepOpen(open);
}
async function loadHooksKeepOpen(open) {
  await saveHooks(true);
  await loadHooks();
  open.forEach(id => { const el = $(id); if (el) el.classList.add("open"); });
}
function collectHooks() {
  document.querySelectorAll("#hookList [data-h]").forEach(el => {
    const h = HOOKS[parseInt(el.dataset.h, 10)];
    if (!h) return;
    h[el.dataset.k] = el.type === "checkbox" ? el.checked : el.value;
  });
}
async function saveHooks(quiet) {
  collectHooks();
  await act("/api/discord", { hooks: HOOKS }, quiet ? null : "Webhooks saved.");
  if (!quiet) loadHooks();
}
function addHook() {
  api("/api/discord/add", {}).then(() => loadHooks());
}
function delHook(i) {
  confirmBox("Remove this webhook?", "Discord keeps the webhook itself — this just stops Lodestone using it.",
    async () => { HOOKS.splice(i, 1); await saveHooks(true); loadHooks(); toast("Removed.", "good"); });
}
function testEvent(i) {
  collectHooks();
  const evs = HOOKS[i].events || [];
  if (!evs.length) { toast("Give it some events first.", "bad"); return; }
  modal("Fire a test event",
    '<label class="fld"><span>Event</span><select class="inp" id="tevK">' +
    EVENTS.filter(e => evs.includes(e[0])).map(e => '<option value="' + e[0] + '">' + esc(e[1]) + "</option>").join("") +
    "</select></label>" +
    '<label class="fld"><span>Title</span><input class="inp" id="tevT" value="Test event"></label>' +
    '<label class="fld"><span>Description</span><input class="inp" id="tevD" value="Fired from the panel."></label>' +
    '<div class="hint">This goes to every webhook subscribed to that event, exactly as the real one would.</div>',
    [["Cancel", "closeModal()"], ["Fire", "doTestEvent()", "gold"]]);
}
async function doTestEvent() {
  const body = { event: $("tevK").value, title: $("tevT").value, description: $("tevD").value };
  closeModal();
  await act("/api/discord/fire", body, "Fired.");
  setTimeout(loadHooks, 1200);
}
async function testHook(i) {
  collectHooks();
  await saveHooks(true);
  try {
    const j = await api("/api/discord/test", { id: HOOKS[i].id });
    toast(j.ok ? "Test posted to Discord." : j.error, j.ok ? "good" : "bad");
  } catch (e) { toast(e.message, "bad"); }
  loadHooks();
}

/* ---------------- chat detection ---------------- */
function loadChatCfg() {
  const st = STATE.chat || {};
  $("chatStat").textContent = st.seen
    ? st.seen + " message(s) picked up" + (st.last ? " · last: " + st.last : "")
    : "nothing seen yet this session";
  api("/api/servers/get?id=" + encodeURIComponent(STATE.server.id)).then(j => {
    $("chatPat").value = (j.server.chat || {}).pattern || "";
  }).catch(() => {});
}
async function saveChat() {
  await act("/api/chat/save", { pattern: $("chatPat").value, enabled: true }, "Saved.");
  sniffChat();
}
async function sniffChat() {
  $("chatHits").innerHTML = '<div class="empty">scanning…</div>';
  try {
    const j = await api("/api/chat/sniff");
    let h = '<div class="hint" style="margin:12px 0 8px">Scanned ' + j.scanned + " line(s). Found " +
      j.count + " chat message(s)" + (j.custom ? " using your custom pattern" : " with auto-detection") + ".";
    if (j.threads.length) h += " Chat arrives on: " + j.threads.map(t => "<b>" + esc(t[0]) + "</b>").join(", ") + ".";
    h += "</div>";
    if (j.hits.length) {
      h += '<div class="items">' + j.hits.slice(-12).reverse().map(x =>
        '<div class="item"><div style="flex:1"><div class="ttl">' + esc(x.player) + " — " + esc(x.message) +
        '</div><div class="sub">' + esc(x.line) + "</div></div></div>").join("") + "</div>";
    } else {
      h += '<div class="empty">No chat found in the recent log. If people have talked since the ' +
           "server started, paste one of those lines into the test box above.</div>";
    }
    if (j.unmatched.length) {
      h += '<div class="hint" style="margin-top:12px">These came off the chat thread but did not parse — ' +
        "they may need a custom pattern:</div><div class=\"code\" style=\"margin-top:6px\">" +
        j.unmatched.map(esc).join("\n") + "</div>";
    }
    $("chatHits").innerHTML = h;
  } catch (e) { $("chatHits").innerHTML = '<div class="empty">' + esc(e.message) + "</div>"; }
}
async function testChatLine() {
  const line = $("chatTestLine").value;
  if (!line.trim()) return;
  try {
    const j = await api("/api/chat/test", { line });
    $("chatTestOut").innerHTML = '<div class="item" style="margin-bottom:11px"><div style="flex:1">' +
      (j.matched
        ? '<div class="ttl" style="color:var(--green)">Parsed as ' + esc(j.player) + ": " + esc(j.message) + "</div>"
        : '<div class="ttl" style="color:var(--red)">Not recognised as chat</div>') +
      '<div class="sub">' + (j.auto_matched && !j.matched
        ? "auto-detection would have read it as " + esc(j.auto_player) + ": " + esc(j.auto_message) +
          " — clear your custom pattern to use that"
        : (j.matched ? "this line will be relayed" : "give it a custom pattern above, or check it really is a chat line")) +
      "</div></div></div>";
  } catch (e) { toast(e.message, "bad"); }
}

/* ---------------- servers ---------------- */
/* ---------------- mods & plugins ---------------- */
// One browser, two hosts: the Mods & plugins tab installs straight away, the
// Create server wizard collects a list. The engine filters by what the server
// loads, so a Fabric server is never offered a Bukkit plugin.
const CB = {};
const SRC_NAMES = { modrinth: "Modrinth", hangar: "Hangar", spigot: "SpigotMC" };
function swName(sw) { return sw === "neoforge" ? "NeoForge" : sw ? sw[0].toUpperCase() + sw.slice(1) : ""; }
function fmtCount(n) {
  n = +n || 0;
  return n >= 1e6 ? (n / 1e6).toFixed(1).replace(/\.0$/, "") + "M" : n >= 1e3 ? (n / 1e3).toFixed(1).replace(/\.0$/, "") + "k" : String(n);
}
function cbInit(host, target, profile, mode) {
  const st = CB[host] = { host, target, profile, mode, source: "modrinth", offset: 0, items: [], total: 0 };
  const srcs = profile.sources || [];
  if (!srcs.length) { $(host).innerHTML = '<div class="empty">' + esc(profile.reason || "This server takes no mods or plugins.") + "</div>"; return; }
  st.source = srcs[0];
  $(host).innerHTML =
    '<div class="row wrap" id="' + host + '-src"></div>' +
    '<div class="row" style="margin-top:9px"><input class="inp" id="' + host + '-q" placeholder="Search by name, or leave empty to browse the most popular" onkeydown="if(event.key===&quot;Enter&quot;)cbSearch(this.dataset.h,0)" data-h="' + host + '">' +
    '<select class="inp" id="' + host + '-sort" style="width:170px" data-h="' + host + '" onchange="cbSearch(this.dataset.h,0)">' +
    '<option value="relevance">Relevance</option><option value="downloads">Downloads</option>' +
    '<option value="updated">Recently updated</option><option value="newest">Newest</option></select>' +
    '<button class="btn gold" data-h="' + host + '" onclick="cbSearch(this.dataset.h,0)">Search</button></div>' +
    '<label class="tog" style="margin-top:8px"><input type="checkbox" id="' + host + '-all" data-h="' + host + '" onchange="cbSearch(this.dataset.h,0)"><span class="box"></span>' +
    '<span class="lbl">show other versions too</span></label>' +
    '<div class="chips" id="' + host + '-cats" style="margin-top:9px"></div>' +
    '<div class="hint" id="' + host + '-status" style="margin-top:8px"></div>' +
    '<div class="items" id="' + host + '-out"></div>' +
    '<div class="row" style="justify-content:flex-end;margin-top:8px">' +
    '<button class="btn sm" id="' + host + '-prev" data-h="' + host + '" onclick="cbPage(this.dataset.h,-1)">Previous</button>' +
    '<button class="btn sm" id="' + host + '-next" data-h="' + host + '" onclick="cbPage(this.dataset.h,1)">Next</button></div>';
  cbSources(host);
  cbCats(host);
  cbSearch(host, 0);
}
// Category chips for the source on screen (Modrinth and Hangar have them).
async function cbCats(host) {
  const st = CB[host], src = st.source;
  st.cats = st.cats || {};
  if (!st.cats[src]) {
    try { st.cats[src] = (await api("/api/content/categories?" + st.target + "&source=" + encodeURIComponent(src))).items || []; }
    catch (e) { st.cats[src] = []; }
  }
  if (CB[host] !== st || st.source !== src) return;
  const cats = st.cats[src];
  $(host + "-cats").innerHTML = cats.length ? ['<button class="btn xs' + (!st.category ? " gold" : "") + '" data-h="' + host +
    '" data-c="" onclick="cbCat(this.dataset.h,this.dataset.c)">All</button>'].concat(cats.map(c =>
    '<button class="btn xs' + (st.category === c.id ? " gold" : "") + '" data-h="' + host + '" data-c="' + esc(c.id) +
    '" onclick="cbCat(this.dataset.h,this.dataset.c)">' + esc(c.name) + "</button>")).join("") : "";
}
function cbCat(host, c) {
  const st = CB[host];
  st.category = st.category === c ? "" : c;
  cbCats(host);
  cbSearch(host, 0);
}
function cbSources(host) {
  const st = CB[host];
  $(host + "-src").innerHTML = (st.profile.sources || []).map(s =>
    '<button class="btn sm' + (s === st.source ? " gold" : "") + '" data-h="' + host + '" data-s="' + s +
    '" onclick="cbSource(this.dataset.h,this.dataset.s)">' + esc(SRC_NAMES[s] || s) + "</button>").join("");
}
function cbSource(host, src) { CB[host].source = src; CB[host].category = ""; cbSources(host); cbCats(host); cbSearch(host, 0); }
function cbPage(host, d) { const st = CB[host]; cbSearch(host, Math.max(0, st.offset + d * 20)); }
async function cbSearch(host, offset) {
  const st = CB[host];
  if (!st) return;
  st.offset = offset;
  const seq = st.seq = (st.seq || 0) + 1;
  $(host + "-status").textContent = "searching " + (SRC_NAMES[st.source] || st.source) + "…";
  let j;
  try {
    j = await api("/api/content/search?" + st.target + "&source=" + encodeURIComponent(st.source) +
      "&q=" + encodeURIComponent($(host + "-q").value) + "&offset=" + offset +
      "&sort=" + encodeURIComponent($(host + "-sort").value) + ($(host + "-all").checked ? "&all=1" : "") +
      (st.category ? "&category=" + encodeURIComponent(st.category) : ""));
  } catch (e) {
    if (seq !== st.seq) return;
    $(host + "-out").innerHTML = '<div class="empty">' + esc(e.message) + "</div>";
    $(host + "-status").textContent = "";
    return;
  }
  if (seq !== st.seq) return;
  st.items = j.items || [];
  st.total = j.total || 0;
  const p = j.profile || st.profile;
  $(host + "-status").textContent = st.items.length
    ? fmtCount(st.total) + " " + (p.kind === "mod" ? "mods" : "plugins") + " for " + p.label + " " + (p.mc || "") +
      " on " + SRC_NAMES[st.source] + " · showing " + (offset + 1) + "–" + (offset + st.items.length) : "";
  $(host + "-prev").disabled = offset <= 0;
  $(host + "-next").disabled = offset + st.items.length >= st.total;
  cbRender(host);
}
function cbRender(host) {
  const st = CB[host];
  $(host + "-out").innerHTML = st.items.length ? st.items.map((h, i) => {
    const fits = h.compatible === true ? '<span class="pill on">fits</span>'
      : h.compatible === false ? '<span class="pill bad">does not fit</span>' : '<span class="pill warn">unverified</span>';
    const added = st.mode === "wizard" && wzPicked(h);
    return '<div class="item"' + (h.compatible === false ? ' style="opacity:.8"' : "") + ">" +
      (h.icon ? '<img src="' + esc(h.icon) + '" alt="" style="width:40px;height:40px;border-radius:9px;flex:none;object-fit:cover" loading="lazy">' : "") +
      '<div style="flex:1;min-width:0"><div class="ttl">' + esc(h.name) + (h.author ? ' <span class="hint">by ' + esc(h.author) + "</span>" : "") + "  " + fits + "</div>" +
      '<div class="sub">' + fmtCount(h.downloads) + " downloads" + ((h.categories || []).length ? " · " + esc(h.categories.join(", ")) : "") +
      " · " + esc(h.description) + "</div>" +
      (h.note ? '<div class="sub" style="color:var(--' + (h.compatible === false || !h.installable ? "red" : "gold") + ')">' + esc(h.note) + "</div>" : "") + "</div>" +
      '<button class="btn xs' + (added ? "" : " gold") + '" data-h="' + host + '" data-i="' + i + '" onclick="cbPick(this.dataset.h,+this.dataset.i)"' +
      (h.installable && !added ? "" : " disabled") + ">" + (added ? "Added" : st.mode === "wizard" ? "Add" : "Install") + "</button>" +
      (h.url ? '<a class="btn xs" href="' + esc(h.url) + '" target="_blank" rel="noopener">Page</a>' : "") + "</div>";
  }).join("") : '<div class="empty">Nothing found.</div>';
}
function cbPick(host, i) {
  const st = CB[host];
  const h = st.items[i];
  if (st.mode === "wizard") { wzAdd(h); cbRender(host); return; }
  ctChooseVersion(h);
}
function planHtml(pl) {
  let out = (pl.install || []).map(it =>
    '<div class="item"><div style="flex:1"><div class="ttl">' + esc(it.name) + " " + esc(it.version) + "</div>" +
    '<div class="sub">' + (it.reason === "requested" ? "from " + esc(SRC_NAMES[it.source] || it.source)
      : esc(it.reason) + " — installed automatically") + "</div></div>" +
    (it.auto ? '<span class="pill">dependency</span>' : '<span class="pill on">will install</span>') + "</div>").join("");
  out += (pl.blocked || []).map(b =>
    '<div class="item"><div style="flex:1"><div class="ttl">' + esc(b.name) + " will not be installed</div>" +
    '<div class="sub" style="color:var(--red)">' + esc(b.reason) + "</div></div></div>").join("");
  out += (pl.warnings || []).map(w => '<div class="hint" style="color:var(--gold)">' + esc(w) + "</div>").join("");
  if ((pl.optional || []).length)
    out += '<div class="hint">Optional extras that work with it: ' + esc(pl.optional.map(o => o.name + " (" + o.for + ")").join(", ")) + "</div>";
  if (!(pl.install || []).length && !(pl.blocked || []).length) out += '<div class="hint">Already installed — nothing to do.</div>';
  return '<div class="items">' + out + "</div>";
}

let CT = { tab: "installed", profile: null, updates: {}, items: [], pick: null, project: null };
function ctSid() { return (STATE.server || {}).id || ""; }
function ctTab(t) {
  CT.tab = t;
  ctShow();
}
function ctShow() {
  const none = CT.profile && !CT.profile.kind;
  $("ctNone").style.display = none ? "" : "none";
  $("ctInstCard").style.display = !none && CT.tab === "installed" ? "" : "none";
  $("ctBrowseCard").style.display = !none && CT.tab === "browse" ? "" : "none";
  $("ctTabInst").classList.toggle("gold", CT.tab === "installed");
  $("ctTabBrowse").classList.toggle("gold", CT.tab === "browse");
  if (!none && CT.tab === "browse" && CT.profile && (!CB.ctBrowse || CB.ctBrowse.sid !== ctSid())) {
    cbInit("ctBrowse", "sid=" + encodeURIComponent(ctSid()), CT.profile, "install");
    CB.ctBrowse.sid = ctSid();
  }
}
async function ctLoad() {
  let j;
  try { j = await api("/api/content/installed?sid=" + encodeURIComponent(ctSid())); }
  catch (e) { $("ctList").innerHTML = '<div class="empty">' + esc(e.message) + "</div>"; return; }
  const p = j.profile || {};
  if (CT.profile && (CT.profile.sid !== p.sid || CT.profile.software !== p.software || CT.profile.mc !== p.mc)) {
    delete CB.ctBrowse;
    CT.updates = {};
    CT.filter = "all";
  }
  CT.profile = p;
  CT.items = j.items || [];
  $("ctHead").textContent = p.kind === "mod" ? "Mods" : p.kind === "plugin" ? "Plugins" : "Mods & plugins";
  $("ctAbout").textContent = p.kind ? p.label + " " + (p.mc || "(version unknown)") + " · " +
    (p.kind === "mod" ? "mods" : "plugins") + " go in " + p.dir + "/" : (p.server || "");
  $("ctNoneMsg").textContent = p.reason || "";
  ctRender();
  ctShow();
}
function ctSortSet(v) { prefSet("ctSort", v); ctRender(); }
function ctFilterSet(v) { CT.filter = v; ctRender(); }
function ctProblem(x) { return (x.problems || []).length > 0; }
function ctPass(x, f) {
  return f === "attention" ? ctProblem(x) || !!CT.updates[x.file] : f === "yours" ? !x.auto
    : f === "deps" ? !!x.auto : f === "disabled" ? !x.enabled : true;
}
function fmtBytes(n) { return n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.round(n / 1024) + " KB"; }
function ctPic(x) {
  const src = x.icon || (x.jar_icon ? "/api/content/icon?sid=" + encodeURIComponent(ctSid()) + "&file=" + encodeURIComponent(x.file) : "");
  return src ? '<img class="sicon smooth' + (x.enabled ? "" : " off") + '" src="' + esc(src) + '" alt="" loading="lazy">' : "";
}
function ctRender() {
  const items = CT.items || [];
  const n = Object.keys(CT.updates).length;
  const what = CT.profile && CT.profile.kind === "mod" ? "mod" : "plugin";
  CT.filter = CT.filter || "all";
  $("ctCount").textContent = items.length + " " + what + (items.length === 1 ? "" : "s") + " in " + ((CT.profile || {}).dir || "") + "/" +
    (n ? " · " + n + " update" + (n === 1 ? "" : "s") + " available" : "");
  $("ctUpdAll").style.display = n ? "" : "none";
  $("ctUpdAll").textContent = "Update all (" + n + ")";
  const probs = items.filter(ctProblem).length;
  const stat = (label, val, col) => '<div class="stat"><b' + (col ? ' style="color:var(--' + col + ')"' : "") + ">" + esc(val) + "</b><span>" + esc(label) + "</span></div>";
  $("ctStats").innerHTML = items.length || CT.profile && CT.profile.kind ? stat(what === "mod" ? "Mods" : "Plugins", items.filter(x => !x.auto).length, "green") +
    stat("Dependencies", items.filter(x => x.auto).length) + stat("Need attention", probs, probs ? "red" : "") +
    stat("Updates", n || "—", n ? "gold" : "") + stat("On disk", fmtBytes(items.reduce((a, x) => a + (x.size || 0), 0))) : "";
  $("ctChips").innerHTML = [["all", "All"], ["attention", "Needs attention"], ["yours", "Installed by you"], ["deps", "Dependencies"], ["disabled", "Disabled"]]
    .map(([k, l]) => '<button class="btn xs' + (CT.filter === k ? " gold" : "") + '" data-f="' + k + '" onclick="ctFilterSet(this.dataset.f)">' +
      l + " " + items.filter(x => ctPass(x, k)).length + "</button>").join("");
  const sort = pref("ctSort", "name");
  $("ctSort").value = sort;
  const q = ($("ctFind").value || "").trim().toLowerCase();
  let list = items.map((x, i) => Object.assign({ _i: i }, x))
    .filter(x => ctPass(x, CT.filter) && (!q || (x.name + " " + x.file + " " + x.source).toLowerCase().includes(q)));
  const byName = (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
  list.sort(sort === "recent" ? (a, b) => (b.installed_at || 0) - (a.installed_at || 0)
    : sort === "size" ? (a, b) => (b.size || 0) - (a.size || 0)
    : sort === "source" ? (a, b) => (a.source || "~").localeCompare(b.source || "~") || byName(a, b) : byName);
  if (!items.length) { $("ctList").innerHTML = '<div class="empty">No ' + what + "s yet. Find some under Browse.</div>"; return; }
  if (!list.length) { $("ctList").innerHTML = '<div class="empty">Nothing matches that.</div>'; return; }
  const row = x => {
    const u = CT.updates[x.file], i = x._i;
    return '<div class="item"' + (x.enabled ? "" : ' style="opacity:.7"') + ">" + ctPic(x) + '<div style="flex:1;min-width:0"><div class="ttl">' + esc(x.name) +
      (x.enabled ? "" : '  <span class="pill">disabled</span>') + (x.auto ? '  <span class="pill">dependency</span>' : "") +
      (ctProblem(x) ? '  <span class="pill bad">problem</span>' : "") + (u ? '  <span class="pill warn">update</span>' : "") + "</div>" +
      '<div class="sub">' + esc([x.version, x.source ? SRC_NAMES[x.source] : "added by hand", x.file].filter(Boolean).join(" · ")) + "</div>" +
      (x.problems || []).map(pr => '<div class="sub" style="color:var(--red)">' + esc(pr) + "</div>").join("") +
      (x.notes || []).map(pr => '<div class="sub" style="color:var(--gold)">' + esc(pr) + "</div>").join("") +
      ((x.required_by || []).length ? '<div class="sub">Needed by ' + esc(x.required_by.join(", ")) + "</div>" : "") + "</div>" +
      (u ? '<button class="btn xs gold" data-i="' + i + '" onclick="ctUpdate(+this.dataset.i)">Update to ' + esc(u.latest) + "</button>" : "") +
      '<button class="btn xs" data-i="' + i + '" onclick="ctToggle(+this.dataset.i)">' + (x.enabled ? "Disable" : "Enable") + "</button>" +
      (x.url ? '<a class="btn xs" href="' + esc(x.url) + '" target="_blank" rel="noopener">Page</a>' : "") +
      '<button class="btn xs danger" data-i="' + i + '" onclick="ctRemove(+this.dataset.i)">Remove</button></div>';
  };
  if (CT.filter !== "all") { $("ctList").innerHTML = list.map(row).join(""); return; }
  // Everything, in the order it deserves attention.
  const attn = x => ctProblem(x) || !!CT.updates[x.file];
  const parts = [["Needs attention", list.filter(attn)], ["Installed by you", list.filter(x => !attn(x) && !x.auto && x.enabled)],
                 ["Dependencies", list.filter(x => !attn(x) && x.auto && x.enabled)], ["Disabled", list.filter(x => !attn(x) && !x.enabled)]];
  $("ctList").innerHTML = parts.filter(p => p[1].length).map(([t, xs]) =>
    '<div class="grphead">' + esc(t) + " · " + xs.length + "</div>" + xs.map(row).join("")).join("");
}
async function ctChooseVersion(h) {
  let j;
  try {
    j = await api("/api/content/project?sid=" + encodeURIComponent(ctSid()) + "&source=" + encodeURIComponent(h.source) +
      "&id=" + encodeURIComponent(h.id));
  } catch (e) { toast(e.message, "bad"); return; }
  const p = CT.project = j.project;
  const vs = p.versions || [];
  if (!vs.length) { toast("No versions are published.", "bad"); return; }
  const first = vs.find(v => v.compatible) || vs[0];
  CT.pick = { source: h.source, project_id: p.id, version_id: first.id, name: p.name };
  const shots = (p.gallery || []).slice(0, 3);
  modal("Install " + p.name,
    '<div class="hint">' + esc(p.description) + "</div>" +
    (shots.length ? '<div class="shots">' + shots.map(g => '<a href="' + esc(g.url) + '" target="_blank" rel="noopener"><img src="' +
      esc(g.url) + '" alt="' + esc(g.title || "") + '" title="' + esc(g.title || "") + '" loading="lazy"></a>').join("") + "</div>" : "") +
    '<label class="fld"><span>Version</span><select class="inp" id="ctVer" onchange="ctPreview(this.value)">' +
    vs.map(v => '<option value="' + esc(v.id) + '"' + (v.id === first.id ? " selected" : "") + ">" + esc(v.number) +
      (v.channel !== "release" ? " (" + esc(v.channel) + ")" : "") + (v.compatible ? "" : " — does not fit") + "</option>").join("") +
    "</select></label>" + '<div id="ctPlan"><div class="hint">working out what it needs…</div></div>' +
    (h.source === "spigot" ? '<div class="hint">' + esc(p.body || "") + "</div>" : ""),
    [["Cancel", "closeModal()"], ["Install", "ctDoInstall()", "gold"]]);
  ctPreview(first.id);
}
async function ctPreview(vid) {
  CT.pick.version_id = vid;
  try {
    const pl = await api("/api/content/plan", { sid: ctSid(), items: [{ source: CT.pick.source, project_id: CT.pick.project_id, version_id: vid }] });
    if ($("ctPlan")) $("ctPlan").innerHTML = planHtml(pl);
  } catch (e) { if ($("ctPlan")) $("ctPlan").innerHTML = '<div class="hint" style="color:var(--red)">' + esc(e.message) + "</div>"; }
}
async function ctDoInstall() {
  const it = CT.pick;
  closeModal();
  toast("Installing " + it.name + " — the console has the detail.");
  try {
    const j = await act("/api/content/install", { sid: ctSid(), items: [it] });
    ((j.report || {}).warnings || []).forEach(w => toast(w));
  } catch (e) { /* toasted */ }
  ctLoad();
}
async function ctCheckUpdates() {
  $("ctCount").textContent = "checking for updates…";
  try {
    const j = await api("/api/content/updates?sid=" + encodeURIComponent(ctSid()));
    CT.updates = {};
    (j.items || []).forEach(u => { CT.updates[u.file] = u; });
    (j.errors || []).forEach(e => toast(e, "bad"));
    if (!(j.items || []).length) toast("Everything is up to date.", "good");
  } catch (e) { toast(e.message, "bad"); }
  ctLoad();
}
async function ctUpdate(i) {
  const x = CT.items[i];
  try { await act("/api/content/update", { sid: ctSid(), files: [x.file] }); delete CT.updates[x.file]; } catch (e) { /* toasted */ }
  ctLoad();
}
async function ctUpdateAll() {
  try { await act("/api/content/update", { sid: ctSid() }); CT.updates = {}; } catch (e) { /* toasted */ }
  ctLoad();
}
async function ctToggle(i) {
  try { await act("/api/content/toggle", { sid: ctSid(), file: CT.items[i].file }, null); } catch (e) { /* toasted */ }
  ctLoad();
}
function ctRemove(i) {
  const x = CT.items[i];
  const need = x.required_by || [];
  confirmBox("Remove " + x.name + "?",
    (need.length ? need.join(", ") + (need.length === 1 ? " needs" : " need") + " it and will stop working. " : "") +
    "This deletes " + x.file + " from " + CT.profile.dir + "/. Its config folder is kept.",
    async () => {
      try { await act("/api/content/remove", { sid: ctSid(), file: x.file }); delete CT.updates[x.file]; } catch (e) { /* toasted */ }
      ctLoad();
    });
}

/* ---------------- create server ---------------- */
let WZ = null;
const WZ_STEPS = ["Software", "Mods & plugins", "Settings", "Create"];
function wzDefaults() {
  return { step: 0, catalog: null, snapshots: false, mc: "", software: "", build: "", builds: [], java: null, javaNeed: 0,
           selection: [], dirTouched: false, job: "", f: { name: "My server", dir: "", port: "25565", min_ram: "1G", max_ram: "4G",
           java: "auto", extra: "", motd: "A Minecraft Server", "max-players": "20", "view-distance": "10", "level-seed": "",
           difficulty: "easy", gamemode: "survival", pvp: true, hardcore: false, "white-list": false, "online-mode": true,
           "enable-command-block": false, eula: false } };
}
async function wzOpen() {
  WZ = wzDefaults();
  $("srvHead").style.display = "none";
  $("srvTools").style.display = "none";
  $("srvList").style.display = "none";
  $("srvEditCard").style.display = "none";
  $("wizard").style.display = "";
  $("wizard").innerHTML = '<div class="card"><div class="card-b"><div class="empty">Asking Mojang, PaperMC, Fabric, Forge, NeoForge and the rest what they publish…</div></div></div>';
  await wzCatalog();
  wzGo(0);
}
function wzClose() {
  WZ = null;
  $("wizard").style.display = "none";
  $("wizard").innerHTML = "";
  $("srvHead").style.display = "";
  $("srvTools").style.display = "";
  $("srvList").style.display = "";
  loadServers();
}
async function wzCatalog() {
  try { WZ.catalog = await api("/api/create/catalog" + (WZ.snapshots ? "?snapshots=1" : "")); }
  catch (e) { $("wizard").innerHTML = '<div class="card"><div class="card-b"><div class="empty">Could not load the version list: ' + esc(e.message) + "</div></div></div>"; return; }
  Object.entries(WZ.catalog.errors || {}).forEach(([sw, err]) => toast(swName(sw) + " could not be reached: " + err, "bad"));
  if (!WZ.mc || !WZ.catalog.versions.some(v => v.id === WZ.mc)) WZ.mc = WZ.catalog.latest;
  const used = WZ.catalog.used_ports || [];
  let port = 25565;
  while (used.includes(port)) port++;
  WZ.f.port = String(port);
  const ram = WZ.catalog.ram_mb || 0;
  if (ram) WZ.f.max_ram = ram >= 16000 ? "6G" : ram >= 8000 ? "4G" : "2G";
  wzSuggestDir();
}
function wzSlug(name) { return (String(name || "").trim().replace(/\s+/g, "-").replace(/[^A-Za-z0-9._-]/g, "")) || "server"; }
function wzSuggestDir() {
  if (WZ.dirTouched || !WZ.catalog || !WZ.catalog.default_dir) return;
  const root = WZ.catalog.default_dir;
  const sep = root.includes("\\") ? "\\" : "/";
  WZ.f.dir = root.replace(/[\\/]+$/, "") + sep + wzSlug(WZ.f.name);
}
function wzSw(id) { return ((WZ.catalog || {}).software || []).find(x => x.id === id) || {}; }
function wzCollect() {
  document.querySelectorAll("#wizard [data-wf]").forEach(el => {
    WZ.f[el.dataset.wf] = el.type === "checkbox" ? el.checked : el.value;
  });
}
function wzGo(step) {
  if (WZ.step === 2) wzCollect();
  if (step < 0) { wzClose(); return; }
  WZ.step = Math.max(0, Math.min(3, step));
  const kind = wzSw(WZ.software).content;
  const names = WZ_STEPS.map((n, i) => i === 1 && WZ.software ? (kind === "mod" ? "Mods" : kind === "plugin" ? "Plugins" : n) : n);
  const stepper = names.map((n, i) =>
    '<button class="btn sm' + (i === WZ.step ? " gold" : "") + '"' + (i > WZ.step || WZ.job ? " disabled" : "") +
    ' data-i="' + i + '" onclick="wzGo(+this.dataset.i)">' + (i + 1) + "  " + esc(n) + "</button>").join("");
  const hints = ["Only software with a build for the Minecraft version you pick can be chosen.",
    "Dependencies are added automatically, for the exact version you are creating.",
    "Nothing is downloaded until the last step.", ""];
  $("wizard").innerHTML =
    '<div class="card" style="flex:none"><div class="card-h"><h2>Create a server</h2>' +
    '<div class="path">Lodestone downloads the server software, the right Java and your mods or plugins, then puts it on the shelf.</div>' +
    '<div class="spacer"></div><button class="btn sm" onclick="wzClose()">Cancel</button></div>' +
    '<div class="card-b"><div class="row wrap">' + stepper + "</div></div></div>" +
    '<div id="wzBody"></div>' +
    '<div class="card" style="flex:none" id="wzFoot"><div class="card-h"><div class="hint">' + esc(hints[WZ.step]) + '</div><div class="spacer"></div>' +
    '<button class="btn" onclick="wzGo(WZ.step-1)">' + (WZ.step === 0 ? "Cancel" : "Back") + "</button>" +
    '<button class="btn gold" id="wzNext" onclick="wzNext()">' + (WZ.step === 3 ? "Create server" : "Next") + "</button></div></div>";
  [wzSoftware, wzContent, wzSettings, wzReview][WZ.step]();
}
async function wzNext() {
  if (WZ.step === 0) {
    if (!WZ.software) { toast("Pick the server software.", "bad"); return; }
    if (!WZ.build) { toast("Wait for the builds to load, or pick one.", "bad"); return; }
    try { WZ.java = await api("/api/java?mc=" + encodeURIComponent(WZ.mc) + "&software=" + encodeURIComponent(WZ.software)); }
    catch (e) { WZ.java = null; toast("Could not look for Java: " + e.message, "bad"); }
    wzGo(1);
  } else if (WZ.step === 1) {
    wzGo(2);
  } else if (WZ.step === 2) {
    wzCollect();
    try {
      const j = await api("/api/create/validate", wzSpec());
      (j.warnings || []).forEach(w => toast(w));
    } catch (e) { toast(e.message, "bad"); return; }
    if (!WZ.f.eula) toast("The EULA is not ticked: the server will be created, but will not start until you accept it.");
    wzGo(3);
  } else {
    wzCreate();
  }
}
function wzSoftware() {
  const v = (WZ.catalog.versions || []).find(x => x.id === WZ.mc) || { software: [] };
  const tiles = (WZ.catalog.software || []).map(sw => {
    const ok = v.software.includes(sw.id);
    const kind = sw.content === "mod" ? "Mods" : sw.content === "plugin" ? "Plugins" : "No mods or plugins";
    return '<div class="ev' + (sw.id === WZ.software ? " on" : "") + '" style="' + (ok ? "cursor:pointer" : "opacity:.45;cursor:not-allowed") +
      '" data-sw="' + sw.id + '"' + (ok ? ' onclick="wzPickSw(this.dataset.sw)"' : "") + '><span class="tick"></span>' +
      '<img class="art" src="/api/art/' + sw.id + '.png?size=64" alt=""><div>' +
      '<div class="evn">' + esc(sw.name) + '</div><div class="evd">' + (ok ? esc(kind) : "No build for " + esc(WZ.mc)) + " · " + esc(sw.about) + "</div></div></div>";
  }).join("");
  $("wzBody").innerHTML =
    '<div class="card" style="flex:none"><div class="card-h"><h2>Minecraft version</h2><div class="path">releases only, unless you ask for snapshots</div>' +
    '<div class="spacer"></div><label class="tog"><input type="checkbox" onchange="wzSnaps(this.checked)"' + (WZ.snapshots ? " checked" : "") +
    '><span class="box"></span><span class="lbl">show snapshots</span></label></div><div class="card-b">' +
    '<select class="inp" style="width:240px" onchange="wzPickMc(this.value)">' + wzVersionOptions() + "</select>" +
    '<div class="hint" style="margin-top:6px">' + v.software.length + " of 8 server types have a build for " + esc(WZ.mc) + ".</div></div></div>" +
    '<div class="card" style="flex:none"><div class="card-h"><h2>Server software</h2><div class="path">what runs the server — and decides whether it takes mods or plugins</div></div>' +
    '<div class="card-b"><div class="evgrid">' + tiles + "</div></div></div>" +
    '<div id="wzBuilds"></div>';
  if (WZ.software) wzBuilds();
}
// Grouped by line - 26.x, 1.21, 1.20 … - so a long list reads like a table of contents.
function wzLine(mc) {
  const p = String(mc).split(/[.-]/);
  return +p[0] >= 26 ? p[0] + ".x" : p.length > 1 ? p[0] + "." + p[1] : mc;
}
function wzVersionOptions() {
  let out = "", line = null;
  (WZ.catalog.versions || []).forEach(x => {
    const l = wzLine(x.id);
    if (l !== line) { out += (line === null ? "" : "</optgroup>") + '<optgroup label="' + esc(l) + '">'; line = l; }
    out += '<option value="' + esc(x.id) + '"' + (x.id === WZ.mc ? " selected" : "") + ">" + esc(x.id) +
      (x.type === "release" ? "" : " (" + esc(x.type) + ")") + "</option>";
  });
  return out + (line === null ? "" : "</optgroup>");
}
async function wzSnaps(on) { WZ.snapshots = on; await wzCatalog(); wzGo(0); }
function wzPickMc(mc) {
  WZ.mc = mc;
  const v = (WZ.catalog.versions || []).find(x => x.id === mc) || { software: [] };
  if (!v.software.includes(WZ.software)) WZ.software = "";
  WZ.build = "";
  WZ.selection = [];
  wzGo(0);
}
function wzPickSw(sw) {
  if (WZ.software !== sw) WZ.selection = [];
  WZ.software = sw;
  WZ.build = "";
  wzGo(0);
}
async function wzBuilds() {
  $("wzBuilds").innerHTML = '<div class="card"><div class="card-b"><div class="hint">Loading ' + esc(swName(WZ.software)) + " builds…</div></div></div>";
  let j;
  try { j = await api("/api/create/builds?software=" + encodeURIComponent(WZ.software) + "&mc=" + encodeURIComponent(WZ.mc)); }
  catch (e) { $("wzBuilds").innerHTML = '<div class="card"><div class="card-b"><div class="hint" style="color:var(--red)">' + esc(e.message) + "</div></div></div>"; WZ.build = ""; return; }
  WZ.builds = j.items || [];
  WZ.javaNeed = j.java;
  if (!WZ.build || !WZ.builds.some(b => b.id === WZ.build)) WZ.build = (WZ.builds.find(b => b.recommended) || WZ.builds[0] || {}).id || "";
  let notes = '<div class="hint" style="margin-top:6px">Minecraft ' + esc(WZ.mc) + " needs Java " + esc(j.java) +
    ". Lodestone finds an installed one, or downloads it for you.</div>";
  if (WZ.software === "spigot")
    notes += '<div class="hint" style="color:var(--gold)">Spigot is not published as a download — BuildTools compiles it on this PC. ' +
      "That takes several minutes and needs Git (BuildTools fetches a portable copy on Windows).</div>";
  $("wzBuilds").innerHTML = '<div class="card" style="flex:none"><div class="card-h"><h2>' + esc(swName(WZ.software)) + " build</h2></div>" +
    '<div class="card-b"><select class="inp" style="width:360px" onchange="WZ.build=this.value">' +
    WZ.builds.map(b => '<option value="' + esc(b.id) + '"' + (b.id === WZ.build ? " selected" : "") + ">" + esc(b.label) +
      (b.recommended ? " — recommended" : "") + "</option>").join("") + "</select>" + notes + "</div></div>";
}
function wzTarget() { return "software=" + encodeURIComponent(WZ.software) + "&mc=" + encodeURIComponent(WZ.mc); }
async function wzContent() {
  const kind = wzSw(WZ.software).content;
  if (!kind) {
    $("wzBody").innerHTML = '<div class="card"><div class="card-b"><div class="empty">' + esc(swName(WZ.software)) +
      " runs no mods or plugins — this step is only for modded and plugin servers. Press Next.</div></div></div>";
    return;
  }
  const noun = kind === "mod" ? "mods" : "plugins";
  const presets = ((WZ.catalog.presets || {})[WZ.software]) || [];
  $("wzBody").innerHTML =
    '<div class="card" style="flex:none"><div class="card-h"><h2>Your ' + noun + '</h2><div class="path">installed into ' +
    (kind === "mod" ? "mods/" : "plugins/") + " when the server is created</div><div class=\"spacer\"></div>" +
    (presets.length ? '<button class="btn sm" onclick="wzPreset()" title="' + esc(presets.map(p => p.name).join(", ")) + '">Add the recommended set</button>' : "") +
    '</div><div class="card-b"><div id="wzSel"></div><div id="wzPlan"></div></div></div>' +
    '<div class="card" style="flex:none"><div class="card-h"><h2>Find ' + noun + '</h2><div class="path">only ' + noun + " for " +
    esc(swName(WZ.software)) + " " + esc(WZ.mc) + ' are offered</div></div><div class="card-b" id="wzBrowse"></div></div>';
  wzSelRender();
  try {
    const j = await api("/api/content/profile?" + wzTarget());
    cbInit("wzBrowse", wzTarget(), j.profile, "wizard");
  } catch (e) { $("wzBrowse").innerHTML = '<div class="empty">' + esc(e.message) + "</div>"; }
}
// The recommended set names projects by slug, search results by id: either counts.
function wzPicked(h) {
  return WZ.selection.some(x => x.source === h.source && (x.project_id === h.id || x.project_id === h.slug));
}
function wzAdd(h) {
  if (wzPicked(h)) return;
  WZ.selection.push({ source: h.source, project_id: h.id, name: h.name });
  wzSelRender();
}
function wzPreset() {
  (((WZ.catalog.presets || {})[WZ.software]) || []).forEach(p => {
    if (!WZ.selection.some(x => x.source === p.source && x.project_id === p.project_id)) WZ.selection.push(p);
  });
  wzSelRender();
  if (CB.wzBrowse) cbRender("wzBrowse");
}
function wzUnselect(i) {
  WZ.selection.splice(i, 1);
  wzSelRender();
  if (CB.wzBrowse) cbRender("wzBrowse");
}
async function wzSelRender() {
  if (!$("wzSel")) return;
  if (!WZ.selection.length) {
    $("wzSel").innerHTML = '<div class="hint">Nothing yet — add some below, or skip this step. You can always add more later from the Mods &amp; plugins tab.</div>';
    $("wzPlan").innerHTML = "";
    return;
  }
  $("wzSel").innerHTML = '<div class="chips">' + WZ.selection.map((x, i) =>
    '<span class="chip">' + esc(x.name) + " · " + esc(SRC_NAMES[x.source] || x.source) +
    ' <a href="#" data-i="' + i + '" onclick="event.preventDefault();wzUnselect(+this.dataset.i)">✕</a></span>').join("") + "</div>";
  const seq = WZ.planSeq = (WZ.planSeq || 0) + 1;
  $("wzPlan").innerHTML = '<div class="hint">checking versions and dependencies…</div>';
  try {
    const pl = await api("/api/content/plan", { software: WZ.software, mc: WZ.mc,
      items: WZ.selection.map(x => ({ source: x.source, project_id: x.project_id })) });
    if (WZ && seq === WZ.planSeq && $("wzPlan")) $("wzPlan").innerHTML = '<div class="hint" style="margin-top:8px">What will be installed</div>' + planHtml(pl);
  } catch (e) { if ($("wzPlan")) $("wzPlan").innerHTML = '<div class="hint" style="color:var(--red)">' + esc(e.message) + "</div>"; }
}
function wzSettings() {
  const f = WZ.f;
  const j = WZ.java || { items: [], need: WZ.javaNeed, pick: "" };
  const pick = (j.items || []).find(x => x.path === j.pick);
  const inp = (k, label, hint, attrs) => '<label class="fld"><span>' + label + '</span><input class="inp" data-wf="' + k + '" value="' +
    esc(f[k]) + '"' + (attrs || "") + ">" + (hint ? '<div class="hint">' + hint + "</div>" : "") + "</label>";
  const sel = (k, label, opts) => '<label class="fld"><span>' + label + '</span><select class="inp" data-wf="' + k + '">' +
    opts.map(o => '<option value="' + esc(o[0]) + '"' + (String(f[k]) === o[0] ? " selected" : "") + ">" + esc(o[1]) + "</option>").join("") + "</select></label>";
  const tog = (k, label) => '<label class="tog" style="margin:4px 14px 8px 0"><input type="checkbox" data-wf="' + k + '"' + (f[k] ? " checked" : "") +
    '><span class="box"></span><span class="lbl">' + label + "</span></label>";
  const javaOpts = [["auto", pick ? "Automatic — Java " + pick.major + " (" + pick.path + ")" : "Automatic — download Java " + j.need + " (none installed fits)"],
    ["download", "Download Java " + j.need + " (Eclipse Temurin) into Lodestone's folder"]]
    .concat((j.items || []).map(x => [x.path, "Java " + x.major + " · " + x.path + (x.fits ? "" : " — " + x.note)]));
  const ram = (WZ.catalog || {}).ram_mb || 0;
  $("wzBody").innerHTML =
    '<div class="card" style="flex:none"><div class="card-h"><h2>Server</h2></div><div class="card-b">' +
    '<div class="grid2">' + inp("name", "Name", "", ' oninput="WZ.f.name=this.value;wzSuggestDir();if(!WZ.dirTouched){const d=document.querySelector(&quot;#wizard [data-wf=dir]&quot;);if(d)d.value=WZ.f.dir;}"') +
    inp("port", "Port", "Each server that runs at the same time needs its own.") + "</div>" +
    '<label class="fld"><span>Install folder</span><div class="row"><input class="inp" data-wf="dir" value="' + esc(f.dir) +
    '" oninput="WZ.dirTouched=true"><button class="btn sm" onclick="wzPickDir()">Browse</button></div>' +
    '<div class="hint">A new or empty folder. Nothing that is already there gets overwritten.</div></label></div></div>' +
    '<div class="card" style="flex:none"><div class="card-h"><h2>Memory and Java</h2><div class="path">' +
    (ram ? "this PC has " + (ram / 1024).toFixed(1) + " GB of memory" : "") + '</div></div><div class="card-b">' +
    '<div class="grid2">' + inp("min_ram", "Minimum RAM") + inp("max_ram", "Maximum RAM",
      WZ.software === "forge" || WZ.software === "neoforge" ? "Modpacks want 6-8G; a light one 4G." : "2-4G is plenty for a few friends.") + "</div>" +
    sel("java", "Java", javaOpts) + (j.need ? '<div class="hint" style="margin:-4px 0 10px">Minecraft ' + esc(WZ.mc) + " needs Java " + j.need + ".</div>" : "") +
    inp("extra", "Extra JVM flags", "Passed straight to Java, like the Extra JVM flags in a server's settings.") + "</div></div>" +
    '<div class="card" style="flex:none"><div class="card-h"><h2>Gameplay</h2><div class="path">written to server.properties — change any of it later in Properties</div></div><div class="card-b">' +
    inp("motd", "MOTD", "The line under the name in the server list. &amp; colour codes work.") +
    '<div class="grid2">' + sel("difficulty", "Difficulty", [["peaceful", "Peaceful"], ["easy", "Easy"], ["normal", "Normal"], ["hard", "Hard"]]) +
    sel("gamemode", "Game mode", [["survival", "Survival"], ["creative", "Creative"], ["adventure", "Adventure"], ["spectator", "Spectator"]]) + "</div>" +
    '<div class="grid3">' + inp("max-players", "Max players") + inp("view-distance", "View distance") + inp("level-seed", "World seed", "Blank for random.") + "</div>" +
    '<div class="row wrap">' + tog("pvp", "PvP") + tog("hardcore", "Hardcore") + tog("enable-command-block", "Command blocks") +
    tog("white-list", "Whitelist") + tog("online-mode", "Online mode") + "</div>" +
    '<div class="hint">Online mode checks that players own Minecraft. Leave it on; for testing with non-premium accounts use Offline mode in the Network tab after creating.</div></div></div>' +
    '<div class="card" style="flex:none"><div class="card-h"><h2>Minecraft EULA</h2><div class="path">Mojang\'s licence — a server does not start until it is accepted</div>' +
    '<div class="spacer"></div><a class="btn sm" href="https://aka.ms/MinecraftEULA" target="_blank" rel="noopener">Read the EULA</a></div>' +
    '<div class="card-b">' + tog("eula", "I accept the Minecraft EULA") +
    '<div class="hint">Lodestone writes eula=true only if you tick this yourself.</div></div></div>';
}
async function wzPickDir() {
  try {
    const j = await api("/api/pick?kind=folder");
    if (j.path) {
      WZ.dirTouched = true;
      const d = document.querySelector("#wizard [data-wf=dir]");
      if (d) d.value = j.path.replace(/[\\/]+$/, "") + (j.path.includes("\\") ? "\\" : "/") + wzSlug(WZ.f.name);
    }
  } catch (e) { toast(e.message, "bad"); }
}
function wzSpec() {
  const f = WZ.f;
  const props = {};
  ["motd", "max-players", "view-distance", "level-seed", "difficulty", "gamemode", "pvp", "hardcore", "white-list",
   "online-mode", "enable-command-block"].forEach(k => { props[k] = f[k]; });
  return { name: f.name, software: WZ.software, mc: WZ.mc, build: WZ.build, dir: f.dir, port: f.port, min_ram: f.min_ram,
           max_ram: f.max_ram, java: f.java, extra: f.extra, eula: !!f.eula, properties: props,
           content: WZ.selection.map(x => ({ source: x.source, project_id: x.project_id, name: x.name })) };
}
function wzReview() {
  const f = WZ.f;
  const b = WZ.builds.find(x => x.id === WZ.build) || {};
  const row = (k, v, col) => '<div class="krow"><span class="k">' + esc(k) + '</span><span' + (col ? ' style="color:var(--' + col + ')"' : "") + ">" + esc(v) + "</span></div>";
  $("wzBody").innerHTML = '<div class="card" style="flex:none"><div class="card-h"><img class="sicon" src="/api/art/' + esc(WZ.software) +
    '.png?size=64" alt="" style="width:34px;height:34px;margin-right:10px"><h2>Ready to create ' + esc(f.name) + '</h2>' +
    '<div class="path">everything is downloaded and verified before it is used</div></div><div class="card-b"><div class="klist">' +
    row("Software", swName(WZ.software) + " — " + (b.label || WZ.build)) + row("Minecraft", WZ.mc) + row("Folder", f.dir) +
    row("Java", f.java === "auto" ? "Automatic" : f.java === "download" ? "Download Java " + ((WZ.java || {}).need || "") : f.java) +
    row("Memory", f.min_ram + " – " + f.max_ram) + row("Port", f.port) +
    row(wzSw(WZ.software).content === "mod" ? "Mods" : "Plugins", WZ.selection.length ? WZ.selection.map(x => x.name).join(", ") : "none") +
    row("EULA", f.eula ? "accepted" : "not accepted — accept it before the first start", f.eula ? "" : "gold") + "</div></div></div>";
}
async function wzCreate() {
  $("wzNext").disabled = true;
  let j;
  try { j = await api("/api/create", wzSpec()); }
  catch (e) { toast(e.message, "bad"); $("wzNext").disabled = false; return; }
  WZ.job = j.job;
  $("wzFoot").style.display = "none";
  $("wzBody").innerHTML = '<div class="card" style="flex:none"><div class="card-h"><h2 id="wzTitle">Creating ' + esc(WZ.f.name) + '…</h2>' +
    '<div class="spacer"></div><span class="pill" id="wzPct">0%</span></div><div class="card-b">' +
    '<div style="height:8px;background:var(--panel2,#2a2a35);border-radius:4px;overflow:hidden"><div id="wzBar" style="height:8px;width:0;background:var(--green);transition:width .3s"></div></div>' +
    '<div class="items" id="wzSteps" style="margin-top:10px"></div><div id="wzOutcome"></div></div></div>' +
    '<div class="card"><div class="card-h"><h2>Log</h2></div><div class="card-b"><pre class="code" id="wzLog" style="max-height:320px;overflow:auto;white-space:pre-wrap"></pre></div></div>';
  let since = 0;
  const lines = [];
  while (WZ && WZ.job) {
    let job;
    try { job = (await api("/api/jobs?id=" + encodeURIComponent(WZ.job) + "&since=" + since)).job; }
    catch (e) { await new Promise(r => setTimeout(r, 2000)); continue; }
    if (!WZ) return;
    since = job.next;
    lines.push(...(job.log || []));
    if (lines.length > 400) lines.splice(0, lines.length - 400);
    const log = $("wzLog");
    log.textContent = lines.join("\n");
    log.scrollTop = log.scrollHeight;
    $("wzBar").style.width = (job.pct || 0) + "%";
    $("wzPct").textContent = (job.pct || 0) + "%";
    const pill = { done: ["on", "done"], running: ["warn", "working"], failed: ["bad", "failed"], warn: ["warn", "with warnings"] };
    $("wzSteps").innerHTML = job.steps.map(s => '<div class="item"><div style="flex:1"><div class="ttl">' + esc(s.label) + "</div>" +
      (s.detail ? '<div class="sub">' + esc(s.detail) + "</div>" : "") + '</div><span class="pill ' + ((pill[s.state] || [""])[0]) + '">' +
      esc((pill[s.state] || ["", "waiting"])[1]) + "</span></div>").join("");
    if (job.state === "running") { await new Promise(r => setTimeout(r, 800)); continue; }
    const warn = (job.warnings || []).map(w => '<div class="hint" style="color:var(--gold)">' + esc(w) + "</div>").join("");
    if (job.state === "done") {
      $("wzTitle").textContent = WZ.f.name + " is ready";
      WZ.created = job.result.server_id;
      $("wzOutcome").innerHTML = warn + '<div class="row" style="margin-top:10px">' +
        (WZ.f.eula ? '<button class="btn gold" onclick="wzOpenCreated(true)">Start it</button>' : "") +
        '<button class="btn" onclick="wzOpenCreated(false)">Open it</button>' +
        '<button class="btn" onclick="wzClose()">Back to servers</button></div>';
      toast(WZ.f.name + " created.", "good");
      loadShelf();
    } else {
      $("wzTitle").textContent = "Could not create " + WZ.f.name;
      $("wzOutcome").innerHTML = warn + '<div class="hint" style="color:var(--red);font-size:13px;margin-top:8px">' + esc(job.error) + "</div>" +
        '<div class="hint">Anything half-made was removed again, so you can change a setting and try once more.</div>' +
        '<div class="row" style="margin-top:10px"><button class="btn gold" onclick="WZ.job=&quot;&quot;;wzGo(2)">Change settings</button>' +
        '<button class="btn" onclick="wzClose()">Back to servers</button></div>';
    }
    break;
  }
}
async function wzOpenCreated(start) {
  const id = WZ.created;
  wzClose();
  await switchServer(id);
  showView("console");
  if (start) { try { await act("/api/start", { sid: id }, "Starting."); } catch (e) { /* toasted */ } }
}

async function loadServers() {
  await loadShelf();
  srvRender();
}
function pref(key, def) { try { return localStorage.getItem("lodestone." + key) || def; } catch (e) { return def; } }
function prefSet(key, v) { try { localStorage.setItem("lodestone." + key, v); } catch (e) { /* private window */ } }
function srvSortSet(v) { prefSet("srvSort", v); srvRender(); }
function srvFilterSet(v) { prefSet("srvFilter", v); srvRender(); }
// A server's picture: the Minecraft item or block picked for it (or the one its
// software suggests), drawn by the engine from the game's own textures.
function srvPic(s, size) {
  const src = s.icon ? "/api/art/icon/" + s.icon + ".png?size=" + Math.min(256, Math.ceil(size * 2 / 16) * 16)
    : s.has_icon ? "/api/icon?sid=" + encodeURIComponent(s.id)
    : s.software ? "/api/art/" + encodeURIComponent(s.software) + ".png?size=64" : "";
  return src ? '<img class="sicon" src="' + src + '" alt="" style="width:' + size + "px;height:" + size + 'px">' : "";
}
function srvRender() {
  const all = SERVERS || [];
  const filter = pref("srvFilter", "all"), sort = pref("srvSort", "status");
  $("srvSort").value = sort;
  const pass = (s, f) => f === "running" ? s.running : f === "local" ? s.kind === "local" : f === "remote" ? s.kind !== "local" : true;
  $("srvChips").innerHTML = [["all", "All"], ["running", "Running"], ["local", "Local"], ["remote", "Remote"]].map(([k, l]) =>
    '<button class="btn xs' + (filter === k ? " gold" : "") + '" data-f="' + k + '" onclick="srvFilterSet(this.dataset.f)">' +
    l + " " + all.filter(s => pass(s, k)).length + "</button>").join("");
  const q = ($("srvFind").value || "").trim().toLowerCase();
  let list = all.filter(s => pass(s, filter) && (!q || (s.name + " " + s.where + " " + swName(s.software) + " " + (s.mc || "")).toLowerCase().includes(q)));
  const byName = (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: "base" });
  list.sort(sort === "newest" ? (a, b) => (b.created || 0) - (a.created || 0) || byName(a, b)
    : sort === "port" ? (a, b) => (a.port == null ? 1e9 : a.port) - (b.port == null ? 1e9 : b.port) || byName(a, b) : byName);
  const key = s => sort === "status" ? (s.running ? "Running" : "Not running")
    : sort === "software" ? (s.software ? swName(s.software) : s.kind === "local" ? "Added by hand" : "Remote")
    : sort === "kind" ? (s.kind === "local" ? "On this PC" : "Remote · SFTP") : "";
  const groups = [];
  list.forEach(s => { const k = key(s); let g = groups.find(x => x.k === k); if (!g) groups.push(g = { k, items: [] }); g.items.push(s); });
  const rank = k => ["Running", "On this PC"].includes(k) ? 0 : ["Not running", "Remote · SFTP", "Added by hand", "Remote"].includes(k) ? 2 : 1;
  groups.sort((a, b) => rank(a.k) - rank(b.k) || a.k.localeCompare(b.k));
  $("srvList").innerHTML = !all.length
    ? '<div class="empty">No servers yet. <a href="#" onclick="event.preventDefault();wzOpen()">Create one</a> — Lodestone downloads everything it needs.</div>'
    : !list.length ? '<div class="empty">No server matches that.</div>'
    : groups.map(g => (g.k ? '<div class="grphead">' + esc(g.k) + " · " + g.items.length + "</div>" : "") + g.items.map(srvItem).join("")).join("");
}
function srvItem(s) {
  return '<div class="item' + (s.active ? " active" : "") + '">' + srvPic(s, 40) + '<div style="flex:1">' +
    '<div class="ttl">' + esc(s.name) + (s.active ? '  <span class="pill on">active</span>' : "") +
      (s.running ? '  <span class="pill on">running</span>' : "") + "</div>" +
    '<div class="sub">' + (s.kind === "local" ? "local" : "remote / sftp") +
      (s.software ? " · " + esc(swName(s.software)) + " " + esc(s.mc) : "") + (s.port ? " · port " + s.port : "") + " · " + esc(s.where) +
      (s.rcon ? " · rcon" : "") + (s.hooks ? " · " + s.hooks + " webhook" + (s.hooks > 1 ? "s" : "") : "") + "</div></div>" +
    (s.kind === "local"
      ? (s.running
          ? '<button class="btn xs" data-sid="' + s.id + '" onclick="srvStop(this.dataset.sid)">Stop</button>'
          : '<button class="btn xs" data-sid="' + s.id + '" onclick="srvStart(this.dataset.sid)">Start</button>')
      : "") +
    (s.kind === "local"
      ? '<button class="btn xs" data-sid="' + s.id + '" onclick="installVia(this.dataset.sid)" title="ViaVersion, ViaBackwards and ViaRewind">' +
        ((s.via && s.via.installed && s.via.installed.length) ? "Update Via" : "Install Via") + "</button>" +
        ((s.via && s.via.preset_ok)
          ? '<button class="btn xs" data-sid="' + s.id + '" onclick="installPack(this.dataset.sid)" title="' +
            esc((s.via.preset || []).join(", ")) + '">Install pack</button>'
          : "")
      : "") +
    '<button class="btn xs gold" onclick="openInstance(\'' + s.id + '\')" title="Console, files, mods and the rest of this server">Open</button>' +
    '<button class="btn xs" onclick="editServer(\'' + s.id + '\')">Edit</button>' +
    '<button class="btn xs" onclick="dupServer(\'' + s.id + '\')">Duplicate</button>' +
    '<button class="btn xs danger" onclick="delServer(\'' + s.id + "','" + esc(s.name) + '\')">Remove</button></div>';
}
// ViaVersion and friends. The button is offered for every local server, even
// the ones that cannot take plugins, because "why not?" is a better answer than
// a button that is silently missing.
async function installVia(id) {
  const s = (SERVERS || []).find(x => x.id === id) || {};
  const v = s.via || {};
  if (v.available === false || v.compatible === false) {
    modal("ViaVersion cannot be installed here",
      '<div class="hint" style="font-size:13px;line-height:1.7">' +
      esc(v.reason || "This server does not load Bukkit plugins.") +
      (v.flavour ? "<br><br>Detected as <b>" + esc(v.flavour) + "</b>" +
        (v.how ? " — " + esc(v.how) : "") + "." : "") +
      "<br><br>Via runs on Bukkit servers (Paper, Spigot, Purpur, CraftBukkit " +
      "and their forks) and on proxies (Velocity, Waterfall, BungeeCord). " +
      "Modded and vanilla servers have no plugin loader for it.</div>",
      [["Close", "closeModal()"]]);
    return;
  }
  toast("Fetching ViaVersion, ViaBackwards and ViaRewind — the console has the detail.", "");
  let j;
  try { j = await act("/api/plugins/via", { sid: id }, null); } catch (e) { return; }
  const r = j.report || {};
  toast(r.installed + " installed, " + r.updated + " updated, " +
        r.skipped + " already current", "good");
  loadServers();
}

// The whole recommended set for this platform, rather than Via alone.
async function installPack(id) {
  const s = (SERVERS || []).find(x => x.id === id) || {};
  const v = s.via || {};
  if (!v.preset_ok) {
    modal("Nothing to install here",
      '<div class="hint" style="font-size:13px;line-height:1.7">' +
      esc(v.preset_reason || v.reason || "This server takes neither plugins nor mods.") +
      "</div>", [["Close", "closeModal()"]]);
    return;
  }
  toast("Installing " + (v.preset || []).length + " packages into " +
        (v.target_dir || "plugins") + "/ — the console has the detail.", "");
  let j;
  try { j = await act("/api/plugins/pack", { sid: id }, null); } catch (e) { return; }
  const r = j.report || {};
  toast(r.installed + " installed, " + r.updated + " updated, " +
        r.skipped + " skipped", "good");
  loadServers();
}

// Start and stop any server from the list, not just the one on screen -
// several can be up at once now, and switching to each just to press Start
// would be a poor way to run two.
async function srvStart(id) {
  try { await act("/api/start", { sid: id }, "Starting."); } catch (e) { return; }
  setTimeout(loadServers, 900);
}

async function srvStop(id) {
  try { await act("/api/stop", { sid: id }, "Stopping."); } catch (e) { return; }
  setTimeout(loadServers, 1500);
}

let RAM = null;

async function loadRam() {
  let j;
  try { j = await api("/api/ram"); }
  catch (e) { $("ramOut").innerHTML = '<div class="hint">' + esc(e.message) + "</div>"; return; }
  RAM = j;
  if (j.supported === false) {
    $("ramSummary").textContent = "not available";
    $("ramOut").innerHTML = '<div class="hint">' + esc(j.error || "Windows only.") + "</div>";
    return;
  }
  const gb = mb => (mb / 1024).toFixed(1) + " GB";
  $("ramSummary").textContent = gb(j.free_mb) + " free of " + gb(j.total_mb) +
    "  ·  " + j.percent + "% used";
  let h = '<div class="hint" style="margin-bottom:9px">Closing everything listed as ' +
    "closeable would free about <b>" + gb(j.reclaimable_mb) + "</b>. " +
    "A 100+ mod server wants 6–8 GB.</div>";
  h += '<div class="items">';
  (j.processes || []).forEach(p => {
    h += '<div class="item"><div style="flex:1">' +
      '<div class="ttl">' + esc(p.label || p.name) +
        (p.holds_work ? '  <span class="pill">may have unsaved work</span>' : "") +
        (p.closeable ? "" : '  <span class="pill">protected</span>') + "</div>" +
      '<div class="sub">' + (p.mb >= 1024 ? (p.mb / 1024).toFixed(1) + " GB" : p.mb + " MB") +
        " · " + p.pids.length + " process" + (p.pids.length > 1 ? "es" : "") +
        (p.note ? " · " + esc(p.note) : "") + "</div></div>" +
      (p.closeable
        ? '<button class="btn xs" data-app="' + esc(p.name) +
          '" onclick="ramClose(this.dataset.app)">Close</button>'
        : "") + "</div>";
  });
  h += "</div>";
  $("ramOut").innerHTML = h;
}

async function ramClose(name) {
  const p = ((RAM || {}).processes || []).find(x => x.name === name) || {};
  const warn = p.holds_work
    ? "<br><br><b>" + esc(p.label || name) + " may have unsaved work.</b> This sends a " +
      "normal close request, so it can still prompt you — but save first to be sure."
    : "";
  modal("Close " + esc(p.label || name) + "?",
    '<div class="hint" style="font-size:13px;line-height:1.7">This frees about <b>' +
    (p.mb >= 1024 ? (p.mb / 1024).toFixed(1) + " GB" : (p.mb || 0) + " MB") +
    "</b> across " + ((p.pids || []).length) + " process" +
    (((p.pids || []).length) > 1 ? "es" : "") + "." + warn + "</div>",
    [["Cancel", "closeModal()"],
     ["Close it", "closeModal(); ramDoClose('" + esc(name) + "')"]]);
}

async function ramDoClose(name) {
  let j;
  try { j = await act("/api/ram/close", { name: name }, null); } catch (e) { return; }
  toast(j.msg + (j.freed_mb ? "  (+" + j.freed_mb + " MB free)" : ""),
        j.still_running ? "" : "good");
  loadRam();
}

async function doImport() {
  const j = await act("/api/servers/import", {}, null);
  toast(j.msg, j.added && j.added.length ? "good" : "");
  loadServers();
}

// Step inside a server: select it and show its console.
async function openInstance(id) {
  const cur = (SERVERS || []).find(x => x.active);
  if (!cur || cur.id !== id) {
    await act("/api/servers/activate", { id }, null);
    LINES = []; LASTSEQ = 0; renderLog();
    await loadShelf();
  }
  showView("console");
}

// "Add a server I already have": a folder, looked at by the engine, confirmed, opened.
async function addFolder() {
  modal("Add a server you already have",
    '<label class="fld"><span>Server folder</span><input class="inp" id="afPath" placeholder="C:\\Users\\you\\Desktop\\my-server">' +
    '<div class="hint">The folder with the server jar in it. Lodestone works out the jar, version, port and Java.</div></label>' +
    '<div id="afOut"></div>',
    [["Cancel", "closeModal()"], ["Look at it", "afDetect()", "gold"]]);
  setTimeout(() => $("afPath") && $("afPath").focus(), 50);
}
let AF = null;
async function afDetect() {
  const path = ($("afPath").value || "").trim();
  if (!path) return;
  let d;
  try { d = await api("/api/servers/detect", { path }); } catch (e) { toast(e.message, "bad"); return; }
  if (d.existing) { closeModal(); toast(d.existing.name + " is already in your library - opening it."); openInstance(d.existing.id); return; }
  if (!d.found) {
    $("afOut").innerHTML = '<div class="hint" style="color:var(--red);margin-top:8px">' + esc(d.error) + "</div>" +
      (d.empty ? '<div class="row" style="margin-top:8px"><button class="btn sm gold" onclick="closeModal();wzOpen()">Create a new server instead</button></div>' : "");
    return;
  }
  AF = d;
  const ram = ["1G", "2G", "3G", "4G", "6G", "8G", "10G", "12G", "16G"];
  $("afOut").innerHTML = '<div class="item" style="margin-top:10px"><img class="sicon" src="/api/art/' + esc(d.software || "vanilla") +
    '.png?size=64" alt="" style="width:44px;height:44px"><div style="flex:1"><div class="ttl">' + esc((swName(d.software) || "Minecraft server") + " " + (d.mc || "")) +
    '</div><div class="sub">' + esc(d.args_file ? "starts from its args file" : (d.jar.split(/[\\/]/).pop())) + " · port " + d.port +
    (d.has_world ? " · world found" : " · no world yet") + (d.eula ? " · EULA accepted" : " · EULA not accepted yet") + "</div></div></div>" +
    '<div class="grid3"><label class="fld"><span>Name</span><input class="inp" id="afName" value="' + esc(d.name) + '"></label>' +
    '<label class="fld"><span>Memory</span><select class="inp" id="afRam">' + ram.map(r => "<option" + (r === d.max_ram ? " selected" : "") + ">" + r + "</option>").join("") + "</select></label>" +
    '<label class="fld"><span>Java</span><input class="inp" id="afJava" value="' + esc(d.java || "java") + '"><div class="hint">' + esc(d.java_note || "") + "</div></label></div>" +
    (d.notes || []).map(n => '<div class="hint">' + esc(n) + "</div>").join("");
  $("mFoot").innerHTML = '<button class="btn" onclick="closeModal()">Cancel</button><button class="btn gold" onclick="afAdd()">Add and open</button>';
}
async function afAdd() {
  const d = AF;
  let j;
  try {
    j = await act("/api/servers/add-local", { path: d.path, name: $("afName").value, jar: d.jar, args_file: d.args_file,
      software: d.software, mc: d.mc, loader_version: d.loader_version, java: $("afJava").value, max_ram: $("afRam").value }, null);
  } catch (e) { return; }
  closeModal();
  await loadServers();
  await openInstance(j.server.id);
  toast("Added " + j.server.name + (d.eula ? ". Press Start when you are ready." : ". Accept the EULA, then press Start."), "good");
}

async function newServer(kind) {
  const j = await act("/api/servers/save", { kind, name: kind === "local" ? "New local server" : "New remote server" }, "Added.");
  await loadServers();
  editServer(j.server.id);
}
async function editServer(id) {
  const j = await api("/api/servers/get?id=" + encodeURIComponent(id));
  EDITING = j.server;
  const s = EDITING;
  const fld = (group, key, label, hint, type) =>
    '<label class="fld"><span>' + label + '</span><input class="inp" data-g="' + group + '" data-k="' + key +
    '" type="' + (type || "text") + '" value="' + esc(group ? (s[group] || {})[key] : s[key]) + '">' +
    (hint ? '<div class="hint">' + hint + "</div>" : "") + "</label>";
  let body = '<div class="grid2">' +
    '<label class="fld"><span>Name</span><input class="inp" data-k="name" value="' + esc(s.name) + '"></label>' +
    '<label class="fld"><span>Kind</span><select class="inp" data-k="kind" onchange="EDITING.kind=this.value;editServerRender()">' +
    '<option value="local"' + (s.kind === "local" ? " selected" : "") + ">Local — a folder on this machine</option>" +
    '<option value="sftp"' + (s.kind === "sftp" ? " selected" : "") + ">Remote — reached over SFTP</option></select></label></div>";
  // the icon: any Minecraft item or block, typed with suggestions, or blank for automatic
  const look = s.look || {}, row = (SERVERS || []).find(x => x.id === s.id) || {};
  body += '<label class="fld"><span>Icon</span><div class="row"><img id="iconPrev" class="sicon" alt="" style="width:48px;height:48px" src="/api/art/icon/' +
    esc(look.icon || row.icon_auto || "block/lodestone") + '.png?size=96">' +
    '<input class="inp" list="iconList" data-g="look" data-k="icon" value="' + esc(look.icon || "") + '" placeholder="automatic — ' +
    esc(row.icon_auto || "") + '" oninput="iconPrev(this.value)" style="flex:1"></div>' +
    '<div class="hint">Any Minecraft item or block — start typing: diamond, piston, creeper… Blank picks one from what the server runs.</div></label>' +
    '<datalist id="iconList"></datalist>';
  if (s.kind === "local") {
    body += '<div class="grid2">' +
      '<label class="fld"><span>Server folder</span><div class="row"><input class="inp" data-g="local" data-k="cwd" value="' +
        esc(s.local.cwd) + '"><button class="btn sm" onclick="pick(\'folder\',\'local\',\'cwd\')">Browse</button></div></label>' +
      '<label class="fld"><span>Server JAR</span><div class="row"><input class="inp" data-g="local" data-k="jar" value="' +
        esc(s.local.jar) + '"><button class="btn sm" onclick="pick(\'jar\',\'local\',\'jar\')">Browse</button></div></label></div>' +
      '<div class="grid3">' + fld("local", "java", "Java command") +
      fld("local", "min_ram", "Min RAM") + fld("local", "max_ram", "Max RAM") + "</div>" +
      fld("local", "extra", "Extra JVM flags", "Passed straight through, space separated.");
  } else {
    body += '<div class="grid3">' + fld("sftp", "host", "Host") + fld("sftp", "port", "Port") +
      fld("sftp", "user", "Username") + "</div>" +
      '<div class="grid2">' + fld("sftp", "password", "Password", "Stored in plain text in ~/.lodestone.json.", "password") +
      fld("sftp", "root", "Server folder on the host",
          "Where server.properties lives. On Minehut that is usually /server, not /.") + "</div>" +
      '<div class="row"><button class="btn sm" onclick="testSftp()">Test connection</button>' +
      '<span class="hint" id="sftpMsg">Checks each step in turn and tells you which one fails.</span></div>' +
      '<div id="sftpOut"></div>';
  }
  if (s.kind === "sftp") {
    const mh = s.minehut || {};
    body += '<div class="card" style="margin-top:6px"><div class="card-h"><h2>Minehut</h2>' +
      '<div class="path">start, stop and send commands — things SFTP cannot do</div>' +
      '<div class="spacer"></div>' +
      '<label class="tog"><input type="checkbox" data-g="minehut" data-k="enabled"' +
      (mh.enabled ? " checked" : "") + '><span class="box"></span><span class="lbl">Enabled</span></label>' +
      '</div><div class="card-b">' +
      '<label class="fld"><span>Server ID</span><input class="inp" data-g="minehut" data-k="server_id" value="' +
        esc(mh.server_id || "") + '" placeholder="the id in your dashboard URL">' +
        '<div class="hint">It is the long hex string in ' +
        "<code>dashboard.minehut.com/servers/&lt;this&gt;</code> — the same value as your SFTP username.</div></label>" +
      '<div class="grid2">' +
      '<label class="fld"><span>Auth token</span><input class="inp" type="password" data-g="minehut" data-k="token" placeholder="' +
        (mh.has_token ? "saved — type to replace" : "minehut_auth_token") + '"></label>' +
      '<label class="fld"><span>Session id</span><input class="inp" type="password" data-g="minehut" data-k="session_id" placeholder="' +
        (mh.has_session ? "saved — type to replace" : "minehut_session_id") + '"></label></div>' +
      '<div class="hint" style="margin-bottom:11px">Minehut has no official API and no way to log in ' +
        "from a script, so these come from the dashboard itself: open " +
        "<b>dashboard.minehut.com</b>, press F12, and in <b>Application</b> (Chrome) or <b>Storage</b> (Firefox) &rarr; " +
        "<b>Local Storage</b> copy <code>minehut_auth_token</code> and <code>minehut_session_id</code>. " +
        "<b>Since 2026 the dashboard logs in with a secure cookie and may not keep them there any more</b> - " +
        "then leave these empty: Lodestone still shows the server's status from Minehut's public info, and you " +
        "start / stop it in the dashboard. They expire, so if actions start failing with a 401, grab them again. " +
        "Stored in ~/.lodestone.json and never shown back to you." +
        "<br>Because the API is unofficial, Minehut can change it at any time.</div>" +
      '<div class="row"><button class="btn sm" onclick="testMinehut()">Test connection</button>' +
      '<span class="hint" id="mhMsg"></span></div></div></div>';
  }
  body += '<div class="card" style="margin-top:6px"><div class="card-h"><h2>RCON</h2>' +
    '<div class="path">lets the console send commands when the panel does not own the process</div>' +
    '<div class="spacer"></div>' +
    '<label class="tog"><input type="checkbox" data-g="rcon" data-k="enabled"' + (s.rcon.enabled ? " checked" : "") +
    '><span class="box"></span><span class="lbl">Enabled</span></label></div><div class="card-b">' +
    '<div class="grid3">' + fld("rcon", "host", "Host", s.kind === "sftp" ? "Blank uses the SFTP host." : "Blank uses 127.0.0.1") +
    fld("rcon", "port", "Port") + fld("rcon", "password", "Password", "Must match rcon.password in server.properties.", "password") + "</div>" +
    '<div class="row"><button class="btn sm" onclick="testRcon()">Test connection</button>' +
    '<span class="hint" id="rconMsg"></span></div></div></div>';
  $("srvEditBody").innerHTML = body;
  iconList();
  $("srvEditTitle").textContent = "Edit — " + s.name;
  $("srvEditCard").style.display = "flex";
  $("srvEditCard").scrollIntoView({ behavior: "smooth" });
}
function editServerRender() { collectServer(); editServer(EDITING.id); }
// The item and block names for the icon box, fetched once.
let ICONS = null;
async function iconList() {
  if (!ICONS) {
    try { ICONS = await api("/api/art/icons"); } catch (e) { return; }
  }
  const dl = $("iconList");
  if (!dl) return;
  if (!ICONS.ready) { dl.innerHTML = ""; return; }
  dl.innerHTML = ICONS.items.map(i => '<option value="' + esc(i.key) + '">' + esc(i.label + " · " + i.group) + "</option>").join("");
}
function iconPrev(v) {
  const row = (SERVERS || []).find(x => EDITING && x.id === EDITING.id) || {};
  const key = (v || "").trim() || row.icon_auto;
  if (ICONS && ICONS.items && key && !ICONS.items.some(i => i.key === key)) return;
  if (key) $("iconPrev").src = "/api/art/icon/" + key + ".png?size=96";
}
function collectServer() {
  document.querySelectorAll("#srvEditBody [data-k]").forEach(el => {
    const g = el.dataset.g, k = el.dataset.k;
    const v = el.type === "checkbox" ? el.checked : el.value;
    if (g) { EDITING[g] = EDITING[g] || {}; EDITING[g][k] = v; }
    else EDITING[k] = v;
  });
}
async function saveServer() {
  collectServer();
  await act("/api/servers/save", EDITING, "Saved.");
  closeSrvEdit(); loadServers();
}
function closeSrvEdit() { $("srvEditCard").style.display = "none"; EDITING = null; }
async function dupServer(id) { await act("/api/servers/duplicate", { id }, "Duplicated."); loadServers(); }
function delServer(id, name) {
  confirmBox("Remove " + name + "?", "The server files are left alone — this only removes the entry here.",
    async () => { await act("/api/servers/delete", { id }, "Removed."); loadServers(); });
}
async function pick(kind, group, key) {
  try {
    const j = await api("/api/pick?kind=" + kind);
    if (!j.path) return;
    const el = document.querySelector('#srvEditBody [data-g="' + group + '"][data-k="' + key + '"]');
    if (el) el.value = j.path;
  } catch (e) { toast(e.message, "bad"); }
}
async function testSftp() {
  collectServer();
  await act("/api/servers/save", EDITING, null);
  $("sftpMsg").textContent = "testing…";
  $("sftpOut").innerHTML = "";
  try {
    const j = await api("/api/sftp/test", { id: EDITING.id });
    $("sftpMsg").textContent = j.ok ? "everything checks out" : "stopped at the step marked below";
    let h = '<div class="items" style="margin-top:11px">' + j.steps.map(st =>
      '<div class="item"><span class="pill ' + (st.ok ? "on" : "bad") + '">' +
      (st.ok ? "ok" : "fail") + '</span><div style="flex:1"><div class="ttl">' + esc(st.step) +
      '</div><div class="sub">' + esc(st.detail) + "</div></div></div>").join("") + "</div>";
    if (j.suggest_root) {
      h += '<div class="item" style="margin-top:11px;border-color:var(--green)">' +
        '<div style="flex:1"><div class="ttl">The server files are in ' + esc(j.suggest_root) + "</div>" +
        '<div class="sub">Use that as the server folder?</div></div>' +
        '<button class="btn xs gold" onclick="useRoot(\'' + esc(j.suggest_root) + '\')">Use it</button></div>';
    }
    if (j.listing && j.listing.length) {
      h += '<div class="hint" style="margin-top:11px">What is on the account:</div>' +
        '<div class="code" style="margin-top:5px">' + j.listing.map(esc).join("\n") + "</div>";
    }
    $("sftpOut").innerHTML = h;
  } catch (e) {
    $("sftpMsg").textContent = e.message;
  }
}
async function useRoot(root) {
  await act("/api/sftp/use-root", { id: EDITING.id, root }, "Server folder set to " + root);
  const el = document.querySelector('#srvEditBody [data-g="sftp"][data-k="root"]');
  if (el) el.value = root;
  if (EDITING.sftp) EDITING.sftp.root = root;
  testSftp();
}
async function testMinehut() {
  collectServer();
  await act("/api/servers/save", EDITING, null);
  $("mhMsg").textContent = "asking Minehut…";
  try {
    const j = await api("/api/minehut/test", { id: EDITING.id });
    $("mhMsg").textContent = j.ok ? j.out : j.error;
  } catch (e) { $("mhMsg").textContent = e.message; }
}
async function testRcon() {
  collectServer();
  await act("/api/servers/save", EDITING, null);
  $("rconMsg").textContent = "testing…";
  try {
    const j = await api("/api/rcon/test", { id: EDITING.id });
    $("rconMsg").textContent = j.ok ? "connected — " + (j.out || "no reply") : j.error;
  } catch (e) { $("rconMsg").textContent = e.message; }
}
async function installParamiko() {
  toast("Installing paramiko…", "");
  const j = await act("/api/install", {}, null);
  toast(j.msg, j.ok ? "good" : "bad");
}

/* ---------------- api ---------------- */
const ENDPOINTS = [
  ["GET", "/api/v1/health", "Panel version and uptime", "read"],
  ["GET", "/api/v1/servers", "Every server on the shelf", "read"],
  ["GET", "/api/v1/status", "Active server status, players, uptime", "read"],
  ["GET", "/api/v1/console?since=N", "Console lines after sequence N", "read"],
  ["GET", "/api/v1/players", "Who is online right now", "read"],
  ["POST", "/api/v1/command", "Run one command — {command}", "control"],
  ["POST", "/api/v1/start", "Start the active server", "control"],
  ["POST", "/api/v1/stop", "Stop it cleanly", "control"],
  ["POST", "/api/v1/restart", "Stop then start", "control"],
  ["POST", "/api/v1/player", "Moderate — {player, action, reason}", "control"],
  ["GET", "/api/v1/files?path=", "List a folder", "read"],
  ["GET", "/api/v1/file?path=", "Read a text file", "read"],
  ["PUT", "/api/v1/file", "Write a file — {path, content}", "files"],
  ["GET", "/api/v1/properties", "server.properties as JSON", "read"],
  ["PATCH", "/api/v1/properties", "Change keys — {values:{k:v}}", "files"],
  ["GET", "/api/v1/offline", "Offline / non-premium mode state", "read"],
  ["POST", "/api/v1/offline", "Offline mode — {enabled, allow_lan}", "admin"],
  ["GET", "/api/v1/content", "Installed mods or plugins, with problems", "read"],
  ["POST", "/api/v1/content/install", "Install — {items:[{source, project_id, version_id?}]}", "files"],
  ["POST", "/api/v1/content/update", "Update — {files?} (all when omitted)", "files"],
  ["POST", "/api/v1/servers/create", "Create a server — same body as the wizard; returns a job", "admin"],
  ["GET", "/api/v1/jobs?id=", "Progress of a create job", "read"],
  ["GET", "/api/v1/backups", "List backups", "read"],
  ["POST", "/api/v1/backup", "Start one — {label, what}", "control"],
  ["POST", "/api/v1/webhook", "Fire a custom Discord event — {title, description, event}", "control"],
  ["POST", "/api/v1/assistant", "Run the assistant — {instruction}", "control"],
];
async function loadApi() {
  const j = await api("/api/tokens");
  $("apiOn").checked = j.enabled;
  $("apiBase").textContent = location.origin + "/api/v1";
  const box = $("tokList");
  box.innerHTML = j.items.length ? j.items.map(t =>
    '<div class="tokrow"><div style="flex:1"><div class="ttl">' + esc(t.name) + "</div>" +
    '<div class="sub">' + esc(t.prefix) + "… · " + t.scopes.join(", ") + " · made " + esc(t.created) +
    " · " + t.calls + " calls · last " + esc(t.last_used) + "</div></div>" +
    '<button class="btn xs danger" onclick="delToken(\'' + t.id + '\')">Revoke</button></div>').join("")
    : '<div class="empty">No tokens yet. The API stays shut until you make one and switch it on.</div>';
  $("epList").innerHTML = ENDPOINTS.map(e =>
    '<div class="ep"><span class="verb ' + (e[0] === "GET" ? "get" : (e[0] === "DELETE" ? "del" : "post")) + '">' +
    e[0] + '</span><span class="u">' + esc(e[1]) + '</span><span class="d">' + esc(e[3]) + "</span></div>").join("") +
    '<div class="code" style="margin-top:13px"><span class="c"># every call carries the token</span>\n' +
    '<span class="m">curl</span> <span class="u">' + location.origin + '/api/v1/status</span> \\\n' +
    '  -H <span class="u">"Authorization: Bearer lds_…"</span>\n\n' +
    '<span class="c"># run a command</span>\n' +
    '<span class="m">curl</span> -X POST <span class="u">' + location.origin + '/api/v1/command</span> \\\n' +
    '  -H <span class="u">"Authorization: Bearer lds_…"</span> \\\n' +
    '  -H <span class="u">"Content-Type: application/json"</span> \\\n' +
    '  -d <span class="u">\'{"command":"say hello from a script"}\'</span></div>';
}
async function toggleApi() {
  await act("/api/tokens/enable", { enabled: $("apiOn").checked },
    $("apiOn").checked ? "API on." : "API off.");
}
function newToken() {
  modal("New API token",
    '<label class="fld"><span>Name</span><input class="inp" id="tkName" placeholder="deploy script"></label>' +
    '<span style="font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:var(--dim);font-weight:700">Scopes</span>' +
    '<div class="evgrid" style="margin-top:6px">' +
    [["read", "Read", "Status, console, files, properties"],
     ["control", "Control", "Start, stop, commands, moderation, backups"],
     ["files", "Files", "Write files and change properties"],
     ["admin", "Admin", "Everything, including server records"]].map(s =>
      '<label class="ev" style="cursor:pointer"><input type="checkbox" style="display:none" class="tk-sc" value="' + s[0] + '"' +
      (s[0] === "read" ? " checked" : "") + ' onchange="this.closest(\'.ev\').classList.toggle(\'on\',this.checked)">' +
      '<span class="tick"></span><div><div class="evn">' + s[1] + '</div><div class="evd">' + s[2] + "</div></div></label>").join("") +
    "</div>",
    [["Cancel", "closeModal()"], ["Create", "doNewToken()", "gold"]]);
  setTimeout(() => document.querySelectorAll(".tk-sc").forEach(c =>
    c.closest(".ev").classList.toggle("on", c.checked)), 10);
}
async function doNewToken() {
  const name = $("tkName").value || "token";
  const scopes = [...document.querySelectorAll(".tk-sc")].filter(c => c.checked).map(c => c.value);
  closeModal();
  const j = await act("/api/tokens/create", { name, scopes }, "Token created.");
  $("tokSecret").innerHTML = '<div class="secret"><div class="t">Copy this now — it is not shown again.</div>' +
    "<code>" + esc(j.token) + "</code></div>";
  loadApi();
}
function delToken(id) {
  confirmBox("Revoke this token?", "Anything using it stops working immediately.",
    async () => { await act("/api/tokens/delete", { id }, "Revoked."); loadApi(); });
}

/* ---------------- settings ---------------- */
/* ---------------- updates ---------------- */
let UPD_TOLD = false;
function updWhen(iso) {
  const t = Date.parse(iso);
  if (!t) return iso || "";
  const m = (Date.now() - t) / 60000;
  return m < 2 ? "just now" : m < 60 ? Math.round(m) + " min ago" : m < 1440 ? Math.round(m / 60) + " h ago"
    : m < 43200 ? Math.round(m / 1440) + " days ago" : new Date(t).toLocaleDateString();
}
async function loadUpdates() {
  try { renderUpdates(await api("/api/update")); }
  catch (e) { $("updBody").innerHTML = '<div class="hint">' + esc(e.message) + "</div>"; }
}
async function updDo(path, busy, body) {
  toast(busy);
  try { renderUpdates(await act(path, body || {}, "")); } catch (e) { loadUpdates(); }
}
function updSave(extra) {
  const v = id => ($(id) || {}).value;
  const body = extra || { enabled: true, auto_install: $("updAuto").checked, auto_restart: $("updAutoRestart").checked,
    hours: v("updHours"), repo: v("updRepo"), branch: v("updBranch"), path: v("updPath"),
    use_git_login: $("updGit").checked, token: v("updToken") };
  updDo("/api/update/settings", "Saving…", body);
}
function renderUpdates(u) {
  const st = u.settings || {}, info = u.info || {}, state = u.state, applied = u.applied || {};
  $("updFrom").textContent = "github.com/" + st.repo + " · " + st.branch + (st.path ? " · " + st.path + "/" : "");
  $("updEnabled").checked = !!st.enabled;
  let head, sub = "", col = "dim";
  if (!u.supported) { head = "Updates are not available here"; sub = u.reason; col = "gold"; }
  else if (state === "ready") { head = applied.rolled_back ? "The previous version is back" : "Update installed — restart to use it";
    sub = applied.rolled_back ? "Restart Lodestone to use it." : (applied.sha ? "Now at " + applied.sha.slice(0, 7) + " · " + (applied.title || "") : ""); col = "green"; }
  else if (state === "available") { head = info.behind == null ? "An update is available" : info.behind + " new change" + (info.behind === 1 ? "" : "s") + " available";
    sub = "Latest: " + ((info.latest || {}).title || ""); col = "gold"; }
  else if (state === "current") { head = "Up to date"; sub = (info.current || {}).title || ""; col = "green"; }
  else if (state === "checking") head = "Checking GitHub…";
  else if (state === "installing") head = "Installing the update… " + (u.pct || 0) + "%";
  else if (state === "error") { head = "The last check failed"; sub = u.error; col = "red"; }
  else { head = "Not checked yet"; sub = st.enabled ? "Lodestone checks a little after it starts." : ""; }
  const btn = (label, fn, cls) => '<button class="btn sm ' + (cls || "") + '" onclick="' + fn + '">' + label + "</button>";
  let acts = "";
  if (u.supported) {
    if (state === "ready") acts += btn("Restart now", "updRestart(false)", "gold");
    if (state === "available") acts += btn("Install now", "updDo(&quot;/api/update/install&quot;,&quot;Downloading the update…&quot;)", "gold");
    acts += btn("Check now", "updDo(&quot;/api/update/check&quot;,&quot;Asking GitHub…&quot;)", state === "ready" || state === "available" ? "" : "gold");
    if (u.can_rollback && state !== "installing") acts += btn("Go back to the previous version", "updRollback()");
  }
  const commits = info.commits || [];
  const inst = u.installed || {};
  const row = (k, val) => '<div class="krow"><span class="k">' + esc(k) + "</span><span>" + esc(val) + "</span></div>";
  const sel = (id, opts, cur) => '<select class="inp" id="' + id + '">' + opts.map(o => '<option value="' + o[0] + '"' +
    (String(cur) === o[0] ? " selected" : "") + ">" + o[1] + "</option>").join("") + "</select>";
  const tog = (id, on, label) => '<label class="tog" style="margin:4px 0 8px"><input type="checkbox" id="' + id + '"' + (on ? " checked" : "") +
    '><span class="box"></span><span class="lbl">' + label + "</span></label>";
  $("updBody").innerHTML = '<div class="grid2"><div>' +
    '<div class="ttl" style="font-size:15px;color:var(--' + col + ')">' + esc(head) + "</div>" +
    (sub ? '<div class="hint">' + esc(sub) + "</div>" : "") +
    '<div class="row wrap" style="margin:10px 0">' + acts + "</div>" +
    (u.running_servers && state === "ready" ? '<div class="hint">A server is running — restarting stops it cleanly first.</div>' : "") +
    (commits.length ? '<div class="grphead">What\'s new</div><div class="items">' + commits.slice(0, 12).map(c =>
      '<div class="item"><div style="flex:1"><div class="ttl">' + esc(c.title) + '</div><div class="sub">' + esc(c.short) + " · " +
      esc(c.author) + " · " + esc(updWhen(c.date)) + "</div></div></div>").join("") + "</div>" : "") +
    '<div class="klist" style="margin-top:10px">' +
    row("Installed version", inst.sha ? inst.sha.slice(0, 7) + " · " + updWhen(inst.date)
      : info.mode === "git" ? "git checkout · " + ((info.current || {}).short || "") : "not stamped yet — the first update stamps it") +
    row("Folder", u.root) + row("Signed in to GitHub with", info.auth || "—") +
    row("Last checked", u.checked_at ? updWhen(new Date(u.checked_at * 1000).toISOString()) : "never") + "</div></div><div>" +
    tog("updAuto", st.auto_install, "Install updates by itself") +
    '<div class="hint" style="margin:-4px 0 8px">Downloaded, checked and put in place in the background. Nothing changes until Lodestone restarts.</div>' +
    tog("updAutoRestart", st.auto_restart, "Restart by itself when no server is running") +
    '<label class="fld"><span>Check for updates</span>' + sel("updHours", [["1", "every hour"], ["3", "every 3 hours"], ["6", "every 6 hours"],
      ["12", "every 12 hours"], ["24", "once a day"], ["168", "once a week"]], st.hours) + "</label>" +
    '<label class="fld"><span>Repository</span><input class="inp" id="updRepo" value="' + esc(st.repo) + '"></label>' +
    '<div class="grid2"><label class="fld"><span>Branch</span><input class="inp" id="updBranch" value="' + esc(st.branch) + '"></label>' +
    '<label class="fld"><span>Folder</span><input class="inp" id="updPath" value="' + esc(st.path) + '"></label></div>' +
    tog("updGit", st.use_git_login, "Use the GitHub login git already has") +
    '<label class="fld"><span>GitHub token</span><input class="inp" id="updToken" type="password" placeholder="' +
    (st.has_token ? "saved — type to replace, or - to remove" : "only needed for a private repo") + '">' +
    '<div class="hint">Optional. A fine-grained token with Contents: read-only on this repository.</div></label>' +
    btn("Save", "updSave()") + "</div></div>";
  if (state === "checking" || state === "installing") setTimeout(() => { if ($("view-settings").classList.contains("show")) loadUpdates(); }, 1000);
}
function updRollback() {
  confirmBox("Go back to the previous version?",
    "The files the last update replaced are put back. Restart Lodestone afterwards to use them.",
    () => updDo("/api/update/rollback", "Putting the previous version back…"));
}
async function updRestart(force) {
  let j;
  try {
    const r = await fetch("/api/update/restart", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ force: !!force }) });
    j = await r.json();
  } catch (e) { toast("Could not reach the panel.", "bad"); return; }
  if (j.needs_force) { confirmBox("Stop the servers and restart?", j.error, () => updRestart(true)); return; }
  if (j.ok === false) { toast(j.error || "Could not restart.", "bad"); return; }
  document.body.innerHTML = '<div style="display:flex;height:100vh;align-items:center;justify-content:center;' +
    'font-family:var(--f-display);color:var(--dim);text-align:center;line-height:2">Restarting Lodestone into the update…' +
    '<br><span style="font-size:12px">This page reloads when it is back.</span></div>';
  // the new engine opens its own tab; this one follows if it is still around
  const t0 = Date.now();
  setTimeout(function poll() {
    fetch("/api/me").then(r => r.ok ? location.reload() : null).catch(() => null);
    if (Date.now() - t0 < 120000) setTimeout(poll, 2000);
  }, 4000);
}
function paintUpdate(u) {
  if (!u) return;
  const nav = document.querySelector('.nav[data-view="settings"]');
  if (nav) nav.classList.toggle("hasupd", !!u.ready);
  if (u.ready && !UPD_TOLD) { UPD_TOLD = true; toast("A Lodestone update is installed — restart from Settings › Updates to use it.", "good"); }
}

async function loadSettings() {
  loadUpdates();
  const j = await api("/api/me");
  $("setBody").innerHTML =
    '<div class="grid2"><div>' +
    '<label class="fld"><span>Panel address</span><select class="inp" id="setHost">' +
    '<option value="127.0.0.1"' + (j.host === "127.0.0.1" ? " selected" : "") + ">127.0.0.1 — this machine only</option>" +
    '<option value="0.0.0.0"' + (j.host === "0.0.0.0" ? " selected" : "") + ">0.0.0.0 — anyone on the LAN</option></select>" +
    '<div class="hint">There is no login on the UI. On 0.0.0.0 every device on your network can control ' +
    "your servers. Takes effect when you restart the panel.</div></label>" +
    '<label class="fld"><span>Port</span><input class="inp" id="setPort" value="' + esc(j.port) + '">' +
    '<div class="hint">Restart the panel to move it.</div></label>' +
    '<label class="fld"><span>How much to show</span><select class="inp" id="setMode" onchange="setMode(this.value)">' +
    '<option value="simple"' + (MODE === "simple" ? " selected" : "") + ">Simple — the essentials</option>" +
    '<option value="advanced"' + (MODE === "advanced" ? " selected" : "") + ">Advanced — everything</option></select>" +
    '<div class="hint">Simple hides log search, plugins, worlds, automation, the assistant, Discord ' +
      "and the API. Nothing is lost — the tabs come straight back.</div></label>" +
    '<label class="fld"><span>Text size</span>' +
    '<div class="row"><input type="range" min="10" max="28" value="' + SZ.mono +
      '" oninput="SZ.mono=+this.value;applySize()" style="flex:1">' +
      '<span class="pill" id="szMono">' + SZ.mono + 'px mono</span></div>' +
    '<div class="row" style="margin-top:8px"><input type="range" min="12" max="22" value="' + SZ.ui +
      '" oninput="setUiSize(this.value)" style="flex:1">' +
      '<span class="pill">interface</span></div>' +
    '<div class="hint">Also on the console and the file editor, and with Ctrl +/- while editing. ' +
      "Kept in this browser.</div></label>" +
    '<button class="btn gold" onclick="saveSettings()">Save</button>' +
    '<button class="btn danger" style="margin-left:8px" onclick="quitPanel()">Quit Lodestone</button>' +
    '<div class="hint">Closing the window only hides Lodestone — it keeps running in the ' +
      "background and keeps port " + esc(j.port) + ". Use Quit to stop it properly.</div>" +
    "</div><div>" +
    '<div class="krow"><span class="k">Reachable at</span><span class="v">' + esc(j.url) + "</span></div>" +
    '<div class="krow"><span class="k">LAN address</span><span class="v">' + esc(j.lan_ip) + ":" + esc(j.port) + "</span></div>" +
    '<div class="krow"><span class="k">Config file</span><span class="v">' + esc(j.config) + "</span></div>" +
    '<div class="krow"><span class="k">Backups kept in</span><span class="v">' + esc(j.backups) + "</span></div>" +
    '<div class="krow"><span class="k">paramiko</span><span class="v">' + (j.paramiko ? "installed" : "missing") + "</span></div>" +
    (j.paramiko ? "" : '<button class="btn sm" style="margin-top:10px" onclick="installParamiko()">Install paramiko</button>') +
    "</div></div>";
}
async function quitPanel() {
  const go = async (force) => {
    let j;
    try {
      const r = await fetch("/api/quit", { method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ force: !!force }) });
      j = await r.json();
    } catch (e) { toast("Could not reach the panel.", "bad"); return; }
    if (j.needs_force) {
      confirmBox("The server is still running",
        "Quitting Lodestone will stop your Minecraft server first, then close the panel.",
        () => go(true));
      return;
    }
    if (j.ok === false) { toast(j.error || "Could not quit.", "bad"); return; }
    document.body.innerHTML =
      '<div style="display:flex;height:100vh;align-items:center;justify-content:center;' +
      'font-family:var(--f-display);color:var(--dim);text-align:center;line-height:2">' +
      "Lodestone has shut down.<br><span style=\"font-size:12px\">You can close this window." +
      "</span></div>";
  };
  confirmBox("Quit Lodestone?",
    "The panel will stop and release its port. Any Minecraft server it is running will be " +
    "stopped first. Port forwards Lodestone created are removed.",
    () => go(false));
}

async function saveSettings() {
  await act("/api/settings", { host: $("setHost").value, port: parseInt($("setPort").value, 10) },
    "Saved — restart Lodestone to apply.");
}

/* ---------------- network / hosting ---------------- */
let NET = null, NETPOLL = null;

/* The rail badge is driven by the status stream, so it stays right even when
   the Network tab is not open. */
function paintNetBadge(h) {
  const b = $("bNet");
  if (!b) return;
  if (!h || h.available === false) { b.textContent = "n/a"; b.style.color = ""; return; }
  if (h.busy) { b.textContent = "…"; b.style.color = "var(--warn)"; return; }
  if (h.mode === "internet") {
    if (h.online) { b.textContent = "public"; b.style.color = "var(--green)"; }
    else if (h.connectivity_state === "unknown" || h.connectivity_state === "checking") {
      /* Not knowing is not the same as knowing it is unreachable, and a red
         badge is a claim. Say unknown when it is unknown. */
      b.textContent = h.stale ? "recheck" : "unknown";
      b.style.color = "var(--warn)";
    } else { b.textContent = "not public"; b.style.color = "var(--red)"; }
  } else {
    b.textContent = h.mode === "local" ? "local" : "lan";
    b.style.color = "";
  }
  if (NET && $("view-network").classList.contains("show")) {
    // Something finished in the background — pick up the full picture once.
    if (NET.busy && !h.busy) loadNetwork();
  }
}

const NET_MODES = [
  ["local", "Local only", "Only this PC can connect. The server binds to 127.0.0.1."],
  ["lan", "LAN", "Anyone on your home network can connect. No router changes."],
  ["internet", "Internet", "Friends outside your network can connect. Asks your " +
   "router to forward the Minecraft port, then checks from outside whether it worked."],
  ["tunnel", "Tunnel", "For when port forwarding cannot work — CGNAT, or a router " +
   "you have no password for. Runs a playit.gg or ngrok agent that dials out, so nothing " +
   "has to be opened on any router. Traffic goes through their servers and the address " +
   "is theirs."]
];

const NET_STATE_CLASS = {
  reachable: "on", not_reachable: "bad", server_offline: "bad",
  port_blocked: "bad", cgnat_suspected: "bad", ipv6_only: "warn",
  checking: "warn", unknown: ""
};

async function loadNetwork() {
  loadOffline();
  let j;
  try { j = await api("/api/hosting"); }
  catch (e) { $("netModes").innerHTML = '<div class="hint">' + esc(e.message) + "</div>"; return; }
  NET = j;
  if (j.available === false) {
    $("netModes").innerHTML = '<div class="hint">' + esc(j.error || "Networking support is not available.") +
      "<br><br>Local and LAN hosting still work — this only affects automatic " +
      "router configuration and the reachability test.</div>";
    $("netDash").innerHTML = "";
    return;
  }
  renderNetModes(j);
  renderNetDash(j);
  renderNetSteps(j);
  renderNetFix(j);
  renderNetSettings(j);
  const p = $("netPhase");
  p.className = "pill " + (j.busy ? "warn" : (j.online ? "on" : (j.phase === "failed" ? "bad" : "")));
  p.textContent = j.busy ? "working…" : (j.mode === "internet"
    ? (j.online ? "online" : j.connectivity_label.toLowerCase()) : j.mode_label.toLowerCase());
  $("netStamp").textContent = j.checked_at
    ? "checked " + new Date(j.checked_at * 1000).toLocaleTimeString() : "not checked yet";
  netAudit(false);
  if (j.busy) netStartPoll(); else netStopPoll();
}

/* ---------------- offline / non-premium mode ---------------- */
let OFF = null;

async function loadOffline() {
  let j;
  try { j = await api("/api/offline"); }
  catch (e) { $("netOffline").innerHTML = '<div class="hint">' + esc(e.message) + "</div>"; return; }
  OFF = j;
  renderOffline(j);
}

function renderOffline(j) {
  const p = $("offPillNet");
  p.className = "pill" + (j.enabled ? " bad" : "");
  p.textContent = j.enabled ? "OFFLINE MODE: testing" : "off";
  const file = j.kind === "velocity" ? "<code>velocity.toml</code>" : "<code>server.properties</code>";
  const dis = j.available ? "" : " disabled";
  const tog = (id, on, label, hint) =>
    '<label class="tog" style="display:flex;margin-bottom:9px"><input type="checkbox" id="' + id +
    '"' + (on ? " checked" : "") + dis + ' onchange="offlineSave()"><span class="box"></span>' +
    '<span class="lbl">' + label + "</span></label>" +
    (hint ? '<div class="hint" style="margin:-6px 0 12px 47px">' + hint + "</div>" : "");
  let h = "";
  if (!j.available) {
    h += '<div class="hint" style="margin-bottom:11px;color:var(--warn)">' + esc(j.why_not);
    if (j.behind && j.behind.id) {
      h += '  <a href="#" onclick="switchServer(\'' + esc(j.behind.id) +
        '\');return false">Open ' + esc(j.behind.name) + "</a>";
    }
    h += "</div>";
  }
  h += tog("offOn", j.enabled, "Offline / non-premium mode (testing)",
      "Lets non-premium accounts join. <b>Any username can join, including yours with OP.</b> " +
      "Player UUIDs change, so Skript data kept per <code>{...::%uuid%}</code> looks reset " +
      "until you switch back. Turning it off restores the previous " + file + " values exactly.") +
    tog("offLan", j.allow_lan, "Allow LAN",
      "Off: the server binds to 127.0.0.1, so only this PC can join. " +
      "On: devices on your home network can join too.");
  const cur = j.current || {};
  if (j.available && Object.keys(cur).length) {
    h += '<div class="hint">' + file + " now: " + Object.keys(cur).map(k =>
      "<code>" + esc(k) + "=" + esc(cur[k] === null ? "(not set)" : cur[k]) + "</code>").join("  ") + "</div>";
  }
  if ((j.auth_plugins || []).length) {
    h += '<div class="hint" style="margin-top:9px;color:var(--warn)">Auth plugin found (' +
      esc(j.auth_plugins.join(", ")) + "). Internet and tunnel hosting stay allowed while offline " +
      "mode is on, but make sure every player has to register.</div>";
  } else if (j.available) {
    h += '<div class="hint" style="margin-top:9px">While this is on, Lodestone refuses internet and ' +
      "tunnel hosting (playit, UPnP, NAT-PMP, PCP) for this server.</div>";
  }
  if (j.restart_needed) {
    h += '<div class="hint" style="margin-top:9px;color:var(--warn)"><b>Restart the server to apply.</b></div>';
  }
  $("netOffline").innerHTML = h;
}

async function offlineSave() {
  const on = $("offOn").checked, lan = $("offLan").checked;
  const go = async () => {
    try {
      await act("/api/offline", { enabled: on, allow_lan: lan },
        on ? "Offline mode saved." : "Offline mode off. Previous values restored.");
    } catch (e) { /* act() already said why */ }
    loadOffline();
  };
  if (on && !(OFF && OFF.enabled)) {
    if (OFF) renderOffline(OFF);   // Cancel leaves the switch where it was
    confirmBox("Turn on offline mode?",
      "Anyone who can reach this server can join with any username, including yours with OP. " +
      "Player UUIDs change, so per-player data looks reset until you switch back.", go);
    return;
  }
  go();
}

function netStartPoll() {
  if (NETPOLL) return;
  NETPOLL = setInterval(() => {
    if (!$("view-network").classList.contains("show")) { netStopPoll(); return; }
    loadNetwork();
  }, 1500);
}
function netStopPoll() { if (NETPOLL) { clearInterval(NETPOLL); NETPOLL = null; } }

function renderNetModes(j) {
  if (!j.can_host) {
    $("netModes").innerHTML = '<div class="hint">' + esc(j.why_not) + "</div>";
    return;
  }
  let h = '<div class="items">';
  for (const [key, name, why] of NET_MODES) {
    const on = j.mode === key;
    h += '<div class="item' + (on ? " active" : "") + '">' +
      '<div style="flex:1"><div class="ttl">' + name + (on ? "  ·  current" : "") + "</div>" +
      '<div class="sub">' + why + "</div></div>" +
      (on
        ? ((key === "internet" || key === "tunnel")
            ? '<button class="btn sm danger" onclick="netDisable()">Turn off</button>'
            : '<span class="pill on">active</span>')
        : '<button class="btn sm' + (key === "internet" ? " gold" : "") +
          '" onclick="netSetMode(\'' + key + '\')">Use ' + name.toLowerCase() + "</button>") +
      "</div>";
  }
  h += "</div>";
  if (j.property_error) {
    h += '<div class="hint" style="margin-top:11px;color:var(--warn)">' + esc(j.property_error) + "</div>";
  } else if ((j.property_conflicts || []).length) {
    h += '<div class="hint" style="margin-top:11px"><b>server.properties does not match this mode:</b><br>';
    for (const c of j.property_conflicts) {
      h += "<br><code>" + esc(c.key) + "</code> is <code>" + esc(c.is || "(empty)") +
        "</code>, should be <code>" + esc(c.want || "(empty)") + "</code> — " + esc(c.why);
    }
    h += '<br><br><button class="btn sm gold" onclick="netApplyProps()">Fix server.properties</button>' +
      "  Restart the server afterwards for it to take effect.</div>";
  }
  $("netModes").innerHTML = h;
}

function dot(state, text) {
  const colour = state === "good" ? "var(--green)" : state === "bad" ? "var(--red)"
    : state === "warn" ? "var(--warn)" : "var(--faint)";
  return '<span style="color:' + colour + '">' +
    (state === "good" ? "✓ " : state === "bad" ? "✗ " : "● ") + esc(text) + "</span>";
}

function addr(text) {
  if (!text) return '<span class="v">—</span>';
  return '<span class="v" style="cursor:pointer" title="Click to copy" onclick="netCopy(\'' +
    esc(text).replace(/'/g, "\\'") + '\')">' + esc(text) + "</span>";
}

function netCopy(text) {
  try { navigator.clipboard.writeText(text); toast("Copied " + text, "good"); }
  catch (e) { toast("Could not copy — select it by hand.", "bad"); }
}

function renderNetDash(j) {
  const running = j.running, internet = j.mode === "internet";
  const cs = j.connectivity_state || "unknown";
  let h = '<div class="krow"><span class="k">Server</span>' +
    '<span class="v">' + dot(running ? "good" : "off", running ? "ONLINE" : "OFFLINE") + "</span></div>";
  h += '<div class="krow"><span class="k">Mode</span><span class="v">' +
    esc((j.mode_label || "").toUpperCase()) + "</span></div>";
  h += '<div class="krow"><span class="k">Minecraft port</span><span class="v">' +
    esc(j.port) + " " + esc(j.protocol) +
    (j.different_ports ? "  (external " + esc(j.external_port) + ")" : "") + "</span></div>";
  h += '<div class="krow"><span class="k">This PC</span>' + addr(j.local_address) + "</div>";
  h += '<div class="krow"><span class="k">Local network</span>' + addr(j.lan_address) + "</div>";

  if (j.mode === "tunnel") {
    const t = j.tunnel || {};
    const isNg = t.provider === "ngrok";
    const pname = isNg ? "ngrok" : "playit.gg";
    h += '<div class="krow"><span class="k">' + esc(pname) + ' agent</span><span class="v">' +
      (t.running ? dot("good", "Running" + (t.uptime ? " (" + t.uptime + "s)" : ""))
        : t.installed ? dot("bad", "Not running") : dot("bad", "Not installed")) +
      "</span></div>";
    if (t.agent_path) {
      h += '<div class="krow"><span class="k">Agent</span><span class="v" style="font-size:10px">' +
        esc(t.agent_path) + "</span></div>";
    }
    h += '<div class="krow"><span class="k">Give friends</span>' +
      (t.address ? addr(t.address) : '<span class="v">—</span>') + "</div>";
    if (t.needs_claim && t.claim_url) {
      h += '<div class="krow"><span class="k">Action needed</span><span class="v">' +
        dot("warn", isNg ? "Add an authtoken" : "Claim the tunnel") + "</span></div>";
    }
    h += '<div class="krow"><span class="k">External connectivity</span><span class="v">' +
      dot(cs === "reachable" ? "good" : cs === "checking" ? "warn" : "bad",
          j.connectivity_label) + "</span></div>";
  }

  if (internet) {
    const pub = j.public_ip || {};
    h += '<div class="krow"><span class="k">Public</span>' +
      (j.public_address ? addr(j.public_address)
        : '<span class="v">' + esc(pub.ipv4_error || (pub.enabled === false
            ? "lookup switched off" : "unknown")) + "</span>") + "</div>";
    if (pub.ipv4 && !pub.ipv4_confirmed) {
      h += '<div class="krow"><span class="k"></span><span class="v" style="color:var(--warn)">' +
        "only one source confirmed this address</span></div>";
    }
    if (pub.ipv4_previous) {
      h += '<div class="krow"><span class="k">Address changed</span><span class="v">was ' +
        esc(pub.ipv4_previous) + "</span></div>";
    }
    if (j.ipv6_address) {
      h += '<div class="krow"><span class="k">IPv6</span>' + addr(j.ipv6_address) + "</div>";
    }
    h += '<div class="krow"><span class="k">Port forward</span><span class="v">' +
      (j.mapping_active ? dot("good", "Active — " + j.mapping_text)
        : dot("bad", "None")) + "</span></div>";
    if (j.mapping && j.mapping.provider) {
      h += '<div class="krow"><span class="k">Made with</span><span class="v">' +
        esc(j.mapping.provider) +
        (j.mapping.seconds_left != null
          ? ", lease " + esc(j.mapping.seconds_left) + "s left, auto-renewed" : ", permanent") +
        "</span></div>";
    }
    if (j.router_external_ip) {
      h += '<div class="krow"><span class="k">Router WAN</span><span class="v">' +
        esc(j.router_external_ip) + "</span></div>";
    }
    const fw = j.firewall || {};
    if (j.windows) {
      h += '<div class="krow"><span class="k">Windows Firewall</span><span class="v">' +
        (fw.likely_blocking === true ? dot("bad", "Not allowing port " + j.port)
          : fw.likely_blocking === false ? dot("good", fw.rule_exists ? "Rule in place" : "Allowed")
          : dot("off", fw.detail || "not checked")) +
        "</span></div>";
    }
    h += '<div class="krow"><span class="k">External connectivity</span><span class="v">' +
      dot(cs === "reachable" ? "good" : cs === "checking" ? "warn" : "bad",
          j.connectivity_label) + "</span></div>";
  }

  h += '<div class="krow"><span class="k">RCON</span><span class="v">' +
    dot("off", "Local/LAN only — never forwarded") + "</span></div>";
  h += '<div class="krow"><span class="k">SFTP / SSH</span><span class="v">' +
    dot("off", "Local/LAN only — never forwarded") + "</span></div>";
  h += '<div class="krow"><span class="k">Panel</span><span class="v">' +
    dot("off", "Port " + esc((j.reserved || {}).panel) + ", never forwarded") + "</span></div>";

  h += '<div class="hint" style="margin-top:12px;padding-top:11px;border-top:2px solid var(--line)">' +
    '<b>' + esc(j.headline) + "</b>";
  if (j.connectivity && j.connectivity.reason) h += "<br>" + esc(j.connectivity.reason);
  if (j.error) h += '<br><span style="color:var(--red)">' + esc(j.error) + "</span>";
  h += "</div>";

  if (j.mode === "tunnel") {
    const t = j.tunnel || {};
    const isNg = t.provider === "ngrok";
    const pname = isNg ? "ngrok" : "playit.gg";
    const phome = t.download || (isNg ? "https://ngrok.com/download" : "https://playit.gg/download");
    if (!t.installed) {
      h += '<div class="hint" style="margin-top:11px;border-left:3px solid var(--warn);padding-left:11px">' +
        "<b>The " + esc(pname) + " agent is not installed.</b><br>Download it from " +
        '<a href="' + esc(phome) + '" target="_blank" rel="noopener">' + esc(phome.replace(/^https?:\/\//, "")) + "</a>" +
        ", then point Lodestone at it in Settings below. Lodestone will not " +
        "download or install it for you.</div>";
    } else if (t.needs_claim && t.claim_url) {
      h += '<div class="hint" style="margin-top:11px;border-left:3px solid var(--warn);padding-left:11px">' +
        (isNg
          ? "<b>ngrok needs an authtoken.</b><br>Copy yours from " +
            '<a href="' + esc(t.claim_url) + '" target="_blank" rel="noopener">' + esc(t.claim_url) + "</a>" +
            " and run <code>ngrok config add-authtoken &lt;your token&gt;</code> in a " +
            "terminal once. Then press Test connection. Lodestone never asks you for " +
            "the token and never stores it — it stays in ngrok's own config."
          : "<b>Claim the tunnel to finish.</b><br>Open " +
            '<a href="' + esc(t.claim_url) + '" target="_blank" rel="noopener">' + esc(t.claim_url) + "</a>" +
            ", sign in to playit, and approve it. Then press Test connection. " +
            "Lodestone cannot do this step for you — it needs your playit account.") +
        "</div>";
    } else if (t.address) {
      h += '<div class="hint" style="margin-top:11px">Friends connect to <b>' + esc(t.address) +
        "</b> instead of your IP. This works from behind CGNAT and needs no port " +
        "forwarding. Traffic passes through " + esc(pname) + " servers, so expect a little " +
        "extra latency." +
        (isNg ? " On the free plan this address is reissued every time the agent " +
                "restarts, so re-send it to friends after a restart." : "") +
        "</div>";
    }
    if ((t.log || []).length) {
      h += '<div class="hint" style="margin-top:10px"><b>Agent output</b><pre style="white-space:pre-wrap;' +
        'font-size:10.5px;margin:6px 0 0;color:var(--faint)">' +
        esc(t.log.join("\n")) + "</pre></div>";
    }
  }

  if (internet && (cs === "cgnat_suspected" || cs === "ipv6_only")) {
    h += '<div class="hint" style="margin-top:11px;border-left:3px solid var(--red);padding-left:11px">' +
      "<b>Direct internet hosting unavailable.</b><br>" +
      "Your network may be using CGNAT, which prevents incoming connections.<br><br>" +
      "<b>Possible solutions:</b><br>" +
      "&bull; Use IPv6 if your friends' connections support it" +
      (j.ipv6_address ? " — your address is <code>" + esc(j.ipv6_address) + "</code>" : "") + "<br>" +
      "&bull; Ask your ISP for a public IPv4 address — many will provide one on request<br>" +
      "&bull; Configure port forwarding on the outermost router if there are two<br>" +
      "&bull; Use a tunnel, VPN or relay (Tailscale, playit.gg, ngrok, a small VPS)</div>";
  }
  if (internet && cs === "port_blocked") {
    h += '<div class="hint" style="margin-top:11px">The router is forwarding the port but ' +
      "Windows is not answering on it. " +
      '<button class="btn sm gold" onclick="netFirewall(\'add\')">Allow through firewall</button></div>';
  }
  if (internet && cs === "server_offline") {
    h += '<div class="hint" style="margin-top:11px">Start the Minecraft server, then test again. ' +
      "Nothing can reach a server that is not running.</div>";
  }
  $("netDash").innerHTML = h;
}

function renderNetSteps(j) {
  const steps = j.steps || [];
  $("netStepsCard").style.display = steps.length ? "" : "none";
  if (!steps.length) return;
  const NAME = {
    interfaces: "Local addresses", properties: "server.properties",
    public_ip: "Public address", mapping: "Router port forward",
    firewall: "Windows Firewall", connectivity: "Reachable from outside",
    done: "Result"
  };
  const MARK = { ok: ["good", "done"], fail: ["bad", "failed"], warn: ["warn", "warning"],
                 run: ["warn", "working…"], skip: ["off", "skipped"] };
  let h = "";
  for (const s of steps) {
    const m = MARK[s.state] || ["off", s.state];
    h += '<div class="krow"><span class="k">' + esc(NAME[s.key] || s.key) + "</span>" +
      '<span class="v" style="max-width:62%">' + dot(m[0], m[1]) + "</span></div>";
    if (s.detail) {
      h += '<div class="hint" style="margin:-2px 0 8px 14px">' + esc(s.detail) + "</div>";
    }
  }
  const att = j.mapping_attempts || [];
  if (att.length > 1 || (att.length === 1 && !att[0].ok)) {
    h += '<div class="hint" style="margin-top:10px"><b>Port mapping attempts</b><br>';
    for (const a of att) {
      h += "<br>" + (a.ok ? "✓ " : "✗ ") + esc(a.provider) + " — " + esc(a.detail || "");
      if (a.hint) h += '<br><span style="color:var(--faint);margin-left:14px">' + esc(a.hint) + "</span>";
    }
    h += "</div>";
  }
  $("netSteps").innerHTML = h;
}
function netHideSteps() { $("netStepsCard").style.display = "none"; }

function renderNetSettings(j) {
  const s = j.settings || {};
  const tog = (id, on, label, hint) =>
    '<label class="tog" style="display:flex;margin-bottom:9px"><input type="checkbox" id="' + id +
    '"' + (on ? " checked" : "") + '><span class="box"></span><span class="lbl">' + label +
    "</span></label>" + (hint ? '<div class="hint" style="margin:-6px 0 12px 47px">' + hint + "</div>" : "");
  let h = '<div class="grid2"><div>' +
    '<label class="fld"><span>Minecraft port</span><input class="inp" id="netPort" value="' +
      esc(j.port) + '"><div class="hint">Must match <code>server-port</code> in server.properties. ' +
      "Lodestone will not publish port " + esc((j.reserved || {}).panel) + " (the panel)" +
      ((j.reserved || {}).rcon ? ", " + esc(j.reserved.rcon) + " (RCON)" : "") +
      " or 22 (SFTP/SSH).</div></label>" +
    '<label class="fld"><span>External port</span><input class="inp" id="netExtPort" value="' +
      esc(j.different_ports ? j.external_port : "") + '" placeholder="same as the Minecraft port">' +
      '<div class="hint">Leave empty unless your ISP blocks 25565 or you host two servers. ' +
      "Friends then connect to the external port.</div></label>" +
    "</div><div>" +
    tog("netUpnp", s.upnp, "UPnP IGD", "The one nearly every home router speaks.") +
    tog("netNatpmp", s.natpmp, "NAT-PMP", "RFC 6886. Tried when UPnP is off or refuses.") +
    tog("netPcp", s.pcp, "PCP", "RFC 6887. Newer, and the one some CGNAT boxes answer.") +
    tog("netFirewall", s.firewall, "Manage the Windows Firewall rule",
        "Creates one inbound allow rule for this port only. Never disables the firewall. " +
        "Needs an administrator prompt.") +
    tog("netPublicIp", s.public_ip_lookup, "Look up my public address",
        "Asks api.ipify.org and icanhazip.com what address the internet sees. " +
        "Nothing but the request itself is sent.") +
    tog("netExternal", s.external_test, "Test reachability from outside",
        "Asks mcstatus.io and mcsrvstat.us to connect to your address and port. " +
        "This is the only way to prove the server is publicly reachable. " +
        "With it off, the status can never rise above “unknown”.") +
    '<label class="fld"><span>Tunnel provider</span>' +
      '<select class="inp" id="netProvider" onchange="netProviderChanged()">' +
      '<option value="playit"' + (s.tunnel_provider === "ngrok" ? "" : " selected") + ">playit.gg</option>" +
      '<option value="ngrok"' + (s.tunnel_provider === "ngrok" ? " selected" : "") + ">ngrok</option>" +
      "</select>" +
      '<div class="hint">Which agent Tunnel mode runs. Both dial out, so neither needs ' +
      "anything opened on your router. playit.gg is approved once in a browser; ngrok is " +
      "authenticated once with <code>ngrok config add-authtoken</code>. Lodestone asks for " +
      "neither credential and stores neither.</div></label>" +
    '<div id="netPlayitRow"' + (s.tunnel_provider === "ngrok" ? ' style="display:none"' : "") + ">" +
    '<label class="fld"><span>playit.gg agent</span>' +
      '<div class="row"><input class="inp" id="netAgent" value="' + esc(s.tunnel_agent || "") +
      '" placeholder="found automatically">' +
      '<button class="btn sm" onclick="netPickAgent()">Browse</button></div>' +
      '<div class="hint">Only needed if Lodestone cannot find it. ' +
      "Get it from <a href=\"https://playit.gg/download\" target=\"_blank\" " +
      'rel="noopener">playit.gg/download</a>.' +
      "</div></label></div>" +
    '<div id="netNgrokRow"' + (s.tunnel_provider === "ngrok" ? "" : ' style="display:none"') + ">" +
    '<label class="fld"><span>ngrok agent</span>' +
      '<div class="row"><input class="inp" id="netNgrok" value="' + esc(s.ngrok_agent || "") +
      '" placeholder="found automatically">' +
      '<button class="btn sm" onclick="netPickNgrok()">Browse</button></div>' +
      '<div class="hint">Only needed if Lodestone cannot find it. ' +
      "Get it from <a href=\"https://ngrok.com/download\" target=\"_blank\" " +
      'rel="noopener">ngrok.com/download</a>, then run ' +
      "<code>ngrok config add-authtoken &lt;your token&gt;</code> once." +
      "</div></label></div>" +
    tog("netAuto", s.auto_enable, "Re-enable internet hosting when Lodestone starts",
        "Off by default. The port is opened only while Lodestone is running either way.") +
    "</div></div>";
  h += '<div class="hint" style="margin-top:6px;border-top:2px solid var(--line);padding-top:11px">' +
    "<b>What gets published.</b> Only the Minecraft port, only TCP, and only while internet " +
    "mode is on. RCON, SFTP, SSH and this panel are never forwarded — the port picker refuses " +
    "them outright. Turning internet mode off removes the mapping Lodestone created and leaves " +
    "every other rule on your router alone.</div>";
  if (j.windows) {
    const fw = j.firewall || {};
    h += '<div class="row wrap" style="margin-top:11px">' +
      '<button class="btn sm" onclick="netFirewall(\'check\')">Check firewall</button>' +
      '<button class="btn sm" onclick="netFirewall(\'add\')">Allow port ' + esc(j.port) + "</button>" +
      '<button class="btn sm danger" onclick="netFirewall(\'remove\')">Remove Lodestone’s rule</button>' +
      '<span class="pill">' + esc(fw.rule || "no rule") + "</span>" +
      (fw.admin ? '<span class="pill on">running as admin</span>' : "") + "</div>";
  }
  $("netSettings").innerHTML = h;
}

async function netSetMode(mode) {
  const go = async () => {
    try {
      await act("/api/hosting/mode", { mode: mode, port: parseInt($("netPort") ?
        $("netPort").value : NET.port, 10) || NET.port }, "Switching…");
    } catch (e) { return; }
    loadNetwork();
    netStartPoll();
  };
  if (mode !== "internet") return go();
  modal("Publish this server to the internet?",
    '<div class="hint" style="font-size:13px;line-height:1.7">' +
    "Lodestone will ask your router to forward <b>TCP port " + esc(NET ? NET.port : 25565) +
    "</b> to this PC, then check from outside whether it worked.<br><br>" +
    "<b>Anyone who learns your address can attempt to connect.</b> Minecraft servers on public " +
    "addresses are scanned constantly and get join attempts from strangers within hours. " +
    "Turn on the whitelist if you only want your friends in.<br><br>" +
    "Only the Minecraft port is opened. RCON, SFTP and this panel stay on your network. " +
    "Turning internet mode off again removes the forwarding rule." +
    "</div>",
    [["Cancel", "closeModal()"], ["Publish", "closeModal();netConfirmInternet()", "gold"]]);
}
function netConfirmInternet() {
  act("/api/hosting/mode", { mode: "internet" }, "Setting up internet hosting…")
    .then(() => { loadNetwork(); netStartPoll(); }).catch(() => {});
}

function netDisable() {
  confirmBox("Turn off internet hosting?",
    "The port mapping Lodestone created will be removed from your router. Other rules are left " +
    "alone. The server keeps running and stays reachable on your local network.",
    async () => {
      try { await act("/api/hosting/disable", { remove_firewall: false }, "Internet hosting off."); }
      catch (e) { }
      loadNetwork();
    });
}

async function netPickAgent() {
  try {
    const j = await api("/api/pick?kind=anyfile");
    if (j.path) { $("netAgent").value = j.path; toast("Picked. Press Save.", "good"); }
  } catch (e) { toast(e.message, "bad"); }
}

async function netPickNgrok() {
  try {
    const j = await api("/api/pick?kind=anyfile");
    if (j.path) { $("netNgrok").value = j.path; toast("Picked. Press Save.", "good"); }
  } catch (e) { toast(e.message, "bad"); }
}

// Only the selected provider's path field is worth showing; the other one is
// noise that invites pasting the wrong binary into it.
function netProviderChanged() {
  const ng = $("netProvider") && $("netProvider").value === "ngrok";
  if ($("netPlayitRow")) $("netPlayitRow").style.display = ng ? "none" : "";
  if ($("netNgrokRow")) $("netNgrokRow").style.display = ng ? "" : "none";
}

async function netAutofix() {
  modal("Run auto fix?",
    '<div class="hint" style="font-size:13px;line-height:1.7">Lodestone will, in order:' +
    "<br>&bull; correct <code>server-ip</code> and <code>server-port</code> if they do not match this mode" +
    "<br>&bull; start the Minecraft server if it is not running" +
    "<br>&bull; ask your router for the port forward if there is not one" +
    "<br>&bull; add the Windows Firewall rule if nothing allows the port (asks for admin)" +
    "<br>&bull; test again from outside" +
    "<br><br><b>It cannot fix everything.</b> A router upstream of yours, CGNAT, or an " +
    "ISP that blocks inbound connections are all outside this machine's reach — " +
    "anything it cannot do, it will tell you about instead of pretending." +
    "<br><br>Starting a server can take a minute or two.</div>",
    [["Cancel", "closeModal()"], ["Run it", "closeModal();netRunAutofix()", "gold"]]);
}
async function netRunAutofix() {
  try { await act("/api/hosting/autofix", {}, "Auto fix running…"); }
  catch (e) { return; }
  loadNetwork(); netStartPoll();
}

function renderNetFix(j) {
  const f = j.autofix;
  if (!f) { $("netFixCard").style.display = "none"; return; }
  $("netFixCard").style.display = "";
  let h = "";
  if ((f.fixed || []).length) {
    h += '<div style="margin-bottom:12px"><div style="font-size:13px;color:var(--green)">' +
      "Fixed " + f.fixed.length + " thing(s)</div>";
    for (const x of f.fixed) h += '<div class="hint">✓ ' + esc(x) + "</div>";
    h += "</div>";
  } else {
    h += '<div class="hint" style="margin-bottom:12px">Nothing needed fixing on this machine.</div>';
  }
  if ((f.manual || []).length) {
    h += '<div style="font-size:13px;color:var(--warn)">Lodestone cannot do these for you</div>';
    for (const x of f.manual) {
      h += '<div class="hint" style="border-left:3px solid var(--warn);padding-left:10px;margin-top:8px">' +
        esc(x) + "</div>";
    }
  } else if (j.online) {
    h += '<div class="hint" style="color:var(--green)">Everything is working — your server is ' +
      "reachable at <b>" + esc(j.public_address) + "</b>.</div>";
  }
  $("netFix").innerHTML = h;
}

async function netTest() {
  try { await act("/api/hosting/test", { deep_firewall: true }, "Checking…"); } catch (e) { return; }
  loadNetwork(); netStartPoll();
}

async function netProbe() {
  let j;
  try { j = await api("/api/hosting/probe"); } catch (e) { toast(e.message, "bad"); return; }
  let h = '<div class="hint" style="font-size:13px">Gateway: <code>' +
    esc(j.gateway || "not found") + "</code></div><div class=\"items\" style=\"margin-top:10px\">";
  for (const p of j.providers || []) {
    h += '<div class="item"><div style="flex:1"><div class="ttl">' + esc(p.provider) +
      "</div><div class=\"sub\">" + esc(p.protocol) + " — " + esc(p.detail) + "</div></div>" +
      '<span class="pill ' + (p.available ? "on" : "bad") + '">' +
      (p.available ? "supported" : "no") + "</span></div>";
  }
  h += "</div>";
  const rows = j.router_table || [];
  h += '<div class="hint" style="margin-top:12px"><b>Everything your router currently forwards' +
    " (" + rows.length + ")</b> — read only, nothing here was changed.";
  for (const r of rows) {
    h += "<br><code>" + esc(r.protocol) + " " + esc(r.external_port) + " → " +
      esc(r.internal_client) + ":" + esc(r.internal_port) + "</code> " + esc(r.description || "");
  }
  if (!rows.length) h += "<br>Nothing, or your router will not list them.";
  h += "</div>";
  modal("Router support", h, [["Close", "closeModal()"]]);
}

async function netSaveSettings() {
  const body = {
    port: parseInt($("netPort").value, 10),
    external_port: parseInt($("netExtPort").value, 10) || 0,
    upnp: $("netUpnp").checked, natpmp: $("netNatpmp").checked, pcp: $("netPcp").checked,
    firewall: $("netFirewall").checked, public_ip_lookup: $("netPublicIp").checked,
    external_test: $("netExternal").checked, auto_enable: $("netAuto").checked,
    tunnel_provider: ($("netProvider") ? $("netProvider").value : "playit"),
    tunnel_agent: ($("netAgent") ? $("netAgent").value : "").trim(),
    ngrok_agent: ($("netNgrok") ? $("netNgrok").value : "").trim()
  };
  try { await act("/api/hosting/settings", body, "Saved."); } catch (e) { return; }
  loadNetwork();
}

async function netFirewall(action) {
  try {
    const j = await act("/api/hosting/firewall", { action: action },
      action === "check" ? null : "Done.");
    if (action === "check") {
      const r = j.result || {};
      const names = (r.allowing_rules || []).map(x => x.name).join(", ");
      modal("Windows Firewall",
        '<div class="hint" style="font-size:13px;line-height:1.7">' + esc(r.detail || "") +
        "<br><br>Firewall on for the current profile: <b>" +
        (r.state === true ? "yes" : r.state === false ? "no" : "unknown") + "</b>" +
        "<br>Current profile: <b>" + esc(r.current_profile || "unknown") + "</b>" +
        "<br>Lodestone rule: <b>" + (r.rule_exists ? "present" : "not created") + "</b> (" +
        esc(r.rule || "") + ")" +
        (names ? "<br>Rules already allowing this port: " + esc(names) : "") +
        "</div>", [["Close", "closeModal()"]]);
    }
  } catch (e) { return; }
  loadNetwork();
}

async function netApplyProps() {
  try {
    const j = await act("/api/hosting/properties", {},
      "server.properties updated — restart the server to apply.");
    if (j && !j.changed) toast("It was already correct.", "good");
  } catch (e) { return; }
  loadNetwork();
}

async function netAudit(deep) {
  let j;
  try { j = await api("/api/hosting/security" + (deep ? "?router=1" : "")); }
  catch (e) { return; }
  const items = j.items || [];
  $("netWarnCard").style.display = items.length ? "" : "none";
  if (!items.length) return;
  const COL = { bad: "var(--red)", warn: "var(--warn)", info: "var(--dim)" };
  let h = "";
  for (const it of items) {
    h += '<div style="margin-bottom:12px;border-left:3px solid ' + (COL[it.level] || "var(--line2)") +
      ';padding-left:11px">' +
      '<div style="font-size:13px;color:' + (COL[it.level] || "var(--text)") + '">' +
      esc(it.title) + "</div>" +
      '<div class="hint">' + esc(it.detail) + (it.fix ? "<br><b>" + esc(it.fix) + "</b>" : "") +
      "</div></div>";
  }
  $("netWarn").innerHTML = h;
}

/* ---------------- modal ---------------- */
function modal(title, body, buttons) {
  $("mTitle").textContent = title;
  $("mBody").innerHTML = body;
  $("mFoot").innerHTML = (buttons || []).map(b =>
    '<button class="btn ' + (b[2] || "") + '" onclick="' + b[1] + '">' + b[0] + "</button>").join("");
  $("modal").classList.add("on");
}
function closeModal() { $("modal").classList.remove("on"); }
let PENDING = null;
function confirmBox(title, text, fn) {
  PENDING = fn;
  modal(title, '<div class="hint" style="font-size:13px">' + esc(text) + "</div>",
    [["Cancel", "closeModal()"], ["Confirm", "runPending()", "danger"]]);
}
function promptBox(title, label, value, fn) {
  PENDING = () => fn($("mInput").value);
  modal(title, '<label class="fld"><span>' + esc(label) + '</span><input class="inp" id="mInput" value="' +
    esc(value) + '"></label>', [["Cancel", "closeModal()"], ["OK", "runPending()", "gold"]]);
  setTimeout(() => { const el = $("mInput"); if (el) { el.focus(); el.select(); } }, 30);
}
function runPending() { const f = PENDING; PENDING = null; closeModal(); if (f) f(); }
document.addEventListener("keydown", e => {
  if (e.key === "Escape") closeModal();
  if (e.key === "Enter" && $("modal").classList.contains("on") && $("mInput")) runPending();
});

/* ---------------- drag and drop ---------------- */
(function () {
  const zone = $("dropZone");
  let depth = 0;
  window.addEventListener("dragenter", e => {
    if (!$("view-files").classList.contains("show")) return;
    e.preventDefault(); depth++; zone.classList.add("on");
  });
  window.addEventListener("dragover", e => e.preventDefault());
  window.addEventListener("dragleave", () => { if (--depth <= 0) { depth = 0; zone.classList.remove("on"); } });
  window.addEventListener("drop", e => {
    e.preventDefault(); depth = 0; zone.classList.remove("on");
    if (!$("view-files").classList.contains("show")) return;
    if (e.dataTransfer.files.length) doUpload(e.dataTransfer.files);
  });
})();

/* ---------------- boot ---------------- */
(async function () {
  try {
    await loadShelf();
    const recent = await api("/api/recent");
    LINES = recent.lines.map(l => l[1]);
    LASTSEQ = recent.lines.length ? recent.lines[recent.lines.length - 1][0] : 0;
    renderLog();
    paintStatus(await api("/api/status"));
    FEATURES = await api("/api/features");
    renderChips();
  } catch (e) { toast("Could not reach the panel: " + e.message, "bad"); }
  applySize();
  try {
    const me = await api("/api/me");
    if (me.mode) { applyMode(me.mode); }
    else {
      // first run: someone new starts simple, someone with servers keeps everything
      const m = (SERVERS || []).length ? "advanced" : "simple";
      applyMode(m);
      api("/api/ui/mode", { mode: m }).catch(() => {});
    }
  } catch (e) { applyMode("advanced"); }
  // home first: pick the server, then open it
  showView("servers");
  startStream();
})();
</script></body></html>
"""

PAGE = (APP_HTML.replace("__CSS__", COMMON_CSS)
        .replace("__ICON__", ICON_SVG)
        .replace("__VER__", VERSION)
        .replace("__EVENTS__", json.dumps([[e[0], e[1], e[2]] for e in EVENTS])))


# ---------------------------------------------------------------------------
#  desktop bits
# ---------------------------------------------------------------------------


def native_pick(kind):
    """Open the OS picker on the machine running the panel."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as e:
        return None, "No native picker here (%s). Type the path instead." % e
    try:
        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes("-topmost", True)
        except Exception:
            pass
        if kind == "folder":
            p = filedialog.askdirectory(title="Select the server folder")
        elif kind == "anyfile":
            p = filedialog.askopenfilename(title="Select a file")
        else:
            p = filedialog.askopenfilename(title="Select the server JAR",
                                           filetypes=[("Server JAR", "*.jar"), ("All files", "*.*")])
        try:
            root.update()
        except Exception:
            pass
        root.destroy()
    except Exception as e:
        return None, "Could not open the picker (%s). Type the path instead." % e
    return (p or None), None


def already_running(port, timeout=2.5):
    """Is there a Lodestone on this port already?

    Matters because the exe is built --noconsole: when the port is taken there
    is no console for the error to appear in, so a second launch looked like
    nothing happening at all. Now a second launch finds the first and just
    shows it.
    """
    try:
        req = urllib.request.Request("http://127.0.0.1:%d/api/me" % port)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read(65536))
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    # Older builds predate the "app" marker, so fall back on the shape.
    return data.get("app") == APP or ("backups" in data and "config" in data)


def tell_user(title, message):
    """Say something the user will actually see.

    print() is useless in the windowed build, which is how "it just does not
    open" happened in the first place — so on Windows this also raises a real
    dialog.

    The dialog is modal and blocks until it is dismissed, so it is suppressed
    when a test or a script is driving: an automated run must never stop dead
    waiting for somebody to click OK on a box they did not ask for.
    """
    print("%s: %s" % (title, message))
    quiet = os.environ.get("LODESTONE_TEST_HOME") or os.environ.get("LODESTONE_NO_DIALOG")
    if sys.platform.startswith("win") and not quiet:
        try:
            import ctypes
            # MB_ICONERROR | MB_SETFOREGROUND | MB_TOPMOST
            ctypes.windll.user32.MessageBoxW(None, str(message), str(title),
                                             0x10 | 0x10000 | 0x40000)
        except Exception:
            pass


def open_ui(url):
    """Prefer an app-style window (Chrome/Edge --app) over a browser tab."""
    try:
        if sys.platform.startswith("win"):
            for p in (
                os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
                os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
            ):
                if os.path.isfile(p):
                    subprocess.Popen([p, "--app=" + url])
                    return
        elif sys.platform == "darwin":
            for app in ("Google Chrome", "Microsoft Edge", "Chromium"):
                try:
                    subprocess.Popen(["open", "-na", app, "--args", "--app=" + url])
                    return
                except Exception:
                    continue
        else:
            for b in ("google-chrome", "chromium", "chromium-browser", "microsoft-edge"):
                path = shutil.which(b)
                if path:
                    subprocess.Popen([path, "--app=" + url])
                    return
    except Exception:
        pass
    webbrowser.open(url)


ICON_FILE_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" shape-rendering="crispEdges"><rect x="2" y="2" width="28" height="28" fill="#2f3140"/><rect x="4" y="4" width="24" height="24" fill="#4a4d63"/><rect x="6" y="6" width="20" height="20" fill="#23242f"/><rect x="4" y="4" width="24" height="2" fill="#6b6f8a"/><rect x="4" y="26" width="24" height="2" fill="#1b1c25"/><rect x="8" y="8" width="2" height="2" fill="#3a3c4d"/><rect x="22" y="8" width="2" height="2" fill="#3a3c4d"/><rect x="8" y="22" width="2" height="2" fill="#3a3c4d"/><rect x="22" y="22" width="2" height="2" fill="#3a3c4d"/><rect x="14" y="9" width="4" height="14" fill="#123524"/><rect x="9" y="14" width="14" height="4" fill="#123524"/><rect x="15" y="10" width="2" height="12" fill="#22c55e"/><rect x="10" y="15" width="12" height="2" fill="#22c55e"/><rect x="15" y="10" width="2" height="4" fill="#e8fff2"/><rect x="14" y="14" width="4" height="4" fill="#4ade80"/><rect x="15" y="15" width="2" height="2" fill="#eafff3"/></svg>"""

def asset(name):
    """Find a file shipped next to the script, or inside a PyInstaller bundle."""
    here = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    p = here / name
    return p if p.is_file() else None


BASE_URL = "http://127.0.0.1:%d" % DEFAULT_PORT

# The running HTTP server, so the panel can be told to shut itself down. The
# windowed build has no console and no tray icon, so without this the only way
# out is Task Manager — which is how people end up with four copies running and
# a port nobody will give back.
SERVER = None


# ---------------------------------------------------------------------------
#  HTTP
# ---------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "%s/%s" % (APP, VERSION)

    def log_message(self, *a):
        pass

    # -- plumbing ----------------------------------------------------------
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _err(self, msg, code=400):
        return self._json({"ok": False, "error": str(msg)}, code)

    def _text(self, text, ctype="text/html; charset=utf-8", code=200):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def _bytes(self, data, ctype, cache="no-cache"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", cache)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        try:
            self.wfile.write(data)
        except OSError:
            pass

    def _body(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode() or "{}")
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return {}

    def _q(self, key, default=""):
        return (parse_qs(urlparse(self.path).query).get(key, [default]) or [default])[0]

    def _send_file(self, path, filename, ctype="application/octet-stream", cleanup=False):
        try:
            size = os.path.getsize(path)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % filename.replace('"', ""))
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(path, "rb") as f:
                shutil.copyfileobj(f, self.wfile, 65536)
        except OSError as e:
            self._err(e, 500)
        finally:
            if cleanup:
                try:
                    os.remove(path)
                except OSError:
                    pass

    # -- GET ---------------------------------------------------------------
    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith("/api/v1/"):
            return self._api_v1("GET", path)
        try:
            return self._get(path)
        except Conflict as e:
            return self._err(e, 409)
        except Exception as e:
            return self._err(e)

    def _get(self, path):
        if path in ("/", "/index.html"):
            return self._text(PAGE)
        if path == "/icon.svg":
            return self._text(ICON_FILE_SVG, "image/svg+xml")
        if path == "/favicon.ico":
            ico = asset("Lodestone.ico")
            if ico:
                return self._bytes(ico.read_bytes(), "image/x-icon", "max-age=86400")
            return self._text(ICON_FILE_SVG, "image/svg+xml")
        if path == "/api/update":
            return self._json({"ok": True, **PANEL.update_state()})
        if path == "/api/me":
            host = PANEL.cfg.get("panel_host", "127.0.0.1")
            return self._json({
                "app": APP,
                "host": host, "port": PANEL.cfg.get("port", DEFAULT_PORT),
                "lan_ip": local_ip(), "url": BASE_URL, "config": str(CONFIG_PATH),
                "backups": str(BACKUP_ROOT), "paramiko": HAVE_PARAMIKO, "version": VERSION,
                "mode": PANEL.cfg.get("ui", {}).get("mode", "")})
        if path == "/api/status":
            # ?sid= asks about one server by id (compact) - the plain call stays the selected server's
            sid = self._q("sid")
            if sid and sid != PANEL.cfg.get("active"):
                try:
                    return self._json(PANEL.status_for(sid))
                except KeyError:
                    return self._err("No such server.", 404)
            return self._json(PANEL.status())
        if path == "/api/events":
            # The Discord event catalogue. The browser page has it baked in;
            # the desktop app asks for it so the two can never disagree.
            return self._json({"items": [[e[0], e[1], e[2]] for e in EVENTS]})
        if path == "/api/recent":
            # ?sid= reads that server's console buffer, not the selected one's
            sid = self._q("sid") or None
            if sid and not PANEL.find(sid):
                return self._err("No such server.", 404)
            return self._json({"lines": PANEL.recent(400, sid=sid)})
        if path == "/api/stream":
            return self._stream()
        if path == "/api/servers":
            return self._json({"items": PANEL.listing()})
        if path == "/api/profiles":
            return self._json({"ok": True, "items": PANEL.profiles(),
                               "colors": list(PROFILE_COLORS), "aikar": AIKAR_FLAGS})
        if path == "/api/servers/legacy":
            return self._json({"items": PANEL.legacy_available()})
        if path == "/api/servers/get":
            s = PANEL.find(self._q("id"))
            if not s:
                return self._err("No such server.", 404)
            # Minehut credentials belong to a third-party account, so they are
            # never handed back out — the UI shows "saved" and only sends a
            # replacement when the user types one.
            safe = json.loads(json.dumps(s))
            mh = safe.setdefault("minehut", {})
            mh["has_token"] = bool(mh.get("token"))
            mh["has_session"] = bool(mh.get("session_id"))
            mh["token"] = ""
            mh["session_id"] = ""
            return self._json({"server": safe})
        if path == "/api/features":
            return self._json(PANEL.features_get())
        if path == "/api/files":
            return self._json(PANEL.backend().list_dir(self._q("path")))
        if path == "/api/file":
            return self._json(PANEL.backend().read_file(self._q("path")))
        if path == "/api/properties":
            return self._json(PANEL.props_get())
        if path == "/api/backups":
            return self._json(PANEL.backups_list())
        if path == "/api/discord":
            srv = PANEL.server()
            return self._json({"hooks": srv.setdefault("discord", {}).setdefault("hooks", []),
                               "stats": PANEL.discord.stats()})
        if path == "/api/ai":
            return self._json(PANEL.ai_get())
        if path == "/api/logs":
            return self._json({"ok": True, "items": PANEL.log_files()})
        if path == "/api/plugins":
            return self._json(PANEL.plugins_list())
        if path == "/api/worlds":
            return self._json(PANEL.worlds_list())
        if path == "/api/appearance":
            return self._json(PANEL.motd_get())
        if path == "/api/icon":
            try:
                sid = self._q("sid")
                if sid:
                    return self._bytes(PANEL.icon_bytes_for(sid), "image/png")
                return self._bytes(PANEL.icon_bytes(), "image/png")
            except Exception:
                return self._err("No server icon set.", 404)
        if path == "/api/art/icons":
            return self._json({"ok": True, **PANEL.art_icons()})
        if path.startswith("/api/art/icon/") and path.endswith(".png"):
            key = path[len("/api/art/icon/"):-4]
            try:
                size = int(self._q("size", "64") or 64)
            except ValueError:
                size = 64
            data = texmod.png(str(TEX_ROOT), key, size) if HAVE_TEX else None
            if not data:
                return self._err("No such icon (yet).", 404)
            return self._bytes(data, "image/png", "max-age=86400")
        if path == "/api/art/sheet":
            # one picture holding a whole group of icons, in catalogue order
            if not HAVE_TEX:
                return self._err("No icons.", 404)
            group = self._q("group", "")
            try:
                cell = max(16, min(96, int(self._q("size", "48") or 48)))
            except ValueError:
                cell = 48
            keys = [x["key"] for x in texmod.catalog(str(TEX_ROOT)) if x["group"] == group]
            data = texmod.sheet(str(TEX_ROOT), keys, cell, 16)
            if not data:
                return self._err("No such group.", 404)
            return self._bytes(data, "image/png", "max-age=86400")
        if path.startswith("/api/art/") and path.endswith(".png"):
            name = path[len("/api/art/"):-4]
            try:
                size = int(self._q("size", "64") or 64)
            except ValueError:
                size = 64
            data = None
            # With the textures here, a kind of server is drawn as the item or
            # block its instances get; the hand-drawn art is the fallback.
            if HAVE_TEX and name in texmod.SOFTWARE_ICON:
                data = texmod.png(str(TEX_ROOT), texmod.SOFTWARE_ICON[name], size)
            if not data:
                data = artmod.png(name, size) if HAVE_ART else None
            if not data:
                return self._err("No such picture.", 404)
            return self._bytes(data, "image/png", "max-age=86400")
        if path == "/api/list":
            return self._json(PANEL.list_get(self._q("which", "ops")))
        if path == "/api/chat/sniff":
            return self._json(PANEL.chat_sniff())
        if path == "/api/ram":
            return self._json({"ok": True, **PANEL.ram_state()})
        if path == "/api/hosting":
            return self._json(PANEL.hosting_state(
                probe=self._q("probe") in ("1", "true", "yes")))
        if path == "/api/hosting/security":
            return self._json({"ok": True, "items": PANEL.hosting_security(
                check_router=self._q("router") in ("1", "true", "yes"))})
        if path == "/api/hosting/probe":
            return self._json(PANEL.hosting_probe())
        if path == "/api/offline":
            return self._json(PANEL.offline_state())
        # -- creating servers
        if path == "/api/create/catalog":
            return self._json({"ok": True, **PANEL.create_catalog(
                self._q("snapshots") in ("1", "true", "yes"))})
        if path == "/api/create/builds":
            return self._json({"ok": True, **PANEL.create_builds(self._q("software"), self._q("mc"))})
        if path == "/api/java":
            return self._json({"ok": True, **PANEL.java_list(self._q("mc"), self._q("software"))})
        if path == "/api/jobs":
            if self._q("id"):
                return self._json({"ok": True, "job": PANEL.job_get(self._q("id"), self._q("since", "0"))})
            return self._json({"ok": True, "items": PANEL.jobs_list()})
        # -- mods and plugins. ?sid= names a server; ?software=&mc= describes
        # one the wizard has not made yet.
        if path.startswith("/api/content/"):
            sid = self._q("sid") or None
            if sid and not PANEL.find(sid):
                return self._err("No such server.", 404)
            sw, mc = self._q("software") or None, self._q("mc") or None
            if path == "/api/content/profile":
                return self._json({"ok": True, "profile": PANEL.content_profile(sid, sw, mc)})
            if path == "/api/content/search":
                try:
                    offset = int(self._q("offset", "0") or 0)
                except ValueError:
                    offset = 0
                return self._json({"ok": True, **PANEL.content_search(
                    self._q("source", "modrinth"), self._q("q"), offset, self._q("sort", "relevance"),
                    self._q("all") in ("1", "true", "yes"), sid, sw, mc, self._q("category"))})
            if path == "/api/content/project":
                return self._json({"ok": True, **PANEL.content_project(
                    self._q("source", "modrinth"), self._q("id"), sid, sw, mc)})
            if path == "/api/content/installed":
                return self._json({"ok": True, **PANEL.content_installed(sid)})
            if path == "/api/content/categories":
                return self._json({"ok": True, **PANEL.content_categories(
                    self._q("source", "modrinth"), sid, sw, mc)})
            if path == "/api/content/icon":
                try:
                    return self._bytes(PANEL.content_icon(self._q("file"), sid), "image/png", "max-age=3600")
                except RuntimeError as e:
                    return self._err(e, 404)
            if path == "/api/content/updates":
                return self._json({"ok": True, **PANEL.content_updates(sid)})
        # -- development assistant and tools
        if path.startswith("/api/agent/") or path.startswith("/api/dev/"):
            return self._dev_get(path)
        if path == "/api/perf":
            sid = self._q("sid") or None
            if sid and not PANEL.find(sid):
                return self._err("No such server.", 404)
            return self._json({"ok": True, **PANEL.perf_state(sid)})
        if path == "/api/tokens":
            return self._json({"enabled": bool(PANEL.cfg["api"].get("enabled")),
                               "items": PANEL.api_tokens()})
        if path == "/api/pick":
            p, err = native_pick(self._q("kind", "jar"))
            return self._err(err) if err else self._json({"path": p or ""})
        if path == "/api/download":
            tmp, name = PANEL.backend().fetch_to_temp(self._q("path"))
            cleanup = PANEL.backend().kind != "local"
            return self._send_file(tmp, name, cleanup=cleanup)
        if path == "/api/download/folder":
            tmp, name = PANEL.backend().zip_folder_to_temp(self._q("path"))
            return self._send_file(tmp, name, "application/zip", cleanup=True)
        if path == "/api/backup/download":
            name = safe_name(self._q("name"))
            p = PANEL.backup_dir() / name
            if not p.is_file():
                return self._err("No such backup.", 404)
            return self._send_file(str(p), name, "application/zip")
        return self._err("Not found", 404)

    # -- development assistant ---------------------------------------------
    # The assistant runs builds and commands on this computer, so its routes
    # answer only this computer - even when the panel is opened to the LAN.
    def _local_only(self):
        host = (self.client_address or ("",))[0]
        return host in ("127.0.0.1", "::1", "::ffff:127.0.0.1") or host.startswith("127.")

    def _dev_get(self, path):
        if not self._local_only():
            return self._err("The assistant and the code tools only answer on this computer.", 403)
        if path == "/api/agent/state":
            return self._json({"ok": True, **PANEL.dev_state()})
        if path == "/api/agent/ollama":
            return self._json({"ok": True, **PANEL.dev_ollama(self._q("fresh") in ("1", "true"))})
        if path == "/api/agent/recommend":
            return self._json({"ok": True, **PANEL.dev_recommend(self._q("model"))})
        ag = PANEL.dev_agent()
        if path == "/api/agent/events":
            try:
                since = int(self._q("since", "0") or 0)
                wait = float(self._q("wait", "0") or 0)
            except ValueError:
                since, wait = 0, 0
            run = ag.get(self._q("run"))
            evs = run.since(since, wait)
            return self._json({"ok": True, "events": evs, "run": run.summary()})
        if path == "/api/agent/runs":
            return self._json({"ok": True, "items": ag.active()})
        ws = PANEL.dev_workspace(self._q("ws")) if self._q("ws") else None
        if path == "/api/agent/history":
            return self._json({"ok": True, "items": ag.store(ws).history()})
        if path == "/api/agent/memory":
            return self._json({"ok": True, "items": ag.store(ws).memory()})
        if path == "/api/agent/changes":
            items = [c for c in ag.store(ws).changes() if not self._q("run") or c.get("run") == self._q("run")]
            return self._json({"ok": True, "items": items[-200:][::-1]})
        if path == "/api/agent/index":
            return self._json({"ok": True, "overview": ag.overview(ws, refresh=False)})
        if path == "/api/dev/files":
            return self._json({"ok": True, **PANEL.dev_list(self._q("ws"), self._q("path"))})
        if path == "/api/dev/file":
            return self._json({"ok": True, **PANEL.dev_read(self._q("ws"), self._q("path"))})
        if path == "/api/dev/skript":
            return self._json({"ok": True, **PANEL.dev_skript(self._q("ws"))})
        if path == "/api/dev/java":
            return self._json({"ok": True, **PANEL.dev_java(self._q("ws"))})
        if path == "/api/dev/find":
            if not ag.index(ws).data.get("files"):
                ag.index(ws).build(budget_s=10)
            return self._json({"ok": True, "items": ag.index(ws).find(self._q("q"), 30)})
        return self._err("Not found", 404)

    def _dev_post(self, path, b):
        if not self._local_only():
            return self._err("The assistant and the code tools only answer on this computer.", 403)
        b = b or {}
        if path == "/api/agent/settings":
            return self._json({"ok": True, **PANEL.dev_settings_save(b), "msg": "Saved."})
        if path == "/api/agent/model":
            return self._json({"ok": True, **PANEL.dev_model_action(b.get("action", ""), b.get("model"))})
        ag = PANEL.dev_agent()
        if path == "/api/agent/workspace/add":
            w = ag.add_workspace(b.get("root", ""), b.get("name", ""), b.get("server_id", ""))
            return self._json({"ok": True, "workspace": w, "msg": "Added %s." % w["name"]})
        if path == "/api/agent/workspace/remove":
            if str(b.get("id", "")).startswith("srv-"):
                return self._err("A server's workspace goes away with the server.")
            ag.remove_workspace(b.get("id", ""))
            return self._json({"ok": True})
        if path == "/api/agent/approve":
            ag.approve(b.get("run", ""), b.get("call_id", ""), b.get("decision", ""), b.get("note", ""))
            return self._json({"ok": True})
        if path == "/api/agent/cancel":
            return self._json({"ok": True, "cancelled": ag.cancel(b.get("run", ""))})
        # these two work on pasted text as well as on a workspace
        if path == "/api/dev/trace":
            return self._json({"ok": True, **PANEL.dev_trace(b.get("text", ""), b.get("ws") or None)})
        if path == "/api/dev/check" and b.get("text") is not None:
            return self._json({"ok": True, "check": PANEL.dev_check(name=b.get("name") or "file.txt",
                                                                    text=b.get("text"))})
        ws = PANEL.dev_workspace(b.get("ws", ""))
        if path == "/api/agent/run":
            run = ag.start(ws, b.get("message", ""), b.get("attachments") or [])
            return self._json({"ok": True, "run": run.summary()})
        if path == "/api/agent/clear":
            ag.store(ws).clear_history()
            return self._json({"ok": True, "msg": "Conversation cleared."})
        if path == "/api/agent/index":
            st = ag.index(ws).build(budget_s=60)
            return self._json({"ok": True, "index": st})
        if path == "/api/agent/memory/add":
            return self._json({"ok": True, "items": ag.store(ws).remember(b.get("text", ""), "you")})
        if path == "/api/agent/memory/delete":
            return self._json({"ok": True, "items": ag.store(ws).forget(b.get("id", ""))})
        if path == "/api/agent/revert":
            res = ag.revert(ws, b.get("change", ""))
            return self._json({"ok": True, **res, "msg": "Put back %s." % res["path"]})
        if path == "/api/dev/file/save":
            return self._json({"ok": True, **PANEL.dev_save(ws["id"], b.get("path", ""), b.get("content", ""),
                                                             b.get("mtime"))})
        if path == "/api/dev/check":
            return self._json({"ok": True, "check": PANEL.dev_check(ws["id"], b.get("path"))})
        if path == "/api/dev/skript/reload":
            return self._json({"ok": True, **PANEL.dev_skript_reload(ws["id"], b.get("script", ""))})
        if path == "/api/dev/build":
            return self._json({"ok": True, "job": PANEL.dev_build(ws["id"], b.get("task", "build"))})
        if path == "/api/dev/build/cancel":
            return self._json({"ok": PANEL.dev_job_cancel(b.get("job", ""))})
        return self._err("Not found", 404)

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        try:
            last = int(self._q("since", "0") or 0)
        except ValueError:
            last = 0
        last_status = 0.0
        try:
            while True:
                for seq, text in PANEL.since(last):
                    last = seq
                    self.wfile.write(("event: line\ndata: %s\n\n"
                                      % json.dumps({"seq": seq, "text": text})).encode())
                now = time.time()
                if now - last_status > 1.5:
                    last_status = now
                    self.wfile.write(("event: status\ndata: %s\n\n"
                                      % json.dumps(PANEL.status())).encode())
                self.wfile.flush()
                time.sleep(0.35)
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            return

    # -- POST --------------------------------------------------------------
    def do_POST(self):
        path = urlparse(self.path).path
        if path.startswith("/api/v1/"):
            return self._api_v1("POST", path)
        try:
            return self._post(path)
        except Conflict as e:
            return self._json({"ok": False, "error": str(e), "disk_mtime": e.disk_mtime}, 409)
        except Exception as e:
            return self._err(e)

    def do_PUT(self):
        if urlparse(self.path).path.startswith("/api/v1/"):
            return self._api_v1("PUT", urlparse(self.path).path)
        return self._err("Not found", 404)

    def do_PATCH(self):
        if urlparse(self.path).path.startswith("/api/v1/"):
            return self._api_v1("PATCH", urlparse(self.path).path)
        return self._err("Not found", 404)

    def do_DELETE(self):
        if urlparse(self.path).path.startswith("/api/v1/"):
            return self._api_v1("DELETE", urlparse(self.path).path)
        return self._err("Not found", 404)

    def _post(self, path):
        # uploads read the body as raw bytes, so handle before _body()
        if path == "/api/icon":
            n = int(self.headers.get("Content-Length", 0) or 0)
            if n > 1_000_000:
                return self._err("That file is far too big for a server icon.")
            return self._json(PANEL.icon_set(self.rfile.read(n)))
        if path == "/api/upload":
            n = int(self.headers.get("Content-Length", 0) or 0)
            res = PANEL.backend().upload(self._q("path"), self._q("name"), self.rfile, n)
            PANEL.emit(">>> uploaded %s (%s)" % (res["name"], fmt_size(res["size"])))
            PANEL.discord.event("files", "File uploaded", "%s — %s" % (res["name"], fmt_size(res["size"])))
            return self._json({"ok": True, **res})

        b = self._body()
        be = PANEL.backend()

        if path == "/api/command":
            ok, out = PANEL.command(b.get("command", ""), who="panel",
                                    sid=b.get("sid") or None)
            return self._json({"ok": ok, "out": out}) if ok else self._err(out)
        if path == "/api/ram/close":
            name = ((b or {}).get("name") or "").strip()
            if not name:
                return self._err("Which application?")
            try:
                res = PANEL.ram_close(name, force=bool((b or {}).get("force")))
            except RuntimeError as e:
                return self._err(str(e))
            return self._json({"ok": True, **res})
        if path == "/api/plugins/pack":
            which = (b or {}).get("sid") or None
            if which and not PANEL.find(which):
                return self._err("No such server.", 404)
            try:
                rep = PANEL.install_pack(which)
            except RuntimeError as e:
                return self._err(str(e))
            if not rep.get("preset_ok", True) and not rep.get("results"):
                return self._err(rep.get("reason") or "Nothing to install here.")
            if not rep.get("ok"):
                return self._err("Some packages failed — see the console.")
            return self._json({"ok": True, "report": rep})
        if path == "/api/plugins/via":
            which = (b or {}).get("sid") or None
            if which and not PANEL.find(which):
                return self._err("No such server.", 404)
            try:
                rep = PANEL.install_via(which)
            except RuntimeError as e:
                return self._err(str(e))
            if not rep.get("compatible"):
                return self._err(rep.get("reason") or "Not a plugin server.")
            if not rep.get("ok"):
                return self._err("Some plugins failed — see the console.")
            return self._json({"ok": True, "report": rep})
        # -- creating servers
        if path == "/api/create":
            return self._json({"ok": True, "job": PANEL.create_start(b)})
        if path == "/api/create/validate":
            spec = PANEL.create_validate(b)
            return self._json({"ok": True, "dir": spec["dir"], "warnings": spec.get("warnings", [])})
        if path == "/api/java/install":
            return self._json({"ok": True, "job": PANEL.java_install(b.get("major"))})
        if path == "/api/art/fetch":
            return self._json({"ok": True, **PANEL.art_fetch(b.get("force"))})
        if path == "/api/servers/detect":
            # The look always succeeds; "found" says whether there is a server there.
            r = PANEL.detect_folder(b.get("path", ""))
            return self._json(dict(r, ok=True, found=r["ok"]))
        if path == "/api/servers/add-local":
            return self._json({"ok": True, "server": PANEL.add_local(b)})
        # -- mods and plugins
        if path.startswith("/api/content/"):
            which = (b or {}).get("sid") or None
            if which and not PANEL.find(which):
                return self._err("No such server.", 404)
            if path == "/api/content/plan":
                return self._json({"ok": True, **PANEL.content_plan(
                    b.get("items") or [], which, b.get("software") or None, b.get("mc") or None)})
            if path == "/api/content/install":
                rep = PANEL.content_install(b.get("items") or [], which)
                n = len(rep["installed"]) + len(rep["updated"])
                msg = "%d installed%s" % (n, ", %d failed" % len(rep["failed"]) if rep["failed"] else "")
                if rep.get("restart"):
                    msg += " - restart the server to load %s" % ("it" if n == 1 else "them")
                return self._json({"ok": True, "report": rep, "msg": msg + "."})
            if path == "/api/content/update":
                rep = PANEL.content_update(b.get("files") or None, which)
                return self._json({"ok": True, "report": rep,
                                   "msg": rep.get("msg") or "%d updated." % (
                                       len(rep["installed"]) + len(rep["updated"]))})
            if path == "/api/content/remove":
                res = PANEL.content_remove(b.get("file", ""), which)
                return self._json({"ok": True, **res, "msg": "Removed %s." % res["name"]})
            if path == "/api/content/toggle":
                return self._json({"ok": True, **PANEL.content_toggle(b.get("file", ""), which)})
        if path in ("/api/start", "/api/stop", "/api/restart", "/api/kill"):
            # An explicit id lets the Servers tab drive a server that is not the
            # one currently on screen; without one this behaves exactly as before.
            which = (b or {}).get("sid") or None
            if which and not PANEL.find(which):
                return self._err("No such server.", 404)
            action = {"/api/start": PANEL.start, "/api/stop": PANEL.stop,
                      "/api/restart": PANEL.restart, "/api/kill": PANEL.kill}[path]
            ok, msg = action(which)
            return self._json({"ok": ok, "msg": msg}) if ok else self._err(msg)
        if path == "/api/eula":
            PANEL.accept_eula()
            return self._json({"ok": True, "msg": "EULA accepted."})
        if path == "/api/connect":
            ok, msg = PANEL.connect()
            return self._json({"ok": ok, "msg": msg}) if ok else self._err(msg)
        if path == "/api/disconnect":
            ok, msg = PANEL.disconnect()
            return self._json({"ok": ok, "msg": msg})
        if path == "/api/player":
            ok, out = PANEL.player_action(b.get("player", ""), b.get("action", ""), b.get("reason", ""))
            return self._json({"ok": ok, "out": out}) if ok else self._err(out)
        if path == "/api/macro":
            ok, msg = PANEL.macro_run(b.get("name", ""))
            return self._json({"ok": ok, "msg": msg}) if ok else self._err(msg)
        if path == "/api/features":
            return self._json(PANEL.features_save(b))

        # -- files
        if path == "/api/file/save":
            res = be.write_file(b.get("path", ""), b.get("content", ""), b.get("mtime"))
            PANEL.discord.event("files", "File edited", b.get("path", ""))
            return self._json({"ok": True, **res})
        if path == "/api/file/new":
            return self._json({"ok": True, **be.new_file(b.get("path", ""), b.get("name", ""))})
        if path == "/api/file/mkdir":
            return self._json({"ok": True, **be.mkdir(b.get("path", ""), b.get("name", ""))})
        if path == "/api/file/rename":
            return self._json({"ok": True, **be.rename(b.get("path", ""), b.get("name", ""))})
        if path == "/api/file/copy":
            return self._json({"ok": True, **be.copy(b.get("path", ""))})
        if path == "/api/file/delete":
            be.delete(b.get("path", ""))
            PANEL.discord.event("files", "File deleted", b.get("path", ""))
            return self._json({"ok": True})

        # -- properties / backups
        if path == "/api/properties":
            return self._json(PANEL.props_save(b.get("values", {})))
        if path == "/api/backup/create":
            ok, msg = PANEL.backup_create(b.get("label", ""), b.get("what", "world"))
            return self._json({"ok": ok, "msg": msg}) if ok else self._err(msg)
        if path == "/api/backup/restore":
            PANEL.backup_restore(b.get("name", ""))
            return self._json({"ok": True, "msg": "Restore started."})
        if path == "/api/backup/delete":
            PANEL.backup_delete(b.get("name", ""))
            return self._json({"ok": True})

        # -- shelf
        if path == "/api/servers/save":
            return self._json({"ok": True, "server": PANEL.upsert(b)})
        if path == "/api/servers/delete":
            PANEL.delete_server(b.get("id", ""))
            return self._json({"ok": True})
        if path == "/api/servers/duplicate":
            return self._json({"ok": True, "server": PANEL.duplicate(b.get("id", ""))})
        if path == "/api/servers/activate":
            return self._json({"ok": True, "server": PANEL.activate(b.get("id", ""))})
        if path == "/api/servers/look":
            s = PANEL.set_look(b)
            return self._json({"ok": True, "profile": s.get("profile", ""), "look": s.get("look", {})})
        if path == "/api/profiles/save":
            return self._json({"ok": True, "profile": PANEL.profile_save(b)})
        if path == "/api/profiles/delete":
            return self._json({"ok": PANEL.profile_delete(b.get("id", ""))})
        if path == "/api/profiles/apply":
            return self._json(PANEL.profile_apply(b.get("id", ""), b.get("sid") or None))
        if path == "/api/logs/search":
            return self._json(PANEL.log_search(b.get("query", ""), b.get("files"),
                                               int(b.get("limit") or 400),
                                               bool(b.get("regex")), bool(b.get("case"))))
        if path == "/api/worlds/activate":
            return self._json(PANEL.world_activate(b.get("name", "")))
        if path == "/api/worlds/reset":
            return self._json(PANEL.world_reset(b.get("name", ""), b.get("mode", "contents")))
        if path == "/api/appearance":
            return self._json(PANEL.motd_save(b.get("values", {})))
        if path == "/api/icon/clear":
            return self._json(PANEL.icon_clear())
        if path == "/api/plugins/toggle":
            return self._json(PANEL.plugin_toggle(b.get("path", "")))
        if path == "/api/list/edit":
            ok, out = PANEL.list_edit(b.get("which", ""), b.get("player", ""), bool(b.get("add")))
            return self._json({"ok": ok, "out": out}) if ok else self._err(out)
        if path == "/api/chat/test":
            return self._json(PANEL.chat_test(b.get("line", "")))
        if path == "/api/chat/save":
            srv = PANEL.server()
            srv.setdefault("chat", {})["pattern"] = str(b.get("pattern", ""))
            srv["chat"]["enabled"] = bool(b.get("enabled", True))
            PANEL._chat_re_cache = (None, None)
            PANEL.save()
            return self._json({"ok": True})
        if path == "/api/servers/import":
            added = PANEL.import_legacy()
            return self._json({"ok": True, "added": added,
                               "msg": ("Imported: " + ", ".join(added)) if added
                                      else "Nothing new to import."})
        if path == "/api/sftp/test":
            srv = PANEL.find(b.get("id")) or PANEL.server()
            if srv.get("kind") != "sftp":
                return self._err("That server is not a remote one.")
            return self._json(SftpBackend(srv["sftp"]).diagnose())
        if path == "/api/sftp/use-root":
            srv = PANEL.find(b.get("id")) or PANEL.server()
            srv["sftp"]["root"] = str(b.get("root", "")) or "/"
            PANEL.backends.pop(srv["id"], None)
            PANEL.save()
            return self._json({"ok": True, "root": srv["sftp"]["root"]})
        if path == "/api/minehut/status":
            return self._json(PANEL.minehut_status(b.get("id")))
        if path == "/api/minehut/test":
            mh = PANEL.minehut(b.get("id"))
            if not mh:
                return self._err("Turn on Minehut for this server first.")
            ok, msg = mh.test()
            return self._json({"ok": ok, "out": msg, "error": "" if ok else msg})
        if path == "/api/minehut/action":
            ok, msg = PANEL.minehut_action(b.get("what", ""), b.get("id"))
            return self._json({"ok": ok, "msg": msg}) if ok else self._err(msg)
        if path == "/api/rcon/test":
            ok, out = PANEL.rcon_test(b.get("id"))
            return self._json({"ok": ok, "out": out, "error": "" if ok else out})
        if path == "/api/install":
            ok, msg = SftpBackend.install_paramiko()
            return self._json({"ok": ok, "msg": msg})

        # -- discord
        if path == "/api/discord":
            srv = PANEL.server()
            hooks = b.get("hooks") or []
            prev = {h.get("id"): h for h in srv.get("discord", {}).get("hooks", [])}
            for h in hooks:
                h.setdefault("id", new_id())
                h["events"] = [e for e in (h.get("events") or []) if e in EVENT_KEYS]
                old = prev.get(h.get("id")) or {}
                for k in ("sent", "failed", "last_error", "status_id"):
                    h.setdefault(k, old.get(k, 0 if k in ("sent", "failed") else ""))
                for k, v in (("thread_id", ""), ("chat_style", "player"),
                             ("status_msg", False), ("attach_log", True)):
                    h.setdefault(k, v)
            srv.setdefault("discord", {})["hooks"] = hooks
            PANEL.save()
            return self._json({"ok": True, "hooks": hooks})
        if path == "/api/discord/add":
            PANEL.server().setdefault("discord", {}).setdefault("hooks", []).append(Discord.blank_hook())
            PANEL.save()
            return self._json({"ok": True})
        if path == "/api/discord/fire":
            ev = b.get("event", "command")
            if ev not in EVENT_KEYS:
                return self._err("Unknown event.")
            PANEL.discord.event(ev, b.get("title", "Test event"),
                                b.get("description", ""), b.get("player", ""))
            return self._json({"ok": True, "msg": "Queued to every hook subscribed to that event."})
        if path == "/api/discord/test":
            for h in PANEL.discord.hooks():
                if h.get("id") == b.get("id"):
                    if not h.get("url"):
                        return self._err("Paste the webhook URL first.")
                    ok, msg = PANEL.discord.test(h)
                    return self._json({"ok": ok, "error": "" if ok else msg})
            return self._err("No such webhook.", 404)

        # -- assistant
        if path == "/api/ai/save":
            ok, msg = PANEL.ai_save(b)
            return self._json({"ok": ok, "msg": msg})
        if path == "/api/ai/models":
            ok, models, base = PANEL.ai_models()
            return self._json({"ok": ok, "models": models if ok else [],
                               "base": base, "error": "" if ok else models})
        if path == "/api/ai/test":
            ok, msg = PANEL.ai_test()
            return self._json({"ok": ok, "msg": msg, "error": "" if ok else msg})
        if path == "/api/ai/clear":
            ok, msg = PANEL.ai_clear()
            return self._json({"ok": ok, "msg": msg})
        if path == "/api/ai/attach":
            ok, msg, name, content = PANEL.ai_attach()
            return self._json({"ok": ok, "error": "" if ok else msg,
                               "name": name, "content": content})
        if path == "/api/ai/run":
            steps = []
            ok, reply, log, usage = PANEL.ai_run(b.get("instruction", ""),
                                                 b.get("attachments") or [],
                                                 lambda a, n: steps.append({"action": a, "detail": n}))
            return self._json({"ok": ok, "reply": reply, "log": log, "usage": usage}) if ok                 else self._err(reply)

        if path.startswith("/api/agent/") or path.startswith("/api/dev/"):
            return self._dev_post(path, b)
        if path == "/api/perf/measure":
            which = (b or {}).get("sid") or None
            if which and not PANEL.find(which):
                return self._err("No such server.", 404)
            return self._json({"ok": True, "tps": PANEL.perf_measure(which)})

        # -- hosting
        if path == "/api/hosting/mode":
            return self._json({"ok": True, "state": PANEL.hosting_apply(
                b.get("mode", "lan"), b.get("port"), b.get("external_port"))})
        if path == "/api/hosting/disable":
            return self._json(PANEL.hosting_disable(
                remove_firewall=bool(b.get("remove_firewall"))))
        if path == "/api/hosting/test":
            return self._json(PANEL.hosting_test(
                deep_firewall=bool(b.get("deep_firewall"))))
        if path == "/api/hosting/autofix":
            return self._json(PANEL.hosting_autofix())
        if path == "/api/hosting/settings":
            return self._json(PANEL.hosting_settings(b))
        if path == "/api/hosting/firewall":
            return self._json(PANEL.hosting_firewall(b.get("action", "check")))
        if path == "/api/hosting/properties":
            return self._json(PANEL.hosting_apply_properties())

        # -- offline / non-premium mode
        if path == "/api/offline":
            return self._json(PANEL.offline_set(bool(b.get("enabled")), b.get("allow_lan")))

        # -- api tokens
        if path == "/api/tokens/enable":
            PANEL.cfg["api"]["enabled"] = bool(b.get("enabled"))
            PANEL.save()
            return self._json({"ok": True})
        if path == "/api/tokens/create":
            made = PANEL.api_token_create(b.get("name", ""), b.get("scopes", []))
            return self._json({"ok": True, "token": made["token"]})
        if path == "/api/tokens/delete":
            return self._json({"ok": PANEL.api_token_delete(b.get("id", ""))})

        # -- shut the panel down
        # -- updates
        if path == "/api/update/check":
            return self._json({"ok": True, **PANEL.update_check()})
        if path == "/api/update/install":
            st = PANEL.update_install()
            return self._json({"ok": True, **st, "msg": "Update installed - restart Lodestone to use it."
                               if st["applied"] else "Already up to date."})
        if path == "/api/update/rollback":
            return self._json({"ok": True, **PANEL.update_rollback(),
                               "msg": "The previous version is back - restart Lodestone to use it."})
        if path == "/api/update/settings":
            return self._json({"ok": True, **PANEL.update_settings(b or {}), "msg": "Saved."})
        if path == "/api/update/restart":
            res = PANEL.update_restart(bool(b.get("force")), int(b.get("app_pid") or 0))
            return self._json(res, 200 if res.get("ok") else 409)
        if path == "/api/quit":
            running = PANEL.is_running()
            if running and not b.get("force"):
                return self._json({
                    "ok": False, "needs_force": True,
                    "error": "The Minecraft server is still running. Quitting "
                             "Lodestone would kill it."}, 409)
            def bye():
                # shutdown() blocks until the serve loop stops, and the serve
                # loop is what is running this request — calling it inline
                # deadlocks. Hand it to another thread and let this reply out.
                time.sleep(0.4)
                try:
                    if running:
                        PANEL.stop()
                        for _ in range(60):
                            if not PANEL.is_running():
                                break
                            time.sleep(0.5)
                except Exception:
                    pass
                try:
                    PANEL.hosting_shutdown()
                except Exception:
                    pass
                if SERVER:
                    SERVER.shutdown()
            threading.Thread(target=bye, daemon=True).start()
            return self._json({"ok": True,
                               "msg": "Shutting down — you can close this window."})

        # -- settings
        if path == "/api/ui/mode":
            mode = b.get("mode")
            if mode not in ("simple", "advanced"):
                return self._err("Unknown mode.")
            PANEL.cfg.setdefault("ui", {})["mode"] = mode
            PANEL.save()
            return self._json({"ok": True, "mode": mode})
        if path == "/api/settings":
            if b.get("host") in ("127.0.0.1", "0.0.0.0"):
                PANEL.cfg["panel_host"] = b["host"]
            try:
                port = int(b.get("port") or 0)
                if 1024 <= port <= 65535:
                    PANEL.cfg["port"] = port
            except (TypeError, ValueError):
                pass
            PANEL.save()
            return self._json({"ok": True})

        return self._err("Not found", 404)

    # -- REST API v1 -------------------------------------------------------
    def _api_v1(self, method, path):
        """Token-authenticated surface for scripts. Separate from the UI routes
        on purpose: the UI trusts whoever can reach it, this does not."""
        if not PANEL.cfg["api"].get("enabled"):
            return self._json({"ok": False, "error": "The API is switched off. Turn it on in the "
                                                     "API tab of the panel."}, 503)
        tok = PANEL.api_auth(self.headers.get("Authorization", ""))
        if not tok:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Bearer realm="%s"' % APP)
            self.send_header("Content-Type", "application/json")
            body = json.dumps({"ok": False, "error": "Bad or missing bearer token."}).encode()
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass
            return
        scopes = set(tok.get("scopes", []))

        def need(*want):
            if "admin" in scopes or scopes.intersection(want):
                return True
            self._json({"ok": False, "error": "This token needs one of: %s" % ", ".join(want)}, 403)
            return False

        route = path[len("/api/v1/"):].strip("/")
        b = self._body() if method in ("POST", "PUT", "PATCH") else {}
        be = PANEL.backend()
        PANEL.save()   # persist the token's last-used stamp

        try:
            # ---- read
            if method == "GET" and route == "health":
                return self._json({"ok": True, "app": APP, "version": VERSION,
                                   "servers": len(PANEL.servers()),
                                   "active": PANEL.server().get("name")})
            if method == "GET" and route == "servers":
                if not need("read"):
                    return
                return self._json({"ok": True, "items": PANEL.listing()})
            if method == "GET" and route == "status":
                if not need("read"):
                    return
                return self._json({"ok": True, "status": PANEL.status()})
            if method == "GET" and route == "hosting":
                if not need("read"):
                    return
                return self._json({"ok": True, "hosting": PANEL.hosting_state()})
            if method == "POST" and route == "hosting":
                # admin only: this reconfigures the router and the host firewall,
                # which is well past what a "control" token is for.
                if not need("admin"):
                    return
                mode = b.get("mode", "")
                if mode == "off":
                    return self._json(PANEL.hosting_disable(
                        remove_firewall=bool(b.get("remove_firewall"))))
                if not HAVE_NET:
                    return self._err(NET_ERROR or "Networking support is not "
                                                  "available.", 503)
                if mode not in netmod.MODES:
                    return self._err("mode must be one of local, lan, internet, off.")
                return self._json({"ok": True, "hosting": PANEL.hosting_apply(
                    mode, b.get("port"), b.get("external_port"))})
            if method == "GET" and route == "offline":
                if not need("read"):
                    return
                return self._json({"ok": True, "offline": PANEL.offline_state()})
            if method == "POST" and route == "offline":
                # admin only, like hosting: it changes who can log in.
                if not need("admin"):
                    return
                return self._json({"ok": True, "offline": PANEL.offline_set(
                    bool(b.get("enabled")), b.get("allow_lan"))})
            if method == "GET" and route == "content":
                if not need("read"):
                    return
                return self._json({"ok": True, **PANEL.content_installed()})
            if method == "POST" and route == "content/install":
                # "files": it writes jars into the server folder.
                if not need("files"):
                    return
                return self._json({"ok": True, "report": PANEL.content_install(b.get("items") or [])})
            if method == "POST" and route == "content/update":
                if not need("files"):
                    return
                return self._json({"ok": True, "report": PANEL.content_update(b.get("files") or None)})
            if method == "POST" and route == "servers/create":
                # admin only: it downloads and runs installers on this machine.
                if not need("admin"):
                    return
                return self._json({"ok": True, "job": PANEL.create_start(b)})
            if method == "GET" and route == "jobs":
                if not need("read"):
                    return
                if self._q("id"):
                    return self._json({"ok": True, "job": PANEL.job_get(self._q("id"), self._q("since", "0"))})
                return self._json({"ok": True, "items": PANEL.jobs_list()})
            if method == "POST" and route == "hosting/test":
                if not need("read"):
                    return
                return self._json(PANEL.hosting_test())
            if method == "GET" and route == "players":
                if not need("read"):
                    return
                return self._json({"ok": True, "players": sorted(PANEL.players),
                                   "count": len(PANEL.players)})
            if method == "GET" and route == "console":
                if not need("read"):
                    return
                try:
                    since = int(self._q("since", "0") or 0)
                except ValueError:
                    since = 0
                lines = PANEL.since(since)
                return self._json({"ok": True, "lines": [{"seq": s, "text": t} for s, t in lines],
                                   "last": lines[-1][0] if lines else since})
            if method == "GET" and route == "files":
                if not need("read"):
                    return
                return self._json({"ok": True, **be.list_dir(self._q("path"))})
            if method == "GET" and route == "file":
                if not need("read"):
                    return
                return self._json({"ok": True, **be.read_file(self._q("path"))})
            if method == "GET" and route == "properties":
                if not need("read"):
                    return
                return self._json(PANEL.props_get())
            if method == "GET" and route == "backups":
                if not need("read"):
                    return
                return self._json(PANEL.backups_list())

            # ---- control
            if method == "POST" and route == "command":
                if not need("control"):
                    return
                ok, out = PANEL.command(b.get("command", ""), who="api:%s" % tok.get("name"))
                return self._json({"ok": ok, "output": out} if ok else {"ok": False, "error": out},
                                  200 if ok else 400)
            if method == "POST" and route in ("start", "stop", "restart"):
                if not need("control"):
                    return
                ok, msg = getattr(PANEL, route)()
                return self._json({"ok": ok, "message": msg} if ok else {"ok": False, "error": msg},
                                  200 if ok else 400)
            if method == "POST" and route == "player":
                if not need("control"):
                    return
                ok, out = PANEL.player_action(b.get("player", ""), b.get("action", ""), b.get("reason", ""))
                return self._json({"ok": ok, "output": out} if ok else {"ok": False, "error": out},
                                  200 if ok else 400)
            if method == "POST" and route == "backup":
                if not need("control"):
                    return
                ok, msg = PANEL.backup_create(b.get("label", "api"), b.get("what", "world"))
                return self._json({"ok": ok, "message": msg} if ok else {"ok": False, "error": msg},
                                  200 if ok else 400)
            if method == "POST" and route == "webhook":
                if not need("control"):
                    return
                ev = b.get("event", "command")
                if ev not in EVENT_KEYS:
                    return self._json({"ok": False, "error": "Unknown event. One of: %s"
                                                             % ", ".join(EVENT_KEYS)}, 400)
                PANEL.discord.event(ev, b.get("title", "Custom event"),
                                    b.get("description", ""), b.get("player", ""))
                return self._json({"ok": True, "message": "Queued to subscribed webhooks."})

            if method == "POST" and route == "assistant":
                if not need("control"):
                    return
                ok, reply, log, usage = PANEL.ai_run(b.get("instruction", ""))
                return self._json({"ok": ok, "reply": reply, "steps": log, "usage": usage}
                                  if ok else {"ok": False, "error": reply}, 200 if ok else 400)

            # ---- writes
            if method == "PUT" and route == "file":
                if not need("files"):
                    return
                res = be.write_file(b.get("path", ""), b.get("content", ""), b.get("mtime"))
                return self._json({"ok": True, **res})
            if method == "PATCH" and route == "properties":
                if not need("files"):
                    return
                return self._json(PANEL.props_save(b.get("values", {})))
            if method == "DELETE" and route == "file":
                if not need("files"):
                    return
                be.delete(self._q("path"))
                return self._json({"ok": True})

            # ---- admin
            if method == "POST" and route == "servers/activate":
                if not need("admin"):
                    return
                return self._json({"ok": True, "server": PANEL.activate(b.get("id", ""))})

            return self._json({"ok": False, "error": "No such endpoint: %s %s" % (method, path)}, 404)
        except Conflict as e:
            return self._json({"ok": False, "error": str(e), "disk_mtime": e.disk_mtime}, 409)
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 400)


class PanelServer(ThreadingHTTPServer):
    """SO_REUSEADDR means opposite things on the two platforms. On Unix it
    avoids a TIME_WAIT wait; on Windows it lets a second process bind straight
    over the first, so launching Lodestone twice would silently give you two
    instances fighting for the port. Off on Windows, on everywhere else."""

    allow_reuse_address = not sys.platform.startswith("win")
    daemon_threads = True


# ---------------------------------------------------------------------------
#  main
# ---------------------------------------------------------------------------


def main():
    global BASE_URL
    port = int(PANEL.cfg.get("port") or DEFAULT_PORT)
    for a in sys.argv[1:]:
        if a.isdigit():
            port = int(a)
    host = PANEL.cfg.get("panel_host", "127.0.0.1")
    # --headless: the desktop app (desktop/, C# + Avalonia) is the window and
    # this process is only its engine, so no browser page is opened. The page
    # is still served at / for anyone who wants it.
    headless = "--headless" in sys.argv[1:]

    try:
        httpd = PanelServer((host, port), Handler)
    except OSError as e:
        # The commonest cause by far: a previous Lodestone is still running
        # because closing the browser window never stopped it. Rather than
        # failing invisibly, hand the user the one that is already there.
        if already_running(port):
            print("%s is already running on port %d — %s." % (
                APP, port, "leaving it be" if headless else "opening it"))
            if not headless:
                open_ui("http://127.0.0.1:%d" % port)
            return 0
        tell_user("%s could not start" % APP,
                  "Port %d on %s is being used by something that is not %s.\n\n"
                  "%s\n\nEither close that program, or start Lodestone on a "
                  "different port:\n\n    py lodestone.py %d"
                  % (port, host, APP, e, port + 1))
        return 1
    global SERVER
    SERVER = httpd
    httpd.daemon_threads = True

    shown = "127.0.0.1" if host == "127.0.0.1" else local_ip()
    BASE_URL = "http://%s:%d" % (shown, port)
    srv = PANEL.server()
    hooks = len([h for h in srv.get("discord", {}).get("hooks", []) if h.get("enabled")])

    print("%s %s  ->  %s" % (APP, VERSION, BASE_URL))
    print("  servers   %d on the shelf  (active: %s)" % (len(PANEL.servers()), srv.get("name")))
    print("  config    %s" % CONFIG_PATH)
    print("  backups   %s" % BACKUP_ROOT)
    print("  discord   %d webhook%s active" % (hooks, "" if hooks == 1 else "s"))
    print("  api       %s" % ("on, %d token(s)" % len(PANEL.cfg["api"].get("tokens", []))
                              if PANEL.cfg["api"].get("enabled") else "off"))
    hosting = PANEL.hosting_brief(srv["id"])
    print("  hosting   %s%s" % (
        hosting.get("mode_label", "LAN"),
        ", public" if hosting.get("online") else ""))
    if not HAVE_PARAMIKO:
        print("  note      paramiko missing — remote servers need it (Settings can install it)")
    if not HAVE_NET:
        print("  note      %s" % NET_ERROR)
        print("            Local and LAN hosting still work; the Network tab does not.")
    if host == "0.0.0.0":
        print("  warning   LAN access is on and the UI has no login")
    print("Ctrl+C to stop.")

    PANEL.emit(">>> %s %s ready on %s" % (APP, VERSION, BASE_URL))
    if srv.get("kind") == "sftp" and HAVE_PARAMIKO:
        threading.Thread(target=PANEL.connect, daemon=True).start()
    # Off the main thread: re-enabling internet hosting talks to the router and
    # to the internet, and the UI must be up before any of that starts.
    threading.Thread(target=PANEL.hosting_autostart, daemon=True).start()
    # The item and block textures instance icons are drawn from, fetched once
    # from Mojang. Not under the test suite, which never goes online.
    if HAVE_TEX and not os.environ.get("LODESTONE_TEST_HOME"):
        texmod.ensure_async(str(TEX_ROOT))
    # Updates from GitHub. Not under the test suite, which never goes online.
    if not (os.environ.get("LODESTONE_NO_UPDATE") or os.environ.get("LODESTONE_TEST_HOME")):
        threading.Thread(target=PANEL._update_loop, daemon=True).start()

    if not headless:
        threading.Thread(target=lambda: (time.sleep(0.6),
                                         open_ui("http://127.0.0.1:%d" % port)), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        # Closing the port we opened is the last thing to go wrong quietly, so
        # it happens first and on its own: a failure here leaves a hole in the
        # router that nothing is left running to close.
        try:
            PANEL.hosting_shutdown()
        except Exception:
            pass
        try:
            PANEL._stop_remote_tail()
            for be in PANEL.backends.values():
                be.close()
        except Exception:
            pass
        httpd.server_close()
    return 0


def _log_to(path):
    """--log FILE: send stdout and stderr to a file.

    The desktop app starts this engine with no console (pythonw, or a
    --noconsole build), where print() goes nowhere and a crash on the way up
    leaves nothing to read. With a log file the app can show the reason.
    """
    try:
        f = open(path, "a", encoding="utf-8", buffering=1)
    except OSError:
        return
    sys.stdout = f
    sys.stderr = f
    global _LOG_FILE
    _LOG_FILE = f
    # A crash in native code (a driver, ctypes) kills the process without a
    # Python traceback; faulthandler still writes where it happened.
    try:
        import faulthandler
        faulthandler.enable(f)
    except Exception:
        pass
    import atexit
    print("[%s] engine %s started, pid %d, Python %s" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), VERSION, os.getpid(), sys.version.split()[0]), flush=True)
    atexit.register(lambda: print("[%s] engine stopped" % time.strftime("%Y-%m-%d %H:%M:%S"), flush=True))


_LOG_FILE = None


if __name__ == "__main__":
    if "--log" in sys.argv:
        _i = sys.argv.index("--log")
        if _i + 1 < len(sys.argv):
            _log_to(sys.argv[_i + 1])
            del sys.argv[_i:_i + 2]
    sys.exit(main())
