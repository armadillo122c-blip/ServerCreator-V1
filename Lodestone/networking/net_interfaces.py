"""NetworkInterfaceService — what this machine's own addressing looks like.

Everything else needs answers from here first: which local IPv4 to hand the
router, whether we have a routable IPv6, and where the default gateway is
(NAT-PMP and PCP are addressed to it directly, they have no discovery step).

No third-party libraries and no packets leave the machine: a UDP `connect()`
sends nothing, it just makes the kernel pick the route it would use, which is
the only portable way to learn "the address the LAN sees me on".
"""

import ipaddress
import os
import re
import socket
import subprocess
import sys
import time

# 100.64.0.0/10 — RFC 6598. An address in here on your *WAN* side means the ISP
# has put you behind their own NAT, and no amount of port forwarding on your own
# router will make you reachable.
CGNAT_NET = ipaddress.ip_network("100.64.0.0/10")

_PRIVATE4 = [ipaddress.ip_network(n) for n in
             ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
              "169.254.0.0/16", "127.0.0.0/8", "100.64.0.0/10")]

_IPV4_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")
_IPV4_FIND_RE = re.compile(r"(\d{1,3}(?:\.\d{1,3}){3})")

# Kept short: these run on the request path for the status endpoint.
_CMD_TIMEOUT = 4


def run_quiet(argv, timeout=_CMD_TIMEOUT):
    """Run a system command without flashing a console window on Windows.
    Returns (returncode, combined_output). Never raises."""
    try:
        kw = {}
        if sys.platform.startswith("win"):
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            kw["startupinfo"] = si
            kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        r = subprocess.run(argv, capture_output=True, text=True,
                           timeout=timeout, errors="replace", **kw)
        return r.returncode, (r.stdout or "") + "\n" + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return -1, "timed out after %ss" % timeout
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return -1, str(e)


def _out(argv):
    return run_quiet(argv)[1]


def is_valid_ipv4(text):
    text = (text or "").strip()
    if not _IPV4_RE.match(text):
        return False
    try:
        ipaddress.IPv4Address(text)
        return True
    except ValueError:
        return False


def is_valid_ipv6(text):
    try:
        ipaddress.IPv6Address((text or "").strip())
        return True
    except ValueError:
        return False


def is_private_v4(text):
    try:
        addr = ipaddress.IPv4Address((text or "").strip())
    except ValueError:
        return False
    return any(addr in net for net in _PRIVATE4)


def is_cgnat_v4(text):
    try:
        return ipaddress.IPv4Address((text or "").strip()) in CGNAT_NET
    except ValueError:
        return False


def is_global_v6(text):
    try:
        addr = ipaddress.IPv6Address((text or "").strip())
    except ValueError:
        return False
    return bool(addr.is_global) and not addr.is_link_local and not addr.is_loopback


class NetworkInterfaceService:
    """Local addressing facts, cached briefly so the status endpoint can ask on
    every poll without re-probing the OS each time."""

    def __init__(self, cache_seconds=20):
        self.cache_seconds = cache_seconds
        self._cache = {}

    # -- cache ---------------------------------------------------------------
    def _cached(self, key, fn):
        hit = self._cache.get(key)
        now = time.time()
        if hit and now - hit[0] < self.cache_seconds:
            return hit[1]
        val = fn()
        self._cache[key] = (now, val)
        return val

    def invalidate(self):
        self._cache.clear()

    # -- IPv4 ----------------------------------------------------------------
    @staticmethod
    def _probe_local_v4():
        s = None
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(1.0)
            s.connect(("8.8.8.8", 80))       # no packet is sent for UDP
            return s.getsockname()[0]
        except OSError:
            return ""
        finally:
            if s:
                try:
                    s.close()
                except OSError:
                    pass

    def local_ipv4(self):
        """The address this machine uses to reach the rest of the network — the
        one the router must be told to forward to."""
        def go():
            ip = self._probe_local_v4()
            if ip and ip != "0.0.0.0":
                return ip
            # No default route (offline, or a VM with only a host-only adapter).
            # Fall back to whatever the hostname resolves to that is not loopback.
            for cand in self._hostname_v4s():
                if not cand.startswith("127."):
                    return cand
            return "127.0.0.1"
        return self._cached("v4", go)

    @staticmethod
    def _hostname_v4s():
        out = []
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                addr = info[4][0]
                if addr not in out:
                    out.append(addr)
        except (OSError, socket.gaierror, UnicodeError):
            pass
        return out

    def all_ipv4(self):
        """Every IPv4 this host answers on. Shown in the UI so a user with a VPN
        or several adapters can see why the local address is not what they
        expected."""
        def go():
            found = []
            primary = self._probe_local_v4()
            if primary:
                found.append(primary)
            for a in self._hostname_v4s():
                if a not in found:
                    found.append(a)
            return found
        return self._cached("v4all", go)

    # -- IPv6 ----------------------------------------------------------------
    def global_ipv6(self):
        """A globally-routable IPv6 for this machine, or "".

        Worth having: on a CGNAT-ed IPv4 line, IPv6 is very often the only way
        friends can actually reach the server — and it needs no port mapping at
        all, only a firewall pinhole.
        """
        def go():
            s = None
            try:
                s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
                s.settimeout(1.0)
                s.connect(("2001:4860:4860::8888", 80))
                addr = s.getsockname()[0].split("%")[0]
                return addr if is_global_v6(addr) else ""
            except OSError:
                return ""
            finally:
                if s:
                    try:
                        s.close()
                    except OSError:
                        pass
        return self._cached("v6", go)

    def has_ipv6(self):
        return bool(self.global_ipv6())

    # -- the gateway ---------------------------------------------------------
    def gateway_ipv4(self):
        """Default gateway. NAT-PMP and PCP are sent straight at it, so getting
        this wrong means both silently time out."""
        return self._cached("gw", self._find_gateway)

    def gateway_candidates(self):
        """The gateway plus the usual suspects for this subnet. Probing three
        UDP addresses costs nothing and rescues the case where the OS route
        table is unreadable (locked-down Windows, an odd container)."""
        out = []
        gw = self.gateway_ipv4()
        if gw:
            out.append(gw)
        local = self.local_ipv4()
        if local and not local.startswith("127."):
            base = local.rsplit(".", 1)[0]
            for last in ("1", "254"):
                cand = "%s.%s" % (base, last)
                if cand not in out and cand != local:
                    out.append(cand)
        return out

    def _find_gateway(self):
        for fn in (self._gw_proc, self._gw_windows, self._gw_ip_route, self._gw_netstat):
            try:
                gw = fn()
            except Exception:
                gw = ""
            if gw and is_valid_ipv4(gw) and not gw.startswith("0."):
                return gw
        return ""

    @staticmethod
    def _gw_proc():
        """Linux: /proc/net/route holds the routing table as little-endian hex."""
        path = "/proc/net/route"
        if not os.path.isfile(path):
            return ""
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh.readlines()[1:]:
                parts = line.split()
                if len(parts) < 4:
                    continue
                # destination 00000000 == the default route, and RTF_GATEWAY set
                try:
                    if parts[1] != "00000000" or not int(parts[3], 16) & 2:
                        continue
                    raw = int(parts[2], 16)
                except ValueError:
                    continue
                return socket.inet_ntoa(raw.to_bytes(4, "little"))
        return ""

    @staticmethod
    def _gw_windows():
        if not sys.platform.startswith("win"):
            return ""
        best, best_metric = "", None
        for line in _out(["route", "print", "-4"]).splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0] == "0.0.0.0" and parts[1] == "0.0.0.0":
                gw = parts[2]
                try:
                    metric = int(parts[4])
                except ValueError:
                    metric = 9999
                if is_valid_ipv4(gw) and (best_metric is None or metric < best_metric):
                    best, best_metric = gw, metric
        if best:
            return best
        # `route print` is localised on some installs, so scrape ipconfig too.
        for line in _out(["ipconfig"]).splitlines():
            if "gateway" not in line.lower():
                continue
            found = _IPV4_FIND_RE.search(line)
            if found and is_valid_ipv4(found.group(1)) and found.group(1) != "0.0.0.0":
                return found.group(1)
        return ""

    @staticmethod
    def _gw_netstat():
        if sys.platform.startswith("win"):
            return ""
        for line in _out(["netstat", "-rn"]).splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] in ("default", "0.0.0.0"):
                if is_valid_ipv4(parts[1]):
                    return parts[1]
        return ""

    @staticmethod
    def _gw_ip_route():
        if sys.platform.startswith("win"):
            return ""
        m = re.search(r"default\s+via\s+(\d{1,3}(?:\.\d{1,3}){3})",
                      _out(["ip", "route", "show", "default"]))
        return m.group(1) if m else ""

    # -- what is above us ----------------------------------------------------
    def upstream_hops(self, limit=4, timeout=12):
        """Private-address routers between our gateway and the internet.

        "There is another router in front of this one" is true but useless
        advice — the user has to be told *which* box to go and configure. A
        short trace toward a public address answers that: every RFC1918 hop
        after our own gateway is another NAT the user owns or their ISP does,
        and the first of them is the one to log into.

        Returns a list of {ip, hop, ours} in order, nearest first. Never raises;
        an empty list just means we could not find out.
        """
        def go():
            gw = self.gateway_ipv4()
            hops = self._trace(limit, timeout)
            out = []
            for index, ip in hops:
                if not is_valid_ipv4(ip) or ip == gw:
                    continue
                if not is_private_v4(ip):
                    break              # we have reached real internet; stop
                if ip.startswith("127.") or ip.startswith("169.254."):
                    continue
                out.append({"ip": ip, "hop": index,
                            # 10/8 immediately above a home router is almost
                            # always the ISP's carrier network, not the user's.
                            "ours": not ip.startswith("10.")})
            return out
        return self._cached("upstream", go)

    @staticmethod
    def _trace(limit, timeout):
        """(hop_number, address) pairs from the OS traceroute. Numeric only."""
        if sys.platform.startswith("win"):
            argv = ["tracert", "-d", "-4", "-h", str(limit), "-w", "1200", "1.1.1.1"]
        else:
            argv = ["traceroute", "-n", "-m", str(limit), "-w", "2", "1.1.1.1"]
        code, text = run_quiet(argv, timeout=timeout)
        if not text.strip():
            return []
        out = []
        for line in text.splitlines():
            m = re.match(r"\s*(\d{1,2})\s", line)
            if not m:
                continue
            addrs = _IPV4_FIND_RE.findall(line)
            # The last address on the line is the hop itself; the earlier ones
            # are round-trip times that happen to look numeric on some locales.
            real = [a for a in addrs if is_valid_ipv4(a)]
            if real:
                out.append((int(m.group(1)), real[-1]))
        return out

    def edge_router(self):
        """The outermost device on this network the user could configure, or "".

        This is the box a port forward actually has to be made on when the
        gateway turns out to be an inner router.
        """
        for hop in self.upstream_hops():
            if hop["ours"]:
                return hop["ip"]
        return ""

    # -- summary -------------------------------------------------------------
    def snapshot(self):
        local = self.local_ipv4()
        v6 = self.global_ipv6()
        return {
            "local_ipv4": local,
            "all_ipv4": self.all_ipv4(),
            "ipv6": v6,
            "has_ipv6": bool(v6),
            "gateway": self.gateway_ipv4(),
            "gateway_candidates": self.gateway_candidates(),
            "local_is_private": is_private_v4(local),
            # A CGNAT address on the *LAN* side usually means the box you think
            # is your router is really the ISP's, and you are already a layer down.
            "local_is_cgnat": is_cgnat_v4(local),
        }
