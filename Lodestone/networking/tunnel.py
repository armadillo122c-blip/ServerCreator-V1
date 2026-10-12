"""Tunnels — reaching the server when no port forward ever can.

Port mapping assumes the router you can configure is the one the internet
arrives at. Behind carrier-grade NAT, or behind an ISP box you have no password
for, that assumption is simply false and no amount of UPnP will rescue it. A
tunnel sidesteps the whole problem: an agent on this machine makes an *outbound*
connection to a relay, and the relay accepts inbound players on its own public
address and forwards them down the pipe. Outbound always works.

The trade is real and worth stating plainly: traffic passes through somebody
else's servers, latency goes up, and the address belongs to them rather than to
you. That is why this is a separate mode the user chooses, not a silent
fallback when port forwarding fails.

Implemented here: playit.gg and ngrok. Both are free, need no router access,
and are driven the same way — a third-party binary Lodestone will locate, run,
read the output of and stop again, but which it will never download or install
behind the user's back. Neither account step is ours to take: playit needs a
tunnel approved in a browser, ngrok needs an authtoken added once with
`ngrok config add-authtoken`. Lodestone asks for neither and stores neither.
"""

import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque

from .errors import NetworkingError


def _no_window():
    """Keep the agent CLI from flashing a console window on Windows."""
    if not sys.platform.startswith("win"):
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}

PLAYIT_DOWNLOAD = "https://playit.gg/download"
PLAYIT_CLAIM_RE = re.compile(r"https?://(?:www\.)?playit\.gg/(?:claim|mc)/\S+")
# Tunnels are handed out either as a name under playit.gg or as a bare address
# on their anycast range; both turn up in the agent's output.
PLAYIT_ADDR_RE = re.compile(
    r"\b((?:[a-z0-9][a-z0-9-]*\.)+(?:playit\.gg|craft\.ply\.gg|ply\.gg)"
    r"|(?:\d{1,3}\.){3}\d{1,3})(?::(\d{1,5}))?\b", re.I)
PLAYIT_READY_RE = re.compile(r"tunnel (?:is )?(?:running|ready|online)|"
                             r"connected to control|agent (?:is )?online", re.I)
PLAYIT_AUTH_RE = re.compile(r"not (?:yet )?claimed|waiting for (?:the )?claim|"
                            r"visit .*claim|unauthenticated", re.I)

# Where the agent usually lives once installed, so the common case needs no
# path typed in at all.
CANDIDATE_NAMES = ("playit", "playit-agent", "playit-cli",
                   "playit-windows-x86_64-signed", "playit-windows-x86_64")


def _candidate_dirs():
    out = []
    if sys.platform.startswith("win"):
        for var in ("LOCALAPPDATA", "APPDATA", "ProgramFiles", "ProgramFiles(x86)",
                    "USERPROFILE"):
            base = os.environ.get(var)
            if not base:
                continue
            out += [os.path.join(base, "playit_gg"),
                    os.path.join(base, "playit"),
                    os.path.join(base, "Programs", "playit_gg"),
                    os.path.join(base, "Downloads")]
    else:
        home = os.path.expanduser("~")
        out += ["/usr/local/bin", "/usr/bin", "/opt/playit",
                os.path.join(home, ".local", "bin"), os.path.join(home, "Downloads")]
    return out


def find_agent(explicit=""):
    """Locate a playit agent. Returns a path or ""."""
    if explicit:
        return explicit if os.path.isfile(explicit) else ""
    for name in CANDIDATE_NAMES:
        found = shutil.which(name)
        if found:
            return found
    exts = (".exe",) if sys.platform.startswith("win") else ("", ".bin")
    for folder in _candidate_dirs():
        if not os.path.isdir(folder):
            continue
        try:
            entries = os.listdir(folder)
        except OSError:
            continue
        for entry in entries:
            stem = entry.lower()
            if not stem.startswith("playit"):
                continue
            if exts and not any(stem.endswith(e) for e in exts if e):
                if sys.platform.startswith("win"):
                    continue
            full = os.path.join(folder, entry)
            if os.path.isfile(full):
                return full
    return ""


class TunnelProvider:
    """One way of being reachable without touching the router."""

    name = "none"

    def available(self):
        """(bool, detail) — is the tooling present? Never raises."""
        return False, "not implemented"

    def start(self):
        raise NotImplementedError

    def stop(self):
        raise NotImplementedError

    def status(self):
        raise NotImplementedError

    def close(self):
        try:
            self.stop()
        except Exception:
            pass


class PlayitProvider(TunnelProvider):
    """Runs and watches a playit.gg agent as a child process.

    Deliberately does not manage accounts or tunnel configuration: the agent
    prints a claim URL on first run, the user opens it and approves the tunnel
    in their browser, and from then on the agent remembers. All this class does
    is run the thing, read what it says, and hand back the address.
    """

    name = "playit.gg"

    def __init__(self, agent_path="", log=None, port=25565):
        self.agent_path = agent_path
        self._service_style = None
        self._service_text = ""
        self._running_cache = None
        self.log = log or (lambda msg: None)
        self.port = int(port)
        self.proc = None
        self.lines = deque(maxlen=400)
        self._lock = threading.RLock()
        self._claim_url = ""
        self._address = ""
        self._ready = False
        self._started_at = 0.0
        self._exit_code = None

    # -- discovery -----------------------------------------------------------
    def resolve(self):
        path = find_agent(self.agent_path)
        if path:
            self.agent_path = path
        return path

    def available(self):
        path = self.resolve()
        if not path:
            return False, ("No playit agent found. Download it from %s, then "
                           "point Lodestone at it." % PLAYIT_DOWNLOAD)
        return True, path

    def version(self):
        path = self.resolve()
        if not path:
            return ""
        try:
            r = subprocess.run([path, "version"], capture_output=True, text=True,
                               timeout=8, errors="replace")
            out = (r.stdout or r.stderr or "").strip().splitlines()
            return out[0][:60] if out else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    # -- which kind of agent is this? ----------------------------------------
    #
    # There are two generations in the wild and they are driven completely
    # differently, so the right one has to be detected rather than assumed:
    #
    #   0.15.x  a plain foreground program. Run it, read its stdout, kill it.
    #   1.x     a Windows service (playitd) with a CLI in front of it:
    #           `playit start|stop|status`. Running the binary bare opens an
    #           interactive terminal UI, which is no use to us at all.
    #
    # `playit --help` listing a `start` command is the tell.
    def service_style(self):
        if self._service_style is None:
            self._service_style = self._detect_service_style()
        return self._service_style

    def _detect_service_style(self):
        if not self.resolve():
            return False
        try:
            _, text = self._cli("--help", timeout=10)
        except NetworkingError:
            return False
        return bool(re.search(r"^\s+start\s", text, re.M)) and "playitd" in text

    def _cli(self, *args, timeout=25):
        path = self.resolve()
        if not path:
            raise NetworkingError("No playit agent found on this machine.",
                                  "Download it from %s." % PLAYIT_DOWNLOAD)
        try:
            r = subprocess.run([path] + list(args), capture_output=True, text=True,
                               timeout=timeout, errors="replace", **_no_window())
        except subprocess.TimeoutExpired:
            raise NetworkingError("playit %s timed out." % " ".join(args))
        except (OSError, subprocess.SubprocessError) as e:
            raise NetworkingError("Could not run the playit agent: %s" % e)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()

    # -- running -------------------------------------------------------------
    RUNNING_TTL = 4.0

    def running(self):
        """Cheap. The service-style agent needs a subprocess to answer, and the
        panel asks twice a second, so the answer is cached briefly. start() and
        stop() clear it so a state change is never hidden behind the cache."""
        if not self.service_style():
            return bool(self.proc and self.proc.poll() is None)
        now = time.time()
        cached = self._running_cache
        if cached and now - cached[0] < self.RUNNING_TTL:
            return cached[1]
        try:
            _, text = self._cli("status", timeout=12)
        except NetworkingError:
            self._running_cache = (now, False)
            return False
        self._service_text = text
        alive = "not running" not in text.lower()
        self._running_cache = (now, alive)
        return alive

    def _invalidate(self):
        self._running_cache = None

    def start(self):
        if self.service_style():
            return self._start_service()
        if self.running():
            return True, "The playit agent is already running."
        path = self.resolve()
        if not path:
            raise NetworkingError(
                "No playit agent found on this machine.",
                "Download it from %s and set the path in Network settings."
                % PLAYIT_DOWNLOAD)
        with self._lock:
            self.lines.clear()
            self._claim_url = ""
            self._address = ""
            self._ready = False
            self._exit_code = None
        kw = {}
        if sys.platform.startswith("win"):
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.proc = subprocess.Popen(
                [path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, bufsize=1,
                encoding="utf-8", errors="replace", **kw)
        except (OSError, ValueError) as e:
            raise NetworkingError("Could not start the playit agent: %s" % e)
        self._started_at = time.time()
        threading.Thread(target=self._reader, name="lodestone-playit",
                         daemon=True).start()
        self.log("playit agent started (%s)" % path)
        return True, "Started the playit agent."

    def _reader(self):
        try:
            for line in self.proc.stdout:
                line = line.rstrip("\n")
                with self._lock:
                    self.lines.append(line)
                self._scan(line)
        except (ValueError, OSError):
            pass
        code = self.proc.poll() if self.proc else None
        with self._lock:
            self._exit_code = code
            self._ready = False
        self.log("playit agent exited (code %s)" % code)

    def _scan(self, line):
        """Pull the two things that matter out of the agent's chatter."""
        claim = PLAYIT_CLAIM_RE.search(line)
        if claim:
            with self._lock:
                self._claim_url = claim.group(0).rstrip(".,)")
            return
        if PLAYIT_READY_RE.search(line):
            with self._lock:
                self._ready = True
        for host, port in PLAYIT_ADDR_RE.findall(line):
            low = host.lower()
            # Ignore the agent talking about its own local forwarding target.
            if low.startswith(("127.", "0.", "192.168.", "10.")) or low == "localhost":
                continue
            if low.endswith((".playit.gg", ".ply.gg")) or (port and low[0].isdigit()):
                with self._lock:
                    self._address = host + (":" + port if port else "")
                    self._ready = True
                return

    def _start_service(self):
        """v1.x: ask the CLI to bring the playitd service up, then read it back.

        Starting the service may need administrator rights, and it may report
        that no secret is provisioned yet — that is the claim step, and it is
        surfaced rather than treated as a failure.
        """
        code, text = self._cli("start", timeout=40)
        self._service_text = text
        low = text.lower()
        claim = PLAYIT_CLAIM_RE.search(text)
        if claim:
            with self._lock:
                self._claim_url = claim.group(0).rstrip(".,)")
        with self._lock:
            for line in text.splitlines():
                self.lines.append(line)
        if code != 0 and "already" not in low:
            if "denied" in low or "administrator" in low or "elevat" in low:
                raise NetworkingError(
                    "Starting the playit service needs administrator rights.",
                    "Run Lodestone as administrator, or start the agent yourself "
                    "with: playit start")
            if "secret" in low or "claim" in low or "setup" in low:
                raise NetworkingError(
                    "The playit agent has not been set up on this machine yet.",
                    "Run `playit setup` in a terminal once, sign in when it opens "
                    "your browser, then come back and switch to Tunnel mode.")
            raise NetworkingError("Could not start the playit service: %s"
                                  % (text[:200] or "exit code %s" % code))
        self._started_at = time.time()
        self._invalidate()
        self.log("playit service started")
        self._refresh_service_state()
        return True, "Started the playit service."

    def _refresh_service_state(self):
        """Read `playit status` and pull whatever it says about the tunnel."""
        try:
            _, text = self._cli("status", timeout=12)
        except NetworkingError:
            return
        self._service_text = text
        with self._lock:
            self.lines.append(text)
        for line in text.splitlines():
            self._scan(line)

    def stop(self):
        if self.service_style():
            try:
                code, text = self._cli("stop", timeout=30)
            except NetworkingError as e:
                return False, str(e)
            self._service_text = text
            with self._lock:
                self._ready = False
                self._address = ""
            self._invalidate()
            self.log("playit service stopped")
            return (code == 0 or "not running" in text.lower()), (
                text[:160] or "Stopped the playit service.")
        if not self.proc:
            return True, "The playit agent was not running."
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError) as e:
            return False, "Could not stop the playit agent: %s" % e
        finally:
            self.proc = None
            with self._lock:
                self._ready = False
        self.log("playit agent stopped")
        return True, "Stopped the playit agent."

    # -- reporting -----------------------------------------------------------
    def address(self):
        with self._lock:
            return self._address

    def claim_url(self):
        with self._lock:
            return self._claim_url

    def wait_for_address(self, timeout=45):
        """Give the agent a moment to announce its tunnel. Returns the address
        or "" — never raises, because "not yet" is a normal answer."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.address():
                return self.address()
            if not self.running():
                return ""
            if self.claim_url():
                # Waiting on a human to approve it in a browser; no point
                # burning the rest of the timeout.
                return ""
            time.sleep(0.5)
        return self.address()

    def status(self):
        if self.service_style() and self.running():
            self._refresh_service_state()
        with self._lock:
            recent = list(self.lines)[-12:]
            return {
                "provider": self.name,
                "style": "service" if self._service_style else "foreground",
                "installed": bool(self.agent_path),
                "agent_path": self.agent_path,
                "running": self.running(),
                "service_text": self._service_text,
                "ready": self._ready and bool(self._address),
                "address": self._address,
                "claim_url": self._claim_url,
                "needs_claim": bool(self._claim_url) and not self._address,
                "exit_code": self._exit_code,
                "uptime": int(time.time() - self._started_at) if self._started_at
                          and self.running() else 0,
                "log": recent,
                "download": PLAYIT_DOWNLOAD,
            }


NGROK_DOWNLOAD = "https://ngrok.com/download"
NGROK_AUTHTOKEN_URL = "https://dashboard.ngrok.com/get-started/your-authtoken"

# The agent logs in logfmt when driven with --log stdout, and every fact worth
# having arrives as a key=value on one of those lines.
NGROK_URL_RE = re.compile(r"\burl=(?:tcp|https?)://(\S+)", re.I)
NGROK_WEB_RE = re.compile(r"\baddr=(127\.0\.0\.1:\d{2,5}|localhost:\d{2,5})", re.I)
NGROK_ERR_RE = re.compile(r"\b(ERR_NGROK_\d+)\b", re.I)
# 105 is a rejected token, 4018 is no token at all. Both mean the same thing to
# a user: ngrok has not been given an account yet.
NGROK_AUTH_RE = re.compile(r"ERR_NGROK_(?:105|4018)\b|authtoken|"
                           r"failed to auth|not authorized", re.I)
NGROK_READY_RE = re.compile(r"started tunnel|tunnel session started", re.I)
# 8013 is the one that catches Minecraft users out: TCP endpoints are gated
# behind card verification on the free plan, and Minecraft is TCP-only. It is
# not a bug and not something a retry fixes, so it gets its own message.
NGROK_CARD_RE = re.compile(r"ERR_NGROK_8013\b|add a credit or debit card", re.I)
NGROK_CARD_URL = "https://dashboard.ngrok.com/settings#id-verification"
NGROK_CARD_MSG = (
    "ngrok will not open a TCP endpoint on a free account until a card is "
    "added for identity verification, and Minecraft needs TCP. Add one at "
    "%s, or use playit.gg instead \u2014 it is free, needs no card, and is built "
    "for Minecraft." % NGROK_CARD_URL)

NGROK_NAMES = ("ngrok",)


def _ngrok_dirs():
    out = []
    if sys.platform.startswith("win"):
        for var in ("LOCALAPPDATA", "APPDATA", "ProgramFiles", "ProgramFiles(x86)",
                    "USERPROFILE", "ChocolateyInstall"):
            base = os.environ.get(var)
            if not base:
                continue
            out += [os.path.join(base, "Microsoft", "WinGet", "Links"),
                    os.path.join(base, "ngrok"),
                    os.path.join(base, "Programs", "ngrok"),
                    os.path.join(base, "scoop", "shims"),
                    os.path.join(base, "bin"),
                    os.path.join(base, "Downloads")]
        local = os.environ.get("LOCALAPPDATA")
        if local:
            # winget keeps the real binary in a versioned package folder and
            # only sometimes shims it into Links.
            pkgs = os.path.join(local, "Microsoft", "WinGet", "Packages")
            try:
                for entry in os.listdir(pkgs):
                    if entry.lower().startswith("ngrok"):
                        out.append(os.path.join(pkgs, entry))
            except OSError:
                pass
    else:
        home = os.path.expanduser("~")
        out += ["/usr/local/bin", "/usr/bin", "/opt/ngrok", "/snap/bin",
                os.path.join(home, ".local", "bin"), os.path.join(home, "Downloads")]
    return out


def find_ngrok(explicit=""):
    """Locate an ngrok binary. Returns a path or ""."""
    if explicit:
        return explicit if os.path.isfile(explicit) else ""
    for name in NGROK_NAMES:
        found = shutil.which(name)
        if found:
            return found
    exe = "ngrok.exe" if sys.platform.startswith("win") else "ngrok"
    for folder in _ngrok_dirs():
        full = os.path.join(folder, exe)
        if os.path.isfile(full):
            return full
    return ""


def ngrok_config_paths():
    """Where the agent keeps its config, newest layout first.

    Only ever used to answer "is a token configured", never to read one.
    """
    out = []
    if sys.platform.startswith("win"):
        for var in ("LOCALAPPDATA", "APPDATA", "USERPROFILE"):
            base = os.environ.get(var)
            if base:
                out += [os.path.join(base, "ngrok", "ngrok.yml"),
                        os.path.join(base, ".ngrok2", "ngrok.yml")]
        # An ngrok installed from the Microsoft Store runs under MSIX, which
        # redirects its writes into the package's private LocalCache. The agent
        # cheerfully reports saving to %LOCALAPPDATA%\ngrok\ngrok.yml while the
        # file actually lands below, so looking only at the advertised path
        # reports "no authtoken" for a machine that has one.
        local = os.environ.get("LOCALAPPDATA")
        if local:
            pkgs = os.path.join(local, "Packages")
            try:
                entries = os.listdir(pkgs)
            except OSError:
                entries = []
            for entry in entries:
                if not entry.lower().startswith("ngrok"):
                    continue
                cache = os.path.join(pkgs, entry, "LocalCache")
                out += [os.path.join(cache, "Local", "ngrok", "ngrok.yml"),
                        os.path.join(cache, "Roaming", "ngrok", "ngrok.yml")]
    else:
        home = os.path.expanduser("~")
        out += [os.path.join(home, ".config", "ngrok", "ngrok.yml"),
                os.path.join(home, ".ngrok2", "ngrok.yml"),
                os.path.join(home, "Library", "Application Support", "ngrok",
                             "ngrok.yml")]
    return out


def ngrok_has_authtoken():
    """True if an authtoken is configured.

    Deliberately reports only the boolean. Lodestone never reads, stores, logs
    or transmits the token itself - `ngrok config add-authtoken` is the user's
    to run, and the value stays in ngrok's own config file.
    """
    for path in ngrok_config_paths():
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    key, sep, value = stripped.partition(":")
                    if sep and key.strip() == "authtoken" and value.strip():
                        return True
        except OSError:
            continue
    return False


class NgrokProvider(TunnelProvider):
    """Runs `ngrok tcp <port>` and reads back the address it hands out.

    Same bargain as playit: a third-party binary Lodestone will locate, run,
    read and stop, but never download, install or authenticate on the user's
    behalf. ngrok's account step is an authtoken rather than a browser claim,
    and that token is added once with `ngrok config add-authtoken`. Lodestone
    checks only whether one exists.

    Minecraft is a raw TCP protocol, so this opens a *TCP* endpoint, not an
    HTTP one. On the free plan the address is randomly assigned and changes on
    every restart, which is worth saying out loud to anyone handing it to
    friends.
    """

    name = "ngrok"
    API_TTL = 2.0

    def __init__(self, agent_path="", log=None, port=25565):
        self.agent_path = agent_path
        self.log = log or (lambda msg: None)
        self.port = int(port)
        self.proc = None
        self.lines = deque(maxlen=400)
        self._lock = threading.RLock()
        self._address = ""
        self._ready = False
        self._api = ""
        self._api_checked = 0.0
        self._error = ""
        self._needs_token = False
        self._needs_card = False
        self._started_at = 0.0
        self._exit_code = None

    # -- discovery -----------------------------------------------------------
    def resolve(self):
        path = find_ngrok(self.agent_path)
        if path:
            self.agent_path = path
        return path

    def available(self):
        path = self.resolve()
        if not path:
            return False, ("No ngrok agent found. Install it from %s, then "
                           "point Lodestone at it." % NGROK_DOWNLOAD)
        if not ngrok_has_authtoken():
            return False, ("ngrok is installed but has no authtoken yet. Run "
                           "`ngrok config add-authtoken <your token>` once - get "
                           "the token from %s." % NGROK_AUTHTOKEN_URL)
        return True, path

    def version(self):
        path = self.resolve()
        if not path:
            return ""
        try:
            r = subprocess.run([path, "version"], capture_output=True, text=True,
                               timeout=8, errors="replace", **_no_window())
            out = (r.stdout or r.stderr or "").strip().splitlines()
            return out[0][:60] if out else ""
        except (OSError, subprocess.SubprocessError):
            return ""

    # -- running -------------------------------------------------------------
    def running(self):
        return bool(self.proc and self.proc.poll() is None)

    def start(self):
        if self.running():
            return True, "The ngrok agent is already running."
        path = self.resolve()
        if not path:
            raise NetworkingError(
                "No ngrok agent found on this machine.",
                "Install it from %s and set the path in Network settings."
                % NGROK_DOWNLOAD)
        if not ngrok_has_authtoken():
            with self._lock:
                self._needs_token = True
            raise NetworkingError(
                "ngrok has no authtoken on this machine yet.",
                "Run `ngrok config add-authtoken <your token>` in a terminal "
                "once - the token is at %s. Lodestone will not ask you for it "
                "or store it." % NGROK_AUTHTOKEN_URL)
        with self._lock:
            self.lines.clear()
            self._address = ""
            self._ready = False
            self._api = ""
            self._error = ""
            self._needs_token = False
            self._needs_card = False
            self._exit_code = None
        cmd = [path, "tcp", str(self.port), "--log", "stdout",
               "--log-format", "logfmt"]
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, text=True, bufsize=1,
                encoding="utf-8", errors="replace", **_no_window())
        except (OSError, ValueError) as e:
            raise NetworkingError("Could not start the ngrok agent: %s" % e)
        self._started_at = time.time()
        threading.Thread(target=self._reader, name="lodestone-ngrok",
                         daemon=True).start()
        self.log("ngrok agent started (%s tcp %s)" % (path, self.port))
        return True, "Started the ngrok agent."

    def _reader(self):
        try:
            for line in self.proc.stdout:
                line = line.rstrip("\n")
                with self._lock:
                    self.lines.append(line)
                self._scan(line)
        except (ValueError, OSError):
            pass
        code = self.proc.poll() if self.proc else None
        with self._lock:
            self._exit_code = code
            self._ready = False
        self.log("ngrok agent exited (code %s)" % code)

    def _scan(self, line):
        """Pull address, local API and failures out of the agent's logfmt."""
        err = NGROK_ERR_RE.search(line)
        if err or "lvl=eror" in line or "lvl=crit" in line:
            with self._lock:
                if NGROK_CARD_RE.search(line):
                    self._needs_card = True
                    self._error = NGROK_CARD_MSG
                elif not self._needs_card:
                    self._error = line[:300]
                if NGROK_AUTH_RE.search(line) and not self._needs_card:
                    self._needs_token = True
        if "obj=web" in line:
            web = NGROK_WEB_RE.search(line)
            if web:
                with self._lock:
                    self._api = web.group(1)
        got = NGROK_URL_RE.search(line)
        if got:
            addr = got.group(1).strip().rstrip("/")
            with self._lock:
                self._address = addr
                self._ready = True
            return
        if NGROK_READY_RE.search(line):
            with self._lock:
                self._ready = True

    # -- the agent's own API -------------------------------------------------
    #
    # stdout is normally enough, but the local API is the authoritative record
    # of what is actually published, and it survives a log line we failed to
    # parse. Cheap, local, and only consulted while we still have no address.
    def _poll_api(self):
        with self._lock:
            if self._address or not self.running():
                return
            api = self._api or "127.0.0.1:4040"
            if time.time() - self._api_checked < self.API_TTL:
                return
            self._api_checked = time.time()
        try:
            import json as _json
            import urllib.request as _url
            with _url.urlopen("http://%s/api/tunnels" % api, timeout=3) as r:
                data = _json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return
        for tun in data.get("tunnels") or []:
            pub = (tun.get("public_url") or "").strip()
            if not pub:
                continue
            addr = pub.split("://", 1)[-1].rstrip("/")
            with self._lock:
                self._address = addr
                self._ready = True
            return

    def stop(self):
        if not self.proc:
            return True, "The ngrok agent was not running."
        try:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=5)
        except (OSError, subprocess.SubprocessError) as e:
            return False, "Could not stop the ngrok agent: %s" % e
        finally:
            self.proc = None
            with self._lock:
                self._ready = False
                self._address = ""
        self.log("ngrok agent stopped")
        return True, "Stopped the ngrok agent."

    # -- reporting -----------------------------------------------------------
    def address(self):
        with self._lock:
            if self._address:
                return self._address
        self._poll_api()
        with self._lock:
            return self._address

    def claim_url(self):
        with self._lock:
            return NGROK_AUTHTOKEN_URL if self._needs_token else ""

    def wait_for_address(self, timeout=45):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.address():
                return self.address()
            if not self.running():
                return ""
            if self.claim_url():
                return ""
            time.sleep(0.5)
        return self.address()

    def status(self):
        installed = bool(self.resolve())
        running = self.running()
        if running:
            self._poll_api()
        with self._lock:
            recent = list(self.lines)[-12:]
            needs = self._needs_token or (installed and not ngrok_has_authtoken())
            if self._needs_card:
                needs = True
            return {
                "provider": self.name,
                "style": "foreground",
                "installed": installed,
                "agent_path": self.agent_path,
                "running": running,
                "service_text": self._error,
                "ready": self._ready and bool(self._address),
                "address": self._address,
                # The panel already knows how to render "there is a URL the user
                # must visit before this works"; for ngrok that is the token page.
                "claim_url": (NGROK_CARD_URL if self._needs_card
                              else NGROK_AUTHTOKEN_URL if needs else ""),
                "needs_card": self._needs_card,
                "needs_claim": bool(needs) and not self._address,
                "exit_code": self._exit_code,
                "uptime": int(time.time() - self._started_at) if self._started_at
                          and running else 0,
                "log": recent,
                "download": NGROK_DOWNLOAD,
                "error": self._error,
            }


PROVIDERS = {"playit": PlayitProvider, "ngrok": NgrokProvider}
PROVIDER_LABEL = {"playit": "playit.gg", "ngrok": "ngrok"}


def make_provider(name="playit", agent_path="", log=None, port=25565):
    """Build a tunnel provider by name. Unknown names fall back to playit."""
    cls = PROVIDERS.get((name or "").strip().lower(), PlayitProvider)
    return cls(agent_path=agent_path, log=log, port=port)


class TunnelService:
    """Whichever tunnel the user has chosen — playit.gg or ngrok today.

    Everything above this line is provider-specific; everything the panel calls
    is not. A third provider (a VPS, Tailscale Funnel) is a new class, an entry
    in PROVIDERS, and nothing else.
    """

    def __init__(self, provider=None, log=None, name=""):
        self.log = log or (lambda msg: None)
        self.provider = provider or make_provider(name or "playit", log=self.log)

    def provider_name(self):
        return getattr(self.provider, "name", "")

    def set_provider(self, name, agent_path="", port=None):
        """Swap the backing provider, stopping the old one first.

        Switching while a tunnel is up would otherwise leave an orphaned agent
        running with nothing holding a reference to it.
        """
        key = (name or "").strip().lower()
        cls = PROVIDERS.get(key)
        if cls is None or isinstance(self.provider, cls):
            if port is not None and self.provider is not None:
                self.provider.port = int(port)
            return False
        try:
            self.provider.close()
        except Exception:
            pass
        self.provider = cls(agent_path=agent_path, log=self.log,
                            port=int(port if port is not None else 25565))
        self.log("tunnel provider switched to %s" % PROVIDER_LABEL.get(key, key))
        return True

    def available(self):
        return self.provider.available()

    def start(self):
        return self.provider.start()

    def stop(self):
        return self.provider.stop()

    def running(self):
        return self.provider.running()

    def address(self):
        return self.provider.address()

    def wait_for_address(self, timeout=45):
        """Poll until the tunnel announces itself, or we know it will not.

        Goes through address()/running() rather than reaching into the provider,
        so a different tunnel implementation only has to supply those two.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            got = self.address()
            if got:
                return got
            if not self.running():
                return ""
            if self.status().get("needs_claim"):
                # Blocked on a human in a browser; waiting changes nothing.
                return ""
            time.sleep(0.5)
        return self.address()

    def status(self):
        try:
            return self.provider.status()
        except Exception as e:
            return {"provider": getattr(self.provider, "name", "tunnel"),
                    "running": False, "ready": False, "address": "",
                    "error": str(e)}

    def close(self):
        self.provider.close()


def split_address(address, default_port=25565):
    """"host:port" -> (host, port). A tunnel address may omit the port."""
    if not address:
        return "", int(default_port)
    if address.count(":") == 1:
        host, _, port = address.partition(":")
        try:
            return host, int(port)
        except ValueError:
            return host, int(default_port)
    return address, int(default_port)
