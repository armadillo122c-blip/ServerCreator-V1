"""UpnpProvider — port mapping over UPnP IGD, the one nearly every home router
speaks.

Three steps, and each one is a separate way to fail, so each one reports
separately:

  1. SSDP  — an M-SEARCH datagram to the 239.255.255.250:1900 multicast group.
             Anything claiming to be an InternetGatewayDevice answers with a
             LOCATION header.
  2. SCPD  — fetch that LOCATION, which is an XML description of the device
             tree, and walk it for a WANIPConnection or WANPPPConnection
             service. That service has the controlURL we actually post to.
  3. SOAP  — AddPortMapping / DeletePortMapping / GetExternalIPAddress against
             the control URL.

Standard library only: sockets, urllib, ElementTree.

Two details that cause most of the field failures, both handled here:

  * The search socket must be bound to the LAN address, or on a machine with a
    VPN or several adapters the multicast leaves by the wrong interface and
    nothing ever answers.
  * controlURL is very often relative, and a handful of devices publish a
    URLBase that disagrees with the host they served the description from. The
    LOCATION URL wins, because that host demonstrably works.
"""

import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from .errors import (DiscoveryFailed, MappingConflict, MappingRejected,
                     NetworkingError, NotAuthorized, ProviderUnavailable,
                     UnsupportedOperation)
from .interfaces import TCP, PortMapping, PortMappingProvider

SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900

# Asked for in order. WANIPConnection:2 is IGDv2 and supports AddAnyPortMapping;
# the rest are the v1 shapes. rootdevice is the catch-all for routers that do not
# answer a targeted search but do answer a generic one.
SEARCH_TARGETS = (
    "urn:schemas-upnp-org:device:InternetGatewayDevice:2",
    "urn:schemas-upnp-org:device:InternetGatewayDevice:1",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
    "upnp:rootdevice",
)

WAN_SERVICES = (
    "urn:schemas-upnp-org:service:WANIPConnection:2",
    "urn:schemas-upnp-org:service:WANIPConnection:1",
    "urn:schemas-upnp-org:service:WANPPPConnection:1",
)

# The error codes worth naming. Anything else is reported with its number.
UPNP_ERRORS = {
    401: "The router does not implement that action.",
    402: "The router rejected the arguments as invalid.",
    403: "The router refused the request.",
    501: "The router tried and failed to create the mapping.",
    606: "UPnP is present but not authorised for this client.",
    714: "There is no such mapping to remove.",
    715: "The router will not accept a wildcard source address.",
    716: "The router will not accept a wildcard external port.",
    718: "That external port is already mapped to a different machine.",
    724: "The router insists the external and internal ports match.",
    725: "The router only supports permanent leases.",
    726: "The router only accepts a wildcard remote host.",
    727: "The router only accepts a wildcard external port.",
    728: "The router has no free mapping slots left.",
    729: "The port conflicts with a mapping made by another mechanism.",
    732: "The router will not accept a wildcard internal port.",
}

_LOCATION_RE = re.compile(r"^location:\s*(\S+)\s*$", re.I | re.M)
_ST_RE = re.compile(r"^st:\s*(\S+)\s*$", re.I | re.M)
_SERVER_RE = re.compile(r"^server:\s*(.+?)\s*$", re.I | re.M)

MAX_DESC_BYTES = 512 * 1024
MAX_SOAP_BYTES = 256 * 1024


def _localname(tag):
    """ElementTree keeps namespaces as {uri}tag. Devices are inconsistent about
    which namespace they declare, so match on the local name only."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find_text(elem, name):
    for child in elem:
        if _localname(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _iter_named(root, name):
    for elem in root.iter():
        if _localname(elem.tag) == name:
            yield elem


class SsdpDevice:
    """One answer to an M-SEARCH."""

    def __init__(self, location, st="", server="", address=""):
        self.location = location
        self.st = st
        self.server = server
        self.address = address

    def as_dict(self):
        return {"location": self.location, "st": self.st,
                "server": self.server, "address": self.address}


def parse_ssdp_response(raw, address=""):
    """Turn a raw SSDP datagram into an SsdpDevice, or None if it is not one."""
    try:
        text = raw.decode("utf-8", "replace")
    except Exception:
        return None
    if not text.upper().startswith(("HTTP/1.1 200", "HTTP/1.0 200", "NOTIFY")):
        return None
    m = _LOCATION_RE.search(text)
    if not m:
        return None
    loc = m.group(1)
    if not loc.lower().startswith(("http://", "https://")):
        return None
    st = _ST_RE.search(text)
    srv = _SERVER_RE.search(text)
    return SsdpDevice(loc, st.group(1) if st else "",
                      srv.group(1) if srv else "", address)


def discover(local_ip="", timeout=2.5, targets=SEARCH_TARGETS, want=6):
    """Broadcast an M-SEARCH and collect the LOCATIONs that answer.

    Returns a list of SsdpDevice, best-guess-first (IGD answers before generic
    rootdevice ones). Never raises — an empty list means nothing answered.
    """
    found, seen = [], set()
    sock = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)
        except OSError:
            pass
        if local_ip and not local_ip.startswith("127."):
            # Pin the search to the LAN adapter. Without this a VPN or a second
            # NIC quietly swallows the multicast and discovery "just fails".
            try:
                sock.bind((local_ip, 0))
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF,
                                socket.inet_aton(local_ip))
            except OSError:
                pass
        sock.settimeout(0.6)

        for target in targets:
            msg = ("M-SEARCH * HTTP/1.1\r\n"
                   "HOST: %s:%d\r\n"
                   "MAN: \"ssdp:discover\"\r\n"
                   "MX: 2\r\n"
                   "ST: %s\r\n"
                   "USER-AGENT: Lodestone UPnP/1.0\r\n"
                   "\r\n" % (SSDP_ADDR, SSDP_PORT, target)).encode("ascii")
            for _ in range(2):          # UDP; a lost search is a silent failure
                try:
                    sock.sendto(msg, (SSDP_ADDR, SSDP_PORT))
                except OSError:
                    break

        deadline = time.time() + timeout
        while time.time() < deadline and len(found) < want:
            try:
                raw, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            dev = parse_ssdp_response(raw, addr[0] if addr else "")
            if dev and dev.location not in seen:
                seen.add(dev.location)
                found.append(dev)
    except OSError:
        pass
    finally:
        if sock:
            try:
                sock.close()
            except OSError:
                pass

    found.sort(key=lambda d: 0 if "InternetGatewayDevice" in (d.st or "") else 1)
    return found


def _http(url, data=None, headers=None, timeout=6, limit=MAX_SOAP_BYTES):
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read(limit)


class UpnpProvider(PortMappingProvider):
    """One WAN connection service on one gateway.

    Construct it with nothing and it discovers; construct it with a `location`
    and it goes straight there, which is what the tests use.
    """

    name = "UPnP"
    protocol_name = "UPnP IGD"

    def __init__(self, local_ip="", location="", timeout=6, discovery_timeout=2.5):
        self.local_ip = local_ip
        self.location = location
        self.timeout = timeout
        self.discovery_timeout = discovery_timeout
        self.control_url = ""
        self.service_type = ""
        self.device_name = ""
        self.device_model = ""
        self.igd_version = 0
        self._devices = []
        self._last_error = ""

    # -- discovery -----------------------------------------------------------
    def _ensure(self):
        """Resolve a control URL, or raise. Cached once found."""
        if self.control_url:
            return
        locations = [self.location] if self.location else []
        if not locations:
            self._devices = discover(self.local_ip, self.discovery_timeout)
            locations = [d.location for d in self._devices]
        if not locations:
            raise DiscoveryFailed(
                "No UPnP gateway answered on this network.",
                "Most routers call this UPnP or NAT-PMP in their settings and "
                "some ship with it off. Turn it on, or forward the port by hand.")
        last = ""
        for loc in locations:
            try:
                self._load_description(loc)
                if self.control_url:
                    return
            except NetworkingError as e:
                last = str(e)
            except Exception as e:
                last = str(e)
        raise ProviderUnavailable(
            "Found %d UPnP device(s), but none offered a WAN connection service."
            % len(locations),
            last or "The device is probably a printer or a media server, not the router.")

    def _load_description(self, location):
        try:
            status, body = _http(location, timeout=self.timeout, limit=MAX_DESC_BYTES)
        except urllib.error.URLError as e:
            raise ProviderUnavailable("Could not fetch %s (%s)" % (location, e))
        except (OSError, ValueError) as e:
            raise ProviderUnavailable("Could not fetch %s (%s)" % (location, e))
        if status != 200:
            raise ProviderUnavailable("Device description returned HTTP %s" % status)
        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            raise ProviderUnavailable("Device description is not valid XML (%s)" % e)

        url_base = ""
        for elem in _iter_named(root, "URLBase"):
            url_base = (elem.text or "").strip()
            break
        for elem in _iter_named(root, "device"):
            name = _find_text(elem, "friendlyName")
            model = _find_text(elem, "modelName")
            if name and not self.device_name:
                self.device_name = name
                self.device_model = model
            break

        best = None
        for svc in _iter_named(root, "service"):
            stype = _find_text(svc, "serviceType")
            ctrl = _find_text(svc, "controlURL")
            if not stype or not ctrl:
                continue
            if stype in WAN_SERVICES:
                rank = WAN_SERVICES.index(stype)
                if best is None or rank < best[0]:
                    best = (rank, stype, ctrl)
        if best is None:
            return
        _, stype, ctrl = best
        self.service_type = stype
        self.igd_version = 2 if stype.endswith(":2") else 1
        self.control_url = self._resolve(location, url_base, ctrl)
        self.location = location

    @staticmethod
    def _resolve(location, url_base, control_url):
        """Absolute controlURL wins; then URLBase; then the LOCATION host.

        LOCATION is preferred over a *host-differing* URLBase because we already
        know the LOCATION host answers — some firmware publishes a URLBase with
        the WAN address in it, which is unreachable from the LAN.
        """
        if control_url.lower().startswith(("http://", "https://")):
            return control_url
        loc = urllib.parse.urlsplit(location)
        if url_base:
            base = urllib.parse.urlsplit(url_base)
            if base.netloc and base.netloc.split(":")[0] == loc.netloc.split(":")[0]:
                return urllib.parse.urljoin(url_base.rstrip("/") + "/",
                                            control_url.lstrip("/"))
        return urllib.parse.urlunsplit(
            (loc.scheme, loc.netloc, control_url if control_url.startswith("/")
             else "/" + control_url, "", ""))

    def available(self):
        try:
            self._ensure()
        except NetworkingError as e:
            self._last_error = str(e)
            return False, str(e)
        label = self.device_name or "gateway"
        if self.device_model and self.device_model != self.device_name:
            label += " (%s)" % self.device_model
        return True, "%s, IGD v%d" % (label, self.igd_version)

    def info(self):
        return {"provider": self.name, "location": self.location,
                "control_url": self.control_url, "service": self.service_type,
                "device": self.device_name, "model": self.device_model,
                "igd_version": self.igd_version,
                "devices": [d.as_dict() for d in self._devices]}

    # -- SOAP ----------------------------------------------------------------
    @staticmethod
    def build_envelope(service_type, action, args):
        body = "".join("<%s>%s</%s>" % (k, _xml_escape(v), k) for k, v in args)
        return ('<?xml version="1.0"?>'
                '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
                '<s:Body><u:%s xmlns:u="%s">%s</u:%s></s:Body></s:Envelope>'
                % (action, service_type, body, action)).encode("utf-8")

    @staticmethod
    def parse_fault(body):
        """Pull (errorCode, errorDescription) out of a SOAP fault, or None."""
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return None
        for elem in _iter_named(root, "UPnPError"):
            code_txt = _find_text(elem, "errorCode")
            desc = _find_text(elem, "errorDescription")
            try:
                return int(code_txt), desc
            except ValueError:
                return 0, desc or code_txt
        for elem in _iter_named(root, "Fault"):
            return 0, _find_text(elem, "faultstring") or "SOAP fault"
        return None

    @staticmethod
    def _raise_for_code(code, desc, action):
        text = UPNP_ERRORS.get(code) or (desc or "The router refused the request.")
        full = "%s failed: %s (UPnP error %s)" % (action, text, code or "?")
        if code == 718 or code == 729:
            raise MappingConflict(full, "Pick a different external port, or remove "
                                        "the existing mapping on the router.")
        if code in (606, 403):
            raise NotAuthorized(full, "Your router has UPnP restricted. Enable it, "
                                      "or forward the port manually.")
        if code in (401, 725, 726, 727, 724, 732, 715, 716):
            raise UnsupportedOperation(full)
        if code == 714:
            raise MappingRejected(full)
        raise MappingRejected(full)

    def soap(self, action, args=()):
        """POST one SOAP action. Returns a dict of the response fields."""
        self._ensure()
        payload = self.build_envelope(self.service_type, action, list(args))
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": '"%s#%s"' % (self.service_type, action),
            "Connection": "close",
            "User-Agent": "Lodestone/1.0 UPnP/1.0",
            "Content-Length": str(len(payload)),
        }
        try:
            status, body = _http(self.control_url, payload, headers, self.timeout)
        except urllib.error.HTTPError as e:
            body = b""
            try:
                body = e.read(MAX_SOAP_BYTES)
            except Exception:
                pass
            fault = self.parse_fault(body)
            if fault:
                self._raise_for_code(fault[0], fault[1], action)
            raise MappingRejected("%s failed: the router returned HTTP %s."
                                  % (action, e.code))
        except urllib.error.URLError as e:
            raise ProviderUnavailable("Could not reach the router (%s)." % e.reason)
        except (OSError, ValueError) as e:
            raise ProviderUnavailable("Could not reach the router (%s)." % e)

        fault = self.parse_fault(body)
        if fault:
            self._raise_for_code(fault[0], fault[1], action)
        if status != 200:
            raise MappingRejected("%s failed: the router returned HTTP %s."
                                  % (action, status))
        return self._parse_response(body)

    @staticmethod
    def _parse_response(body):
        out = {}
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return out
        for elem in root.iter():
            name = _localname(elem.tag)
            if name.startswith("New"):
                out[name] = (elem.text or "").strip()
        return out

    # -- the actual operations -----------------------------------------------
    def external_ip(self):
        res = self.soap("GetExternalIPAddress")
        ip = res.get("NewExternalIPAddress", "").strip()
        if not ip or ip == "0.0.0.0":
            raise NetworkingError(
                "The router reports no external address yet.",
                "That usually means the WAN link is still coming up.")
        return ip

    def add(self, internal_ip, internal_port, external_port,
            protocol=TCP, lifetime=0, description=""):
        protocol = (protocol or TCP).upper()
        args = [("NewRemoteHost", ""),
                ("NewExternalPort", str(int(external_port))),
                ("NewProtocol", protocol),
                ("NewInternalPort", str(int(internal_port))),
                ("NewInternalClient", internal_ip),
                ("NewEnabled", "1"),
                ("NewPortMappingDescription", description or "Lodestone"),
                ("NewLeaseDuration", str(int(lifetime or 0)))]
        granted = int(external_port)
        try:
            self.soap("AddPortMapping", args)
        except UnsupportedOperation as e:
            # 725: the router only does permanent leases. Retry with 0 rather
            # than making the user care about the difference.
            if "725" in str(e) and int(lifetime or 0) != 0:
                args[-1] = ("NewLeaseDuration", "0")
                self.soap("AddPortMapping", args)
                lifetime = 0
            else:
                raise
        except MappingConflict:
            # Already mapped — but possibly already mapped to *us*, which is a
            # success, not a failure. Only re-raise if it belongs elsewhere.
            existing = self.get(external_port, protocol)
            if (existing and existing.get("internal_client") == internal_ip
                    and int(existing.get("internal_port") or 0) == int(internal_port)):
                lifetime = int(existing.get("lease") or 0)
            elif self.igd_version >= 2:
                granted = self._add_any(args)
            else:
                raise
        return PortMapping(self.name, protocol, granted, internal_port,
                           internal_ip, description, lifetime)

    def _add_any(self, args):
        """IGDv2 lets the router pick a free external port for us."""
        res = self.soap("AddAnyPortMapping", args)
        try:
            return int(res.get("NewReservedPort") or 0)
        except ValueError:
            raise MappingRejected("The router accepted AddAnyPortMapping but "
                                  "did not say which port it reserved.")

    def get(self, external_port, protocol=TCP):
        try:
            res = self.soap("GetSpecificPortMappingEntry",
                            [("NewRemoteHost", ""),
                             ("NewExternalPort", str(int(external_port))),
                             ("NewProtocol", (protocol or TCP).upper())])
        except NetworkingError:
            return None
        if not res:
            return None
        return {"internal_port": res.get("NewInternalPort", ""),
                "internal_client": res.get("NewInternalClient", ""),
                "enabled": res.get("NewEnabled", ""),
                "description": res.get("NewPortMappingDescription", ""),
                "lease": res.get("NewLeaseDuration", "")}

    def remove(self, mapping):
        try:
            self.soap("DeletePortMapping",
                      [("NewRemoteHost", ""),
                       ("NewExternalPort", str(int(mapping.external_port))),
                       ("NewProtocol", mapping.protocol)])
            return True, "Removed %s." % mapping.describe()
        except MappingRejected as e:
            # 714 NoSuchEntryInArray: it is already gone, which is what we wanted.
            if "714" in str(e):
                return True, "Mapping was already gone."
            return False, str(e)
        except NetworkingError as e:
            return False, str(e)

    def list_all(self, limit=64):
        """Walk the router's whole mapping table. Read-only; used so the UI can
        show the user exactly what is there before and after we touch it."""
        out = []
        for i in range(limit):
            try:
                res = self.soap("GetGenericPortMappingEntry",
                                [("NewPortMappingIndex", str(i))])
            except NetworkingError:
                break
            if not res:
                break
            out.append({
                "index": i,
                "external_port": res.get("NewExternalPort", ""),
                "protocol": res.get("NewProtocol", ""),
                "internal_port": res.get("NewInternalPort", ""),
                "internal_client": res.get("NewInternalClient", ""),
                "description": res.get("NewPortMappingDescription", ""),
                "enabled": res.get("NewEnabled", ""),
                "lease": res.get("NewLeaseDuration", ""),
            })
        return out


def _xml_escape(value):
    return (str(value).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&apos;"))
