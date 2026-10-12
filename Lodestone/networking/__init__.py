"""Lodestone networking — making a Minecraft server on this PC reachable.

Everything in here is standard library only, and every entry point either
returns a result or raises a NetworkingError. Nothing in this package is
allowed to take the panel down with it.

Layout, and what each piece is responsible for:

    interfaces.py      the abstractions everything else is written against
    errors.py          one exception hierarchy, all of it printable
    net_interfaces.py  NetworkInterfaceService  — local IPs, gateway, IPv6
    public_ip.py       PublicIpService          — the address the world sees
    upnp.py            UpnpProvider             — UPnP IGD v1/v2
    natpmp.py          NatPmpProvider           — RFC 6886
    pcp.py             PcpProvider              — RFC 6887
    port_mapping.py    PortMappingService       — provider fallback + cleanup
    tunnel.py          TunnelService            — playit.gg / ngrok, when forwarding cannot work
    connectivity.py    ConnectivityTester       — is it reachable, really
    firewall.py        FirewallManager          — Windows Firewall, narrowly
    hosting.py         HostingManager           — the three modes, tied together

The panel only ever talks to HostingManager. Adding a fourth way to get a port
open — a tunnel, a relay, a VPS — means writing one PortMappingProvider and
adding it to the list in PortMappingService.providers(); nothing else changes.
"""

from .connectivity import (CGNAT_SUSPECTED, CHECKING, ConnectivityTester,
                           IPV6_ONLY, NOT_REACHABLE, PORT_BLOCKED, REACHABLE,
                           SERVER_OFFLINE, STATE_LABEL, UNKNOWN,
                           minecraft_ping, port_in_use, tcp_open)
from .errors import (ConnectivityError, DiscoveryFailed, FirewallError,
                     FirewallPermissionDenied, MappingConflict, MappingRejected,
                     NetworkingError, NotAuthorized, PortInUse, PortInvalid,
                     ProviderUnavailable, PublicIpUnavailable,
                     UnsupportedOperation)
from .firewall import FirewallManager, rule_name
from .hosting import (DEFAULT_MC_PORT, HostingManager, MODE_INTERNET, MODE_LAN,
                      MODE_LOCAL, MODE_LABEL, MODE_TUNNEL, MODES,
                      RESERVED_PORTS, validate_port)
from .interfaces import TCP, UDP, PortMapping, PortMappingProvider, PublicIpSource
from .natpmp import NatPmpProvider
from .net_interfaces import (NetworkInterfaceService, is_cgnat_v4,
                             is_global_v6, is_private_v4, is_valid_ipv4,
                             is_valid_ipv6)
from .pcp import PcpProvider
from .port_mapping import PortMappingService
from .public_ip import PublicIpService
from .tunnel import (NGROK_AUTHTOKEN_URL, NGROK_DOWNLOAD, PLAYIT_DOWNLOAD,
                     PROVIDER_LABEL, PROVIDERS, NgrokProvider, PlayitProvider,
                     TunnelProvider, TunnelService, find_agent, find_ngrok,
                     make_provider, ngrok_has_authtoken, split_address)
from .upnp import UpnpProvider, discover

VERSION = "1.0.0"

__all__ = [
    "VERSION",
    # services
    "HostingManager", "NetworkInterfaceService", "PublicIpService",
    "PortMappingService", "ConnectivityTester", "FirewallManager",
    # providers
    "UpnpProvider", "NatPmpProvider", "PcpProvider", "PortMappingProvider",
    "TunnelService", "TunnelProvider", "PlayitProvider", "NgrokProvider",
    "find_agent", "find_ngrok", "make_provider", "ngrok_has_authtoken",
    "split_address", "PLAYIT_DOWNLOAD", "NGROK_DOWNLOAD", "NGROK_AUTHTOKEN_URL",
    "PROVIDERS", "PROVIDER_LABEL",
    "PublicIpSource", "PortMapping", "discover",
    # modes and helpers
    "MODES", "MODE_LOCAL", "MODE_LAN", "MODE_INTERNET", "MODE_TUNNEL",
    "MODE_LABEL",
    "DEFAULT_MC_PORT", "RESERVED_PORTS", "validate_port", "rule_name",
    "TCP", "UDP", "minecraft_ping", "port_in_use", "tcp_open",
    "is_valid_ipv4", "is_valid_ipv6", "is_private_v4", "is_cgnat_v4",
    "is_global_v6",
    # connectivity vocabulary
    "CHECKING", "REACHABLE", "NOT_REACHABLE", "SERVER_OFFLINE", "PORT_BLOCKED",
    "CGNAT_SUSPECTED", "IPV6_ONLY", "UNKNOWN", "STATE_LABEL",
    # errors
    "NetworkingError", "ProviderUnavailable", "DiscoveryFailed",
    "MappingRejected", "MappingConflict", "NotAuthorized",
    "UnsupportedOperation", "PublicIpUnavailable", "ConnectivityError",
    "FirewallError", "FirewallPermissionDenied", "PortInvalid", "PortInUse",
]
