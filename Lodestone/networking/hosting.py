"""HostingManager — the three modes, and the evidence behind the green light.

Local / LAN / Internet differ in exactly two things: what the Minecraft server
binds to, and whether we ask the router for a hole. Everything else in the
panel is untouched.

    mode       server-ip          port mapping   published address
    ---------  -----------------  -------------  ------------------
    local      127.0.0.1          no             127.0.0.1:port
    lan        (empty, all NICs)  no             192.168.x.y:port
    internet   (empty, all NICs)  yes            <public ip>:port
    tunnel     (empty, all NICs)  no             <name>.playit.gg:port

Tunnel mode exists because port forwarding assumes the router you can configure
is the one the internet arrives at, and behind CGNAT or an ISP box you have no
password for, that is simply false.

The rule that governs the whole class: ONLINE is a claim about the internet, so
it is only ever set after something on the internet has actually connected. A
successful UPnP call is not evidence. A local socket test is not evidence. Both
can be true while friends still get "connection timed out", and telling somebody
their server is public when it is not is the one failure that wastes an entire
evening.

Only the Minecraft port is ever published. RCON, SFTP, SSH and the panel's own
HTTP port are refused outright by validate_port(), because a Minecraft server
exposed to the internet is a game server, and an RCON port exposed to the
internet is a remote shell.
"""

import threading
import time

from .connectivity import (CHECKING, CGNAT_SUSPECTED, ConnectivityTester,
                           IPV6_ONLY, NOT_REACHABLE, REACHABLE, SERVER_OFFLINE,
                           STATE_LABEL, UNKNOWN, port_in_use)
from .errors import (FirewallPermissionDenied, NetworkingError, PortInUse,
                     PortInvalid)
from .firewall import FirewallManager
from .interfaces import TCP
from .net_interfaces import NetworkInterfaceService
from .port_mapping import DEFAULT_LEASE, PortMappingService
from .public_ip import PublicIpService
from .tunnel import TunnelService, split_address

MODE_LOCAL = "local"
MODE_LAN = "lan"
MODE_INTERNET = "internet"
MODE_TUNNEL = "tunnel"
MODES = (MODE_LOCAL, MODE_LAN, MODE_INTERNET, MODE_TUNNEL)

MODE_LABEL = {MODE_LOCAL: "Local only", MODE_LAN: "LAN",
              MODE_INTERNET: "Internet", MODE_TUNNEL: "Tunnel"}

# Tunnel mode is the answer when port forwarding cannot work at all: the agent
# dials out, so nothing has to be opened on any router. It is a deliberate
# choice rather than an automatic fallback, because the traffic goes through a
# third party and the address stops being yours.

DEFAULT_MC_PORT = 25565

# Never published, whatever the user types. Each of these is a way into the
# machine rather than into the game.
RESERVED_PORTS = {
    22: "SSH / SFTP",
    23: "Telnet",
    445: "Windows file sharing",
    3389: "Remote Desktop",
    5351: "NAT-PMP / PCP",
    5900: "VNC",
}

PHASE_IDLE = "idle"
PHASE_WORKING = "working"
PHASE_READY = "ready"
PHASE_FAILED = "failed"

# How long a "reachable" verdict is worth anything. Reachability is a claim
# about the world, and the world changes: the server stops, the lease lapses,
# the ISP hands out a new address. Past this the panel says "unknown" and
# re-checks rather than keeping a green light lit on old evidence.
VERDICT_TTL = 300

# How long Auto fix waits for a freshly started server to open its port. World
# generation on a first run is genuinely slow, so this is generous; tests set it
# to something small.
START_TIMEOUT = 120

# How long to wait for a tunnel agent to announce its address before reporting
# back. Short: if it has not said anything by then it is usually waiting on a
# browser claim, which no amount of waiting will resolve.
TUNNEL_WAIT = 45


def validate_port(port, rcon_port=None, panel_port=None, extra_blocked=None):
    """Raise PortInvalid unless this is a sane, publishable Minecraft port."""
    try:
        value = int(port)
    except (TypeError, ValueError):
        raise PortInvalid("%r is not a port number." % port)
    if not 1 <= value <= 65535:
        raise PortInvalid("Port %d is outside the valid range 1-65535." % value)
    # Named services first: "port 22 belongs to SSH / SFTP" tells the user more
    # than "port 22 is privileged", even though both are true.
    if value in RESERVED_PORTS:
        raise PortInvalid(
            "Port %d belongs to %s." % (value, RESERVED_PORTS[value]),
            "Lodestone will not publish a management port to the internet.")
    if value < 1024:
        raise PortInvalid(
            "Port %d is a privileged port." % value,
            "Ports below 1024 need administrator rights and are reserved for "
            "system services. Use 25565, or anything from 1024 upwards.")
    if rcon_port and value == int(rcon_port):
        raise PortInvalid(
            "Port %d is this server's RCON port." % value,
            "RCON is a remote console. It stays on the LAN.")
    if panel_port and value == int(panel_port):
        raise PortInvalid(
            "Port %d is the Lodestone panel's own port." % value,
            "The panel has no login, so it is never published.")
    for blocked, why in (extra_blocked or {}).items():
        if value == int(blocked):
            raise PortInvalid("Port %d is in use by %s." % (value, why))
    return value


class HostingManager:
    """Owns the hosting state for one server. Thread-safe; every long operation
    runs on a worker thread so the HTTP handler never blocks."""

    def __init__(self, interfaces=None, public_ip=None, mapping=None,
                 tester=None, firewall=None, tunnel=None, log=None,
                 on_change=None):
        self.log = log or (lambda msg: None)
        self.on_change = on_change or (lambda: None)
        self.interfaces = interfaces or NetworkInterfaceService()
        self.public_ip = public_ip or PublicIpService()
        self.mapping = mapping or PortMappingService(self.interfaces, log=self.log)
        self.tester = tester or ConnectivityTester(self.interfaces, log=self.log)
        self.firewall = firewall or FirewallManager(log=self.log)
        self.tunnel = tunnel or TunnelService(log=self.log)

        self._lock = threading.RLock()
        self._worker = None
        self.mode = MODE_LOCAL
        self.port = DEFAULT_MC_PORT
        self.external_port = 0          # 0 == same as the internal port
        self.protocol = TCP
        self.server_name = ""
        self.phase = PHASE_IDLE
        self.steps = []                 # ordered progress, shown live
        self.error = ""
        self.mapping_attempts = []
        self.router_external_ip = ""
        self.connectivity = {"state": UNKNOWN, "reason": "Not checked yet."}
        self.firewall_state = {}
        self.last_enabled_at = 0.0
        self.last_checked_at = 0.0
        self.warnings = []
        self.autofix_result = None
        self.start_timeout = START_TIMEOUT
        self.tunnel_wait = TUNNEL_WAIT

    # -- progress -------------------------------------------------------------
    def _step(self, key, state, detail=""):
        with self._lock:
            for s in self.steps:
                if s["key"] == key:
                    s["state"], s["detail"], s["at"] = state, detail, time.time()
                    break
            else:
                self.steps.append({"key": key, "state": state, "detail": detail,
                                   "at": time.time()})
        if detail and state in ("fail", "warn"):
            self.log("hosting: %s — %s" % (key, detail))
        try:
            self.on_change()
        except Exception:
            pass

    def _reset_steps(self):
        with self._lock:
            self.steps = []
            self.error = ""
            self.warnings = []

    def busy(self):
        with self._lock:
            return bool(self._worker and self._worker.is_alive())

    # -- addresses ------------------------------------------------------------
    def local_address(self):
        ip = self.interfaces.local_ipv4()
        return "%s:%d" % (ip, self.port)

    def lan_address(self):
        return self.local_address()

    def public_address(self):
        ip = self.public_ip.snapshot().get("ipv4", "")
        if not ip:
            return ""
        return "%s:%d" % (ip, self.effective_external_port())

    def effective_external_port(self):
        return int(self.external_port or self.port)

    def active_mapping(self):
        """The mapping for *this* port, or None.

        No falling back to "whatever mapping we happen to hold": a leftover
        mapping for a different port would otherwise light up "Port forward:
        Active" for a port that has none.
        """
        want_external = self.effective_external_port()
        for m in self.mapping.owned():
            if m.internal_port == self.port and m.external_port == want_external:
                return m
        return None

    def verdict_age(self):
        """Seconds since the reachability check, or None if it never ran."""
        if not self.last_checked_at:
            return None
        return max(0.0, time.time() - self.last_checked_at)

    def verdict_fresh(self):
        age = self.verdict_age()
        return age is not None and age < VERDICT_TTL

    def _live_state(self):
        """The connectivity state as it stands *now*, expiring stale evidence.

        Returns (state, reason). A reachable verdict also expires early if the
        mapping it depended on has gone, because the hole it proved is closed.
        """
        conn = self.connectivity or {}
        state = conn.get("state", UNKNOWN)
        reason = conn.get("reason", "")
        if state != REACHABLE or self.mode != MODE_INTERNET:
            return state, reason
        if not self.verdict_fresh():
            age = self.verdict_age()
            return UNKNOWN, (
                "The server was reachable when it was last checked%s, but that "
                "was long enough ago that it is no longer evidence. Test again."
                % (" %d minutes ago" % int(age // 60) if age else ""))
        if not self.active_mapping():
            return UNKNOWN, ("The port forward that made this server reachable is "
                             "no longer in place. Test again.")
        return state, reason

    def invalidate_verdict(self, why=""):
        """Throw away the reachability result. Anything that changes what would
        be tested must call this, or the panel keeps answering the old question."""
        with self._lock:
            if (self.connectivity or {}).get("state") in (None, UNKNOWN):
                return
            self.connectivity = {"state": UNKNOWN,
                                 "reason": why or "Not checked since this changed."}
            self.last_checked_at = 0.0

    # -- the properties file --------------------------------------------------
    def desired_properties(self, mode=None):
        """What server.properties must say for this mode.

        Empty server-ip is the important one: Minecraft treats it as "bind every
        interface". A literal 127.0.0.1 there makes the server invisible to the
        LAN and to the internet no matter what the router does, and it is the
        single commonest reason a forwarded port still refuses connections.
        """
        mode = mode or self.mode
        return {"server-ip": "127.0.0.1" if mode == MODE_LOCAL else "",
                "server-port": str(int(self.port))}

    def tunnel_address(self):
        """host:port friends should use in tunnel mode, or ""."""
        return self.tunnel.address() if self.mode == MODE_TUNNEL else ""

    def properties_conflicts(self, current):
        """Compare the live server.properties against what the mode needs.
        Returns a list of {key, is, want, why} — never writes anything."""
        want = self.desired_properties()
        out = []
        for key, value in want.items():
            have = (current or {}).get(key, None)
            if have is None:
                have = ""
            if str(have).strip() != value:
                out.append({"key": key, "is": str(have), "want": value,
                            "why": _why(key, self.mode)})
        if self.mode == MODE_INTERNET:
            status = str((current or {}).get("enable-status", "true")).strip().lower()
            if status == "false":
                out.append({"key": "enable-status", "is": "false", "want": "true",
                            "why": "With status pings off, the server still accepts "
                                   "players but no reachability test can confirm it.",
                            "advisory": True})
        return out

    # -- port -----------------------------------------------------------------
    def set_port(self, port, rcon_port=None, panel_port=None, check_free=True):
        value = validate_port(port, rcon_port, panel_port)
        if check_free and value != self.port and port_in_use(value):
            raise PortInUse(
                "Port %d is already in use on this machine." % value,
                "Stop whatever is holding it, or pick another port.")
        changed = value != self.port
        with self._lock:
            self.port = value
        if changed:
            # The old verdict was about a different port. Keeping it would make
            # the panel claim a never-tested port is publicly reachable.
            self.invalidate_verdict("The port changed since the last check.")
        return value

    def set_external_port(self, port, rcon_port=None, panel_port=None):
        if not port:
            changed = self.external_port != 0
            with self._lock:
                self.external_port = 0
            if changed:
                self.invalidate_verdict("The external port changed since the last check.")
            return 0
        value = validate_port(port, rcon_port, panel_port)
        changed = value != self.external_port
        with self._lock:
            self.external_port = value
        if changed:
            self.invalidate_verdict("The external port changed since the last check.")
        return value

    # -- enable / disable -----------------------------------------------------
    def apply(self, mode, port=None, blocking=False, want_providers=None,
              manage_firewall=True, write_properties=None, rcon_port=None,
              panel_port=None):
        """Switch modes. Returns immediately with the work on a thread unless
        `blocking`. `write_properties` is a callable(dict) the panel supplies so
        this module never has to know about backends."""
        if mode not in MODES:
            raise PortInvalid("Unknown hosting mode %r." % mode)
        if self.busy():
            raise NetworkingError("A hosting change is already running.")
        if port is not None:
            self.set_port(port, rcon_port, panel_port, check_free=False)

        self._reset_steps()
        with self._lock:
            previous = self.mode
            self.mode = mode
            self.phase = PHASE_WORKING
            self.connectivity = {"state": CHECKING, "reason": "Checking…"}

        def work():
            try:
                self._apply_worker(mode, previous, want_providers,
                                   manage_firewall, write_properties)
            except NetworkingError as e:
                with self._lock:
                    self.phase = PHASE_FAILED
                    self.error = str(e)
                self._step("done", "fail", str(e))
            except Exception as e:
                with self._lock:
                    self.phase = PHASE_FAILED
                    self.error = "Unexpected error: %s" % e
                self._step("done", "fail", "Unexpected error: %s" % e)
            finally:
                try:
                    self.on_change()
                except Exception:
                    pass

        if blocking:
            work()
            return self.snapshot()
        with self._lock:
            self._worker = threading.Thread(target=work, name="lodestone-hosting",
                                            daemon=True)
            self._worker.start()
        return self.snapshot()

    def _apply_worker(self, mode, previous, want_providers, manage_firewall,
                      write_properties):
        # Coming down from internet mode always tidies up first, so switching
        # local -> internet -> lan cannot leave a hole open.
        if previous == MODE_INTERNET and mode != MODE_INTERNET:
            self._teardown(manage_firewall)
        if previous == MODE_TUNNEL and mode != MODE_TUNNEL:
            # The agent holds an outbound connection open for as long as it
            # runs; switching away from tunnel mode has to close it, or the
            # server stays quietly published after the user thinks they stopped.
            try:
                self.tunnel.stop()
                self._step("tunnel", "ok", "Stopped the tunnel agent.")
            except Exception as e:
                self._step("tunnel", "warn", "Could not stop the agent: %s" % e)

        self._step("interfaces", "run", "Reading local addresses…")
        self.interfaces.invalidate()
        snap = self.interfaces.snapshot()
        if not snap["local_ipv4"] or snap["local_ipv4"].startswith("127."):
            if mode != MODE_LOCAL:
                self._step("interfaces", "fail",
                           "This machine has no network address — only Local mode "
                           "can work right now.")
                with self._lock:
                    self.phase = PHASE_FAILED
                    self.error = "No LAN address."
                return
        self._step("interfaces", "ok", "Local address %s%s"
                   % (snap["local_ipv4"],
                      ", IPv6 " + snap["ipv6"] if snap["ipv6"] else ""))

        self._step("properties", "run", "Checking server.properties…")
        if write_properties:
            try:
                changed = write_properties(self.desired_properties(mode))
                self._step("properties", "ok",
                           "server-ip=%s, server-port=%d%s"
                           % (self.desired_properties(mode)["server-ip"] or "(all interfaces)",
                              self.port,
                              " — updated" if changed else " — already correct"))
            except Exception as e:
                self._step("properties", "warn",
                           "Could not update server.properties: %s" % e)
        else:
            self._step("properties", "skip", "Not applying property changes.")

        if mode == MODE_TUNNEL:
            self._tunnel_worker()
            return

        if mode != MODE_INTERNET:
            with self._lock:
                self.phase = PHASE_READY
                self.connectivity = {
                    "state": UNKNOWN,
                    "reason": ("Local mode — the server is reachable from this "
                               "machine only." if mode == MODE_LOCAL else
                               "LAN mode — the server is reachable from this "
                               "network. No internet check is run.")}
                self.last_enabled_at = time.time()
            self._step("done", "ok", "%s mode is active." % MODE_LABEL[mode])
            return

        self._internet_worker(want_providers, manage_firewall)

    def _tunnel_worker(self):
        """Bring up the tunnel agent, then prove the address actually works.

        The bar for "online" is the same as for internet mode and is checked the
        same way: something on the internet has to connect. A tunnel that is
        merely *running* is no more evidence than a port mapping that merely
        exists.
        """
        self._step("tunnel", "run", "Starting the tunnel agent…")
        ok, detail = self.tunnel.available()
        if not ok:
            self._step("tunnel", "fail", detail)
            with self._lock:
                self.phase = PHASE_FAILED
                self.error = detail
                self.connectivity = {"state": UNKNOWN, "reason": detail}
            self._step("done", "fail", "No tunnel agent.")
            return
        try:
            self.tunnel.start()
        except NetworkingError as e:
            self._step("tunnel", "fail", str(e))
            with self._lock:
                self.phase = PHASE_FAILED
                self.error = str(e)
                self.connectivity = {"state": UNKNOWN, "reason": str(e)}
            self._step("done", "fail", str(e))
            return

        address = self.tunnel.wait_for_address(self.tunnel_wait)
        status = self.tunnel.status()
        if status.get("needs_claim"):
            # First run: the agent has no tunnel until a human approves it in a
            # browser. That is not a failure, it is a step the user has to take.
            msg = ("The playit agent needs claiming. Open %s, sign in, and "
                   "approve the tunnel — then press Test connection."
                   % status.get("claim_url"))
            self._step("tunnel", "warn", msg)
            with self._lock:
                self.phase = PHASE_READY
                self.connectivity = {"state": UNKNOWN, "reason": msg}
                self.last_enabled_at = time.time()
            self._step("done", "warn", "Waiting for you to claim the tunnel.")
            return
        if not address:
            msg = ("The tunnel agent is running but has not reported an address "
                   "yet. Give it a moment and press Test connection.")
            self._step("tunnel", "warn", msg)
            with self._lock:
                self.phase = PHASE_READY
                self.connectivity = {"state": UNKNOWN, "reason": msg}
                self.last_enabled_at = time.time()
            self._step("done", "warn", "No tunnel address yet.")
            return

        self._step("tunnel", "ok", "Tunnel address: %s" % address)
        self._step("connectivity", "run", "Testing the tunnel from outside…")
        host, port = split_address(address, self.port)
        local = self.tester.local_check(self.port, self.interfaces.local_ipv4())
        if not local["listening"]:
            result = {"state": SERVER_OFFLINE, "checked_at": time.time(),
                      "port": self.port, "local": local,
                      "reason": ("The tunnel is up, but no Minecraft server is "
                                 "listening on port %d of this machine. %s"
                                 % (self.port, local["detail"]))}
        else:
            ext = self.tester.external_check(host, port)
            same = (self.tester.same_server(local.get("status"), ext.get("status"))
                    if ext.get("online") else None)
            if ext.get("online") and same is not False:
                result = {"state": REACHABLE, "reason":
                          "%s reached your server through the tunnel." %
                          (ext.get("source") or "An outside service"),
                          "external": ext, "local": local, "identity": same,
                          "checked_at": time.time(), "port": self.port}
            elif ext.get("conclusive"):
                result = {"state": NOT_REACHABLE, "external": ext, "local": local,
                          "checked_at": time.time(), "port": self.port,
                          "reason": ("The tunnel agent is running but nothing "
                                     "could reach %s. Check the tunnel is "
                                     "pointed at port %d in your playit "
                                     "dashboard." % (address, self.port))}
            else:
                result = {"state": UNKNOWN, "external": ext, "local": local,
                          "checked_at": time.time(), "port": self.port,
                          "reason": ext.get("detail") or
                          "The tunnel could not be verified from outside."}
        with self._lock:
            self.connectivity = result
            self.last_checked_at = time.time()
            self.last_enabled_at = time.time()
            self.phase = (PHASE_READY if result["state"] == REACHABLE
                          else PHASE_FAILED)
            self.warnings = self._build_warnings(result, None)
        state = result["state"]
        self._step("connectivity",
                   "ok" if state == REACHABLE else "warn" if state == UNKNOWN
                   else "fail", result["reason"])
        self._step("done", "ok" if state == REACHABLE else "warn",
                   "Tunnel: %s" % STATE_LABEL.get(state, state))

    def _internet_worker(self, want_providers, manage_firewall):
        want = tuple(want_providers or ("upnp", "natpmp", "pcp"))

        # 1. public address
        self._step("public_ip", "run", "Finding the public address…")
        self.public_ip.invalidate()
        pub4 = self.public_ip.ipv4(force=True)
        pub6 = self.public_ip.ipv6(force=True)
        if pub4:
            snap = self.public_ip.snapshot()
            self._step("public_ip", "ok", "IPv4 %s%s%s"
                       % (pub4,
                          " (confirmed by two sources)" if snap["ipv4_confirmed"] else "",
                          ", IPv6 " + pub6 if pub6 else ""))
        elif pub6:
            self._step("public_ip", "warn",
                       "No public IPv4 found. IPv6 %s is available." % pub6)
        else:
            self._step("public_ip", "fail",
                       self.public_ip.snapshot().get("ipv4_error")
                       or "Could not determine the public address.")

        # 2. router mapping
        self._step("mapping", "run", "Asking the router to forward the port…")
        mapping = None
        try:
            mapping, attempts = self.mapping.ensure(
                self.port, self.effective_external_port(), self.protocol,
                DEFAULT_LEASE, want)
            with self._lock:
                self.mapping_attempts = attempts
            if mapping:
                self.router_external_ip = mapping.external_ip or ""
                self._step("mapping", "ok", "%s via %s"
                           % (mapping.describe(), mapping.provider))
            else:
                self._step("mapping", "fail", "; ".join(
                    "%s: %s" % (a["provider"], a["detail"]) for a in attempts)
                    or "No automatic port mapping protocol worked.")
        except NetworkingError as e:
            with self._lock:
                self.mapping_attempts = [{"provider": "-", "ok": False,
                                          "detail": str(e)}]
            self._step("mapping", "fail", str(e))

        # 3. host firewall
        if manage_firewall and self.firewall.supported:
            self._step("firewall", "run", "Checking Windows Firewall…")
            try:
                state = self.firewall.snapshot(self.port, self.protocol, deep=True)
                with self._lock:
                    self.firewall_state = state
                if state.get("likely_blocking"):
                    self._step("firewall", "warn",
                               "Nothing allows inbound %s %d yet — use "
                               "'Allow through firewall' to add a rule."
                               % (self.protocol, self.port))
                else:
                    self._step("firewall", "ok", state.get("detail") or "Allowed.")
            except NetworkingError as e:
                self._step("firewall", "warn", str(e))
        else:
            self._step("firewall", "skip",
                       "Not Windows — check your own firewall allows inbound "
                       "TCP %d." % self.port)

        # 4. the only thing that earns a green light
        self._step("connectivity", "run", "Testing from outside your network…")
        fw = self.firewall_state or {}
        result = self.tester.run(
            self.port, pub4, pub6, self.router_external_ip,
            self.interfaces.local_ipv4(),
            mapping_active=bool(mapping),
            firewall_blocking=fw.get("likely_blocking"),
            external_port=self.effective_external_port(),
            # A router that took the mapping but cannot name its own WAN address
            # is very probably not the device the connection arrives at.
            router_wan_unknown=bool(mapping) and not self.router_external_ip,
            # Only traced when the mapping went in but the router cannot name
            # its own WAN — the one case where "which box do I configure?" is
            # the question, and the only case worth spending seconds on a trace.
            upstream=(self.interfaces.upstream_hops()
                      if bool(mapping) and not self.router_external_ip else None))
        with self._lock:
            self.connectivity = result
            self.last_checked_at = time.time()
            self.last_enabled_at = time.time()
            self.phase = (PHASE_READY if result["state"] == REACHABLE
                          else PHASE_FAILED if result["state"] in
                          (SERVER_OFFLINE, CGNAT_SUSPECTED, NOT_REACHABLE)
                          else PHASE_READY)
            self.warnings = self._build_warnings(result, mapping)
        # "We could not find out" is not a failure. Marking it one would push
        # users to go fixing something that may not be broken.
        self._step("connectivity",
                   "ok" if result["state"] == REACHABLE
                   else "warn" if result["state"] == UNKNOWN else "fail",
                   result["reason"])
        self._step("done",
                   "ok" if result["state"] == REACHABLE else "warn",
                   "Internet hosting: %s" % STATE_LABEL.get(result["state"],
                                                            result["state"]))

    def _build_warnings(self, result, mapping):
        out = []
        if result["state"] == REACHABLE:
            out.append({"level": "info", "text":
                        "Your server is now reachable from the public internet. "
                        "Anyone who learns the address can attempt to join — turn "
                        "on the whitelist if you only want friends in."})
        if result["state"] == CGNAT_SUSPECTED:
            out.append({"level": "bad", "text": (result.get("reason") or "") +
                        " Port forwarding alone cannot fix this."})
        if mapping and mapping.lifetime:
            out.append({"level": "info", "text":
                        "The router mapping is a %d-second lease and is renewed "
                        "automatically while Lodestone is open. Closing Lodestone "
                        "lets it expire." % mapping.lifetime})
        return out

    # -- auto fix -------------------------------------------------------------
    def autofix(self, write_properties=None, start_server=None, is_running=None,
                want_providers=None, manage_firewall=True, blocking=False):
        """Fix everything that is fixable from this machine, then say what is not.

        Honest about its limits by design. It can correct server.properties,
        start the server, ask the router again, and add the firewall rule. It
        cannot reconfigure a router upstream of this one, undo CGNAT, or make an
        ISP accept inbound connections — and pretending otherwise would just be
        a slower way of failing. Anything it cannot fix comes back in `manual`.
        """
        if self.busy():
            raise NetworkingError("Something is already running.")

        def work():
            fixed, manual = [], []
            try:
                self._autofix_worker(fixed, manual, write_properties, start_server,
                                     is_running, want_providers, manage_firewall)
            except NetworkingError as e:
                with self._lock:
                    self.error = str(e)
                self._step("done", "fail", str(e))
            except Exception as e:
                with self._lock:
                    self.error = "Unexpected error: %s" % e
                self._step("done", "fail", "Unexpected error: %s" % e)
            with self._lock:
                self.autofix_result = {"fixed": fixed, "manual": manual,
                                       "at": time.time()}
            try:
                self.on_change()
            except Exception:
                pass
            return self.autofix_result

        self._reset_steps()
        with self._lock:
            self.phase = PHASE_WORKING
            self.autofix_result = None
        if blocking:
            return work()
        with self._lock:
            self._worker = threading.Thread(target=work, name="lodestone-autofix",
                                            daemon=True)
            self._worker.start()
        return {"fixed": [], "manual": [], "running": True}

    def _autofix_worker(self, fixed, manual, write_properties, start_server,
                        is_running, want_providers, manage_firewall):
        from .connectivity import tcp_open

        # 1. server.properties has to match the mode before anything else can
        #    possibly work — a loopback bind makes every other step pointless.
        self._step("properties", "run", "Checking server.properties…")
        if write_properties:
            try:
                if write_properties(self.desired_properties()):
                    fixed.append("Corrected server.properties (server-ip and "
                                 "server-port). Restart the server for it to apply.")
                    self._step("properties", "ok", "Corrected.")
                else:
                    self._step("properties", "ok", "Already correct.")
            except Exception as e:
                self._step("properties", "warn", str(e))
                manual.append("Could not write server.properties: %s" % e)
        else:
            self._step("properties", "skip", "Not checked.")

        # 2. nothing can reach a server that is not running
        self._step("server", "run", "Checking the Minecraft server…")
        local_ip = self.interfaces.local_ipv4()
        listening = tcp_open(local_ip, self.port, timeout=2.0)
        # Three distinct situations, and exactly one message each. Letting two
        # of these fire together produced "the server is running but not
        # listening" immediately followed by "start the server", which is
        # advice that contradicts itself.
        if listening:
            self._step("server", "ok", "Already running on port %d." % self.port)
        elif is_running and is_running():
            # The process is up but the port is not open: it is still starting,
            # or it is bound somewhere we cannot see. Starting a second copy
            # would be worse than waiting.
            self._step("server", "warn",
                       "The server process is running but nothing is listening on "
                       "port %d yet." % self.port)
            manual.append("The Minecraft server is running but not listening on "
                          "port %d. Give it a moment if it is still starting, then "
                          "check server-port in Properties." % self.port)
        elif start_server:
            self._step("server", "run", "Starting the Minecraft server…")
            try:
                ok, msg = start_server()
            except Exception as e:
                ok, msg = False, str(e)
            if ok:
                # Wall-clock bounded, not iteration bounded: each probe against
                # an address with nothing on it costs a full connect timeout, so
                # counting attempts made the wait many times longer than
                # intended. Give up early if the process died, rather than
                # waiting out the clock on a server that is never coming.
                deadline = time.time() + self.start_timeout
                while time.time() < deadline:
                    if tcp_open(local_ip, self.port, timeout=1.5):
                        listening = True
                        break
                    if is_running and not is_running():
                        break
                    time.sleep(1)
                if listening:
                    fixed.append("Started the Minecraft server.")
                    self._step("server", "ok", "Started and listening on %d." % self.port)
                else:
                    self._step("server", "warn",
                               "Started, but nothing is listening on %d yet." % self.port)
                    manual.append("The server started but is not listening on port %d. "
                                  "Check the console for errors, and that server-port "
                                  "matches." % self.port)
            else:
                self._step("server", "fail", msg)
                if "eula" in (msg or "").lower():
                    # Accepting a licence on somebody's behalf is not a fix, it
                    # is a decision. Point at the button instead.
                    manual.append(
                        "The Minecraft EULA has not been accepted for this server. "
                        "Press 'Accept EULA' in the header, then run Auto fix again "
                        "— Lodestone will not agree to a licence for you.")
                else:
                    manual.append("Could not start the Minecraft server: %s" % msg)
        else:
            self._step("server", "fail", "Not running.")
            manual.append("Start the Minecraft server — nothing can connect to a "
                          "server that is not running.")

        # 3. the router
        self._step("mapping", "run", "Checking the port forward…")
        mapping = self.active_mapping()
        if mapping:
            self._step("mapping", "ok", "Already in place: %s" % mapping.describe())
        else:
            want = tuple(want_providers or ("upnp", "natpmp", "pcp"))
            try:
                mapping, attempts = self.mapping.ensure(
                    self.port, self.effective_external_port(), self.protocol,
                    DEFAULT_LEASE, want)
                with self._lock:
                    self.mapping_attempts = attempts
                if mapping:
                    self.router_external_ip = mapping.external_ip or ""
                    fixed.append("Created the port forward: %s" % mapping.describe())
                    self._step("mapping", "ok", mapping.describe())
                else:
                    self._step("mapping", "fail", "; ".join(
                        "%s: %s" % (a["provider"], a["detail"]) for a in attempts))
                    manual.append(
                        "No automatic port-forwarding protocol worked. Forward "
                        "TCP %d to %s:%d by hand in your router's settings."
                        % (self.effective_external_port(), local_ip, self.port))
            except NetworkingError as e:
                self._step("mapping", "fail", str(e))
                manual.append("Port forwarding failed: %s" % e)

        # 4. the host firewall
        if manage_firewall and self.firewall.supported:
            self._step("firewall", "run", "Checking Windows Firewall…")
            try:
                state = self.firewall.snapshot(self.port, self.protocol, deep=True)
                with self._lock:
                    self.firewall_state = state
                if state.get("likely_blocking"):
                    res = self.firewall.add_rule(self.port, self.protocol)
                    fixed.append(res.get("detail") or "Added a firewall rule.")
                    self._step("firewall", "ok", res.get("detail", ""))
                    with self._lock:
                        self.firewall_state = self.firewall.snapshot(
                            self.port, self.protocol, deep=False)
                else:
                    self._step("firewall", "ok", state.get("detail") or "Allowed.")
            except FirewallPermissionDenied as e:
                self._step("firewall", "warn", str(e))
                manual.append("The firewall rule needs administrator approval. "
                              "Press 'Allow port %d' again and accept the prompt."
                              % self.port)
            except NetworkingError as e:
                self._step("firewall", "warn", str(e))
                manual.append("Firewall check failed: %s" % e)
        else:
            self._step("firewall", "skip", "Not Windows.")

        # 5. and now ask the world again
        self._step("connectivity", "run", "Testing from outside…")
        self.public_ip.invalidate()
        pub4, pub6 = self.public_ip.ipv4(force=True), self.public_ip.ipv6()
        fw = self.firewall_state or {}
        result = self.tester.run(
            self.port, pub4, pub6, self.router_external_ip, local_ip,
            mapping_active=bool(self.active_mapping()),
            firewall_blocking=fw.get("likely_blocking"),
            external_port=self.effective_external_port(),
            router_wan_unknown=bool(self.active_mapping()) and not self.router_external_ip)
        with self._lock:
            self.connectivity = result
            self.last_checked_at = time.time()
            self.warnings = self._build_warnings(result, self.active_mapping())
            self.phase = (PHASE_READY if result["state"] == REACHABLE else PHASE_FAILED)
        state = result["state"]
        self._step("connectivity",
                   "ok" if state == REACHABLE else "warn" if state == UNKNOWN else "fail",
                   result["reason"])
        manual.extend(self._manual_steps(result, pub6))
        self._step("done", "ok" if state == REACHABLE else "warn",
                   "Fixed %d thing(s); %d left that Lodestone cannot do."
                   % (len(fixed), len(manual)))

    def _manual_steps(self, result, pub6=""):
        """The things no button on this machine can do."""
        state = result.get("state")
        if state == REACHABLE:
            return []
        out = []
        if result.get("router_not_edge"):
            edge = result.get("edge_router")
            isp = result.get("isp_nat_hop")
            if edge:
                out.append(
                    "Forward TCP %d on %s — that is the router your connection "
                    "actually arrives at. Point it at %s (this router), which "
                    "already forwards on to this PC. Cleaner still: put this "
                    "router into bridge or access-point mode, and then Lodestone "
                    "can do the whole thing automatically."
                    % (self.effective_external_port(), edge,
                       self.interfaces.gateway_ipv4() or "your inner router"))
            else:
                out.append(
                    "Your router is not the device your internet connection "
                    "arrives at, so a port forward on it cannot work. Forward "
                    "TCP %d on the box in front of it, or put that box into "
                    "bridge mode." % self.effective_external_port())
            if isp:
                out.append(
                    "Before you do: log into %s and read its WAN/Internet "
                    "address. If it shows your public address, forwarding there "
                    "will work. If it shows something like %s, your ISP is doing "
                    "the NAT and no forward on your own equipment can ever be "
                    "reached — ask them for a public IPv4, or use a tunnel."
                    % (edge or "that router", isp))
        addr = result.get("address") or {}
        if addr.get("cgnat"):
            out.append(
                "Your ISP is using carrier-grade NAT, so no port forward on your "
                "own equipment can be reached from outside. Ask your ISP for a "
                "public IPv4 address — many provide one free on request.")
        if addr.get("double_nat") and not result.get("router_not_edge"):
            out.append("There are two routers in series. The forward has to be "
                       "made on the outermost one as well.")
        if state in (CGNAT_SUSPECTED, NOT_REACHABLE, IPV6_ONLY):
            if pub6:
                out.append("You have a public IPv6 address. Friends whose "
                           "connections support IPv6 can reach you at [%s]:%d "
                           "with no port forwarding at all." % (pub6, self.port))
            out.append(
                "If none of the above applies, your ISP may block inbound "
                "connections. Tunnel mode sidesteps all of it: switch the mode "
                "to Tunnel and Lodestone will run a playit.gg agent, which needs "
                "no port forwarding and no router access at all.")
        return out

    # -- security -------------------------------------------------------------
    def security_audit(self, properties=None, rcon=None, panel_host="",
                       panel_port=None, check_router=False):
        """What publishing this port actually exposes.

        Reports; changes nothing. Two classes of finding: things that let
        strangers into the *game* (whitelist, online-mode) and things that let
        strangers into the *machine* (RCON on a public interface, a management
        port forwarded on the router).
        """
        props = {k: str(v).strip().lower() for k, v in (properties or {}).items()}
        rcon = rcon or {}
        out = []

        def add(level, title, detail, fix=""):
            out.append({"level": level, "title": title, "detail": detail, "fix": fix})

        if self.mode == MODE_INTERNET:
            add("info", "Your server address is public",
                "Anyone who learns %s can try to connect. Minecraft servers get "
                "scanned constantly — expect join attempts from strangers within "
                "hours." % (self.public_address() or "the address"),
                "Turn on the whitelist to keep it to people you invite.")

            if props.get("white-list") != "true":
                add("warn", "The whitelist is off",
                    "Any account can join. On a public address that means anyone.",
                    "Set white-list=true in Properties, then add your friends "
                    "with /whitelist add.")
            if props.get("online-mode") == "false":
                add("bad", "Online mode is off",
                    "The server does not verify accounts with Mojang, so anyone "
                    "can connect claiming to be any username — including yours. "
                    "This is dangerous on a public address.",
                    "Set online-mode=true unless you specifically need offline "
                    "mode, and never leave it off on a public server.")

        # -- RCON: a remote console, not a game port --------------------------
        if props.get("enable-rcon") == "true":
            rcon_ip = props.get("rcon.ip", "")
            rcon_port = props.get("rcon.port") or rcon.get("port") or 25575
            if rcon_ip in ("", "0.0.0.0"):
                level = "warn" if self.mode == MODE_INTERNET else "info"
                add(level, "RCON is listening on every interface",
                    "rcon.ip is not set, so RCON binds to the same interfaces as "
                    "the game. Lodestone never forwards port %s, so it is not "
                    "reachable from the internet through anything Lodestone did — "
                    "but a DMZ setting or another program's UPnP rule could "
                    "expose it." % rcon_port,
                    "Set rcon.ip=127.0.0.1 to keep RCON on this machine only.")
            if not (props.get("rcon.password") or rcon.get("password")):
                add("bad", "RCON has no password",
                    "An RCON port with an empty password is a remote console "
                    "anyone who reaches it can use.",
                    "Set a long rcon.password, or set enable-rcon=false.")

        if panel_host == "0.0.0.0":
            add("warn", "The panel itself is on the LAN",
                "Lodestone's own web UI has no login and is bound to 0.0.0.0, so "
                "every device on this network can control your servers. Lodestone "
                "never forwards its own port to the internet.",
                "Set the panel address back to 127.0.0.1 in Settings unless you "
                "trust every device on the network.")

        # -- what the router actually has open --------------------------------
        if check_router and self.mode == MODE_INTERNET:
            watch = {}
            try:
                watch[int(props.get("rcon.port") or rcon.get("port") or 25575)] = "RCON"
            except (TypeError, ValueError):
                pass
            watch[22] = "SSH / SFTP"
            if panel_port:
                try:
                    watch[int(panel_port)] = "the Lodestone panel"
                except (TypeError, ValueError):
                    pass
            mine = self.interfaces.local_ipv4()
            try:
                table = self.mapping.router_table()
            except Exception:
                table = []
            for row in table:
                try:
                    ext = int(row.get("external_port") or 0)
                    internal = int(row.get("internal_port") or 0)
                except (TypeError, ValueError):
                    continue
                if row.get("internal_client") != mine:
                    continue
                what = watch.get(internal) or watch.get(ext)
                if what:
                    add("bad", "Your router forwards %s to this PC" % what,
                        "There is a rule on the router sending external port %d "
                        "to %s:%d, which is %s. Lodestone did not create it."
                        % (ext, mine, internal, what),
                        "Remove that rule in the router's own settings. "
                        "Management ports do not belong on the internet.")
        return out

    # -- test only ------------------------------------------------------------
    def recheck(self, blocking=False, deep_firewall=False):
        """Re-run the reachability assessment without touching the router."""
        if self.busy():
            raise NetworkingError("A hosting change is already running.")

        def work():
            with self._lock:
                self.connectivity = {"state": CHECKING, "reason": "Checking…"}
            try:
                self.on_change()
            except Exception:
                pass
            pub4 = self.public_ip.ipv4()
            pub6 = self.public_ip.ipv6()
            fw = {}
            if self.firewall.supported and self.mode == MODE_INTERNET:
                try:
                    fw = self.firewall.snapshot(self.port, self.protocol,
                                                deep=deep_firewall)
                except NetworkingError:
                    fw = {}
            mapping = self.active_mapping()
            if self.mode == MODE_INTERNET:
                result = self.tester.run(
                    self.port, pub4, pub6, self.router_external_ip,
                    self.interfaces.local_ipv4(), bool(mapping),
                    fw.get("likely_blocking"),
                    external_port=self.effective_external_port(),
                    router_wan_unknown=bool(mapping) and not self.router_external_ip,
            # Only traced when the mapping went in but the router cannot name
            # its own WAN — the one case where "which box do I configure?" is
            # the question, and the only case worth spending seconds on a trace.
            upstream=(self.interfaces.upstream_hops()
                      if bool(mapping) and not self.router_external_ip else None))
            else:
                local = self.tester.local_check(self.port,
                                                self.interfaces.local_ipv4())
                result = {"state": UNKNOWN if local["listening"] else SERVER_OFFLINE,
                          "reason": local["detail"], "local": local,
                          "checked_at": time.time(), "port": self.port}
            with self._lock:
                self.connectivity = result
                self.firewall_state = fw or self.firewall_state
                self.last_checked_at = time.time()
            # Keep the step list honest. It is written by apply(), so without
            # this a later re-check leaves the two disagreeing on screen — the
            # summary saying one thing and "What happened" still showing the
            # verdict from minutes ago.
            state = result.get("state")
            self._step("connectivity",
                       "ok" if state == REACHABLE
                       else "warn" if state in (UNKNOWN, CHECKING) else "fail",
                       result.get("reason", ""))
            self._step("done",
                       "ok" if state == REACHABLE else "warn",
                       "Re-checked: %s" % STATE_LABEL.get(state, state))
            try:
                self.on_change()
            except Exception:
                pass
            return result

        if blocking:
            return work()
        with self._lock:
            self._worker = threading.Thread(target=work, name="lodestone-hosting-test",
                                            daemon=True)
            self._worker.start()
        return {"state": CHECKING, "reason": "Checking…"}

    # -- cleanup --------------------------------------------------------------
    def disable(self, remove_firewall_rule=False, blocking=True,
                write_properties=None):
        """Back to LAN mode, mapping removed. Safe to call twice."""
        def work():
            results = self._teardown(remove_firewall_rule)
            with self._lock:
                self.mode = MODE_LAN
                self.phase = PHASE_IDLE
                self.connectivity = {"state": UNKNOWN,
                                     "reason": "Internet hosting is off."}
                self.warnings = []
            if write_properties:
                try:
                    write_properties(self.desired_properties(MODE_LAN))
                except Exception as e:
                    self._step("properties", "warn", str(e))
            self._step("done", "ok", "Internet hosting disabled.")
            return results

        if blocking:
            return work()
        threading.Thread(target=work, daemon=True).start()
        return []

    def _teardown(self, remove_firewall_rule):
        self._reset_steps()
        results = []
        if self.tunnel.running():
            self._step("tunnel", "run", "Stopping the tunnel agent…")
            try:
                ok, detail = self.tunnel.stop()
                self._step("tunnel", "ok" if ok else "warn", detail)
            except Exception as e:
                self._step("tunnel", "warn", str(e))
        owned = self.mapping.owned()
        if owned:
            self._step("mapping", "run", "Removing the port mapping…")
            results = self.mapping.release()
            ok = [r for r in results if r["ok"]]
            self._step("mapping", "ok" if len(ok) == len(results) else "warn",
                       "; ".join("%s: %s" % (r["mapping"], r["detail"])
                                 for r in results))
        else:
            self._step("mapping", "skip", "There was no mapping to remove.")
        if remove_firewall_rule and self.firewall.supported:
            self._step("firewall", "run", "Removing the firewall rule…")
            try:
                res = self.firewall.remove_rule(self.port, self.protocol)
                self._step("firewall", "ok", res.get("detail", ""))
            except NetworkingError as e:
                self._step("firewall", "warn", str(e))
        self.router_external_ip = ""
        return results

    def shutdown(self):
        """Called when the panel exits. Best effort and must never raise."""
        try:
            self.tunnel.close()
        except Exception:
            pass
        try:
            if self.mapping.owned():
                self.mapping.release()
        except Exception:
            pass
        try:
            self.mapping.close()
        except Exception:
            pass

    # -- what the UI renders --------------------------------------------------
    def brief(self):
        """The cheap summary. Cached values only — no sockets, no subprocesses,
        no DNS. The panel's status poll runs twice a second and calls this, so
        anything expensive in here would show up as a laggy UI."""
        with self._lock:
            state, _ = self._live_state()
            online = self.mode == MODE_INTERNET and state == REACHABLE
            return {
                "mode": self.mode, "mode_label": MODE_LABEL[self.mode],
                "port": self.port, "phase": self.phase,
                "busy": bool(self._worker and self._worker.is_alive()),
                "online": online,
                "connectivity_state": state,
                "connectivity_label": STATE_LABEL.get(state, "Unknown"),
                "mapping_active": bool(self.active_mapping()),
                "tunnel_running": self.tunnel.running(),
                "stale": self.mode == MODE_INTERNET and not self.verdict_fresh(),
                "error": self.error,
            }

    def snapshot(self, include_probe=False):
        # Gathered before taking the lock: on a cold cache these shell out to
        # `route print`, and holding the lock across that would stall the
        # worker thread mid-operation.
        pub = self.public_ip.snapshot()
        net = self.interfaces.snapshot()
        tunnel_status = self.tunnel.status()
        with self._lock:
            mapping = self.active_mapping()
            conn = dict(self.connectivity or {})
            state, live_reason = self._live_state()
            if state != conn.get("state"):
                # Evidence expired between the check and now. Show the state
                # that is true today, and say why it changed.
                conn["state"] = state
                conn["reason"] = live_reason
                conn["expired"] = True
            external_port = self.effective_external_port()
            public_addr = ("%s:%d" % (pub["ipv4"], external_port)) if pub["ipv4"] else ""
            online = (self.mode in (MODE_INTERNET, MODE_TUNNEL)
                      and state == REACHABLE)
            out = {
                "mode": self.mode,
                "mode_label": MODE_LABEL[self.mode],
                "phase": self.phase,
                "busy": bool(self._worker and self._worker.is_alive()),
                "error": self.error,
                "steps": list(self.steps),
                "warnings": list(self.warnings),

                "port": self.port,
                "external_port": external_port,
                "protocol": self.protocol,
                "different_ports": external_port != self.port,

                "local_ipv4": net["local_ipv4"],
                "all_ipv4": net["all_ipv4"],
                "ipv6": net["ipv6"],
                "gateway": net["gateway"],
                "local_address": "127.0.0.1:%d" % self.port,
                "lan_address": "%s:%d" % (net["local_ipv4"], self.port),
                "public_address": public_addr,
                "ipv6_address": ("[%s]:%d" % (net["ipv6"], self.port)
                                 if net["ipv6"] else ""),
                "public_ip": pub,
                "router_external_ip": self.router_external_ip,

                "mapping": mapping.as_dict() if mapping else None,
                "mapping_text": mapping.describe() if mapping else "",
                "mapping_active": bool(mapping),
                "mapping_attempts": list(self.mapping_attempts),
                "all_mappings": self.mapping.export(),

                "firewall": dict(self.firewall_state),
                "connectivity": conn,
                "connectivity_state": state,
                "connectivity_label": STATE_LABEL.get(state, "Unknown"),
                "checked_at": self.last_checked_at,
                "checked_age": self.verdict_age(),
                "stale": self.mode == MODE_INTERNET and not self.verdict_fresh(),
                "verdict_ttl": VERDICT_TTL,
                "identity_confirmed": conn.get("identity"),
                "autofix": self.autofix_result,
                "tunnel": tunnel_status,
                "tunnel_address": tunnel_status.get("address", ""),
                "enabled_at": self.last_enabled_at,

                # The headline. Deliberately conservative: it is only true when
                # a machine outside this network actually got an answer.
                "online": online,
                "headline": self._headline(online, state),
                # What to actually give friends. In tunnel mode the address
                # belongs to the relay, not to this network.
                "advertise": (tunnel_status.get("address") or ""
                              if self.mode == MODE_TUNNEL else
                              public_addr if online else
                              "%s:%d" % (net["local_ipv4"], self.port)
                              if self.mode == MODE_LAN else
                              "127.0.0.1:%d" % self.port),
            }
        if include_probe:
            out["providers"] = self.mapping.probe()
        return out

    def _headline(self, online, state):
        if self.mode == MODE_LOCAL:
            return "Local only — this machine can connect."
        if self.mode == MODE_LAN:
            return "LAN — devices on this network can connect."
        if self.mode == MODE_TUNNEL:
            st = self.tunnel.status()
            if self.phase == PHASE_WORKING:
                return "Bringing the tunnel up…"
            if st.get("needs_claim"):
                return ("ngrok is waiting for an authtoken."
                        if st.get("provider") == "ngrok"
                        else "Tunnel waiting to be claimed in your browser.")
            if online:
                return "Online through the tunnel — %s" % st.get("address", "")
            if not st.get("running"):
                return "Tunnel mode, but the agent is not running."
            return "Tunnel running, not confirmed reachable: %s" % STATE_LABEL.get(
                state, "Unknown")
        if self.phase == PHASE_WORKING:
            return "Setting up internet hosting…"
        if online:
            return "Online — reachable from the internet."
        return "Internet mode, not confirmed reachable: %s" % STATE_LABEL.get(
            state, "Unknown")


def _why(key, mode):
    if key == "server-ip":
        if mode == MODE_LOCAL:
            return ("Local mode binds the server to the loopback address so "
                    "nothing outside this machine can reach it.")
        return ("An empty server-ip makes Minecraft listen on every network "
                "interface. Anything else — 127.0.0.1 especially — makes a "
                "forwarded port useless.")
    if key == "server-port":
        return "The server has to listen on the port being published."
    return ""
