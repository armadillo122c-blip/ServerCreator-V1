"""NatPmpProvider — NAT Port Mapping Protocol, RFC 6886.

Much smaller than UPnP: two fixed-layout datagrams to the default gateway on
UDP 5351, no discovery step and no XML. Apple's AirPort started it and most
consumer firmware now answers it, so it is a good second string when UPnP is
switched off but "NAT-PMP" is not.

There is no discovery, which cuts both ways: we have to be told the gateway
address, and a wrong guess is indistinguishable from "the router does not speak
this" — both are silence. NetworkInterfaceService.gateway_candidates() exists
for exactly that, and this class will try each of them.

Wire format, all big-endian:

  external address request   [ver=0][op=0]
  external address response  [ver=0][op=128][result u16][epoch u32][ip 4]

  map request                [ver=0][op=1|2][rsv u16][int port u16]
                             [suggested ext port u16][lifetime u32]
  map response               [ver=0][op=129|130][result u16][epoch u32]
                             [int port u16][ext port u16][lifetime u32]

  op 1 = UDP, op 2 = TCP. Deleting is a map with lifetime 0 and suggested
  external port 0.
"""

import socket
import struct
import time

from .errors import (DiscoveryFailed, MappingRejected, NetworkingError,
                     NotAuthorized, ProviderUnavailable)
from .interfaces import TCP, UDP, PortMapping, PortMappingProvider

NATPMP_PORT = 5351
VERSION = 0

OP_EXTERNAL = 0
OP_MAP_UDP = 1
OP_MAP_TCP = 2

RESULT_TEXT = {
    0: "Success.",
    1: "The router speaks a different version of NAT-PMP.",
    2: "The router refused: NAT-PMP is present but not authorised.",
    3: "The router has no working internet connection.",
    4: "The router is out of mapping resources.",
    5: "The router does not support that operation.",
}

# RFC 6886 says start at 250ms and double. We stop far earlier than the RFC's
# 9 retries because this runs while somebody watches a spinner.
RETRY_DELAYS = (0.25, 0.5, 1.0)


class NatPmpProvider(PortMappingProvider):

    name = "NAT-PMP"
    protocol_name = "NAT-PMP (RFC 6886)"

    def __init__(self, gateway="", gateways=(), timeout=1.0):
        self.gateway = gateway
        self.gateways = [g for g in ([gateway] if gateway else []) + list(gateways) if g]
        self.timeout = timeout
        self.port = NATPMP_PORT
        self._epoch = 0
        self._working = gateway or ""

    # -- transport -----------------------------------------------------------
    def _exchange(self, payload, expect_op, gateway):
        """Send, retry with backoff, return the response body. Raises on silence."""
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(self.timeout)
            for delay in RETRY_DELAYS:
                try:
                    sock.sendto(payload, (gateway, self.port))
                except OSError as e:
                    raise ProviderUnavailable("Could not send to %s (%s)." % (gateway, e))
                deadline = time.time() + delay
                while time.time() < deadline:
                    remaining = max(0.05, deadline - time.time())
                    sock.settimeout(min(self.timeout, remaining))
                    try:
                        data, addr = sock.recvfrom(1024)
                    except socket.timeout:
                        break
                    except ConnectionResetError:
                        # Windows reports an ICMP port-unreachable this way:
                        # nothing is listening on 5351.
                        raise DiscoveryFailed(
                            "%s is not listening for NAT-PMP." % gateway)
                    except OSError:
                        break
                    if addr and addr[0] != gateway:
                        continue
                    if len(data) < 4 or data[0] != VERSION:
                        continue
                    if data[1] != expect_op:
                        continue
                    return data
            raise DiscoveryFailed("%s did not answer NAT-PMP." % gateway)
        finally:
            if sock:
                try:
                    sock.close()
                except OSError:
                    pass

    def _try_gateways(self, payload, expect_op):
        """Walk the candidate gateways, remembering the one that works."""
        order = ([self._working] if self._working else []) + \
                [g for g in self.gateways if g != self._working]
        if not order:
            raise ProviderUnavailable(
                "No default gateway found, so there is nowhere to send NAT-PMP.")
        last = None
        for gw in order:
            try:
                data = self._exchange(payload, expect_op, gw)
                self._working = gw
                self.gateway = gw
                return data
            except NetworkingError as e:
                last = e
        raise last or DiscoveryFailed("No gateway answered NAT-PMP.")

    @staticmethod
    def _check_result(code, what):
        if code == 0:
            return
        text = RESULT_TEXT.get(code, "The router returned NAT-PMP error %d." % code)
        msg = "%s failed: %s" % (what, text)
        if code == 2:
            raise NotAuthorized(msg, "Turn NAT-PMP on in the router, or forward "
                                     "the port by hand.")
        if code in (1, 5):
            raise ProviderUnavailable(msg)
        raise MappingRejected(msg)

    # -- operations ----------------------------------------------------------
    def available(self):
        try:
            ip = self.external_ip()
        except NetworkingError as e:
            return False, str(e)
        return True, "gateway %s, WAN %s" % (self.gateway, ip)

    def external_ip(self):
        data = self._try_gateways(struct.pack("!BB", VERSION, OP_EXTERNAL),
                                  OP_EXTERNAL + 128)
        if len(data) < 12:
            raise ProviderUnavailable("The NAT-PMP reply was too short to parse.")
        _, _, result, epoch = struct.unpack("!BBHI", data[:8])
        self._check_result(result, "Reading the external address")
        self._epoch = epoch
        return socket.inet_ntoa(data[8:12])

    def _map(self, protocol, internal_port, external_port, lifetime):
        op = OP_MAP_TCP if (protocol or TCP).upper() == TCP else OP_MAP_UDP
        payload = struct.pack("!BBHHHI", VERSION, op, 0,
                              int(internal_port), int(external_port),
                              int(lifetime))
        data = self._try_gateways(payload, op + 128)
        if len(data) < 16:
            raise MappingRejected("The NAT-PMP reply was too short to parse.")
        _, _, result, epoch, got_internal, got_external, got_lifetime = \
            struct.unpack("!BBHIHHI", data[:16])
        self._epoch = epoch
        return result, got_internal, got_external, got_lifetime

    def add(self, internal_ip, internal_port, external_port,
            protocol=TCP, lifetime=0, description=""):
        protocol = (protocol or TCP).upper()
        # NAT-PMP has no notion of a permanent lease. 0 means *delete*, so a
        # request for "forever" becomes the longest sane lease and the renewal
        # thread keeps it alive.
        want = int(lifetime or 0) or 3600
        result, got_internal, got_external, got_lifetime = \
            self._map(protocol, internal_port, external_port, want)
        self._check_result(result, "Creating the mapping")
        if got_internal and int(got_internal) != int(internal_port):
            raise MappingRejected(
                "The router mapped internal port %s instead of %s."
                % (got_internal, internal_port))
        return PortMapping(self.name, protocol, got_external or external_port,
                           internal_port, internal_ip, description,
                           got_lifetime or want)

    def remove(self, mapping):
        try:
            # Delete is "lifetime 0, external port 0" — the router matches on
            # the internal port, so this can only ever remove our own mapping.
            result, _, _, _ = self._map(mapping.protocol,
                                        mapping.internal_port, 0, 0)
            if result != 0:
                return False, RESULT_TEXT.get(
                    result, "NAT-PMP error %d." % result)
            return True, "Removed %s." % mapping.describe()
        except NetworkingError as e:
            return False, str(e)

    def info(self):
        return {"provider": self.name, "gateway": self.gateway,
                "candidates": list(self.gateways), "epoch": self._epoch}


__all__ = ["NatPmpProvider", "NATPMP_PORT", "OP_MAP_TCP", "OP_MAP_UDP",
           "OP_EXTERNAL", "RESULT_TEXT", "UDP"]
