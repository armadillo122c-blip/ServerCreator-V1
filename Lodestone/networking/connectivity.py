"""ConnectivityTester — is the server reachable from the internet, really?

The trap this module exists to avoid: a TCP connect to your own port always
succeeds, from inside. It proves the process is listening and proves nothing at
all about the internet. A panel that lights up green on that check is lying.

So there are three separate questions here and they are answered separately:

  1. Is a Minecraft server listening locally?  — connect to the LAN address and
     speak the Server List Ping handshake. Not just "something accepted a TCP
     connection": an SSH daemon would pass that.
  2. Is the address we would publish even capable of receiving connections? —
     a public IP inside RFC 1918 or RFC 6598 means CGNAT and the answer is no,
     whatever the router said about the mapping.
  3. Does a machine somewhere else on the internet get an answer on that port?
     — the only way to know is to have one try, so a third-party Minecraft
     status API is asked to ping the address. That request necessarily
     discloses the address and port being tested, which is why it is a distinct
     step the user can switch off.

A hairpin test (connecting to our own public IP from inside) is deliberately
*not* treated as evidence: plenty of routers do not implement NAT loopback, so
failure means nothing, and success can be served by the router itself.
"""

import json
import socket
import struct
import time
import urllib.error
import urllib.parse
import urllib.request

from .errors import ConnectivityError
from .net_interfaces import is_cgnat_v4, is_private_v4, is_valid_ipv4

# The vocabulary the UI renders. Anything else is a bug.
CHECKING = "checking"
REACHABLE = "reachable"
NOT_REACHABLE = "not_reachable"
SERVER_OFFLINE = "server_offline"
PORT_BLOCKED = "port_blocked"
CGNAT_SUSPECTED = "cgnat_suspected"
IPV6_ONLY = "ipv6_only"
UNKNOWN = "unknown"

STATE_LABEL = {
    CHECKING: "Checking…",
    REACHABLE: "Reachable",
    NOT_REACHABLE: "Not reachable",
    SERVER_OFFLINE: "Server offline",
    PORT_BLOCKED: "Port blocked",
    CGNAT_SUSPECTED: "CGNAT suspected",
    IPV6_ONLY: "IPv6 only",
    UNKNOWN: "Unknown",
}

USER_AGENT = "Lodestone/1.0 (+minecraft server panel)"

# Both of these actually open a connection to the address from their own
# infrastructure, which is the property we need. Both also cache for about a
# minute, so a re-test straight after a fix can show the stale answer — the
# caller is told when a result came back suspiciously fast.
EXTERNAL_CHECKERS = (
    ("mcstatus.io", "https://api.mcstatus.io/v2/status/java/{host}:{port}?timeout=4.0"),
    ("mcsrvstat.us", "https://api.mcsrvstat.us/3/{host}:{port}"),
)


# -- Minecraft Server List Ping ----------------------------------------------

def write_varint(value):
    out = bytearray()
    value &= 0xFFFFFFFF
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def read_varint(sock, limit=5):
    """Read a varint off a socket. Raises ConnectivityError on a short read."""
    num = 0
    for i in range(limit):
        chunk = sock.recv(1)
        if not chunk:
            raise ConnectivityError("The server closed the connection mid-reply.")
        byte = chunk[0]
        num |= (byte & 0x7F) << (7 * i)
        if not byte & 0x80:
            return num
    raise ConnectivityError("The server sent a malformed reply.")


def _packet(packet_id, payload=b""):
    body = write_varint(packet_id) + payload
    return write_varint(len(body)) + body


def _mc_string(text):
    raw = text.encode("utf-8")
    return write_varint(len(raw)) + raw


def _ping_once(host, port, timeout=3.0, protocol=-1):
    """Speak Server List Ping to host:port.

    Returns the server's status dict (version, players, description) on
    success. Raises ConnectivityError with a reason on failure.

    A plain TCP connect is not enough here — this is what tells "a Minecraft
    server is on that port" apart from "something is on that port".
    """
    sock = None
    started = time.time()
    try:
        try:
            sock = socket.create_connection((host, int(port)), timeout)
        except socket.timeout:
            raise ConnectivityError("Nothing answered %s:%s within %ss."
                                    % (host, port, timeout))
        except ConnectionRefusedError:
            raise ConnectivityError("Connection refused on %s:%s — nothing is "
                                    "listening there." % (host, port))
        except socket.gaierror as e:
            raise ConnectivityError("Could not resolve %s (%s)." % (host, e))
        except OSError as e:
            raise ConnectivityError("Could not reach %s:%s (%s)." % (host, port, e))
        sock.settimeout(timeout)

        handshake = _packet(0x00,
                            write_varint(protocol if protocol >= 0 else 0xFFFFFFFF)
                            + _mc_string(str(host))
                            + struct.pack(">H", int(port))
                            + write_varint(1))
        # Everything from here on talks to a socket that may be an SSH daemon, a
        # web server, or a Minecraft server mid-restart. Any of them can reset
        # the connection at any point, and none of that may escape as a bare
        # OSError — the caller's whole contract is that it gets a reason string.
        try:
            sock.sendall(handshake)
            sock.sendall(_packet(0x00))          # status request
        except OSError as e:
            raise ConnectivityError("%s:%s accepted the connection then dropped "
                                    "it (%s)." % (host, port, e))

        try:
            length = read_varint(sock)
        except ConnectivityError:
            raise ConnectivityError(
                "Something is listening on %s:%s but it did not answer the "
                "Minecraft handshake." % (host, port))
        except socket.timeout:
            raise ConnectivityError("%s:%s accepted the connection but never "
                                    "replied." % (host, port))
        except OSError as e:
            raise ConnectivityError("%s:%s dropped the connection (%s)."
                                    % (host, port, e))
        if length <= 0 or length > 4_000_000:
            raise ConnectivityError("That port answered with something that is "
                                    "not a Minecraft server.")
        buf = bytearray()
        try:
            while len(buf) < length:
                chunk = sock.recv(min(65536, length - len(buf)))
                if not chunk:
                    break
                buf += chunk
        except socket.timeout:
            raise ConnectivityError("%s:%s stopped replying part-way through."
                                    % (host, port))
        except OSError:
            # A service that is not Minecraft will often send a banner whose
            # first byte we read as a length, then close. That is a definite
            # "not a Minecraft server", not a transient failure.
            raise ConnectivityError(
                "%s:%s is not answering the Minecraft protocol — it closed the "
                "connection part-way through the reply." % (host, port))
        if len(buf) < length:
            raise ConnectivityError(
                "%s:%s did not send a complete Minecraft reply — most likely it "
                "is some other service." % (host, port))

        view = memoryview(buf)
        pos, packet_id = _read_varint_bytes(view, 0)
        if packet_id != 0x00:
            raise ConnectivityError("Unexpected packet 0x%02x from the server."
                                    % packet_id)
        pos, str_len = _read_varint_bytes(view, pos)
        raw = bytes(view[pos:pos + str_len])
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise ConnectivityError("The server replied with unreadable JSON.")
        data["_latency_ms"] = int((time.time() - started) * 1000)
        return data
    finally:
        if sock:
            try:
                sock.close()
            except OSError:
                pass



# Protocol -1 means "I do not know my version", and is sent on the wire as the
# 0xFFFFFFFF sentinel. Servers accept it happily. A relay sitting in front of
# one does not have to: playit.gg parses the handshake and resets the
# connection outright, which made every tunnel look unreachable while it was in
# fact serving players. The server behind it answers a real protocol number, so
# a dropped connection on the sentinel earns exactly one retry.
FALLBACK_PROTOCOL = 767


def minecraft_ping(host, port, timeout=3.0, protocol=-1):
    """Speak Server List Ping to host:port.

    Returns the server's status dict (version, players, description) on
    success. Raises ConnectivityError with a reason on failure.

    A plain TCP connect is not enough here - this is what tells "a Minecraft
    server is on that port" apart from "something is on that port".
    """
    try:
        return _ping_once(host, port, timeout, protocol)
    except ConnectivityError as e:
        # Only the sentinel is worth retrying, and only when the far end hung up
        # rather than refused or timed out - otherwise this doubles the wait for
        # every address that is genuinely dead.
        if protocol >= 0 or "dropped the connection" not in str(e):
            raise
    return _ping_once(host, port, timeout, FALLBACK_PROTOCOL)

def _read_varint_bytes(view, pos):
    num, shift = 0, 0
    while pos < len(view):
        byte = view[pos]
        pos += 1
        num |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return pos, num
        shift += 7
        if shift > 35:
            break
    raise ConnectivityError("Malformed varint in the server reply.")


def tcp_open(host, port, timeout=2.0):
    """Bare 'did a TCP connection complete'. Useful only for local checks."""
    try:
        with socket.create_connection((host, int(port)), timeout):
            return True
    except OSError:
        return False


def port_in_use(port, host="0.0.0.0"):
    """Can we bind it? If not, something else on this machine holds it.

    SO_REUSEADDR is deliberately NOT set: on Linux it would let the bind succeed
    against a socket in TIME_WAIT and report a free port that is not.
    """
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind((host, int(port)))
        return False
    except OSError:
        return True
    finally:
        if sock:
            try:
                sock.close()
            except OSError:
                pass


# -- the tester ---------------------------------------------------------------

class ConnectivityTester:

    def __init__(self, interfaces, checkers=EXTERNAL_CHECKERS, timeout=8,
                 allow_external=True, log=None):
        self.interfaces = interfaces
        self.checkers = list(checkers)
        self.timeout = timeout
        self.allow_external = allow_external
        self.log = log or (lambda msg: None)

    # -- step 1: local --------------------------------------------------------
    def local_check(self, port, host=""):
        """Is a Minecraft server listening on this machine?"""
        host = host or self.interfaces.local_ipv4() or "127.0.0.1"
        out = {"host": host, "port": int(port), "listening": False,
               "minecraft": False, "detail": "", "status": None}
        try:
            data = minecraft_ping(host, port, timeout=3.0)
            out["listening"] = True
            out["minecraft"] = True
            out["status"] = _summarise_status(data)
            out["detail"] = "Minecraft server answered in %sms." % data.get("_latency_ms", 0)
            return out
        except ConnectivityError as e:
            out["detail"] = str(e)
        # SLP can be off (enable-status=false) while the server still accepts
        # joins, so fall back to a plain connect before calling it offline.
        if tcp_open(host, port, timeout=2.0):
            out["listening"] = True
            out["detail"] = ("Something is listening on %s:%s but it did not answer "
                             "a status ping. If you have enable-status=false in "
                             "server.properties that is expected." % (host, port))
        return out

    # -- step 2: the address --------------------------------------------------
    @staticmethod
    def classify_address(public_ipv4, router_external_ip="", local_ipv4=""):
        """Decide whether the address we would publish can receive connections.

        Two independent CGNAT tells:
          * the address a public service reports for us is itself in 100.64/10;
          * the router says its WAN address is private, but the outside world
            sees something different — there is another NAT above ours.
        """
        out = {"cgnat": False, "double_nat": False, "reason": "", "usable": True}
        if router_external_ip and (is_cgnat_v4(router_external_ip)
                                   or is_private_v4(router_external_ip)):
            out["cgnat"] = is_cgnat_v4(router_external_ip)
            out["double_nat"] = True
            out["usable"] = False
            out["reason"] = (
                "Your router's own internet address is %s, which is a private or "
                "carrier-grade NAT address. Forwarding a port on this router only "
                "gets traffic as far as your ISP's equipment."
                % router_external_ip)
            return out
        if public_ipv4 and is_cgnat_v4(public_ipv4):
            out["cgnat"] = True
            out["usable"] = False
            out["reason"] = ("Your public address %s is in the carrier-grade NAT "
                             "range (100.64.0.0/10)." % public_ipv4)
            return out
        if (public_ipv4 and router_external_ip and is_valid_ipv4(router_external_ip)
                and public_ipv4 != router_external_ip):
            out["double_nat"] = True
            out["usable"] = False
            out["reason"] = (
                "Your router thinks its internet address is %s, but the internet "
                "sees %s. There is a second router or an ISP NAT in between."
                % (router_external_ip, public_ipv4))
            return out
        if public_ipv4 and is_private_v4(public_ipv4):
            out["usable"] = False
            out["reason"] = ("The address reported as public, %s, is a private "
                             "address." % public_ipv4)
        return out

    # -- step 3: from outside -------------------------------------------------
    def external_check(self, host, port):
        """Ask somebody on the internet to connect. Returns a dict; `online` is
        True only on a positive answer, never on a failed request."""
        result = {"online": False, "checked": [], "detail": "",
                  "conclusive": False, "source": ""}
        if not self.allow_external:
            result["detail"] = ("The external reachability check is switched off, "
                                "so public reachability cannot be confirmed.")
            return result
        if not host:
            result["detail"] = "No public address to test."
            return result
        errors = []
        for name, template in self.checkers:
            url = template.format(host=urllib.parse.quote(str(host)), port=int(port))
            entry = {"service": name, "online": None, "detail": ""}
            try:
                data = self._fetch_json(url)
            except ConnectivityError as e:
                entry["detail"] = str(e)
                errors.append("%s: %s" % (name, e))
                result["checked"].append(entry)
                continue
            online = bool(data.get("online"))
            entry["online"] = online
            entry["detail"] = _summarise_external(data)
            result["checked"].append(entry)
            if online:
                result["online"] = True
                result["conclusive"] = True
                result["source"] = name
                result["detail"] = "%s reached the server from outside your network." % name
                result["status"] = _summarise_status(data)
                return result
        answered = [c for c in result["checked"] if c["online"] is not None]
        if answered:
            result["conclusive"] = True
            result["detail"] = ("%s could not reach the server from outside."
                                % ", ".join(c["service"] for c in answered))
        else:
            result["detail"] = ("No reachability service could be contacted (%s). "
                                "Public reachability is unknown, not confirmed."
                                % "; ".join(errors[:2]))
        return result

    def _fetch_json(self, url):
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                if r.status != 200:
                    raise ConnectivityError("HTTP %s" % r.status)
                raw = r.read(256 * 1024)
        except urllib.error.HTTPError as e:
            raise ConnectivityError("HTTP %s" % e.code)
        except urllib.error.URLError as e:
            raise ConnectivityError("unreachable (%s)" % e.reason)
        except (OSError, ValueError) as e:
            raise ConnectivityError("unreachable (%s)" % e)
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            raise ConnectivityError("unreadable reply")
        if not isinstance(data, dict):
            raise ConnectivityError("unexpected reply shape")
        return data

    # -- the whole thing ------------------------------------------------------
    @staticmethod
    def same_server(local_status, remote_status):
        """Is the server the outside world reached the same one running here?

        Returns True / False / None (cannot tell).

        This matters on a shared public address — CGNAT especially, where the
        same IP belongs to many subscribers. Without it, another customer with
        25565 forwarded to *their* server would make us report ours as publicly
        reachable, which is the exact false-positive this whole module exists to
        avoid.

        Deliberately reluctant to declare a mismatch: version strings come from
        two different formatters ("1.20.4" against "Paper 1.20.4"), so a
        difference only counts when neither contains the other.
        """
        if not isinstance(local_status, dict) or not isinstance(remote_status, dict):
            return None
        signals = []
        lv = str(local_status.get("version") or "").strip().lower()
        rv = str(remote_status.get("version") or "").strip().lower()
        if lv and rv:
            signals.append(lv == rv or lv in rv or rv in lv)
        lm, rm = local_status.get("players_max"), remote_status.get("players_max")
        if isinstance(lm, int) and isinstance(rm, int):
            signals.append(lm == rm)
        if not signals:
            return None
        if all(signals):
            return True
        return False

    def run(self, port, public_ipv4="", public_ipv6="", router_external_ip="",
            local_ip="", mapping_active=False, firewall_blocking=None,
            external_port=None, router_wan_unknown=False, upstream=None):
        """The full assessment. Returns a dict with a `state` from the list at
        the top of this module and a human-readable `reason`.

        The bar for REACHABLE is deliberately high: an external service must
        have actually connected. Everything else degrades to a state that says
        so plainly, because reporting a server as publicly reachable when it is
        not is the single worst thing this feature could do.
        """
        started = time.time()
        # The port the world would dial is the EXTERNAL one. Probing the
        # internal port when the two differ tests an address nothing forwards.
        probe_port = int(external_port or port)
        out = {"state": UNKNOWN, "reason": "", "checked_at": started,
               "port": int(port), "probe_port": probe_port, "local": None,
               "address": None, "external": None, "public_ipv4": public_ipv4,
               "public_ipv6": public_ipv6, "identity": None, "took_ms": 0}

        local = self.local_check(port, local_ip)
        out["local"] = local
        if not local["listening"]:
            out["state"] = SERVER_OFFLINE
            out["reason"] = ("No Minecraft server is listening on port %s of this "
                             "machine. %s" % (port, local["detail"]))
            out["took_ms"] = int((time.time() - started) * 1000)
            return out

        address = self.classify_address(public_ipv4, router_external_ip, local_ip)
        out["address"] = address

        if address["cgnat"] or address["double_nat"]:
            # Still worth running the external probe: a few ISPs hand out a
            # CGNAT-looking address and route it anyway, and IPv6 may work.
            ext = self.external_check(public_ipv4, probe_port) if public_ipv4 else None
            out["external"] = ext
            same = (self.same_server(local.get("status"), (ext or {}).get("status"))
                    if ext and ext["online"] else None)
            out["identity"] = same
            if ext and ext["online"] and same is True:
                out["state"] = REACHABLE
                out["reason"] = ext["detail"] + (
                    " (Your address looked like CGNAT, but the connection got "
                    "through anyway, and it reached this server.)")
            elif ext and ext["online"]:
                # Something answered, but on a shared address we cannot show it
                # was us — and saying "reachable" here is precisely the lie this
                # feature must not tell.
                out["state"] = UNKNOWN
                out["reason"] = (
                    "A Minecraft server answered on %s:%d, but it could not be "
                    "confirmed as yours — this address is shared with other "
                    "customers of your ISP, so the reply may belong to someone "
                    "else. %s" % (public_ipv4, probe_port, address["reason"]))
            elif public_ipv6:
                out["state"] = IPV6_ONLY
                out["reason"] = (address["reason"] +
                                 " You do have a public IPv6 address, which friends "
                                 "on IPv6 can use directly.")
            else:
                out["state"] = CGNAT_SUSPECTED
                out["reason"] = address["reason"]
            out["took_ms"] = int((time.time() - started) * 1000)
            return out

        if not public_ipv4:
            if public_ipv6:
                out["state"] = IPV6_ONLY
                out["reason"] = ("No public IPv4 address is available. Friends on "
                                 "IPv6 can still connect using the IPv6 address.")
            else:
                out["state"] = UNKNOWN
                out["reason"] = ("The public address could not be determined, so "
                                 "reachability cannot be tested.")
            out["took_ms"] = int((time.time() - started) * 1000)
            return out

        ext = self.external_check(public_ipv4, probe_port)
        out["external"] = ext
        if ext["online"]:
            same = self.same_server(local.get("status"), ext.get("status"))
            out["identity"] = same
            if same is False:
                # A different server answered on our address and port. Either
                # the mapping points elsewhere or the address is shared.
                out["state"] = UNKNOWN
                out["reason"] = (
                    "Something answered on %s:%d, but it is not this server — "
                    "the version or player limit does not match. The port may be "
                    "forwarded to a different machine."
                    % (public_ipv4, probe_port))
                out["took_ms"] = int((time.time() - started) * 1000)
                return out
            out["state"] = REACHABLE
            out["reason"] = ext["detail"]
        elif not ext["conclusive"]:
            out["state"] = UNKNOWN
            out["reason"] = ext["detail"]
        elif firewall_blocking:
            out["state"] = PORT_BLOCKED
            out["reason"] = ("The server is running and the router mapping is in "
                             "place, but Windows Firewall is not allowing inbound "
                             "connections on port %s." % port)
        elif not mapping_active:
            out["state"] = NOT_REACHABLE
            out["reason"] = ("Nothing reached the server from outside, and no port "
                             "mapping is active. The port needs forwarding on the "
                             "router.")
        else:
            out["state"] = NOT_REACHABLE
            out["reason"] = ("The mapping is in place and the server is running, "
                             "but the connection did not get through. Common causes: "
                             "a host firewall, a second router in front of this one, "
                             "or an ISP that blocks inbound connections.")
            if router_wan_unknown:
                # The router accepted the mapping but cannot say what its own
                # internet address is, while the outside world can see one. That
                # is not a normal edge router: it is in bridge/AP mode, or it is
                # itself behind the ISP's equipment. Either way the hole we just
                # punched is in the wrong box, and that is worth saying outright
                # rather than leaving in a list of "common causes".
                out["reason"] += (
                    " Your router accepted the port forward but does not know its "
                    "own internet address, while the internet sees %s. That means "
                    "this router is not the device your connection actually comes "
                    "in through — it is in bridge or access-point mode, or your ISP "
                    "has another router in front of it. A port forward here cannot "
                    "reach the internet no matter what it says."
                    % (public_ipv4 or "a public address"))
                out["router_not_edge"] = True
                out["upstream"] = list(upstream or [])
                mine = [h for h in (upstream or []) if h.get("ours")]
                isp = [h for h in (upstream or []) if not h.get("ours")]
                if mine:
                    out["edge_router"] = mine[0]["ip"]
                    out["reason"] += (
                        " The next router out is %s — that is the one the port has "
                        "to be forwarded on." % mine[0]["ip"])
                if isp:
                    out["isp_nat_hop"] = isp[0]["ip"]
                    out["reason"] += (
                        " Above that is %s, which is inside your ISP's own network, "
                        "so they may be running NAT too — check what WAN address %s "
                        "reports before assuming a forward there will work."
                        % (isp[0]["ip"], mine[0]["ip"] if mine else "your router"))
        out["took_ms"] = int((time.time() - started) * 1000)
        return out


def _summarise_status(data):
    """Squash a Minecraft status blob down to the few fields worth showing, and
    nothing that identifies the people on the server."""
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    if isinstance(version, dict):
        version = version.get("name") or version.get("name_clean") or ""
    players = data.get("players")
    online = maxi = None
    if isinstance(players, dict):
        online, maxi = players.get("online"), players.get("max")
    return {"version": version or "", "players_online": online,
            "players_max": maxi,
            "latency_ms": data.get("_latency_ms")}


def _summarise_external(data):
    if data.get("online"):
        return "online"
    return str(data.get("error") or data.get("debug", {}).get("error")
               or "reported offline")[:200]
