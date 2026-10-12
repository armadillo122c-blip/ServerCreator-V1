"""PcpProvider — Port Control Protocol, RFC 6887.

NAT-PMP's successor. Same UDP port (5351), a longer fixed-layout packet, and
two things that matter here:

  * it carries the client's own address in the request, so a router behind a
    second NAT can tell you so (result 12, ADDRESS_MISMATCH) instead of quietly
    mapping a port that leads nowhere;
  * it is the protocol carrier-grade NAT boxes actually implement, so on some
    CGNAT lines this is the only thing that can open a real hole.

Both directions are 60 bytes.

  request  header 24  [ver=2][R=0|opcode][rsv u16][lifetime u32][client IP 16]
           MAP     36 [nonce 12][proto u8][rsv 3][int port u16]
                      [suggested ext port u16][suggested ext IP 16]

  response header 24  [ver=2][R=1|opcode][rsv u8][result u8][lifetime u32]
                      [epoch u32][rsv 12]
           MAP     36 [nonce 12][proto u8][rsv 3][int port u16]
                      [assigned ext port u16][assigned ext IP 16]

The nonce is the mapping's identity: refreshing or deleting requires the same
12 bytes back, which is precisely what stops one client tearing down another's
mapping. We keep it on the PortMapping so cleanup still works after a restart.
"""

import ipaddress
import os
import socket
import struct
import time

from .errors import (DiscoveryFailed, MappingRejected, NetworkingError,
                     NotAuthorized, ProviderUnavailable, UnsupportedOperation)
from .interfaces import TCP, PortMapping, PortMappingProvider

PCP_PORT = 5351
PCP_VERSION = 2

OP_ANNOUNCE = 0
OP_MAP = 1
OP_PEER = 2

PROTO_TCP = 6
PROTO_UDP = 17

REQUEST_LEN = 60
RESPONSE_LEN = 60

RESULT_TEXT = {
    0: "Success.",
    1: "The router does not speak PCP version 2.",
    2: "The router refused: PCP is present but not authorised for this client.",
    3: "The router called the request malformed.",
    4: "The router does not support port mapping over PCP.",
    5: "The router rejected an option in the request.",
    6: "The router called an option malformed.",
    7: "The router has no working internet connection.",
    8: "The router is out of mapping resources.",
    9: "The router will not map that protocol.",
    10: "This machine has used up its mapping quota on the router.",
    11: "The router cannot provide the external address that was asked for.",
    12: "The address in the request did not match what the router saw — there "
        "is another NAT between this machine and the gateway.",
    13: "Too many remote peers for one mapping.",
}

RETRY_DELAYS = (0.25, 0.5, 1.0)


def pack_addr(ip):
    """PCP carries every address as 16 bytes. IPv4 goes in as ::ffff:a.b.c.d."""
    if not ip:
        return b"\x00" * 16
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return b"\x00" * 16
    if addr.version == 4:
        return b"\x00" * 10 + b"\xff\xff" + addr.packed
    return addr.packed


def unpack_addr(raw):
    if len(raw) != 16:
        return ""
    if raw[:10] == b"\x00" * 10 and raw[10:12] == b"\xff\xff":
        return socket.inet_ntoa(raw[12:16])
    if raw == b"\x00" * 16:
        return ""
    try:
        return str(ipaddress.IPv6Address(raw))
    except ValueError:
        return ""


def build_map_request(client_ip, nonce, protocol, internal_port,
                      external_port, external_ip="", lifetime=3600):
    proto = PROTO_TCP if (protocol or TCP).upper() == TCP else PROTO_UDP
    header = struct.pack("!BBHI", PCP_VERSION, OP_MAP, 0, int(lifetime)) \
        + pack_addr(client_ip)
    body = nonce + struct.pack("!B3xHH", proto, int(internal_port),
                               int(external_port)) + pack_addr(external_ip)
    return header + body


def parse_map_response(data):
    """Returns a dict, or raises ValueError if this is not a PCP MAP reply."""
    if len(data) < 24:
        raise ValueError("short PCP response")
    version, r_op, _, result, lifetime, epoch = struct.unpack("!BBBBII", data[:12])
    if version != PCP_VERSION:
        raise ValueError("wrong PCP version %d" % version)
    if not r_op & 0x80:
        raise ValueError("not a PCP response")
    out = {"opcode": r_op & 0x7f, "result": result,
           "lifetime": lifetime, "epoch": epoch}
    if len(data) >= RESPONSE_LEN and out["opcode"] == OP_MAP:
        # MAP payload starts at 24: nonce 12, protocol 1, reserved 3,
        # internal port 2, assigned external port 2, assigned external IP 16.
        nonce = data[24:36]
        proto, internal_port, external_port = struct.unpack("!B3xHH", data[36:44])
        out.update({"nonce": nonce, "protocol": proto,
                    "internal_port": internal_port,
                    "external_port": external_port,
                    "external_ip": unpack_addr(data[44:60])})
    return out


class PcpProvider(PortMappingProvider):

    name = "PCP"
    protocol_name = "PCP (RFC 6887)"

    def __init__(self, client_ip="", gateway="", gateways=(), timeout=1.0):
        self.client_ip = client_ip
        self.gateway = gateway
        self.gateways = [g for g in ([gateway] if gateway else []) + list(gateways) if g]
        self.timeout = timeout
        self.port = PCP_PORT
        self._epoch = 0
        self._working = gateway or ""
        self._last_external_ip = ""

    # -- transport -----------------------------------------------------------
    def _exchange(self, payload, gateway):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(self.timeout)
            if self.client_ip and not self.client_ip.startswith("127."):
                try:
                    sock.bind((self.client_ip, 0))
                except OSError:
                    pass
            for delay in RETRY_DELAYS:
                try:
                    sock.sendto(payload, (gateway, self.port))
                except OSError as e:
                    raise ProviderUnavailable("Could not send to %s (%s)." % (gateway, e))
                deadline = time.time() + delay
                while time.time() < deadline:
                    sock.settimeout(max(0.05, min(self.timeout, deadline - time.time())))
                    try:
                        data, addr = sock.recvfrom(1100)
                    except socket.timeout:
                        break
                    except ConnectionResetError:
                        raise DiscoveryFailed("%s is not listening for PCP." % gateway)
                    except OSError:
                        break
                    if addr and addr[0] != gateway:
                        continue
                    if len(data) < 4:
                        continue
                    if data[0] != PCP_VERSION:
                        # A NAT-PMP-only box answers version 0 / result 1. That is
                        # a definite "no PCP here", not a lost packet.
                        raise UnsupportedOperation(
                            "%s answers NAT-PMP but not PCP." % gateway)
                    return data
            raise DiscoveryFailed("%s did not answer PCP." % gateway)
        finally:
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass

    def _try_gateways(self, payload):
        order = ([self._working] if self._working else []) + \
                [g for g in self.gateways if g != self._working]
        if not order:
            raise ProviderUnavailable(
                "No default gateway found, so there is nowhere to send PCP.")
        last = None
        for gw in order:
            try:
                data = self._exchange(payload, gw)
                self._working = gw
                self.gateway = gw
                return data
            except NetworkingError as e:
                last = e
        raise last or DiscoveryFailed("No gateway answered PCP.")

    @staticmethod
    def _check_result(code, what):
        if code == 0:
            return
        text = RESULT_TEXT.get(code, "The router returned PCP error %d." % code)
        msg = "%s failed: %s" % (what, text)
        if code == 2:
            raise NotAuthorized(msg, "Turn PCP on in the router, or forward the "
                                     "port by hand.")
        if code in (1, 4, 9):
            raise UnsupportedOperation(msg)
        if code == 12:
            raise MappingRejected(
                msg, "Two routers in series (double NAT). The mapping has to be "
                     "made on the outermost one as well.")
        raise MappingRejected(msg)

    # -- operations ----------------------------------------------------------
    def available(self):
        """PCP has no cheap 'are you there' that does not also create state, so
        probe with a zero-lifetime MAP for a port we do not want: routers treat
        that as a delete of a mapping that does not exist, which is a no-op."""
        try:
            payload = build_map_request(self.client_ip, os.urandom(12), TCP,
                                        0, 0, "", 0)
            data = self._try_gateways(payload)
            res = parse_map_response(data)
        except NetworkingError as e:
            return False, str(e)
        except ValueError as e:
            return False, "Unreadable PCP reply (%s)." % e
        if res["result"] in (0, 3, 8, 13):
            # Anything but "I do not speak this" proves a PCP server is there.
            return True, "gateway %s" % self.gateway
        return False, RESULT_TEXT.get(res["result"], "PCP error %d." % res["result"])

    def external_ip(self):
        if self._last_external_ip:
            return self._last_external_ip
        raise NetworkingError(
            "PCP only reports the external address alongside a mapping.",
            "Create the mapping first, then read it from there.")

    def add(self, internal_ip, internal_port, external_port,
            protocol=TCP, lifetime=0, description="", nonce=None):
        protocol = (protocol or TCP).upper()
        want = int(lifetime or 0) or 3600          # PCP lifetime 0 means delete
        nonce = nonce or os.urandom(12)
        payload = build_map_request(internal_ip or self.client_ip, nonce, protocol,
                                    internal_port, external_port, "", want)
        data = self._try_gateways(payload)
        try:
            res = parse_map_response(data)
        except ValueError as e:
            raise MappingRejected("Unreadable PCP reply (%s)." % e)
        self._epoch = res.get("epoch", 0)
        self._check_result(res["result"], "Creating the mapping")
        if res.get("nonce") and res["nonce"] != nonce:
            raise MappingRejected(
                "The router replied about a different mapping than the one asked "
                "for. Refusing to treat that as success.")
        got_port = res.get("external_port") or external_port
        got_ip = res.get("external_ip", "")
        if got_ip:
            self._last_external_ip = got_ip
        return PortMapping(self.name, protocol, got_port, internal_port,
                           internal_ip, description,
                           res.get("lifetime") or want,
                           external_ip=got_ip, nonce=nonce.hex())

    def renew(self, mapping):
        nonce = bytes.fromhex(mapping.nonce) if mapping.nonce else None
        fresh = self.add(mapping.internal_ip, mapping.internal_port,
                         mapping.external_port, mapping.protocol,
                         mapping.lifetime, mapping.description, nonce=nonce)
        mapping.created = fresh.created
        mapping.lifetime = fresh.lifetime
        mapping.external_port = fresh.external_port
        mapping.external_ip = fresh.external_ip or mapping.external_ip
        return mapping

    def remove(self, mapping):
        try:
            nonce = bytes.fromhex(mapping.nonce) if mapping.nonce else os.urandom(12)
        except ValueError:
            nonce = os.urandom(12)
        try:
            payload = build_map_request(mapping.internal_ip or self.client_ip,
                                        nonce, mapping.protocol,
                                        mapping.internal_port, 0, "", 0)
            data = self._try_gateways(payload)
            res = parse_map_response(data)
        except (NetworkingError, ValueError) as e:
            return False, str(e)
        if res["result"] != 0:
            return False, RESULT_TEXT.get(res["result"],
                                          "PCP error %d." % res["result"])
        return True, "Removed %s." % mapping.describe()

    def info(self):
        return {"provider": self.name, "gateway": self.gateway,
                "candidates": list(self.gateways), "epoch": self._epoch,
                "external_ip": self._last_external_ip}
