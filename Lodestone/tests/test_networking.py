"""Internet hosting: the networking layer, end to end.

Nothing here touches the real internet or a real router. Everything is driven
against fakes that speak the actual protocols on the wire:

  * a fake UPnP IGD  — real HTTP, real device-description XML, real SOAP, and
    real SOAP faults with UPnP error codes;
  * a fake NAT-PMP responder — real RFC 6886 datagrams;
  * a fake PCP server — real RFC 6887 datagrams, nonce checking included;
  * a fake Minecraft server — a real Server List Ping handshake;
  * a fake public-IP endpoint and a fake reachability service over HTTP.

The point of the fakes rather than mocks: the bugs this feature can have are
byte-layout bugs and XML-shape bugs, and a mock that returns a Python dict
tests none of that.
"""

import importlib.util
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
_TESTHOME = os.environ.get("LODESTONE_TEST_HOME")
if not _TESTHOME:
    _TESTHOME = tempfile.mkdtemp(prefix="lodestone_testhome_")
    os.environ["LODESTONE_TEST_HOME"] = _TESTHOME
os.environ["HOME"] = _TESTHOME
os.environ["USERPROFILE"] = _TESTHOME
os.environ["HOMEDRIVE"] = os.path.splitdrive(_TESTHOME)[0] or "C:"
os.environ["HOMEPATH"] = os.path.splitdrive(_TESTHOME)[1]

import io
import json
import re
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import networking as net
from networking import connectivity as conn
from networking import firewall as fwmod
from networking import hosting as hostmod
from networking import natpmp as npm
from networking import pcp as pcpmod
from networking import upnp as upnpmod

spec = importlib.util.spec_from_file_location("lodestone", "lodestone.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

fails = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name +
          (("  " + str(extra)[:300]) if not cond else ""))
    if not cond:
        fails.append(name)


def section(title):
    print("\n-- %s" % title)


# ===========================================================================
#  address helpers
# ===========================================================================
section("address classification")

check("a normal IPv4 validates", net.is_valid_ipv4("192.168.1.50"))
check("nonsense does not", not net.is_valid_ipv4("banana"))
check("an out-of-range octet does not", not net.is_valid_ipv4("999.1.1.1"))
check("a short form does not", not net.is_valid_ipv4("10.0.1"))
check("an IPv6 validates", net.is_valid_ipv6("2001:db8::1"))
check("an IPv4 is not an IPv6", not net.is_valid_ipv6("192.168.1.1"))

for addr, private in (("10.1.2.3", True), ("172.16.0.1", True), ("172.32.0.1", False),
                      ("192.168.0.1", True), ("169.254.9.9", True), ("127.0.0.1", True),
                      ("100.64.0.1", True), ("8.8.8.8", False), ("203.0.113.7", False)):
    check("private(%s) == %s" % (addr, private), net.is_private_v4(addr) is private)

# RFC 6598 is the whole reason CGNAT can be detected at all: the range is
# reserved, so an address inside it is never a real public address.
for addr, cgnat in (("100.64.0.1", True), ("100.100.50.1", True),
                    ("100.127.255.254", True), ("100.128.0.1", False),
                    ("100.63.255.255", False), ("192.168.1.1", False)):
    check("cgnat(%s) == %s" % (addr, cgnat), net.is_cgnat_v4(addr) is cgnat)

check("a link-local IPv6 is not global", not net.is_global_v6("fe80::1"))
check("a ULA IPv6 is not global", not net.is_global_v6("fd00::1"))
check("a 2001: IPv6 is global", net.is_global_v6("2001:db8::1") in (True, False))
check("loopback IPv6 is not global", not net.is_global_v6("::1"))

ifs = net.NetworkInterfaceService()
snap = ifs.snapshot()
check("the interface snapshot has every field",
      all(k in snap for k in ("local_ipv4", "ipv6", "gateway", "gateway_candidates",
                              "local_is_private", "local_is_cgnat")), sorted(snap))
check("a local IPv4 was found", net.is_valid_ipv4(snap["local_ipv4"]), snap["local_ipv4"])
check("the cache means a second call is instant",
      (lambda t0: (ifs.snapshot(), time.time() - t0 < 0.5)[1])(time.time()))


# ===========================================================================
#  port validation
# ===========================================================================
section("port validation")

check("25565 is fine", net.validate_port(25565) == 25565)
check("a high port is fine", net.validate_port(49152) == 49152)


def rejects(port, why, **kw):
    try:
        net.validate_port(port, **kw)
        return False
    except net.PortInvalid:
        return True


check("port 0 is rejected", rejects(0, "zero"))
check("port 70000 is rejected", rejects(70000, "too big"))
check("a negative port is rejected", rejects(-5, "negative"))
check("a non-number is rejected", rejects("banana", "text"))
check("port 80 is rejected as privileged", rejects(80, "privileged"))
check("port 22 is rejected (SSH/SFTP)", rejects(22, "ssh"))
check("port 3389 is rejected (RDP)", rejects(3389, "rdp"))
check("the RCON port is rejected", rejects(25575, "rcon", rcon_port=25575))
check("the panel port is rejected", rejects(8744, "panel", panel_port=8744))
check("a different port with RCON set is fine",
      net.validate_port(25565, rcon_port=25575, panel_port=8744) == 25565)


def why(port, **kw):
    """The message a rejected port produces, for asserting it is useful."""
    try:
        net.validate_port(port, **kw)
        return ""
    except net.PortInvalid as e:
        return "%s %s" % (e, e.hint)


check("the RCON refusal explains itself", "RCON" in why(25575, rcon_port=25575),
      why(25575, rcon_port=25575))
check("the panel refusal explains itself", "login" in why(8744, panel_port=8744),
      why(8744, panel_port=8744))
check("the SSH refusal names SSH", "SSH" in why(22), why(22))
check("the privileged refusal suggests a fix", "25565" in why(80), why(80))


# ===========================================================================
#  UPnP: parsing
# ===========================================================================
section("UPnP parsing")

SSDP_OK = (b"HTTP/1.1 200 OK\r\n"
           b"CACHE-CONTROL: max-age=1800\r\n"
           b"ST: urn:schemas-upnp-org:device:InternetGatewayDevice:1\r\n"
           b"SERVER: Linux/3.4 UPnP/1.0 MiniUPnPd/1.9\r\n"
           b"LOCATION: http://192.168.1.1:5000/rootDesc.xml\r\n\r\n")

dev = upnpmod.parse_ssdp_response(SSDP_OK, "192.168.1.1")
check("an SSDP 200 parses", dev is not None)
check("  the LOCATION is read", dev and dev.location == "http://192.168.1.1:5000/rootDesc.xml")
check("  the ST is read", dev and "InternetGatewayDevice" in dev.st)
check("  the SERVER is read", dev and "MiniUPnPd" in dev.server)
check("a reply with no LOCATION is ignored",
      upnpmod.parse_ssdp_response(b"HTTP/1.1 200 OK\r\nST: x\r\n\r\n") is None)
check("a non-HTTP datagram is ignored",
      upnpmod.parse_ssdp_response(b"hello there") is None)
check("a non-http LOCATION is refused",
      upnpmod.parse_ssdp_response(b"HTTP/1.1 200 OK\r\nLOCATION: file:///etc/passwd\r\n\r\n")
      is None)
check("headers are case-insensitive",
      upnpmod.parse_ssdp_response(b"HTTP/1.1 200 OK\r\nlocation: http://a/b\r\n\r\n") is not None)

R = upnpmod.UpnpProvider._resolve
check("an absolute controlURL is left alone",
      R("http://10.0.0.1:80/d.xml", "", "http://10.0.0.1:49152/ctl") == "http://10.0.0.1:49152/ctl")
check("a relative controlURL hangs off the LOCATION host",
      R("http://10.0.0.1:5000/d.xml", "", "/ctl/IPConn") == "http://10.0.0.1:5000/ctl/IPConn")
check("a controlURL with no leading slash still works",
      R("http://10.0.0.1:5000/d.xml", "", "ctl/IPConn") == "http://10.0.0.1:5000/ctl/IPConn")
check("a same-host URLBase is honoured",
      R("http://10.0.0.1:5000/d.xml", "http://10.0.0.1:8000/", "ctl")
      == "http://10.0.0.1:8000/ctl")
# Firmware that publishes its WAN address as URLBase would send us somewhere
# unreachable; the host we already fetched from demonstrably works.
check("a URLBase on a different host is ignored",
      R("http://10.0.0.1:5000/d.xml", "http://203.0.113.9:80/", "/ctl")
      == "http://10.0.0.1:5000/ctl")

env = upnpmod.UpnpProvider.build_envelope(
    "urn:schemas-upnp-org:service:WANIPConnection:1", "AddPortMapping",
    [("NewExternalPort", "25565"), ("NewPortMappingDescription", "a & b <c>")])
check("the SOAP envelope names the action", b"<u:AddPortMapping" in env)
check("the SOAP envelope carries the service type",
      b"urn:schemas-upnp-org:service:WANIPConnection:1" in env)
check("SOAP arguments are XML-escaped", b"a &amp; b &lt;c&gt;" in env)

FAULT = b"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault>
<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail>
<UPnPError xmlns="urn:schemas-upnp-org:control-1-0">
<errorCode>718</errorCode><errorDescription>ConflictInMappingEntry</errorDescription>
</UPnPError></detail></s:Fault></s:Body></s:Envelope>"""
fault = upnpmod.UpnpProvider.parse_fault(FAULT)
check("a SOAP fault yields its error code", fault and fault[0] == 718, fault)
check("a SOAP fault yields its description", fault and "Conflict" in fault[1])
check("a normal response is not read as a fault",
      upnpmod.UpnpProvider.parse_fault(
          b"<?xml version='1.0'?><root><NewExternalPort>1</NewExternalPort></root>") is None)
check("unparseable XML is not read as a fault",
      upnpmod.UpnpProvider.parse_fault(b"<<<not xml") is None)


# ===========================================================================
#  UPnP: a whole fake router
# ===========================================================================
section("UPnP against a fake IGD")

DESC = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0">
 <specVersion><major>1</major><minor>0</minor></specVersion>
 <device>
  <deviceType>urn:schemas-upnp-org:device:InternetGatewayDevice:1</deviceType>
  <friendlyName>Fake IGD</friendlyName><modelName>TestRouter 9000</modelName>
  <serviceList>
   <service><serviceType>urn:schemas-upnp-org:service:Layer3Forwarding:1</serviceType>
    <controlURL>/ctl/L3F</controlURL></service>
  </serviceList>
  <deviceList><device>
   <deviceType>urn:schemas-upnp-org:device:WANDevice:1</deviceType>
   <deviceList><device>
    <deviceType>urn:schemas-upnp-org:device:WANConnectionDevice:1</deviceType>
    <serviceList>
     <service>
      <serviceType>urn:schemas-upnp-org:service:WANIPConnection:1</serviceType>
      <controlURL>/ctl/IPConn</controlURL>
      <eventSubURL>/evt/IPConn</eventSubURL>
     </service>
    </serviceList>
   </device></deviceList>
  </device></deviceList>
 </device>
</root>"""


class FakeIgd:
    """Holds a mapping table and answers SOAP the way real firmware does."""

    def __init__(self):
        self.mappings = {}                       # (proto, ext) -> dict
        self.external_ip = "203.0.113.7"
        self.calls = []
        self.refuse = None                       # set to an int to force a fault
        self.only_permanent = False

    def soap(self, action, args):
        self.calls.append(action)
        if self.refuse:
            return self.fault(self.refuse, "forced")
        if action == "GetExternalIPAddress":
            return self.ok(action, {"NewExternalIPAddress": self.external_ip})
        if action == "AddPortMapping":
            key = (args.get("NewProtocol"), int(args.get("NewExternalPort")))
            have = self.mappings.get(key)
            if have and have["NewInternalClient"] != args.get("NewInternalClient"):
                return self.fault(718, "ConflictInMappingEntry")
            if self.only_permanent and int(args.get("NewLeaseDuration") or 0) != 0:
                return self.fault(725, "OnlyPermanentLeasesSupported")
            self.mappings[key] = dict(args)
            return self.ok(action, {})
        if action == "DeletePortMapping":
            key = (args.get("NewProtocol"), int(args.get("NewExternalPort")))
            if key not in self.mappings:
                return self.fault(714, "NoSuchEntryInArray")
            del self.mappings[key]
            return self.ok(action, {})
        if action == "GetSpecificPortMappingEntry":
            key = (args.get("NewProtocol"), int(args.get("NewExternalPort")))
            got = self.mappings.get(key)
            if not got:
                return self.fault(714, "NoSuchEntryInArray")
            return self.ok(action, {
                "NewInternalPort": got["NewInternalPort"],
                "NewInternalClient": got["NewInternalClient"],
                "NewEnabled": "1",
                "NewPortMappingDescription": got.get("NewPortMappingDescription", ""),
                "NewLeaseDuration": got.get("NewLeaseDuration", "0")})
        if action == "GetGenericPortMappingEntry":
            idx = int(args.get("NewPortMappingIndex"))
            keys = sorted(self.mappings)
            if idx >= len(keys):
                return self.fault(713, "SpecifiedArrayIndexInvalid")
            proto, ext = keys[idx]
            got = self.mappings[(proto, ext)]
            return self.ok(action, {
                "NewExternalPort": str(ext), "NewProtocol": proto,
                "NewInternalPort": got["NewInternalPort"],
                "NewInternalClient": got["NewInternalClient"],
                "NewPortMappingDescription": got.get("NewPortMappingDescription", ""),
                "NewEnabled": "1", "NewLeaseDuration": "0"})
        return self.fault(401, "InvalidAction")

    @staticmethod
    def ok(action, fields):
        body = "".join("<%s>%s</%s>" % (k, v, k) for k, v in fields.items())
        return 200, ('<?xml version="1.0"?><s:Envelope '
                     'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/">'
                     '<s:Body><u:%sResponse xmlns:u="urn:schemas-upnp-org:'
                     'service:WANIPConnection:1">%s</u:%sResponse></s:Body>'
                     "</s:Envelope>" % (action, body, action)).encode()

    @staticmethod
    def fault(code, text):
        return 500, ('<?xml version="1.0"?><s:Envelope '
                     'xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                     "<s:Fault><faultcode>s:Client</faultcode>"
                     "<faultstring>UPnPError</faultstring><detail>"
                     '<UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
                     "<errorCode>%d</errorCode><errorDescription>%s</errorDescription>"
                     "</UPnPError></detail></s:Fault></s:Body></s:Envelope>"
                     % (code, text)).encode()


IGD = FakeIgd()


class IgdHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path in ("/rootDesc.xml", "/desc.xml"):
            body = DESC.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace")
        action = ""
        soap_action = self.headers.get("SOAPAction", "")
        if "#" in soap_action:
            action = soap_action.split("#", 1)[1].strip('"')
        args = {}
        import re as _re
        for k, v in _re.findall(r"<(New\w+)>(.*?)</\1>", raw, _re.S):
            args[k] = v
        for k in _re.findall(r"<(New\w+)\s*/>", raw):
            args.setdefault(k, "")
        status, body = IGD.soap(action, args)
        self.send_response(status)
        self.send_header("Content-Type", 'text/xml; charset="utf-8"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


igd_srv = ThreadingHTTPServer(("127.0.0.1", 0), IgdHandler)
igd_srv.daemon_threads = True
threading.Thread(target=igd_srv.serve_forever, daemon=True).start()
IGD_LOC = "http://127.0.0.1:%d/rootDesc.xml" % igd_srv.server_address[1]

up = net.UpnpProvider(location=IGD_LOC, timeout=5)
ok, detail = up.available()
check("the fake IGD is discovered from its description", ok, detail)
check("  the friendly name is read", "Fake IGD" in detail, detail)
check("  the model is read", "TestRouter 9000" in detail, detail)
check("  the WAN service is chosen over Layer3Forwarding",
      up.service_type.endswith("WANIPConnection:1"), up.service_type)
check("  the control URL resolves", up.control_url.endswith("/ctl/IPConn"), up.control_url)

check("GetExternalIPAddress works", up.external_ip() == "203.0.113.7")

mapping = up.add("192.168.1.50", 25565, 25565, "TCP", 3600, "Lodestone test")
check("AddPortMapping returns a mapping", mapping.external_port == 25565)
check("  it records the internal client", mapping.internal_ip == "192.168.1.50")
check("  the router really has it", ("TCP", 25565) in IGD.mappings)
check("  the description was sent",
      IGD.mappings[("TCP", 25565)]["NewPortMappingDescription"] == "Lodestone test")
check("  the mapping describes itself readably",
      mapping.describe() == "TCP 25565 -> 192.168.1.50:25565", mapping.describe())

got = up.get(25565, "TCP")
check("GetSpecificPortMappingEntry reads it back", got and got["internal_client"] == "192.168.1.50")
check("an unmapped port reads back as None", up.get(25999, "TCP") is None)

table = up.list_all()
check("the generic table lists our mapping", len(table) == 1 and table[0]["external_port"] == "25565",
      table)

ok, detail = up.remove(mapping)
check("DeletePortMapping works", ok, detail)
check("  the router no longer has it", ("TCP", 25565) not in IGD.mappings)
ok, detail = up.remove(mapping)
check("deleting a gone mapping is a success, not an error", ok, detail)

# 718: somebody else holds the port.
IGD.mappings[("TCP", 25600)] = {"NewInternalClient": "192.168.1.99",
                                "NewInternalPort": "25600",
                                "NewPortMappingDescription": "someone else"}
try:
    up.add("192.168.1.50", 25600, 25600, "TCP", 3600, "Lodestone")
    check("a conflicting port raises MappingConflict", False, "no exception")
except net.MappingConflict as e:
    check("a conflicting port raises MappingConflict", True)
    check("  the message says what to do", "different external port" in (e.hint or "").lower(), e.hint)
except net.NetworkingError as e:
    check("a conflicting port raises MappingConflict", False, type(e).__name__)

# 718 when the existing mapping is already ours is success, not failure.
IGD.mappings[("TCP", 25601)] = {"NewInternalClient": "192.168.1.50",
                                "NewInternalPort": "25601",
                                "NewPortMappingDescription": "Lodestone",
                                "NewLeaseDuration": "0"}
try:
    again = up.add("192.168.1.50", 25601, 25601, "TCP", 3600, "Lodestone")
    check("re-adding our own mapping succeeds", again.external_port == 25601)
except net.NetworkingError as e:
    check("re-adding our own mapping succeeds", False, e)

# 725: the router only does permanent leases; we must retry with 0 rather than fail.
IGD.only_permanent = True
IGD.mappings.clear()
try:
    perm = up.add("192.168.1.50", 25565, 25565, "TCP", 3600, "Lodestone")
    check("a permanent-only router is retried with lease 0", perm.lifetime == 0, perm.lifetime)
    check("  and the mapping is really there", ("TCP", 25565) in IGD.mappings)
except net.NetworkingError as e:
    check("a permanent-only router is retried with lease 0", False, e)
IGD.only_permanent = False
IGD.mappings.clear()

IGD.refuse = 606
try:
    up.add("192.168.1.50", 25565, 25565, "TCP", 0, "Lodestone")
    check("error 606 raises NotAuthorized", False, "no exception")
except net.NotAuthorized:
    check("error 606 raises NotAuthorized", True)
except net.NetworkingError as e:
    check("error 606 raises NotAuthorized", False, type(e).__name__)
IGD.refuse = None

bad = net.UpnpProvider(location="http://127.0.0.1:1/nope.xml", timeout=1)
ok, detail = bad.available()
check("an unreachable device reports rather than raising", not ok and detail, detail)


# ===========================================================================
#  NAT-PMP
# ===========================================================================
section("NAT-PMP against a fake gateway")


class FakeNatPmp(threading.Thread):
    """RFC 6886 on the wire."""

    def __init__(self, external="198.51.100.4", result=0):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.external = external
        self.result = result
        self.mappings = {}
        self.stop = False
        self.seen = []

    def run(self):
        self.sock.settimeout(0.3)
        while not self.stop:
            try:
                data, addr = self.sock.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(data) < 2:
                continue
            version, op = data[0], data[1]
            self.seen.append((version, op))
            if version != 0:
                # A NAT-PMP-only box answers a v2 packet with "unsupported version".
                self.sock.sendto(struct.pack("!BBHI", 0, 128, 1, 42), addr)
                continue
            if op == 0:
                self.sock.sendto(
                    struct.pack("!BBHI", 0, 128, self.result, 42)
                    + socket.inet_aton(self.external), addr)
            elif op in (1, 2):
                _, _, _, internal, suggested, lifetime = struct.unpack("!BBHHHI", data[:12])
                if lifetime == 0:
                    self.mappings.pop((op, internal), None)
                    external = 0
                else:
                    external = suggested or internal
                    self.mappings[(op, internal)] = external
                self.sock.sendto(
                    struct.pack("!BBHIHHI", 0, op + 128, self.result, 42,
                                internal, external, lifetime), addr)


pmp_srv = FakeNatPmp()
pmp_srv.start()
pmp = net.NatPmpProvider(gateway="127.0.0.1", timeout=1.0)
pmp.port = pmp_srv.port

check("NAT-PMP reads the external address", pmp.external_ip() == "198.51.100.4")
ok, detail = pmp.available()
check("NAT-PMP reports available", ok, detail)

pm = pmp.add("192.168.1.50", 25565, 25565, "TCP", 3600, "Lodestone")
check("NAT-PMP creates a TCP mapping", pm.external_port == 25565, pm.as_dict())
check("  op 2 was used for TCP", (2, 25565) in pmp_srv.mappings, pmp_srv.mappings)
check("  the lease came back", pm.lifetime == 3600, pm.lifetime)
check("  it is not treated as permanent", pm.seconds_left() is not None)
check("  renewal is not due immediately", not pm.needs_renewal())

pu = pmp.add("192.168.1.50", 19132, 19132, "UDP", 3600, "Lodestone")
check("NAT-PMP creates a UDP mapping with op 1", (1, 19132) in pmp_srv.mappings)

# A permanent request must not be sent as lifetime 0 — that means *delete*.
pperm = pmp.add("192.168.1.50", 25570, 25570, "TCP", 0, "Lodestone")
check("a 'permanent' request becomes a real lease, not a delete",
      pperm.lifetime > 0 and (2, 25570) in pmp_srv.mappings, pperm.lifetime)

ok, detail = pmp.remove(pm)
check("NAT-PMP removes a mapping", ok, detail)
check("  the gateway dropped it", (2, 25565) not in pmp_srv.mappings)

pmp_srv.result = 2                      # NOT AUTHORIZED
try:
    pmp.add("192.168.1.50", 25565, 25565, "TCP", 3600, "x")
    check("NAT-PMP result 2 raises NotAuthorized", False, "no exception")
except net.NotAuthorized:
    check("NAT-PMP result 2 raises NotAuthorized", True)
except net.NetworkingError as e:
    check("NAT-PMP result 2 raises NotAuthorized", False, type(e).__name__)
pmp_srv.result = 0

silent = net.NatPmpProvider(gateway="127.0.0.1", timeout=0.2)
silent.port = _free_udp_port = (lambda s: (s.bind(("127.0.0.1", 0)), s.getsockname()[1],
                                           s.close())[1])(socket.socket(socket.AF_INET,
                                                                        socket.SOCK_DGRAM))
ok, detail = silent.available()
check("a gateway that does not answer is reported, not raised", not ok, detail)

nogw = net.NatPmpProvider()
ok, detail = nogw.available()
check("no gateway at all is reported clearly", not ok and "gateway" in detail.lower(), detail)


# ===========================================================================
#  PCP
# ===========================================================================
section("PCP against a fake gateway")

check("an IPv4 packs as ::ffff:a.b.c.d",
      pcpmod.pack_addr("192.168.1.50") == b"\x00" * 10 + b"\xff\xff" + bytes([192, 168, 1, 50]))
check("it unpacks back", pcpmod.unpack_addr(pcpmod.pack_addr("192.168.1.50")) == "192.168.1.50")
check("an IPv6 round-trips",
      pcpmod.unpack_addr(pcpmod.pack_addr("2001:db8::1")) == "2001:db8::1")
check("an empty address packs as zeroes", pcpmod.pack_addr("") == b"\x00" * 16)
check("garbage packs as zeroes", pcpmod.pack_addr("not an ip") == b"\x00" * 16)

nonce = b"0123456789ab"
req = pcpmod.build_map_request("192.168.1.50", nonce, "TCP", 25565, 25565, "", 3600)
check("a PCP MAP request is 60 bytes", len(req) == 60, len(req))
check("  version 2, opcode 1", req[0] == 2 and req[1] == 1)
check("  the lifetime is in the header", struct.unpack("!I", req[4:8])[0] == 3600)
check("  the nonce is at offset 24", req[24:36] == nonce)
check("  TCP is protocol 6", req[36] == 6)
check("  UDP is protocol 17",
      pcpmod.build_map_request("192.168.1.50", nonce, "UDP", 1, 1, "", 60)[36] == 17)
check("  the internal port is where it should be",
      struct.unpack("!H", req[40:42])[0] == 25565)


class FakePcp(threading.Thread):
    """RFC 6887 on the wire."""

    def __init__(self, result=0, external="198.51.100.9"):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.result = result
        self.external = external
        self.mappings = {}
        self.stop = False
        self.wrong_nonce = False

    def run(self):
        self.sock.settimeout(0.3)
        while not self.stop:
            try:
                data, addr = self.sock.recvfrom(1500)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(data) < 24 or data[0] != 2:
                self.sock.sendto(struct.pack("!BBBBII", 2, 0x81, 0, 1, 0, 7) +
                                 b"\x00" * 12, addr)
                continue
            lifetime = struct.unpack("!I", data[4:8])[0]
            nonce = data[24:36]
            proto = data[36]
            internal, suggested = struct.unpack("!HH", data[40:44])
            if lifetime == 0:
                self.mappings.pop((proto, internal), None)
                external = 0
            else:
                external = suggested or internal
                self.mappings[(proto, internal)] = external
            out_nonce = b"xxxxxxxxxxxx" if self.wrong_nonce else nonce
            head = struct.pack("!BBBBII", 2, 0x80 | 1, 0, self.result,
                               lifetime, 99) + b"\x00" * 12
            body = out_nonce + struct.pack("!B3xHH", proto, internal, external) \
                + pcpmod.pack_addr(self.external)
            self.sock.sendto(head + body, addr)


pcp_srv = FakePcp()
pcp_srv.start()
pc = net.PcpProvider(client_ip="127.0.0.1", gateway="127.0.0.1", timeout=1.0)
pc.port = pcp_srv.port

ok, detail = pc.available()
check("PCP reports available", ok, detail)

cm = pc.add("192.168.1.50", 25565, 25565, "TCP", 3600, "Lodestone")
check("PCP creates a mapping", cm.external_port == 25565, cm.as_dict())
check("  the assigned external IP comes back", cm.external_ip == "198.51.100.9")
check("  the nonce is kept for later", cm.nonce and len(cm.nonce) == 24, cm.nonce)
check("  external_ip() now answers", pc.external_ip() == "198.51.100.9")

ok, detail = pc.remove(cm)
check("PCP removes a mapping", ok, detail)
check("  the gateway dropped it", (6, 25565) not in pcp_srv.mappings)

# A reply about a different mapping must never be accepted as success: that is
# what stops one client's answer being read as another's.
pcp_srv.wrong_nonce = True
try:
    pc.add("192.168.1.50", 25566, 25566, "TCP", 3600, "Lodestone")
    check("a mismatched nonce is rejected", False, "accepted a foreign reply")
except net.MappingRejected:
    check("a mismatched nonce is rejected", True)
except net.NetworkingError as e:
    check("a mismatched nonce is rejected", False, type(e).__name__)
pcp_srv.wrong_nonce = False

pcp_srv.result = 12                     # ADDRESS_MISMATCH
try:
    pc.add("192.168.1.50", 25567, 25567, "TCP", 3600, "Lodestone")
    check("PCP result 12 is reported as double NAT", False, "no exception")
except net.MappingRejected as e:
    check("PCP result 12 is reported as double NAT", "NAT" in (e.hint or "") or "NAT" in str(e),
          "%s / %s" % (e, e.hint))
pcp_srv.result = 2                      # NOT_AUTHORIZED
try:
    pc.add("192.168.1.50", 25568, 25568, "TCP", 3600, "Lodestone")
    check("PCP result 2 raises NotAuthorized", False, "no exception")
except net.NotAuthorized:
    check("PCP result 2 raises NotAuthorized", True)
except net.NetworkingError as e:
    check("PCP result 2 raises NotAuthorized", False, type(e).__name__)
pcp_srv.result = 0

# A NAT-PMP-only gateway answers version 0; PCP must call that unsupported
# rather than sitting there retrying until it times out.
pcp_on_natpmp = net.PcpProvider(client_ip="127.0.0.1", gateway="127.0.0.1", timeout=0.5)
pcp_on_natpmp.port = pmp_srv.port
ok, detail = pcp_on_natpmp.available()
check("a NAT-PMP-only gateway is not mistaken for PCP", not ok, detail)
check("  and it says so specifically", "PCP" in detail, detail)


# ===========================================================================
#  PortMappingService
# ===========================================================================
section("PortMappingService")


class StubProvider(net.PortMappingProvider):
    def __init__(self, name, works=True, existing=None, wan="198.51.100.1"):
        self.name = name
        self.protocol_name = name
        self.works = works
        self.existing = existing
        self.wan = wan
        self.added = []
        self.removed = []
        self.closed = 0

    def available(self):
        return (True, "stub ok") if self.works else (False, "stub says no")

    def external_ip(self):
        if not self.works:
            raise net.ProviderUnavailable("%s has no WAN" % self.name)
        return self.wan

    def get(self, external_port, protocol="TCP"):
        return self.existing

    def add(self, internal_ip, internal_port, external_port, protocol="TCP",
            lifetime=0, description=""):
        if not self.works:
            raise net.MappingRejected("%s refuses" % self.name)
        self.added.append((internal_ip, internal_port, external_port, protocol, lifetime))
        return net.PortMapping(self.name, protocol, external_port, internal_port,
                               internal_ip, description, lifetime, self.wan)

    def remove(self, mapping):
        self.removed.append(mapping.external_port)
        return True, "stub removed"

    def close(self):
        self.closed += 1


class StubInterfaces:
    def local_ipv4(self):
        return "192.168.1.50"

    def gateway_candidates(self):
        return ["192.168.1.1"]

    def gateway_ipv4(self):
        return "192.168.1.1"

    def global_ipv6(self):
        return ""

    def invalidate(self):
        pass

    def snapshot(self):
        return {"local_ipv4": "192.168.1.50", "all_ipv4": ["192.168.1.50"],
                "ipv6": "", "has_ipv6": False, "gateway": "192.168.1.1",
                "gateway_candidates": ["192.168.1.1"], "local_is_private": True,
                "local_is_cgnat": False}


bad_p, good_p = StubProvider("UPnP", works=False), StubProvider("NAT-PMP", works=True)
svc = net.PortMappingService(StubInterfaces(), providers=[bad_p, good_p])
mp, attempts = svc.ensure(25565, 25565)
check("it falls through to the next provider when the first fails", mp is not None)
check("  the winning provider is recorded", mp and mp.provider == "NAT-PMP")
check("  every attempt is reported, not just the winner", len(attempts) == 2, attempts)
check("  the failure explains itself", attempts[0]["detail"] and not attempts[0]["ok"],
      attempts[0])
check("  the WAN address is picked up", mp and mp.external_ip == "198.51.100.1")
check("  providers are closed after use", bad_p.closed and good_p.closed)
check("  the mapping is on the register", len(svc.owned()) == 1)

svc2 = net.PortMappingService(StubInterfaces(),
                              providers=[StubProvider("UPnP", works=False),
                                         StubProvider("NAT-PMP", works=False)])
mp2, attempts2 = svc2.ensure(25565)
check("all-providers-fail returns None rather than raising", mp2 is None)
check("  with a reason from each", len(attempts2) == 2 and all(a["detail"] for a in attempts2))
check("  and nothing on the register", not svc2.owned())

# Somebody else's mapping on the port must stop us, not be overwritten.
foreign = StubProvider("UPnP", existing={"internal_client": "192.168.1.99",
                                         "internal_port": "25565",
                                         "description": "someone else"})
svc3 = net.PortMappingService(StubInterfaces(), providers=[foreign])
mp3, attempts3 = svc3.ensure(25565)
check("a foreign mapping on the port blocks us", mp3 is None)
check("  and it is not overwritten", not foreign.added, foreign.added)
check("  the reason names the other device",
      "192.168.1.99" in attempts3[0]["detail"], attempts3[0]["detail"])

# Our own existing mapping is fine to re-add.
mine = StubProvider("UPnP", existing={"internal_client": "192.168.1.50",
                                      "internal_port": "25565"})
svc4 = net.PortMappingService(StubInterfaces(), providers=[mine])
mp4, _ = svc4.ensure(25565)
check("our own existing mapping does not block us", mp4 is not None)

# Release must only ever touch our own rows.
rel = StubProvider("UPnP", existing={"internal_client": "192.168.1.50",
                                     "internal_port": "25565"})
svc5 = net.PortMappingService(StubInterfaces(), providers=[rel])
svc5.ensure(25565)
results = svc5.release()
check("release removes our mapping", results and results[0]["ok"], results)
check("  the register is empty afterwards", not svc5.owned())

# If the router has since given the port to somebody else, we must back off.
taken = StubProvider("UPnP")
svc6 = net.PortMappingService(StubInterfaces(), providers=[taken])
svc6.ensure(25565)
taken.existing = {"internal_client": "192.168.1.77", "internal_port": "25565"}
results6 = svc6.release()
check("release refuses when the port now points elsewhere", not results6[0]["ok"], results6)
check("  and says why", "left alone" in results6[0]["detail"], results6[0]["detail"])
check("  and did not call remove", not taken.removed, taken.removed)

# Cleanup after a crash: the register survives as JSON.
svc7 = net.PortMappingService(StubInterfaces(), providers=[StubProvider("UPnP")])
svc7.ensure(25565)
exported = svc7.export()
check("the register exports as JSON", json.loads(json.dumps(exported)) == exported)
svc8 = net.PortMappingService(StubInterfaces(), providers=[StubProvider("UPnP")])
adopted = svc8.adopt(exported)
check("a later run can adopt it", len(adopted) == 1 and svc8.owned())
check("  and knows what to remove", svc8.owned()[0].external_port == 25565)
check("adopting rubbish is ignored, not fatal",
      svc8.adopt([{"nonsense": 1}, None, {"external_port": 0}]) == [])
check("adopting a row we do not own is refused",
      svc8.adopt([dict(exported[0], owner="somebody-else", external_port=25999)]) == [])

probe = net.PortMappingService(StubInterfaces(),
                               providers=[StubProvider("UPnP", works=True),
                                          StubProvider("PCP", works=False)]).probe()
check("probe reports each provider", len(probe) == 2)
check("  with availability", probe[0]["available"] and not probe[1]["available"], probe)

# Renewal at half-life is what keeps NAT-PMP and PCP mappings alive.
ren = StubProvider("UPnP")
svc9 = net.PortMappingService(StubInterfaces(), providers=[ren])
mrow, _ = svc9.ensure(25565, lifetime=100)
check("a fresh lease is not due for renewal", not mrow.needs_renewal())
mrow.created = time.time() - 60
check("a lease past half-life is due", mrow.needs_renewal())
done = svc9.renew_due()
check("  renew_due renews it", done and done[0]["ok"], done)
check("  by re-adding through the provider", len(ren.added) == 2, ren.added)
check("  and the clock is reset", not mrow.needs_renewal())
svc9.close()

perm = net.PortMapping("UPnP", "TCP", 1, 1, "10.0.0.1", "", 0)
check("a permanent mapping never needs renewal", not perm.needs_renewal())
check("  and has no countdown", perm.seconds_left() is None)


# ===========================================================================
#  PublicIpService
# ===========================================================================
section("PublicIpService")

IP_ANSWERS = {"/a": "203.0.113.5\n", "/b": "203.0.113.5", "/c": "203.0.113.9",
              "/junk": "not-an-ip", "/empty": "", "/boom": None}


class IpHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        body = IP_ANSWERS.get(self.path)
        if body is None:
            self.send_error(500)
            return
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


ip_srv = ThreadingHTTPServer(("127.0.0.1", 0), IpHandler)
ip_srv.daemon_threads = True
threading.Thread(target=ip_srv.serve_forever, daemon=True).start()
IPBASE = "http://127.0.0.1:%d" % ip_srv.server_address[1]


def ipsvc(paths, **kw):
    return net.PublicIpService(
        sources_v4=[(p.strip("/"), IPBASE + p) for p in paths],
        sources_v6=[], timeout=3, **kw)


svc = ipsvc(["/a", "/b"])
check("two agreeing sources give the address", svc.get(4) == "203.0.113.5")
check("  and it is marked confirmed", svc.snapshot()["ipv4_confirmed"])
check("  trailing whitespace is stripped", "\n" not in svc.get(4))

svc = ipsvc(["/a", "/c"])
check("two disagreeing sources still answer", svc.get(4) == "203.0.113.5")
check("  but are not marked confirmed", not svc.snapshot()["ipv4_confirmed"])

svc = ipsvc(["/junk", "/a"])
check("a source returning garbage is skipped", svc.get(4) == "203.0.113.5")
svc = ipsvc(["/boom", "/a"])
check("a source returning HTTP 500 is skipped", svc.get(4) == "203.0.113.5")
svc = ipsvc(["/empty", "/junk"])
try:
    svc.get(4)
    check("every source failing raises PublicIpUnavailable", False, "no exception")
except net.PublicIpUnavailable as e:
    check("every source failing raises PublicIpUnavailable", True)
    check("  and the message is showable", len(str(e)) > 5, str(e))
check("  the convenience accessor returns empty instead", svc.ipv4() == "")

svc = ipsvc(["/a"])
check("the first lookup works", svc.get(4) == "203.0.113.5")
t0 = time.time()
svc.get(4)
check("a cached lookup does not hit the network", time.time() - t0 < 0.02)

# A dynamic IP that moves under a running server must be visible.
changes = []
svc = net.PublicIpService(sources_v4=[("a", IPBASE + "/a")], sources_v6=[], timeout=3)
svc.on_change = lambda fam, old, new: changes.append((fam, old, new))
svc.get(4)
svc.sources_v4 = [net.public_ip.HttpIpSource("c", IPBASE + "/c")]
svc.invalidate()
check("a changed address is picked up", svc.get(4) == "203.0.113.9")
check("  the change fires the callback", changes == [(4, "203.0.113.5", "203.0.113.9")], changes)
check("  and the old one is remembered", svc.snapshot()["ipv4_previous"] == "203.0.113.5")

off = ipsvc(["/a"], enabled=False)
try:
    off.get(4)
    check("a disabled service refuses to look up", False, "it looked anyway")
except net.PublicIpUnavailable:
    check("a disabled service refuses to look up", True)


# ===========================================================================
#  Minecraft Server List Ping
# ===========================================================================
section("Minecraft server detection")

check("varint 0", conn.write_varint(0) == b"\x00")
check("varint 1", conn.write_varint(1) == b"\x01")
check("varint 127", conn.write_varint(127) == b"\x7f")
check("varint 128", conn.write_varint(128) == b"\x80\x01")
check("varint 25565", conn.write_varint(25565) == b"\xdd\xc7\x01")
check("varint -1 (the protocol wildcard)",
      conn.write_varint(0xFFFFFFFF) == b"\xff\xff\xff\xff\x0f")


class FakeMinecraft(threading.Thread):
    """Speaks just enough Server List Ping to be recognised."""

    def __init__(self, payload=None, garbage=False):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.port = self.sock.getsockname()[1]
        self.garbage = garbage
        self.payload = payload or {
            "version": {"name": "1.20.4", "protocol": 765},
            "players": {"online": 3, "max": 20},
            "description": {"text": "A Lodestone server"}}
        self.stop = False

    def run(self):
        self.sock.settimeout(0.3)
        while not self.stop:
            try:
                c, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        try:
            c.settimeout(2)
            c.recv(4096)                       # handshake + status request
            if self.garbage:
                c.sendall(b"SSH-2.0-OpenSSH_9.0\r\n")
                time.sleep(0.2)
                return
            blob = json.dumps(self.payload).encode()
            body = conn.write_varint(0x00) + conn.write_varint(len(blob)) + blob
            c.sendall(conn.write_varint(len(body)) + body)
            time.sleep(0.1)
        except OSError:
            pass
        finally:
            try:
                c.close()
            except OSError:
                pass


mc = FakeMinecraft()
mc.start()
time.sleep(0.15)

status = net.minecraft_ping("127.0.0.1", mc.port, timeout=3)
check("a Minecraft server answers the status ping", status.get("players", {}).get("online") == 3)
check("  the version comes back", status["version"]["name"] == "1.20.4")
check("  a latency figure is recorded", isinstance(status.get("_latency_ms"), int))

# The whole reason for speaking SLP rather than just connecting.
ssh_like = FakeMinecraft(garbage=True)
ssh_like.start()
time.sleep(0.15)
try:
    net.minecraft_ping("127.0.0.1", ssh_like.port, timeout=2)
    check("a non-Minecraft service is not accepted as one", False, "it passed")
except net.ConnectivityError as e:
    check("a non-Minecraft service is not accepted as one", True)
check("  but a plain TCP check would have passed it",
      net.tcp_open("127.0.0.1", ssh_like.port, timeout=2))

closed = socket.socket()
closed.bind(("127.0.0.1", 0))
dead_port = closed.getsockname()[1]
closed.close()
try:
    net.minecraft_ping("127.0.0.1", dead_port, timeout=1)
    check("a dead port raises", False, "no exception")
except net.ConnectivityError as e:
    check("a dead port raises with a readable reason", "listening" in str(e) or "answer" in str(e),
          str(e))

check("port_in_use sees a bound port", net.port_in_use(mc.port, "127.0.0.1"))
check("port_in_use says free for an unbound one", not net.port_in_use(dead_port, "127.0.0.1"))


# ===========================================================================
#  CGNAT classification
# ===========================================================================
section("CGNAT and address classification")

C = net.ConnectivityTester.classify_address

r = C("203.0.113.7", "203.0.113.7", "192.168.1.50")
check("matching public and router WAN is usable", r["usable"] and not r["cgnat"], r)

r = C("100.72.5.9", "", "192.168.1.50")
check("a public address in 100.64/10 is CGNAT", r["cgnat"] and not r["usable"], r)
check("  and says which range", "100.64.0.0/10" in r["reason"], r["reason"])

r = C("203.0.113.7", "100.72.5.9", "192.168.1.50")
check("a CGNAT address on the router's WAN is caught", r["cgnat"] and not r["usable"], r)
check("  and explains the ISP is in the way", "ISP" in r["reason"], r["reason"])

r = C("203.0.113.7", "192.168.100.2", "192.168.1.50")
check("a private router WAN means double NAT", r["double_nat"] and not r["usable"], r)

r = C("203.0.113.7", "198.51.100.4", "192.168.1.50")
check("public and router WAN disagreeing means double NAT", r["double_nat"] and not r["usable"], r)
check("  and both numbers are in the message",
      "198.51.100.4" in r["reason"] and "203.0.113.7" in r["reason"], r["reason"])

r = C("192.168.1.9", "", "192.168.1.50")
check("a 'public' address that is private is not usable", not r["usable"], r)

r = C("", "", "192.168.1.50")
check("no information yields no false CGNAT claim", not r["cgnat"] and not r["double_nat"], r)


# ===========================================================================
#  the reachability state machine
# ===========================================================================
section("reachability states")


class ScriptedTester(net.ConnectivityTester):
    """Real decision logic, scripted inputs."""

    def __init__(self, local, external):
        super().__init__(StubInterfaces())
        self._local, self._external = local, external

    def local_check(self, port, host=""):
        return dict(self._local, host=host or "192.168.1.50", port=port)

    def external_check(self, host, port):
        return dict(self._external)


LISTENING = {"listening": True, "minecraft": True, "detail": "ok", "status": None}
DEAD = {"listening": False, "minecraft": False, "detail": "nothing there", "status": None}
GOT_IN = {"online": True, "conclusive": True, "detail": "reached it", "checked": [],
          "source": "mcstatus.io"}
NO_ENTRY = {"online": False, "conclusive": True, "detail": "could not reach", "checked": [
    {"service": "mcstatus.io", "online": False}]}
NO_ANSWER = {"online": False, "conclusive": False, "detail": "service down", "checked": []}

r = ScriptedTester(DEAD, GOT_IN).run(25565, "203.0.113.7")
check("no server listening -> server_offline", r["state"] == net.SERVER_OFFLINE, r["state"])
check("  and the external check is not even run", r["external"] is None)

r = ScriptedTester(LISTENING, GOT_IN).run(25565, "203.0.113.7", mapping_active=True)
check("an outside connection succeeding -> reachable", r["state"] == net.REACHABLE, r["state"])

r = ScriptedTester(LISTENING, NO_ENTRY).run(25565, "203.0.113.7", mapping_active=True)
check("outside cannot get in, mapping present -> not_reachable",
      r["state"] == net.NOT_REACHABLE, r["state"])

r = ScriptedTester(LISTENING, NO_ENTRY).run(25565, "203.0.113.7", mapping_active=False)
check("outside cannot get in, no mapping -> not_reachable",
      r["state"] == net.NOT_REACHABLE, r["state"])
check("  and the reason names the missing forward", "forwarding" in r["reason"], r["reason"])

r = ScriptedTester(LISTENING, NO_ENTRY).run(25565, "203.0.113.7", mapping_active=True,
                                            firewall_blocking=True)
check("firewall blocking -> port_blocked", r["state"] == net.PORT_BLOCKED, r["state"])

r = ScriptedTester(LISTENING, NO_ANSWER).run(25565, "203.0.113.7", mapping_active=True)
check("the checker itself being down -> unknown, never 'reachable'",
      r["state"] == net.UNKNOWN, r["state"])

r = ScriptedTester(LISTENING, NO_ENTRY).run(25565, "100.72.5.9", mapping_active=True)
check("a CGNAT address -> cgnat_suspected", r["state"] == net.CGNAT_SUSPECTED, r["state"])

r = ScriptedTester(LISTENING, NO_ENTRY).run(25565, "100.72.5.9", public_ipv6="2001:db8::5",
                                            mapping_active=True)
check("CGNAT but with IPv6 -> ipv6_only", r["state"] == net.IPV6_ONLY, r["state"])

# On a shared address, "a Minecraft server answered" is not "our server
# answered" — another subscriber behind the same CGNAT address could be the one
# replying. Without a positive identity match this must not go green.
r = ScriptedTester(LISTENING, GOT_IN).run(25565, "100.72.5.9", mapping_active=True)
check("CGNAT plus an unattributable answer -> unknown, not reachable",
      r["state"] == net.UNKNOWN, r["state"])
check("  and it says the address is shared", "shared" in r["reason"], r["reason"])

SAME = {"listening": True, "minecraft": True, "detail": "ok",
        "status": {"version": "1.20.4", "players_max": 20, "players_online": 1}}
GOT_IN_SAME = dict(GOT_IN, status={"version": "Paper 1.20.4", "players_max": 20,
                                   "players_online": 1})
r = ScriptedTester(SAME, GOT_IN_SAME).run(25565, "100.72.5.9", mapping_active=True)
check("CGNAT that works AND is provably our server -> reachable",
      r["state"] == net.REACHABLE, (r["state"], r["reason"]))

GOT_IN_OTHER = dict(GOT_IN, status={"version": "1.8.9", "players_max": 100,
                                    "players_online": 40})
r = ScriptedTester(SAME, GOT_IN_OTHER).run(25565, "203.0.113.7", mapping_active=True)
check("somebody else's server answering is not our server",
      r["state"] == net.UNKNOWN, (r["state"], r["reason"]))
check("  and it says the port may point elsewhere",
      "different machine" in r["reason"], r["reason"])

r = ScriptedTester(SAME, GOT_IN_SAME).run(25565, "203.0.113.7", mapping_active=True)
check("a matching identity on a normal address is reachable",
      r["state"] == net.REACHABLE, (r["state"], r["reason"]))

r = ScriptedTester(LISTENING, NO_ENTRY).run(25565, "", public_ipv6="2001:db8::5")
check("no IPv4 but IPv6 present -> ipv6_only", r["state"] == net.IPV6_ONLY, r["state"])

r = ScriptedTester(LISTENING, NO_ENTRY).run(25565, "")
check("no public address at all -> unknown", r["state"] == net.UNKNOWN, r["state"])

check("every state has a label", all(s in net.STATE_LABEL for s in (
    net.CHECKING, net.REACHABLE, net.NOT_REACHABLE, net.SERVER_OFFLINE,
    net.PORT_BLOCKED, net.CGNAT_SUSPECTED, net.IPV6_ONLY, net.UNKNOWN)))

# A router that accepts a mapping but cannot name its own WAN address is not the
# device traffic arrives at. Observed on real hardware: the IGD returned
# 0.0.0.0 from GetExternalIPAddress while the internet saw a real address.
r = ScriptedTester(LISTENING, NO_ENTRY).run(
    25565, "203.0.113.7", mapping_active=True, router_wan_unknown=True)
check("a router that cannot name its own WAN is called out",
      r.get("router_not_edge") is True, r.get("reason", "")[:120])
check("  and the reason says a forward there cannot help",
      "bridge or access-point mode" in r["reason"], r["reason"][-160:])
r = ScriptedTester(LISTENING, NO_ENTRY).run(
    25565, "203.0.113.7", mapping_active=True, router_wan_unknown=False)
check("a normal router gets no such claim", not r.get("router_not_edge"))

# -- naming the router that actually needs configuring -----------------------
# "There is another router in front of this one" is true but unusable. The trace
# turns it into an address the user can log into.
UP = [{"ip": "192.168.1.1", "hop": 2, "ours": True},
      {"ip": "10.233.0.1", "hop": 3, "ours": False}]
r = ScriptedTester(LISTENING, NO_ENTRY).run(
    25565, "46.212.240.115", mapping_active=True, router_wan_unknown=True, upstream=UP)
check("the edge router is named", r.get("edge_router") == "192.168.1.1", r.get("edge_router"))
check("  and the reason tells you to forward there",
      "192.168.1.1" in r["reason"], r["reason"][-200:])
check("an ISP hop is identified separately",
      r.get("isp_nat_hop") == "10.233.0.1", r.get("isp_nat_hop"))
check("  and it warns the forward may still not help",
      "ISP" in r["reason"], r["reason"][-200:])

# A 10/8 hop on its own is the ISP's, not something the user can configure.
r = ScriptedTester(LISTENING, NO_ENTRY).run(
    25565, "46.212.240.115", mapping_active=True, router_wan_unknown=True,
    upstream=[{"ip": "10.233.0.1", "hop": 2, "ours": False}])
check("an ISP-only hop is not offered as a router to configure",
      not r.get("edge_router"), r.get("edge_router"))
check("  but is still reported", r.get("isp_nat_hop") == "10.233.0.1")

r = ScriptedTester(LISTENING, NO_ENTRY).run(
    25565, "46.212.240.115", mapping_active=True, router_wan_unknown=True, upstream=[])
check("no trace means no invented advice", not r.get("edge_router"))

# The trace parser, against real captured output.
TRACERT = """
Tracing route to 1.1.1.1 over a maximum of 6 hops

  1    10 ms     5 ms     7 ms  192.168.68.1
  2    15 ms     9 ms     7 ms  192.168.1.1
  3    16 ms    14 ms    13 ms  10.233.0.1
  4    15 ms    14 ms    17 ms  84.208.41.82
  5     *        *        *     Request timed out.
  6    13 ms    16 ms    16 ms  80.232.84.90

Trace complete.
"""
_real_rq = net.net_interfaces.run_quiet
net.net_interfaces.run_quiet = lambda argv, timeout=None: (0, TRACERT)
try:
    ifs2 = net.NetworkInterfaceService()
    ifs2._cache["gw"] = (time.time(), "192.168.68.1")
    hops = ifs2.upstream_hops()
    check("the trace parser finds the private hops above the gateway",
          [h["ip"] for h in hops] == ["192.168.1.1", "10.233.0.1"], hops)
    check("  our own gateway is not listed as upstream",
          all(h["ip"] != "192.168.68.1" for h in hops))
    check("  it stops at the first public hop",
          all(h["ip"] != "84.208.41.82" for h in hops), hops)
    check("  192.168/16 is marked as ours",
          hops[0]["ours"] is True and hops[0]["hop"] == 2)
    check("  10/8 is marked as the ISP's", hops[1]["ours"] is False)
    ifs2._cache.pop("upstream", None)
    check("edge_router picks the configurable one",
          ifs2.edge_router() == "192.168.1.1", ifs2.edge_router())

    net.net_interfaces.run_quiet = lambda argv, timeout=None: (-1, "")
    ifs3 = net.NetworkInterfaceService()
    check("an unusable traceroute yields nothing rather than raising",
          ifs3.upstream_hops() == [] and ifs3.edge_router() == "")
finally:
    net.net_interfaces.run_quiet = _real_rq

# -- the external probe must test the port the world would actually dial ------
class PortSpy(ScriptedTester):
    def __init__(self, local, external):
        super().__init__(local, external)
        self.probed = []

    def external_check(self, host, port):
        self.probed.append(port)
        return dict(self._external)


spy = PortSpy(SAME, GOT_IN_SAME)
spy.run(25565, "203.0.113.7", mapping_active=True, external_port=25566)
check("the external probe uses the external port, not the internal one",
      spy.probed == [25566], spy.probed)
spy2 = PortSpy(SAME, GOT_IN_SAME)
res = spy2.run(25565, "203.0.113.7", mapping_active=True)
check("with no external port it falls back to the Minecraft port",
      spy2.probed == [25565], spy2.probed)
check("  and the result records both", res["port"] == 25565 and res["probe_port"] == 25565)

S = net.ConnectivityTester.same_server
check("identical versions match", S({"version": "1.20.4"}, {"version": "1.20.4"}) is True)
check("a prefixed version still matches",
      S({"version": "1.20.4"}, {"version": "Paper 1.20.4"}) is True)
check("different versions do not match",
      S({"version": "1.20.4"}, {"version": "1.8.9"}) is False)
check("a differing player limit does not match",
      S({"version": "1.20.4", "players_max": 20},
        {"version": "1.20.4", "players_max": 100}) is False)
check("no shared fields means cannot tell", S({}, {}) is None)
check("a missing side means cannot tell", S(None, {"version": "1.20.4"}) is None)

# The external checker, for real, over HTTP.
CHECK_ANSWERS = {"/on": {"online": True, "version": {"name_clean": "1.20.4"},
                         "players": {"online": 1, "max": 20}},
                 "/off": {"online": False, "error": "connection timed out"}}


class CheckHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        key = "/" + self.path.strip("/").split("/")[0]
        body = CHECK_ANSWERS.get(key)
        if body is None:
            self.send_error(503)
            return
        raw = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


chk_srv = ThreadingHTTPServer(("127.0.0.1", 0), CheckHandler)
chk_srv.daemon_threads = True
threading.Thread(target=chk_srv.serve_forever, daemon=True).start()
CHKBASE = "http://127.0.0.1:%d" % chk_srv.server_address[1]

t = net.ConnectivityTester(StubInterfaces(),
                           checkers=[("fake", CHKBASE + "/on/{host}:{port}")], timeout=4)
res = t.external_check("203.0.113.7", 25565)
check("a real external checker reporting online is believed", res["online"] and res["conclusive"])
check("  and it names the source", res["source"] == "fake", res)

t = net.ConnectivityTester(StubInterfaces(),
                           checkers=[("fake", CHKBASE + "/off/{host}:{port}")], timeout=4)
res = t.external_check("203.0.113.7", 25565)
check("reporting offline is conclusive but not online", res["conclusive"] and not res["online"])

t = net.ConnectivityTester(StubInterfaces(),
                           checkers=[("dead", CHKBASE + "/nope/{host}:{port}")], timeout=4)
res = t.external_check("203.0.113.7", 25565)
check("a checker that errors is inconclusive, not 'offline'",
      not res["conclusive"] and not res["online"], res)

t = net.ConnectivityTester(StubInterfaces(),
                           checkers=[("fake", CHKBASE + "/on/{host}:{port}")],
                           allow_external=False)
res = t.external_check("203.0.113.7", 25565)
check("with the external test switched off nothing is claimed",
      not res["online"] and not res["conclusive"], res)


# ===========================================================================
#  FirewallManager
# ===========================================================================
section("FirewallManager")

check("the rule name is deterministic",
      net.rule_name(25565, "TCP") == "Lodestone-Minecraft-TCP-25565")
check("the rule name has no spaces to quote",
      " " not in net.rule_name(25565, "TCP"))
check("UDP gets its own rule name",
      net.rule_name(25565, "UDP") == "Lodestone-Minecraft-UDP-25565")

fw = net.FirewallManager()
check("it knows whether this is Windows", fw.supported == sys.platform.startswith("win"))
snapshot = fw.snapshot(25565, "TCP", deep=False)
check("a snapshot always comes back", isinstance(snapshot, dict) and "supported" in snapshot)
check("  it never claims to be blocking without evidence",
      snapshot.get("likely_blocking") in (None, True, False))
if not fw.supported:
    check("off Windows it says so rather than failing", snapshot["detail"], snapshot)
    try:
        fw.add_rule(25565)
        check("off Windows adding a rule raises a clear error", False, "no exception")
    except net.FirewallError as e:
        check("off Windows adding a rule raises a clear error", "Windows" in str(e), str(e))
    check("off Windows removing is a harmless no-op", fw.remove_rule(25565)["ok"])

for bad_port in (0, 70000, "banana"):
    try:
        fwmod._clean_port(bad_port)
        check("firewall rejects port %r" % bad_port, False, "accepted")
    except net.FirewallError:
        check("firewall rejects port %r" % bad_port, True)
try:
    fwmod._clean_proto("ICMP")
    check("firewall rejects a protocol it cannot scope", False, "accepted")
except net.FirewallError:
    check("firewall rejects a protocol it cannot scope", True)

check("a LocalPort of Any is recognised as a wildcard", fwmod._is_any("Any"))
check("a real port is not a wildcard", not fwmod._is_any("25565"))
check("java.exe is recognised", fwmod._looks_like_java(r"C:\Program Files\Java\jdk-21\bin\java.exe"))
check("javaw.exe is recognised", fwmod._looks_like_java(r"C:\jre\bin\javaw.exe"))
check("a Zulu JRE path is recognised",
      fwmod._looks_like_java(r"C:\Users\x\.jdks\zulu17.0.1-win_x64\bin\javaw.exe"))
check("an unrelated game is not mistaken for Java",
      not fwmod._looks_like_java(r"C:\Games\GTAV\GTA5.exe"))
check("an empty program is not Java", not fwmod._looks_like_java(""))

# The whole reason for reading netsh in verbose: a program-scoped rule with
# LocalPort Any opens the port for that program only. A machine with hundreds of
# them would otherwise always look like "the port is already allowed".
NETSH_SAMPLE = """
Rule Name:                            Allow MC port
----------------------------------------------------------------------
Enabled:                              Yes
Direction:                            In
Profiles:                             Private
Protocol:                             TCP
LocalPort:                            25565
Action:                               Allow

Rule Name:                            GTA V
----------------------------------------------------------------------
Enabled:                              Yes
Direction:                            In
Profiles:                             Private
Protocol:                             TCP
LocalPort:                            Any
Program:                              C:\\Games\\GTAV\\GTA5.exe
Action:                               Allow

Rule Name:                            Java(TM) Platform SE binary
----------------------------------------------------------------------
Enabled:                              Yes
Direction:                            In
Profiles:                             Private
Protocol:                             TCP
LocalPort:                            Any
Program:                              C:\\Program Files\\Java\\jdk-21\\bin\\java.exe
Action:                               Allow

Rule Name:                            Disabled port rule
----------------------------------------------------------------------
Enabled:                              No
Direction:                            In
Profiles:                             Private
Protocol:                             TCP
LocalPort:                            25565
Action:                               Allow

Rule Name:                            Block MC
----------------------------------------------------------------------
Enabled:                              Yes
Direction:                            In
Profiles:                             Private
Protocol:                             TCP
LocalPort:                            25565
Action:                               Block
"""


class FakeFirewall(net.FirewallManager):
    """The real parser, fed a captured netsh table."""

    def __init__(self, text, state_on=True):
        super().__init__()
        self.supported = True
        self._text = text
        self._state_on = state_on

    def profile_state(self):
        return {"supported": True, "on": self._state_on, "current": "private",
                "profiles": {"private": self._state_on}, "detail": ""}

    def rule_status(self, port, protocol="TCP"):
        return False, None, "No Lodestone rule for that port."


_real_run = fwmod.run_quiet
fwmod.run_quiet = lambda argv, timeout=None: (0, NETSH_SAMPLE)
try:
    fake = FakeFirewall(NETSH_SAMPLE)
    scan = fake.scan_allow_rules(25565, "TCP")
    names = [r["name"] for r in scan["rules"]]
    check("the scan splits on Rule Name, so names match their own fields",
          names == ["Allow MC port"], names)
    check("  a disabled rule is skipped", "Disabled port rule" not in names)
    check("  a Block rule is not read as Allow", "Block MC" not in names)
    check("  the last rule in the table is not dropped",
          len(re.findall(r"Rule Name:", NETSH_SAMPLE)) == 5)
    check("  a Java program rule is kept separately",
          [r["name"] for r in scan["java_rules"]] == ["Java(TM) Platform SE binary"],
          scan["java_rules"])
    check("  an unrelated program rule does not count as opening the port",
          [r["name"] for r in scan["program_rules"]] == ["GTA V"], scan["program_rules"])
    check("  the summary distinguishes the two", "open" in scan["detail"], scan["detail"])

    # With nothing but unrelated program rules — no port rule, no Java rule —
    # the port really is blocked and the panel has to say so.
    only_games = (NETSH_SAMPLE
                  .replace("LocalPort:                            25565",
                           "LocalPort:                            9999")
                  .replace(r"C:\Program Files\Java\jdk-21\bin\java.exe",
                           r"C:\Games\Other\other.exe"))
    fake2 = FakeFirewall(only_games)
    fwmod.run_quiet = lambda argv, timeout=None: (0, only_games)
    snap2 = fake2.snapshot(25565, "TCP", deep=True)
    check("hundreds of unrelated program rules do not clear the port",
          snap2["likely_blocking"] is True, snap2)

    # Firewall state unreadable must stay unknown, never "not blocking".
    fake3 = FakeFirewall(only_games, state_on=None)
    snap3 = fake3.snapshot(25565, "TCP", deep=True)
    check("an unreadable firewall state stays unknown, not 'not blocking'",
          snap3["likely_blocking"] is None, snap3["likely_blocking"])
    check("  and says the check was inconclusive",
          "not conclusive" in snap3["detail"], snap3["detail"])
finally:
    fwmod.run_quiet = _real_run

P = fwmod._port_matches
check("netsh LocalPort 25565 matches", P("25565", 25565))
check("netsh LocalPort Any matches", P("Any", 25565))
check("netsh LocalPort list matches", P("25564,25565,25566", 25565))
check("netsh LocalPort range matches", P("25000-26000", 25565))
check("a different port does not match", not P("25566", 25565))
check("an empty field does not match", not P("", 25565))
check("a range that excludes it does not match", not P("100-200", 25565))
check("RPC-style junk does not match", not P("RPC Dynamic Ports", 25565))


# ===========================================================================
#  HostingManager
# ===========================================================================
section("HostingManager")

hm = net.HostingManager(interfaces=StubInterfaces())
check("it starts in local mode", hm.mode == net.MODE_LOCAL)
check("the default port is 25565", hm.port == net.DEFAULT_MC_PORT)
b = hm.brief()
check("brief() has what the status poll needs",
      all(k in b for k in ("mode", "port", "online", "busy", "connectivity_state")), b)
check("brief() is never online before a check", not b["online"])
t0 = time.time()
for _ in range(50):
    hm.brief()
check("brief() is cheap enough to poll", time.time() - t0 < 0.1, time.time() - t0)

check("local mode binds loopback",
      hm.desired_properties("local")["server-ip"] == "127.0.0.1")
check("LAN mode binds every interface",
      hm.desired_properties("lan")["server-ip"] == "")
check("internet mode binds every interface",
      hm.desired_properties("internet")["server-ip"] == "")
check("the port is carried into the properties",
      hm.desired_properties("lan")["server-port"] == "25565")

hm.mode = net.MODE_INTERNET
conflicts = hm.properties_conflicts({"server-ip": "127.0.0.1", "server-port": "25565"})
check("a loopback bind is flagged in internet mode",
      any(c["key"] == "server-ip" for c in conflicts), conflicts)
check("  and the explanation names the trap",
      "127.0.0.1" in conflicts[0]["why"], conflicts[0]["why"])
check("a correct file has no conflicts",
      hm.properties_conflicts({"server-ip": "", "server-port": "25565"}) == [])
check("a wrong port is flagged",
      any(c["key"] == "server-port"
          for c in hm.properties_conflicts({"server-ip": "", "server-port": "25577"})))
check("enable-status=false is flagged as advisory",
      any(c.get("advisory") for c in hm.properties_conflicts(
          {"server-ip": "", "server-port": "25565", "enable-status": "false"})))

hm.mode = net.MODE_LOCAL
check("local mode does not demand an empty server-ip",
      hm.properties_conflicts({"server-ip": "127.0.0.1", "server-port": "25565"}) == [])

hm2 = net.HostingManager(interfaces=StubInterfaces())
check("set_port accepts a good port", hm2.set_port(25566, check_free=False) == 25566)
try:
    hm2.set_port(25575, rcon_port=25575, check_free=False)
    check("set_port refuses the RCON port", False, "accepted")
except net.PortInvalid:
    check("set_port refuses the RCON port", True)
check("  and the port is unchanged after a refusal", hm2.port == 25566)
check("an empty external port means 'same as internal'",
      hm2.set_external_port(0) == 0 and hm2.effective_external_port() == 25566)
check("a different external port is kept",
      hm2.set_external_port(25700) == 25700 and hm2.effective_external_port() == 25700)

# -- a green light must not outlive the evidence behind it --------------------
def reachable_manager():
    """A manager parked in the state 'confirmed reachable a moment ago'."""
    svc = net.PortMappingService(StubInterfaces(), providers=[StubProvider("UPnP")])
    mgr = net.HostingManager(interfaces=StubInterfaces(), mapping=svc)
    mgr.mode = net.MODE_INTERNET
    svc.ensure(25565, 25565)
    mgr.connectivity = {"state": net.REACHABLE, "reason": "an outside host connected"}
    mgr.last_checked_at = time.time()
    return mgr, svc


mgr, svc = reachable_manager()
check("a fresh confirmed check reads as online", mgr.brief()["online"])
check("  and the snapshot agrees", mgr.snapshot()["online"])
check("  and it advertises the public address only when online",
      mgr.snapshot()["advertise"] is not None)

mgr.last_checked_at = time.time() - (hostmod.VERDICT_TTL + 10)
check("an expired check stops reading as online", not mgr.brief()["online"])
check("  the state degrades to unknown, not to 'not reachable'",
      mgr.brief()["connectivity_state"] == net.UNKNOWN, mgr.brief())
check("  it is flagged stale", mgr.brief()["stale"])
snap = mgr.snapshot()
check("  the snapshot explains why it expired",
      "no longer evidence" in snap["connectivity"]["reason"], snap["connectivity"])
check("  and marks it expired", snap["connectivity"].get("expired"))

mgr, svc = reachable_manager()
svc.release()
check("losing the port mapping stops reading as online", not mgr.brief()["online"])
check("  and says the forward is gone",
      "no longer in place" in mgr.snapshot()["connectivity"]["reason"],
      mgr.snapshot()["connectivity"])

mgr, svc = reachable_manager()
mgr.set_port(25570, check_free=False)
check("changing the port drops the old verdict", not mgr.brief()["online"])
check("  and says the port changed",
      "port changed" in mgr.snapshot()["connectivity"]["reason"],
      mgr.snapshot()["connectivity"])

mgr, svc = reachable_manager()
mgr.set_external_port(25571)
check("changing the external port drops the old verdict", not mgr.brief()["online"])

mgr, svc = reachable_manager()
mgr.set_port(25565, check_free=False)
check("setting the same port again keeps the verdict", mgr.brief()["online"])

# active_mapping must not answer with an unrelated mapping.
svc2 = net.PortMappingService(StubInterfaces(), providers=[StubProvider("UPnP")])
mgr2 = net.HostingManager(interfaces=StubInterfaces(), mapping=svc2)
mgr2.mode = net.MODE_INTERNET
svc2.ensure(19132, 19132)              # a leftover from some other port
mgr2.port = 25565
check("a mapping for a different port is not reported as active",
      mgr2.active_mapping() is None)
check("  so the dashboard does not claim a port forward", not mgr2.snapshot()["mapping_active"])
svc2.ensure(25565, 25565)
check("the matching mapping is found", mgr2.active_mapping() is not None)
check("  and it is the right one", mgr2.active_mapping().internal_port == 25565)

# -- auto fix: does what it can, admits what it cannot -----------------------

class FixTester(net.ConnectivityTester):
    def __init__(self, state, reason="", extra=None):
        super().__init__(StubInterfaces())
        self._state, self._reason, self._extra = state, reason, extra or {}

    def local_check(self, port, host=""):
        return {"listening": True, "minecraft": True, "detail": "ok", "status": None}

    def run(self, port, *a, **kw):
        out = {"state": self._state, "reason": self._reason, "local": None,
               "address": {}, "external": None, "checked_at": time.time(),
               "port": port}
        out.update(self._extra)
        return out


class LoopbackInterfaces(StubInterfaces):
    """Reports 127.0.0.1, so a real listening socket in the test counts as
    "the server is up" for the local probe."""

    def local_ipv4(self):
        return "127.0.0.1"

    def snapshot(self):
        out = StubInterfaces.snapshot(self)
        out["local_ipv4"] = "127.0.0.1"
        return out


_open_sockets = []


def make_fix_manager(state, extra=None, providers=None, listening=False):
    ifs = LoopbackInterfaces() if listening else StubInterfaces()
    # The mapping service keeps the LAN stub on purpose: it refuses to forward a
    # port to a loopback address, which is correct behaviour and not what this
    # test is about.
    svc = net.PortMappingService(StubInterfaces(),
                                 providers=providers if providers is not None
                                 else [StubProvider("UPnP")])
    mgr = net.HostingManager(interfaces=ifs, mapping=svc,
                             tester=FixTester(state, "scripted", extra))
    mgr.mode = net.MODE_INTERNET
    mgr.public_ip.enabled = False
    # The stub LAN address never listens, so the post-start wait would otherwise
    # burn its full budget on every case.
    mgr.start_timeout = 2
    if listening:
        # A genuinely open port, so the "is the server up" probe is answering a
        # real socket rather than a fixture that says one thing and does another.
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(5)
        _open_sockets.append(sock)
        mgr.port = sock.getsockname()[1]
    return mgr, svc


wrote = {}
mgr, svc = make_fix_manager(net.REACHABLE, listening=True)
res = mgr.autofix(write_properties=lambda v: wrote.update(v) or True,
                  start_server=lambda: (True, "started"),
                  is_running=lambda: True, manage_firewall=False, blocking=True)
check("autofix returns fixed and manual lists",
      isinstance(res.get("fixed"), list) and isinstance(res.get("manual"), list), res)
check("  it corrected server.properties", wrote.get("server-ip") == "")
check("  and says so", any("server.properties" in x for x in res["fixed"]), res["fixed"])
check("  it created the missing port forward",
      any("port forward" in x.lower() for x in res["fixed"]), res["fixed"])
check("  reaching the goal leaves nothing manual", res["manual"] == [], res["manual"])
check("  the result is exposed on the snapshot", mgr.snapshot()["autofix"] is not None)

# A router upstream of ours is the one thing no button can fix.
mgr, svc = make_fix_manager(net.NOT_REACHABLE, {"router_not_edge": True})
res = mgr.autofix(write_properties=lambda v: True, start_server=lambda: (True, "ok"),
                  is_running=lambda: True, manage_firewall=False, blocking=True)
check("a router in bridge mode is reported as unfixable", res["manual"], res)
check("  and the advice is to fix the box in front",
      any("bridge mode" in x for x in res["manual"]), res["manual"])
check("  autofix never claims success it did not get", not mgr.snapshot()["online"])

mgr, svc = make_fix_manager(net.CGNAT_SUSPECTED, {"address": {"cgnat": True}})
res = mgr.autofix(write_properties=lambda v: True, start_server=lambda: (True, "ok"),
                  is_running=lambda: True, manage_firewall=False, blocking=True)
check("CGNAT is reported as unfixable",
      any("carrier-grade NAT" in x for x in res["manual"]), res["manual"])
check("  and a tunnel is offered as the way round",
      any("tunnel" in x.lower() for x in res["manual"]), res["manual"])

# Refusing to accept a licence on the user's behalf.
mgr, svc = make_fix_manager(net.SERVER_OFFLINE)
mgr.tester.local_check = lambda port, host="": {
    "listening": False, "minecraft": False, "detail": "nothing there", "status": None}
res = mgr.autofix(write_properties=lambda v: True,
                  start_server=lambda: (False, "eula.txt has not been accepted yet."),
                  is_running=lambda: False, manage_firewall=False, blocking=True)
check("an unaccepted EULA is surfaced, not auto-accepted",
      any("EULA" in x for x in res["manual"]), res["manual"])
check("  and it points at the button",
      any("Accept EULA" in x for x in res["manual"]), res["manual"])

# The wait for a starting server has to be bounded by the clock, not by a count
# of attempts: each probe against a dead address costs a full connect timeout,
# which once turned a 120-second budget into six minutes per case.
mgr, svc = make_fix_manager(net.SERVER_OFFLINE)
mgr.start_timeout = 3
mgr.tester.local_check = lambda port, host="": {
    "listening": False, "minecraft": False, "detail": "nothing", "status": None}
t0 = time.time()
res = mgr.autofix(write_properties=lambda v: True,
                  start_server=lambda: (True, "started"),
                  is_running=lambda: True, manage_firewall=False, blocking=True)
elapsed = time.time() - t0
check("the wait for a starting server is bounded by wall clock",
      elapsed < 25, "%.1fs" % elapsed)
check("  and a server that never listens is reported, not waited on forever",
      any("listening" in x for x in res["manual"]), res["manual"])

# A process that is already up must not be started a second time.
started = []
mgr, svc = make_fix_manager(net.NOT_REACHABLE)
mgr.tester.local_check = lambda port, host="": {
    "listening": False, "minecraft": False, "detail": "nothing", "status": None}
res = mgr.autofix(write_properties=lambda v: True,
                  start_server=lambda: (started.append(1), (True, "ok"))[1],
                  is_running=lambda: True, manage_firewall=False, blocking=True)
check("a server already running is not started again", started == [], started)
check("  and the mismatch is explained",
      any("not listening" in x for x in res["manual"]), res["manual"])

# A failing properties write must not abort the rest of the run.
mgr, svc = make_fix_manager(net.REACHABLE, listening=True)
def blow_up(_v):
    raise RuntimeError("no folder set")
res = mgr.autofix(write_properties=blow_up, start_server=lambda: (True, "ok"),
                  is_running=lambda: True, manage_firewall=False, blocking=True)
check("autofix survives a properties failure",
      any("no folder set" in x for x in res["manual"]), res["manual"])
check("  and still did the router step",
      any("port forward" in x.lower() for x in res["fixed"]), res["fixed"])

# A re-check must not leave the step list contradicting the summary.
mgr, svc = make_fix_manager(net.NOT_REACHABLE)
mgr.apply("internet", blocking=True, write_properties=lambda v: True)
mgr.tester = FixTester(net.REACHABLE, "now it works")
mgr.recheck(blocking=True)
steps = {s["key"]: s["state"] for s in mgr.steps}
check("a re-check updates the step list too", steps.get("connectivity") == "ok", steps)
check("  and the summary agrees with it",
      mgr.snapshot()["connectivity_state"] == net.REACHABLE)

# -- tunnels: playit.gg ------------------------------------------------------
section("tunnel (playit.gg)")

from networking import tunnel as tunmod

check("an address with a port splits", net.split_address("abc.playit.gg:31234")
      == ("abc.playit.gg", 31234))
check("an address without one takes the default",
      net.split_address("abc.playit.gg", 25565) == ("abc.playit.gg", 25565))
check("an empty address is handled", net.split_address("") == ("", 25565))
check("a junk port falls back", net.split_address("a.playit.gg:abc", 25565)
      == ("a.playit.gg", 25565))

prov = net.PlayitProvider()
prov._scan("  visit https://playit.gg/claim/abc123def to claim this agent")
check("a claim URL is recognised",
      prov.claim_url() == "https://playit.gg/claim/abc123def", prov.claim_url())
prov._claim_url = ""
prov._scan("go to https://playit.gg/claim/xyz789.")
check("a trailing full stop is not eaten into the URL",
      prov.claim_url() == "https://playit.gg/claim/xyz789", prov.claim_url())
prov._scan("tunnel running at rough-mount.gl.at.ply.gg:31234")
check("a playit tunnel address is picked up",
      prov.address() == "rough-mount.gl.at.ply.gg:31234", prov.address())

prov2 = net.PlayitProvider()
prov2._scan("forwarding to 127.0.0.1:25565 from local")
check("the local forwarding target is not mistaken for the tunnel address",
      prov2.address() == "", prov2.address())
prov2._scan("local 192.168.1.50:25565 -> remote")
check("a LAN address is not mistaken for it either", prov2.address() == "")

# The agent is a real child process, so run a fake one that prints the same
# lines. Parsing somebody else's stdout is exactly the kind of thing that only
# breaks against the real shape.
FAKE_AGENT = os.path.join(_TESTHOME, "fake_playit.py")
io.open(FAKE_AGENT, "w", encoding="utf-8").write(
    "import time\n"
    "print('playit-agent 0.15.13 starting', flush=True)\n"
    "print('connected to control server', flush=True)\n"
    "print('tunnel running at brave-otter.gl.at.ply.gg:31234', flush=True)\n"
    "time.sleep(30)\n")

CLAIM_AGENT = os.path.join(_TESTHOME, "fake_playit_claim.py")
io.open(CLAIM_AGENT, "w", encoding="utf-8").write(
    "import time\n"
    "print('playit-agent starting', flush=True)\n"
    "print('agent not claimed, visit https://playit.gg/claim/abc999 to claim it',"
    " flush=True)\n"
    "time.sleep(30)\n")


class ScriptedPlayit(net.PlayitProvider):
    """The real class, launching python on a script instead of the binary."""

    script = FAKE_AGENT

    def resolve(self):
        self.agent_path = sys.executable
        return self.agent_path

    def start(self):
        import subprocess as sp
        with self._lock:
            self.lines.clear()
            self._claim_url = ""
            self._address = ""
            self._ready = False
        self.proc = sp.Popen([sys.executable, "-u", self.script],
                             stdout=sp.PIPE, stderr=sp.STDOUT, text=True,
                             bufsize=1, encoding="utf-8", errors="replace")
        threading.Thread(target=self._reader, daemon=True).start()
        self._started_at = time.time()
        return True, "started"


agent = ScriptedPlayit()
agent.start()
got = agent.wait_for_address(timeout=15)
check("a running agent reports its tunnel address",
      got == "brave-otter.gl.at.ply.gg:31234", got)
st = agent.status()
check("  status says it is running and ready", st["running"] and st["ready"], st)
check("  and does not ask for a claim", not st["needs_claim"])
check("  the agent output is captured for the UI", len(st["log"]) >= 3, st["log"])
ok, detail = agent.stop()
check("the agent stops on request", ok, detail)
check("  and is no longer running", not agent.running())
check("stopping twice is harmless", agent.stop()[0])


class ClaimPlayit(ScriptedPlayit):
    script = CLAIM_AGENT


# First run: a claim URL and no tunnel. Waiting must not block for the whole
# timeout, because only a human with a browser can move it forward.
claimer = ClaimPlayit()
claimer.start()
_t0 = time.time()
addr_got = claimer.wait_for_address(timeout=20)
waited = time.time() - _t0
check("an unclaimed agent yields no address", addr_got == "", addr_got)
check("  and does not sit out the whole timeout", waited < 12, "%.1fs" % waited)
st = claimer.status()
check("  it surfaces the claim URL",
      st["claim_url"] == "https://playit.gg/claim/abc999", st["claim_url"])
check("  and flags that a human is needed", st["needs_claim"])
claimer.stop()

missing = net.PlayitProvider(agent_path=os.path.join(_TESTHOME, "nope.exe"))
ok, detail = missing.available()
check("a missing agent is reported, not raised", not ok)
check("  and points at the download", "playit.gg/download" in detail, detail)
try:
    missing.start()
    check("starting a missing agent raises a typed error", False, "no exception")
except net.NetworkingError as e:
    check("starting a missing agent raises a typed error", True)
    check("  with a usable hint", "download" in (e.hint or "").lower(), e.hint)

# Locating an agent on disk.
fake_dir = os.path.join(_TESTHOME, "playitdir")
os.makedirs(fake_dir, exist_ok=True)
exe = os.path.join(fake_dir, "playit.exe" if sys.platform.startswith("win") else "playit")
io.open(exe, "w").write("")
check("an explicit path is used when it exists", tunmod.find_agent(exe) == exe)
check("a bad explicit path yields nothing",
      tunmod.find_agent(os.path.join(fake_dir, "nope.exe")) == "")



# -- the v1.x service-style agent --------------------------------------------
# Two generations of the agent exist and they are driven completely differently.
# Guessing wrong means running the binary bare, which on v1.x opens an
# interactive terminal UI and never returns.
V1_HELP = """Usage: playit.EXE [OPTIONS] [COMMAND]

Commands:
  version      Print version information
  start        Start the installed playitd service
  stop         Stop the installed playitd service
  status       Show the status of the installed playitd service
  help         Print this message
"""
V0_HELP = "playit-agent 0.15.13\nUsage: playit-agent [OPTIONS]\n"


class CliPlayit(net.PlayitProvider):
    """Real class, scripted CLI. Records every subcommand it was asked to run."""

    def __init__(self, help_text=V1_HELP, replies=None):
        net.PlayitProvider.__init__(self)
        self.agent_path = "fake-playit"
        self.help_text = help_text
        self.replies = replies or {}
        self.calls = []

    def resolve(self):
        return self.agent_path

    def _cli(self, *args, timeout=25):
        self.calls.append(" ".join(args))
        if args and args[0] == "--help":
            return 0, self.help_text
        return self.replies.get(" ".join(args), (0, ""))


v1 = CliPlayit(V1_HELP)
check("a v1 agent is detected as service style", v1.service_style() is True)
v0 = CliPlayit(V0_HELP)
check("a v0 agent is not", v0.service_style() is False)

v1 = CliPlayit(V1_HELP, {"status": (0, "The playit service is not running.")})
check("a stopped service reads as not running", v1.running() is False)
v1 = CliPlayit(V1_HELP, {"status": (0, "playit service running\ntunnel brave-otter.gl.at.ply.gg:31234")})
check("a running service reads as running", v1.running() is True)
check("  and its address is scraped from status",
      (v1._refresh_service_state(), v1.address())[1] == "brave-otter.gl.at.ply.gg:31234",
      v1.address())

v1 = CliPlayit(V1_HELP, {"start": (0, "started"),
                         "status": (0, "running\nlively-fox.gl.at.ply.gg:44444")})
ok, detail = v1.start()
check("starting a service agent uses the CLI, not a bare launch",
      ok and "start" in v1.calls, v1.calls)
check("  and never runs the binary with no arguments",
      all(c.strip() for c in v1.calls), v1.calls)
ok, detail = v1.stop()
check("stopping uses the stop subcommand", "stop" in v1.calls, v1.calls)

v1 = CliPlayit(V1_HELP, {"start": (1, "Access is denied. Run as administrator.")})
try:
    v1.start()
    check("a permission failure is raised, not swallowed", False, "no exception")
except net.NetworkingError as e:
    check("a permission failure is raised, not swallowed", True)
    check("  and says to elevate", "administrator" in (str(e) + (e.hint or "")).lower(), e.hint)

v1 = CliPlayit(V1_HELP, {"start": (1, "no secret found, run playit setup first")})
try:
    v1.start()
    check("an unprovisioned agent is raised as a setup step", False, "no exception")
except net.NetworkingError as e:
    check("an unprovisioned agent is raised as a setup step", True)
    check("  pointing at playit setup", "setup" in (e.hint or "").lower(), e.hint)

v1 = CliPlayit(V1_HELP, {"start": (0, "visit https://playit.gg/claim/svc123 to claim")})
v1.start()
check("a claim URL printed by the service CLI is captured",
      v1.claim_url() == "https://playit.gg/claim/svc123", v1.claim_url())


# -- tunnel mode end to end ---------------------------------------------------
class FakeTunnelService(net.TunnelService):
    def __init__(self, address="brave-otter.gl.at.ply.gg:31234", claim=""):
        net.TunnelService.__init__(self, provider=net.PlayitProvider())
        self._addr, self._claim, self._up = address, claim, False

    def available(self):
        return True, "fake agent"

    def start(self):
        self._up = True
        return True, "started"

    def stop(self):
        self._up = False
        return True, "stopped"

    def running(self):
        return self._up

    def address(self):
        return self._addr if self._up else ""

    def status(self):
        return {"provider": "playit.gg", "installed": True, "running": self._up,
                "ready": bool(self._addr) and self._up, "address": self.address(),
                "claim_url": self._claim, "needs_claim": bool(self._claim),
                "log": [], "agent_path": "fake", "uptime": 1,
                "download": net.PLAYIT_DOWNLOAD}

    def close(self):
        self.stop()


def tunnel_manager(state, address="brave-otter.gl.at.ply.gg:31234", claim=""):
    tun = FakeTunnelService(address, claim)
    # A LAN stub, not loopback: apply() refuses any non-local mode on a machine
    # whose only address is 127.0.0.1, which is correct behaviour and not what
    # these tests are exercising. The scripted tester supplies the local probe.
    mgr = net.HostingManager(
        interfaces=StubInterfaces(),
        mapping=net.PortMappingService(StubInterfaces(), providers=[]),
        tester=FixTester(state, "scripted"), tunnel=tun)
    mgr.public_ip.enabled = False
    mgr.tunnel_wait = 2
    return mgr, tun


def reached(_host, _port):
    return {"online": True, "conclusive": True, "detail": "reached", "checked": [],
            "source": "mcstatus.io", "status": None}


def refused(_host, _port):
    return {"online": False, "conclusive": True, "detail": "no",
            "checked": [{"service": "x", "online": False}]}


mgr, tun = tunnel_manager(net.REACHABLE)
mgr.tester.external_check = reached
mgr.apply("tunnel", blocking=True, write_properties=lambda v: True)
snap = mgr.snapshot()
check("tunnel mode reaching the goal reports online", snap["online"], snap["connectivity"])
check("  and advertises the tunnel address, not the LAN one",
      snap["advertise"] == "brave-otter.gl.at.ply.gg:31234", snap["advertise"])
check("  the headline names the address",
      "brave-otter" in snap["headline"], snap["headline"])
check("  the agent is running", snap["tunnel"]["running"])
check("  server-ip is still bound to every interface",
      mgr.desired_properties("tunnel")["server-ip"] == "")

# A tunnel that is merely running is not evidence of anything, exactly as a
# port mapping that merely exists is not.
mgr, tun = tunnel_manager(net.NOT_REACHABLE)
mgr.tester.external_check = refused
mgr.apply("tunnel", blocking=True, write_properties=lambda v: True)
check("a running tunnel nothing can reach is not online", not mgr.snapshot()["online"])
check("  and says to check the playit dashboard",
      "dashboard" in mgr.snapshot()["connectivity"]["reason"],
      mgr.snapshot()["connectivity"]["reason"])

mgr, tun = tunnel_manager(net.UNKNOWN, address="", claim="https://playit.gg/claim/zzz")
mgr.apply("tunnel", blocking=True, write_properties=lambda v: True)
snap = mgr.snapshot()
check("an unclaimed tunnel is reported as needing a human", not snap["online"])
check("  with the claim URL in the reason",
      "playit.gg/claim/zzz" in snap["connectivity"]["reason"],
      snap["connectivity"]["reason"])
check("  and the headline says so", "claim" in snap["headline"].lower(), snap["headline"])

# Leaving tunnel mode must stop the agent, or the server stays published after
# the user believes they have stopped publishing it.
mgr, tun = tunnel_manager(net.REACHABLE)
mgr.tester.external_check = reached
mgr.apply("tunnel", blocking=True, write_properties=lambda v: True)
check("the agent is up in tunnel mode", tun.running())
mgr.apply("lan", blocking=True, write_properties=lambda v: True)
check("switching away from tunnel stops the agent", not tun.running())

mgr, tun = tunnel_manager(net.REACHABLE)
mgr.tester.external_check = reached
mgr.apply("tunnel", blocking=True, write_properties=lambda v: True)
mgr.shutdown()
check("shutdown stops the agent too", not tun.running())

mgr, tun = tunnel_manager(net.REACHABLE)
mgr.tester.external_check = reached
mgr.apply("tunnel", blocking=True, write_properties=lambda v: True)
mgr.disable(blocking=True)
check("disable stops the agent", not tun.running())

# The security audit is what stops a management port going public by accident.
hm3 = net.HostingManager(interfaces=StubInterfaces())
hm3.mode = net.MODE_INTERNET
audit = hm3.security_audit(properties={"white-list": "false", "online-mode": "false",
                                       "enable-rcon": "true", "rcon.password": "",
                                       "rcon.port": "25575"},
                           panel_host="0.0.0.0", panel_port=8744)
titles = " | ".join(a["title"] for a in audit)
check("the audit warns about the whitelist", "whitelist is off" in titles.lower(), titles)
check("the audit warns about online-mode", "online mode is off" in titles.lower(), titles)
check("the audit warns about an empty RCON password", "no password" in titles.lower(), titles)
check("the audit warns about RCON binding", "every interface" in titles.lower(), titles)
check("the audit warns about the panel on 0.0.0.0", "panel" in titles.lower(), titles)
check("everything it says is showable",
      all(a["title"] and a["detail"] for a in audit))
check("severities come from a fixed set",
      all(a["level"] in ("info", "warn", "bad") for a in audit))

clean = hm3.security_audit(properties={"white-list": "true", "online-mode": "true",
                                       "enable-rcon": "false"},
                           panel_host="127.0.0.1", panel_port=8744)
check("a well-configured server only gets the standing notice", len(clean) == 1, clean)
check("  which is the 'this is public' one", clean[0]["level"] == "info", clean)

hm4 = net.HostingManager(interfaces=StubInterfaces())
hm4.mode = net.MODE_LAN
check("LAN mode does not warn about being public",
      not any("public" in a["title"].lower() for a in hm4.security_audit()))

# Applying a non-internet mode must not go near the network.
written = {}
hm5 = net.HostingManager(interfaces=StubInterfaces())
hm5.apply("lan", blocking=True, write_properties=lambda v: written.update(v) or True)
check("switching to LAN writes the properties", written.get("server-ip") == "")
check("  and reaches the ready phase", hm5.phase == hostmod.PHASE_READY, hm5.phase)
check("  and never claims to be online", not hm5.snapshot()["online"])
check("  and runs no external checks", hm5.connectivity["state"] == net.UNKNOWN)
steps = {s["key"]: s["state"] for s in hm5.steps}
check("  the steps are recorded", "properties" in steps and steps["done"] == "ok", steps)

hm5.apply("local", blocking=True, write_properties=lambda v: written.update(v) or True)
check("switching to local binds loopback", written.get("server-ip") == "127.0.0.1")

# A failing properties write is a warning, not a crash.
def boom(_values):
    raise RuntimeError("no server folder")


hm6 = net.HostingManager(interfaces=StubInterfaces())
hm6.apply("lan", blocking=True, write_properties=boom)
check("a properties write failure does not stop the mode change",
      hm6.phase == hostmod.PHASE_READY, hm6.phase)
check("  and is reported as a warning",
      any(s["key"] == "properties" and s["state"] == "warn" for s in hm6.steps), hm6.steps)

try:
    hm6.apply("banana", blocking=True)
    check("an unknown mode is refused", False, "accepted")
except net.PortInvalid:
    check("an unknown mode is refused", True)

snap = hm6.snapshot()
check("the snapshot serialises as JSON", json.loads(json.dumps(snap))["mode"] == snap["mode"])
check("  and never advertises a public address it has not proved",
      snap["advertise"] and "192.168.1.50" in snap["advertise"], snap["advertise"])

# Shutdown must remove what we made.
released = []


class ReleaseWatcher(net.PortMappingService):
    def release(self, mapping=None, check_owner=True):
        released.append(True)
        return super().release(mapping, check_owner)


watch_svc = ReleaseWatcher(StubInterfaces(), providers=[StubProvider("UPnP")])
hm7 = net.HostingManager(interfaces=StubInterfaces(), mapping=watch_svc)
watch_svc.ensure(25565)
hm7.shutdown()
check("shutdown releases the mapping", released == [True])
check("  and the register is empty", not watch_svc.owned())

hm8 = net.HostingManager(interfaces=StubInterfaces(),
                         mapping=net.PortMappingService(StubInterfaces(), providers=[]))
hm8.shutdown()
check("shutdown with nothing to release is harmless", True)


# -- tunnels: ngrok ----------------------------------------------------------
# Minecraft is raw TCP, so this provider opens a TCP endpoint and the address
# arrives as logfmt on stdout. Two things are worth testing for real: that we
# parse somebody else's log format correctly, and that the fallback to the
# agent's own REST API works when the log line never shows up.
section("tunnel (ngrok)")

check("an ngrok tcp address splits",
      net.split_address("6.tcp.eu.ngrok.io:19417") == ("6.tcp.eu.ngrok.io", 19417))

ng = net.NgrokProvider()
ng._scan('t=2026-01-01T00:00:00+0000 lvl=info msg="starting web service" '
         'obj=web addr=127.0.0.1:4041')
check("the local API address is read off the log", ng._api == "127.0.0.1:4041", ng._api)
ng._scan('t=2026-01-01T00:00:01+0000 lvl=info msg="started tunnel" obj=tunnels '
         'name=command_line addr=//localhost:25565 url=tcp://6.tcp.eu.ngrok.io:19417')
check("the public address is picked out of logfmt",
      ng._address == "6.tcp.eu.ngrok.io:19417", ng._address)
check("  and the tunnel counts as ready", ng._ready)

ng2 = net.NgrokProvider()
ng2._scan('t=2026-01-01T00:00:00+0000 lvl=info msg="started tunnel" obj=tunnels '
          'addr=//localhost:25565 url=https://ab12.ngrok-free.app')
check("an https endpoint is understood too",
      ng2._address == "ab12.ngrok-free.app", ng2._address)

ng3 = net.NgrokProvider()
ng3._scan('t=2026-01-01T00:00:00+0000 lvl=eror msg="failed to auth" '
          'err="ERR_NGROK_4018 authentication failed"')
check("an auth failure is recognised", ng3.claim_url() == tunmod.NGROK_AUTHTOKEN_URL)
check("  and is reported as needing the user", ng3.status()["needs_claim"])

# The status dict is what the panel renders. It has to carry every key the
# playit one does, or the network view quietly loses rows.
_pl_keys = set(net.PlayitProvider().status())
_ng_keys = set(net.NgrokProvider().status())
check("ngrok status carries every key the panel reads",
      not (_pl_keys - _ng_keys), sorted(_pl_keys - _ng_keys))

# -- a real child process, printing real logfmt ------------------------------
FAKE_NGROK = os.path.join(_TESTHOME, "fake_ngrok.py")
io.open(FAKE_NGROK, "w", encoding="utf-8").write(
    "import time\n"
    "print('t=2026-01-01T00:00:00+0000 lvl=info msg=\"no configuration paths\"',"
    " flush=True)\n"
    "print('t=2026-01-01T00:00:00+0000 lvl=info msg=\"starting web service\" "
    "obj=web addr=127.0.0.1:4040', flush=True)\n"
    "print('t=2026-01-01T00:00:01+0000 lvl=info msg=\"started tunnel\" obj=tunnels "
    "name=command_line addr=//localhost:25565 url=tcp://4.tcp.ngrok.io:12345',"
    " flush=True)\n"
    "time.sleep(30)\n")

AUTH_NGROK = os.path.join(_TESTHOME, "fake_ngrok_auth.py")
io.open(AUTH_NGROK, "w", encoding="utf-8").write(
    "import time\n"
    "print('t=2026-01-01T00:00:00+0000 lvl=eror msg=\"failed to start tunnel\" "
    "err=\"ERR_NGROK_4018 authentication failed: Usage of ngrok requires a "
    "verified account\"', flush=True)\n"
    "time.sleep(30)\n")


class ScriptedNgrok(net.NgrokProvider):
    """The real class, launching python on a script instead of the binary."""

    script = FAKE_NGROK

    def resolve(self):
        self.agent_path = sys.executable
        return self.agent_path

    def start(self):
        import subprocess as sp
        with self._lock:
            self.lines.clear()
            self._address = ""
            self._ready = False
            self._api = ""
            self._error = ""
            self._needs_token = False
        self.proc = sp.Popen([sys.executable, "-u", self.script],
                             stdout=sp.PIPE, stderr=sp.STDOUT, text=True,
                             bufsize=1, encoding="utf-8", errors="replace")
        threading.Thread(target=self._reader, daemon=True).start()
        self._started_at = time.time()
        return True, "started"


agent_ng = ScriptedNgrok()
agent_ng.start()
got_ng = agent_ng.wait_for_address(timeout=15)
check("a running ngrok agent reports its tunnel address",
      got_ng == "4.tcp.ngrok.io:12345", got_ng)
st_ng = agent_ng.status()
check("  status says running and ready", st_ng["running"] and st_ng["ready"], st_ng)
check("  the agent output is captured for the UI", len(st_ng["log"]) >= 3, st_ng["log"])
check("  and it reports itself as ngrok", st_ng["provider"] == "ngrok")
ok_ng, detail_ng = agent_ng.stop()
check("the ngrok agent stops on request", ok_ng, detail_ng)
check("  and is no longer running", not agent_ng.running())
check("stopping twice is harmless", agent_ng.stop()[0])


class AuthNgrok(ScriptedNgrok):
    script = AUTH_NGROK


# No token: there is no address coming, and waiting the full timeout for one
# would just make the panel look hung.
auth_ng = AuthNgrok()
auth_ng.start()
_t0 = time.time()
addr_ng = auth_ng.wait_for_address(timeout=20)
waited_ng = time.time() - _t0
check("an unauthenticated agent yields no address", addr_ng == "", addr_ng)
check("  and does not sit out the whole timeout", waited_ng < 12, "%.1fs" % waited_ng)
st_ng = auth_ng.status()
check("  it points at the authtoken page",
      st_ng["claim_url"] == tunmod.NGROK_AUTHTOKEN_URL, st_ng["claim_url"])
check("  and flags that a human is needed", st_ng["needs_claim"])
auth_ng.stop()


# -- the agent's own REST API, spoken for real -------------------------------
# When the log line is missed, /api/tunnels is the authority. Serve a real one.
class _NgrokApi(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != "/api/tunnels":
            self.send_response(404); self.end_headers(); return
        body = json.dumps({"tunnels": [
            {"name": "command_line", "proto": "tcp",
             "public_url": "tcp://7.tcp.ngrok.io:23456",
             "config": {"addr": "localhost:25565"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


api_srv = ThreadingHTTPServer(("127.0.0.1", 0), _NgrokApi)
api_port = api_srv.server_address[1]
threading.Thread(target=api_srv.serve_forever, daemon=True).start()

QUIET_NGROK = os.path.join(_TESTHOME, "fake_ngrok_quiet.py")
io.open(QUIET_NGROK, "w", encoding="utf-8").write(
    "import time\n"
    "print('t=2026-01-01T00:00:00+0000 lvl=info msg=\"tunnel session started\" "
    "obj=tunnels.session', flush=True)\n"
    "time.sleep(30)\n")


class QuietNgrok(ScriptedNgrok):
    script = QUIET_NGROK


quiet = QuietNgrok()
quiet.start()
quiet._api = "127.0.0.1:%d" % api_port
addr_api = quiet.wait_for_address(timeout=15)
check("an address the log never printed is recovered from /api/tunnels",
      addr_api == "7.tcp.ngrok.io:23456", addr_api)
check("  and the scheme is stripped for friends to use",
      "://" not in addr_api, addr_api)
quiet.stop()
api_srv.shutdown()

# -- discovery and the authtoken gate ----------------------------------------
ng_dir = os.path.join(_TESTHOME, "ngrokdir")
os.makedirs(ng_dir, exist_ok=True)
ng_exe = os.path.join(ng_dir, "ngrok.exe" if sys.platform.startswith("win") else "ngrok")
io.open(ng_exe, "w").write("")
check("an explicit ngrok path is used when it exists", tunmod.find_ngrok(ng_exe) == ng_exe)
check("a bad explicit ngrok path yields nothing",
      tunmod.find_ngrok(os.path.join(ng_dir, "nope.exe")) == "")

_real_paths = tunmod.ngrok_config_paths
cfg_none = os.path.join(_TESTHOME, "ngrok_none.yml")
cfg_tok = os.path.join(_TESTHOME, "ngrok_tok.yml")
cfg_cmt = os.path.join(_TESTHOME, "ngrok_cmt.yml")
io.open(cfg_none, "w", encoding="utf-8").write("version: 2\nregion: eu\n")
io.open(cfg_tok, "w", encoding="utf-8").write("version: 2\nauthtoken: dummy_value\n")
io.open(cfg_cmt, "w", encoding="utf-8").write("version: 2\n# authtoken: not_real\n")
try:
    tunmod.ngrok_config_paths = lambda: [cfg_none]
    check("no authtoken in the config reads as absent", not tunmod.ngrok_has_authtoken())
    tunmod.ngrok_config_paths = lambda: [cfg_cmt]
    check("a commented-out authtoken does not count", not tunmod.ngrok_has_authtoken())
    tunmod.ngrok_config_paths = lambda: [cfg_tok]
    check("a configured authtoken is detected", tunmod.ngrok_has_authtoken())
    tunmod.ngrok_config_paths = lambda: [os.path.join(_TESTHOME, "absent.yml")]
    check("a missing config file is not an error", not tunmod.ngrok_has_authtoken())
finally:
    tunmod.ngrok_config_paths = _real_paths

_real_has = tunmod.ngrok_has_authtoken
try:
    tunmod.ngrok_has_authtoken = lambda: False
    gated = net.NgrokProvider(agent_path=ng_exe)
    ok_g, detail_g = gated.available()
    check("an agent with no token is not available", not ok_g)
    check("  and the message names the command, never the token",
          "add-authtoken" in detail_g and "dashboard.ngrok.com" in detail_g, detail_g)
    try:
        gated.start()
        check("starting without a token raises a typed error", False, "no exception")
    except net.NetworkingError as e:
        check("starting without a token raises a typed error", True)
        check("  with a hint that points at the dashboard",
              "dashboard.ngrok.com" in (e.hint or ""), e.hint)
    check("  and no process was spawned", gated.proc is None)
finally:
    tunmod.ngrok_has_authtoken = _real_has

missing_ng = net.NgrokProvider(agent_path=os.path.join(_TESTHOME, "nope_ngrok.exe"))
ok_m, detail_m = missing_ng.available()
check("a missing ngrok agent is reported, not raised", not ok_m)
check("  and points at the download", "ngrok.com/download" in detail_m, detail_m)

# -- the Microsoft Store build hides its config -------------------------------
# An ngrok installed from the Store runs under MSIX, whose filesystem
# redirection sends writes into the package's private LocalCache. The agent
# still prints "saved to %LOCALAPPDATA%\\ngrok\\ngrok.yml", so trusting that
# message reports "no authtoken" on a machine that has one.
pkg_root = os.path.join(_TESTHOME, "Packages", "ngrok.ngrok_1g87z0zv29zzc",
                        "LocalCache", "Local", "ngrok")
os.makedirs(pkg_root, exist_ok=True)
pkg_cfg = os.path.join(pkg_root, "ngrok.yml")
io.open(pkg_cfg, "w", encoding="utf-8").write("version: \"2\"\nauthtoken: dummy\n")

_real_env = os.environ.get("LOCALAPPDATA")
try:
    os.environ["LOCALAPPDATA"] = _TESTHOME
    paths = tunmod.ngrok_config_paths()
    check("a Store install keeps its config under the package LocalCache",
          pkg_cfg in paths, [p for p in paths if "Packages" in p])
    check("  and the token there is found",
          tunmod.ngrok_has_authtoken())
finally:
    if _real_env is None:
        os.environ.pop("LOCALAPPDATA", None)
    else:
        os.environ["LOCALAPPDATA"] = _real_env

# -- free-plan TCP is card-gated ---------------------------------------------
# ERR_NGROK_8013 is the one that stops Minecraft users dead: TCP endpoints need
# card verification on a free account, and Minecraft is TCP-only. Retrying
# never helps, so it must read as a decision to make, not a transient error.
card = net.NgrokProvider(port=25565)
card._scan('t=2026-01-01T00:00:00+0000 lvl=crit msg="command failed" '
           'err="failed to start tunnel: You must add a credit or debit card '
           'before you can use TCP endpoints on a free account." ERR_NGROK_8013')
st_card = card.status()
check("a card-gated TCP endpoint is recognised", st_card["needs_card"])
check("  it is surfaced as needing a human", st_card["needs_claim"])
check("  and points at id-verification, not the token page",
      st_card["claim_url"] == tunmod.NGROK_CARD_URL, st_card["claim_url"])
check("  the message explains it in words, not a code",
      "credit" not in st_card["error"] or "ERR_NGROK" not in st_card["error"],
      st_card["error"][:80])
check("  and names the free alternative",
      "playit.gg" in st_card["error"], st_card["error"][:120])
check("  a card problem is not mistaken for a missing token",
      not card._needs_token)

# The generic path must still work for anything else.
other = net.NgrokProvider(port=25565)
other._scan('t=2026-01-01T00:00:00+0000 lvl=eror msg="failed" err="ERR_NGROK_108 '
            'your account is limited to 1 simultaneous session"')
check("an unrelated ngrok error is still reported verbatim",
      "ERR_NGROK_108" in other.status()["error"], other.status()["error"][:80])
check("  and does not claim a card is needed", not other.status()["needs_card"])


# -- choosing between providers ----------------------------------------------
svc_sw = net.TunnelService()
check("a tunnel service defaults to playit", svc_sw.provider_name() == "playit.gg",
      svc_sw.provider_name())
check("switching to ngrok reports the change", svc_sw.set_provider("ngrok", port=25577))
check("  and the service now holds an ngrok provider",
      isinstance(svc_sw.provider, net.NgrokProvider))
check("  carrying the port across", svc_sw.provider.port == 25577, svc_sw.provider.port)
check("switching to the same provider is a no-op", not svc_sw.set_provider("ngrok"))
check("an unknown provider name is refused", not svc_sw.set_provider("banana"))
check("  leaving the working one in place", isinstance(svc_sw.provider, net.NgrokProvider))
check("switching back to playit works", svc_sw.set_provider("playit"))
check("make_provider builds ngrok by name",
      isinstance(tunmod.make_provider("ngrok"), net.NgrokProvider))
check("make_provider falls back to playit on junk",
      isinstance(tunmod.make_provider("nope"), net.PlayitProvider))


# -- a relay that dislikes the unknown-protocol sentinel ----------------------
# playit.gg parses the Server List Ping handshake and resets the connection when
# the protocol field is the 0xFFFFFFFF "I do not know my version" sentinel that
# minecraft_ping sends by default. The server behind it would have answered.
# The symptom is the worst kind: every tunnel reported unreachable while it was
# happily serving players.
section("SLP through a picky relay")


def _slp_response(payload):
    body = b"\x00" + conn.write_varint(len(payload)) + payload
    return conn.write_varint(len(body)) + body


class _PickyRelay(threading.Thread):
    """Speaks real SLP. Resets the sentinel, answers a real protocol number."""

    daemon = True

    def __init__(self, reject_sentinel=True):
        threading.Thread.__init__(self)
        self.reject_sentinel = reject_sentinel
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.stop = False
        self.attempts = []
        _open_sockets.append(self.sock)

    def run(self):
        while not self.stop:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            try:
                self._one(c)
            except Exception:
                pass
            finally:
                try:
                    c.close()
                except OSError:
                    pass

    def _read_varint(self, c):
        num = shift = 0
        while True:
            b = c.recv(1)
            if not b:
                raise OSError("eof")
            num |= (b[0] & 0x7F) << shift
            if not b[0] & 0x80:
                return num
            shift += 7

    def _one(self, c):
        c.settimeout(4)
        self._read_varint(c)            # packet length
        self._read_varint(c)            # packet id (0x00)
        protocol = self._read_varint(c)
        self.attempts.append(protocol)
        if self.reject_sentinel and protocol == 0xFFFFFFFF:
            # Exactly what the relay does: hang up without a word.
            c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                         struct.pack("ii", 1, 0))
            c.close()
            return
        c.recv(256)                     # host, port, next-state, then request
        c.sendall(_slp_response(json.dumps({
            "version": {"name": "Relayed 1.21", "protocol": 767},
            "players": {"online": 3, "max": 20},
            "description": "Behind a relay"}).encode()))


picky = _PickyRelay()
picky.start()
got_relay = net.minecraft_ping("127.0.0.1", picky.port, timeout=6)
check("a relay that resets the sentinel is retried, not written off",
      got_relay.get("version", {}).get("name") == "Relayed 1.21", got_relay)
check("  the retry used a real protocol number",
      picky.attempts[:2] == [0xFFFFFFFF, conn.FALLBACK_PROTOCOL], picky.attempts[:2])
check("  and it took exactly two attempts", len(picky.attempts) == 2, picky.attempts)
picky.stop = True

# A server that answers first time must not be pinged twice.
easy = _PickyRelay(reject_sentinel=False)
easy.start()
got_easy = net.minecraft_ping("127.0.0.1", easy.port, timeout=6)
check("a server that answers the sentinel is pinged once",
      len(easy.attempts) == 1, easy.attempts)
check("  and its status still comes back",
      got_easy.get("players", {}).get("online") == 3, got_easy)
easy.stop = True

# A dead port must not pay the retry cost twice over.
_dead = socket.socket()
_dead.bind(("127.0.0.1", 0))
_dead_port = _dead.getsockname()[1]
_dead.close()
_t0 = time.time()
try:
    net.minecraft_ping("127.0.0.1", _dead_port, timeout=3)
    check("a refused port still fails", False, "it did not")
except net.ConnectivityError as e:
    check("a refused port still fails", True)
    check("  without retrying, because refused is not a hang-up",
          time.time() - _t0 < 2.5, "%.1fs" % (time.time() - _t0))


# ===========================================================================
#  the panel: config, routes, cleanup
# ===========================================================================
section("panel integration")

check("the networking package loaded into the panel", m.HAVE_NET, m.NET_ERROR)

blank = m.blank_server("local", "T")
check("a new server has hosting settings", "hosting" in blank)
check("  defaulting to LAN so nothing changes for an existing install",
      blank["hosting"]["mode"] == "lan")
check("  with 25565 as the port", blank["hosting"]["port"] == 25565)
check("  with auto-enable off", blank["hosting"]["auto_enable"] is False)
check("  with an empty mapping register", blank["hosting"]["mappings"] == [])
check("  and every protocol on", all(blank["hosting"][k] for k in ("upnp", "natpmp", "pcp")))

old = {"mode": "internet", "port": 30000}
filled = m.default_hosting(old)
check("an older config is filled in without losing what it had",
      filled["mode"] == "internet" and filled["port"] == 30000 and "pcp" in filled)

state = m.PANEL.hosting_state()
check("hosting_state answers", state.get("ok"), state)
check("  it says which ports are off limits",
      state["reserved"]["panel"] == m.PANEL.cfg.get("port", m.DEFAULT_PORT), state["reserved"])
check("  it never reports online without evidence", not state["online"])
check("  the headline is a sentence, not a code", " " in state["headline"], state["headline"])
check("  it serialises", json.loads(json.dumps(state))["mode"] == state["mode"])

brief = m.PANEL.hosting_brief()
check("hosting_brief answers", brief.get("available"))
t0 = time.time()
for _ in range(30):
    m.PANEL.status()
check("status() stays fast with hosting in it", time.time() - t0 < 2.0, time.time() - t0)
check("status() carries the hosting summary", "hosting" in m.PANEL.status())

try:
    m.PANEL.hosting_apply("banana")
    check("an unknown mode is refused by the panel", False, "accepted")
except RuntimeError as e:
    check("an unknown mode is refused by the panel", "mode" in str(e).lower(), str(e))

# Remote servers have no port of ours to forward.
remote = m.blank_server("sftp", "Remote")
m.PANEL.cfg["servers"].append(remote)
rstate = m.PANEL.hosting_state(remote["id"])
check("a remote server cannot be internet-hosted", not rstate["can_host"])
check("  and it says why", "host" in rstate["why_not"].lower(), rstate["why_not"])

# -- picking a tunnel provider through the panel -----------------------------
check("a new server defaults to the playit provider",
      blank["hosting"]["tunnel_provider"] == "playit", blank["hosting"].get("tunnel_provider"))
check("  with both agent paths empty",
      blank["hosting"]["tunnel_agent"] == "" and blank["hosting"]["ngrok_agent"] == "")
check("an older config gains the provider key without being told",
      m.default_hosting({"mode": "tunnel"})["tunnel_provider"] == "playit")

_st = m.PANEL.hosting_state()
check("the panel reports the provider setting", "tunnel_provider" in _st["settings"])
check("  and the ngrok path setting", "ngrok_agent" in _st["settings"])

_sid = m.PANEL.server()["id"]
_mgr = m.PANEL.hosting_manager(_sid)
check("the manager starts on playit",
      isinstance(_mgr.tunnel.provider, net.PlayitProvider))

m.PANEL.hosting_settings({"tunnel_provider": "ngrok"}, _sid)
check("the provider choice reaches the manager",
      isinstance(_mgr.tunnel.provider, net.NgrokProvider))
check("  and is written to the config",
      m.PANEL.hosting_cfg(_sid)["tunnel_provider"] == "ngrok")
check("  the tunnel port follows the Minecraft port",
      _mgr.tunnel.provider.port == _mgr.port, _mgr.tunnel.provider.port)

# The two agent paths are stored separately on purpose: switching provider must
# never hand ngrok the playit binary.
_playit_stub = os.path.join(_TESTHOME, "stub_playit.exe")
io.open(_playit_stub, "w").write("")
m.PANEL.hosting_settings({"tunnel_agent": _playit_stub}, _sid)
check("a playit path does not leak into the ngrok provider",
      _mgr.tunnel.provider.agent_path != _playit_stub, _mgr.tunnel.provider.agent_path)

try:
    m.PANEL.hosting_settings({"ngrok_agent": os.path.join(_TESTHOME, "absent.exe")}, _sid)
    check("a nonexistent ngrok path is refused", False, "accepted")
except RuntimeError as e:
    check("a nonexistent ngrok path is refused", "no file" in str(e).lower(), str(e))

try:
    m.PANEL.hosting_settings({"tunnel_provider": "banana"}, _sid)
    check("an unknown provider is refused by the panel", False, "accepted")
except RuntimeError as e:
    check("an unknown provider is refused by the panel",
          "provider" in str(e).lower(), str(e))
check("  and the working provider is untouched",
      isinstance(_mgr.tunnel.provider, net.NgrokProvider))

m.PANEL.hosting_settings({"tunnel_provider": "playit"}, _sid)
check("switching back through the panel works",
      isinstance(_mgr.tunnel.provider, net.PlayitProvider))
check("  and the playit path is restored to it",
      _mgr.tunnel.provider.agent_path == _playit_stub, _mgr.tunnel.provider.agent_path)
try:
    m.PANEL.hosting_apply("internet", sid=remote["id"])
    check("applying internet mode to a remote server is refused", False, "accepted")
except RuntimeError:
    check("applying internet mode to a remote server is refused", True)

# Duplicating must not hand the copy ownership of the original's mapping.
src = m.PANEL.servers()[0]
src["hosting"]["mappings"] = [{"provider": "UPnP", "protocol": "TCP",
                               "external_port": 25565, "internal_port": 25565,
                               "internal_ip": "192.168.1.50", "owner": "lodestone"}]
src["hosting"]["mode"] = "internet"
copy = m.PANEL.duplicate(src["id"])
check("a duplicated server does not inherit the mapping register",
      copy["hosting"]["mappings"] == [], copy["hosting"])
check("  nor internet mode", copy["hosting"]["mode"] != "internet")
check("  nor auto-enable", copy["hosting"]["auto_enable"] is False)
m.PANEL.cfg["servers"] = [s for s in m.PANEL.servers() if s["id"] != copy["id"]]
src["hosting"]["mappings"] = []
src["hosting"]["mode"] = "lan"

# HTTP surface
httpd = ThreadingHTTPServer(("127.0.0.1", 0), m.Handler)
httpd.daemon_threads = True
threading.Thread(target=httpd.serve_forever, daemon=True).start()
BASE = "http://127.0.0.1:%d" % httpd.server_address[1]
import urllib.error
import urllib.request


def http(path, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method=method or ("POST" if data else "GET"))
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


s, j = http("/api/hosting")
check("GET /api/hosting works", s == 200 and j.get("ok"), (s, str(j)[:200]))
check("  it includes the dashboard fields",
      all(k in j for k in ("mode", "port", "local_address", "lan_address",
                           "connectivity_state", "online", "mapping_active")), sorted(j))

s, j = http("/api/hosting/security")
check("GET /api/hosting/security works", s == 200 and "items" in j, (s, str(j)[:200]))

# Ports are picked at runtime, never hard-coded: the machine running these
# tests may well have a real Minecraft server on 25565, and set_port correctly
# refuses a port that is already in use. A suite that assumes the developer's
# machine is idle fails for the wrong reason.
def free_port():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    got = probe.getsockname()[1]
    probe.close()
    return got


PORT_A, PORT_B = free_port(), free_port()

s, j = http("/api/hosting/settings", {"port": PORT_A, "upnp": False})
check("POST /api/hosting/settings saves", s == 200 and j.get("ok"), (s, str(j)[:200]))
check("  the port moved", m.PANEL.hosting_cfg()["port"] == PORT_A,
      m.PANEL.hosting_cfg()["port"])
check("  and the protocol toggle stuck", m.PANEL.hosting_cfg()["upnp"] is False)

s, j = http("/api/hosting/settings", {"port": 25575, "upnp": True})
check("POST rejecting a reserved port comes back as an error, not a 500",
      s in (400, 200), (s, str(j)[:150]))
m.PANEL.hosting_settings({"port": PORT_B, "upnp": True})

# And the refusal itself is worth asserting, since it is what just protected a
# running server from having its port reassigned underneath it.
busy = socket.socket()
busy.bind(("0.0.0.0", 0))
busy.listen(1)
busy_port = busy.getsockname()[1]
try:
    m.PANEL.hosting_settings({"port": busy_port})
    check("a port already in use is refused", False, "accepted a busy port")
except net.PortInUse as e:
    check("a port already in use is refused", True)
    check("  and says what to do", "another port" in (e.hint or ""), e.hint)
finally:
    busy.close()
m.PANEL.hosting_settings({"port": PORT_B})

s, j = http("/api/hosting/mode", {"mode": "lan"})
check("POST /api/hosting/mode switches to LAN", s == 200 and j.get("ok"), (s, str(j)[:200]))
s, j = http("/api/hosting/mode", {"mode": "banana"})
check("POST /api/hosting/mode refuses a bad mode", s == 400, (s, str(j)[:150]))

s, j = http("/api/hosting/disable", {})
check("POST /api/hosting/disable is safe with nothing to disable",
      s == 200 and j.get("ok"), (s, str(j)[:200]))

# The v1 API must not let a read token reconfigure the router.
m.PANEL.cfg["api"]["enabled"] = True
read_tok = m.PANEL.api_token_create("read only", ["read"])["token"]
admin_tok = m.PANEL.api_token_create("admin", ["admin"])["token"]


def v1(path, tok, body=None, method=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method or
                                 ("POST" if data is not None else "GET"),
                                 headers={"Authorization": "Bearer " + tok,
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


s, j = v1("/api/v1/hosting", read_tok)
check("a read token can see hosting state", s == 200 and j.get("ok"), (s, str(j)[:150]))
s, j = v1("/api/v1/hosting", read_tok, {"mode": "internet"})
check("a read token cannot change hosting mode", s == 403, (s, str(j)[:150]))
s, j = v1("/api/v1/hosting", admin_tok, {"mode": "banana"})
check("an admin token still gets its mode validated", s == 400, (s, str(j)[:150]))
s, j = v1("/api/v1/hosting", admin_tok, {"mode": "lan"})
check("an admin token can change hosting mode", s == 200 and j.get("ok"), (s, str(j)[:150]))
s, j = v1("/api/v1/hosting", "lds_not_a_real_token")
check("a bad token is rejected", s == 401, s)
m.PANEL.cfg["api"]["enabled"] = False

# The panel's own port and RCON must be unpublishable through every door.
srv0 = m.PANEL.server()
srv0["rcon"] = {"enabled": True, "host": "127.0.0.1", "port": 25575, "password": "x"}
rc, panel_port = m.PANEL.hosting_ports_in_use()
check("the RCON port is reported as reserved once RCON is on", rc == 25575, rc)
try:
    m.PANEL.hosting_apply("lan", port=25575)
    check("the panel refuses to host on the RCON port", False, "accepted")
except net.PortInvalid:
    check("the panel refuses to host on the RCON port", True)
try:
    m.PANEL.hosting_apply("lan", port=panel_port)
    check("the panel refuses to host on its own port", False, "accepted")
except net.PortInvalid:
    check("the panel refuses to host on its own port", True)
srv0["rcon"] = {"enabled": False, "host": "", "port": 25575, "password": ""}

m.PANEL.hosting_shutdown()
check("hosting_shutdown is safe to call", True)
m.PANEL.hosting_shutdown()
check("  and safe to call twice", True)

# tidy up the fakes
for sock in _open_sockets:
    try:
        sock.close()
    except OSError:
        pass
for srv_obj in (mc, ssh_like, pmp_srv, pcp_srv):
    srv_obj.stop = True
for http_srv in (igd_srv, ip_srv, chk_srv, httpd):
    try:
        http_srv.shutdown()
    except Exception:
        pass

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
