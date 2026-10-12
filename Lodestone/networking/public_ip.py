"""PublicIpService — what the internet sees when this machine connects out.

Deliberately narrow. Each source is asked for one thing, an address, over
HTTPS, and the reply is only accepted if it parses as an IP. Nothing else about
the machine is sent: no hostname, no server name, no query string, and a
generic user agent. Several sources are tried because any single one goes down
eventually, and because two independent sources agreeing is the cheapest
confirmation available that the answer is real.

The address is cached with a TTL and the previous value is kept, so a dynamic
IP that changes under a running server is visible rather than silently wrong.

Why this cannot be skipped: the local address and the public address are
different numbers, and a NAT gateway will happily tell you an external address
that is itself private — that is the CGNAT case, and comparing the two is how
it gets caught.
"""

import threading
import time
import urllib.error
import urllib.request

from .errors import PublicIpUnavailable
from .interfaces import PublicIpSource
from .net_interfaces import is_valid_ipv4, is_valid_ipv6

USER_AGENT = "Lodestone/1.0 (+minecraft server panel)"

# Plain-text endpoints that return only the address. Order is preference.
IPV4_SOURCES = (
    ("ipify", "https://api.ipify.org"),
    ("icanhazip", "https://ipv4.icanhazip.com"),
    ("aws", "https://checkip.amazonaws.com"),
    ("ident.me", "https://4.ident.me"),
)

IPV6_SOURCES = (
    ("ipify", "https://api6.ipify.org"),
    ("icanhazip", "https://ipv6.icanhazip.com"),
    ("ident.me", "https://6.ident.me"),
)


class HttpIpSource(PublicIpSource):
    """One plain-text endpoint that echoes the caller's address."""

    def __init__(self, name, url):
        self.name = name
        self.url = url

    def fetch(self, family=4, timeout=5):
        req = urllib.request.Request(self.url, headers={
            "User-Agent": USER_AGENT, "Accept": "text/plain"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                if r.status != 200:
                    raise PublicIpUnavailable("%s returned HTTP %s" % (self.name, r.status))
                text = r.read(256).decode("utf-8", "replace").strip()
        except urllib.error.HTTPError as e:
            raise PublicIpUnavailable("%s returned HTTP %s" % (self.name, e.code))
        except urllib.error.URLError as e:
            raise PublicIpUnavailable("%s unreachable (%s)" % (self.name, e.reason))
        except (OSError, ValueError) as e:
            raise PublicIpUnavailable("%s unreachable (%s)" % (self.name, e))
        text = text.split()[0] if text.split() else ""
        ok = is_valid_ipv4(text) if family == 4 else is_valid_ipv6(text)
        if not ok:
            raise PublicIpUnavailable("%s did not return an IPv%d address" % (self.name, family))
        return text


class PublicIpService:
    """Cached public address lookup, IPv4 and IPv6, with change detection."""

    def __init__(self, ttl=300, timeout=5, sources_v4=None, sources_v6=None,
                 enabled=True):
        self.ttl = ttl
        self.timeout = timeout
        self.enabled = enabled
        self.sources_v4 = [HttpIpSource(n, u) for n, u in (sources_v4 or IPV4_SOURCES)]
        self.sources_v6 = [HttpIpSource(n, u) for n, u in (sources_v6 or IPV6_SOURCES)]
        self._lock = threading.Lock()
        self._state = {
            4: {"ip": "", "previous": "", "at": 0.0, "source": "", "error": "",
                "changed_at": 0.0, "agreed": False},
            6: {"ip": "", "previous": "", "at": 0.0, "source": "", "error": "",
                "changed_at": 0.0, "agreed": False},
        }
        self.on_change = None       # set to fn(family, old, new) if you care

    # -- lookup --------------------------------------------------------------
    def get(self, family=4, force=False):
        """Return the address, or raise PublicIpUnavailable.

        Cheap on the happy path: inside the TTL it never touches the network,
        which matters because the status endpoint polls twice a second.
        """
        if not self.enabled:
            raise PublicIpUnavailable(
                "Public address lookup is switched off.",
                "Turn it back on in Network settings if you want the public "
                "address shown.")
        with self._lock:
            st = self._state[family]
            if not force and st["ip"] and time.time() - st["at"] < self.ttl:
                return st["ip"]
        ip, source, agreed, err = self._probe(family)
        with self._lock:
            st = self._state[family]
            st["at"] = time.time()
            if ip:
                if st["ip"] and st["ip"] != ip:
                    st["previous"] = st["ip"]
                    st["changed_at"] = time.time()
                    cb = self.on_change
                    if cb:
                        try:
                            cb(family, st["previous"], ip)
                        except Exception:
                            pass
                st["ip"], st["source"], st["agreed"], st["error"] = ip, source, agreed, ""
                return ip
            st["error"] = err
            # Keep a stale value rather than blanking the dashboard on one
            # failed lookup, but say that it is stale.
            if st["ip"]:
                return st["ip"]
        raise PublicIpUnavailable(err or "Could not determine the public address.")

    def _probe(self, family):
        sources = self.sources_v4 if family == 4 else self.sources_v6
        results, errors = [], []
        for src in sources:
            try:
                results.append((src.name, src.fetch(family, self.timeout)))
            except PublicIpUnavailable as e:
                errors.append(str(e))
                continue
            # Two agreeing answers is enough; stop paying for more round trips.
            if len(results) >= 2:
                break
        if not results:
            return "", "", False, "; ".join(errors[:3]) or "no source answered"
        if len(results) >= 2 and results[0][1] == results[1][1]:
            return results[0][1], "%s + %s" % (results[0][0], results[1][0]), True, ""
        return results[0][1], results[0][0], False, ""

    # -- convenience ---------------------------------------------------------
    def ipv4(self, force=False):
        try:
            return self.get(4, force)
        except PublicIpUnavailable:
            return ""

    def ipv6(self, force=False):
        try:
            return self.get(6, force)
        except PublicIpUnavailable:
            return ""

    def changed(self, family=4):
        st = self._state[family]
        return bool(st["previous"] and st["previous"] != st["ip"])

    def invalidate(self):
        with self._lock:
            for st in self._state.values():
                st["at"] = 0.0

    def snapshot(self):
        with self._lock:
            v4, v6 = self._state[4], self._state[6]
            return {
                "enabled": self.enabled,
                "ipv4": v4["ip"], "ipv4_source": v4["source"],
                "ipv4_confirmed": v4["agreed"], "ipv4_error": v4["error"],
                "ipv4_previous": v4["previous"], "ipv4_changed_at": v4["changed_at"],
                "ipv6": v6["ip"], "ipv6_source": v6["source"],
                "ipv6_confirmed": v6["agreed"], "ipv6_error": v6["error"],
                "checked_at": max(v4["at"], v6["at"]),
                "stale": bool(v4["ip"] and v4["error"]),
            }
