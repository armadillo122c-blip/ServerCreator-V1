"""The abstractions the rest of Lodestone is written against.

Nothing above this layer knows what UPnP is. It asks a PortMappingProvider for
a mapping and gets a PortMapping or an exception; swapping in a different way
of punching a hole (a new protocol, a tunnel, a stub for tests) means writing
one class, not touching the server manager.
"""

import time

TCP = "TCP"
UDP = "UDP"


class PortMapping:
    """One hole in the router that *we* made.

    `owner` is the thing that makes cleanup safe: we only ever delete mappings
    carrying our own token, so a mapping the user made by hand — or one another
    program made — is never touched.
    """

    OWNER = "lodestone"

    def __init__(self, provider, protocol, external_port, internal_port,
                 internal_ip, description="", lifetime=0, external_ip="",
                 nonce=None, created=None, owner=OWNER):
        self.provider = provider
        self.protocol = (protocol or TCP).upper()
        self.external_port = int(external_port)
        self.internal_port = int(internal_port)
        self.internal_ip = internal_ip
        self.description = description
        self.lifetime = int(lifetime or 0)      # 0 == until the router forgets
        self.external_ip = external_ip
        self.nonce = nonce                      # PCP needs the same nonce to refresh
        self.created = float(created if created is not None else time.time())
        self.owner = owner

    # -- lifetime bookkeeping ------------------------------------------------
    def expires_at(self):
        return 0 if not self.lifetime else self.created + self.lifetime

    def seconds_left(self):
        if not self.lifetime:
            return None                          # permanent as far as we know
        return max(0, int(self.expires_at() - time.time()))

    def needs_renewal(self, margin=0.5):
        """True once we are past `margin` of the lease. Renewing at half-life
        means one lost packet does not cost us the mapping."""
        if not self.lifetime:
            return False
        return time.time() >= self.created + (self.lifetime * margin)

    def describe(self):
        return "%s %d -> %s:%d" % (self.protocol, self.external_port,
                                   self.internal_ip, self.internal_port)

    def as_dict(self):
        return {"provider": self.provider, "protocol": self.protocol,
                "external_port": self.external_port,
                "internal_port": self.internal_port,
                "internal_ip": self.internal_ip,
                "description": self.description, "lifetime": self.lifetime,
                "external_ip": self.external_ip, "nonce": self.nonce,
                "created": self.created, "owner": self.owner,
                "text": self.describe(), "seconds_left": self.seconds_left()}

    @classmethod
    def from_dict(cls, d):
        return cls(provider=d.get("provider", ""), protocol=d.get("protocol", TCP),
                   external_port=d.get("external_port", 0),
                   internal_port=d.get("internal_port", 0),
                   internal_ip=d.get("internal_ip", ""),
                   description=d.get("description", ""),
                   lifetime=d.get("lifetime", 0),
                   external_ip=d.get("external_ip", ""),
                   nonce=d.get("nonce"), created=d.get("created"),
                   owner=d.get("owner", cls.OWNER))


class PortMappingProvider:
    """One way of asking the gateway for a port.

    Implementations must be safe to call from any thread, must never block for
    longer than their own timeout, and must raise a NetworkingError rather than
    letting a socket error escape.
    """

    name = "none"
    protocol_name = "none"

    def available(self):
        """Cheap-ish probe: is there a gateway here that speaks this protocol?
        Returns (bool, detail_string). Never raises."""
        return False, "not implemented"

    def external_ip(self):
        """The address the gateway believes it has on its WAN side. This is the
        one number that gives CGNAT away, so it matters even when a mapping
        succeeds. Raises NetworkingError when unknown."""
        raise NotImplementedError

    def add(self, internal_ip, internal_port, external_port,
            protocol=TCP, lifetime=0, description=""):
        """Create a mapping. Returns a PortMapping. Raises NetworkingError."""
        raise NotImplementedError

    def remove(self, mapping):
        """Delete a mapping this provider created. Returns (ok, detail)."""
        raise NotImplementedError

    def get(self, external_port, protocol=TCP):
        """What the router currently has on that external port, as a dict, or
        None. Used to tell 'already ours' from 'somebody else's'."""
        return None

    def renew(self, mapping):
        """Re-request the same mapping to push the lease out. Default is to
        just add it again, which is what all three protocols actually do."""
        fresh = self.add(mapping.internal_ip, mapping.internal_port,
                         mapping.external_port, mapping.protocol,
                         mapping.lifetime, mapping.description)
        mapping.created = fresh.created
        mapping.lifetime = fresh.lifetime
        mapping.external_port = fresh.external_port
        return mapping

    def close(self):
        pass


class PublicIpSource:
    """Somewhere that will tell you your own address from the outside."""

    name = "none"

    def fetch(self, family=4, timeout=5):
        raise NotImplementedError
