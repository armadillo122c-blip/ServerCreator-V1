"""Every way the networking layer can fail, as a type you can catch.

The rule for this whole package: a networking failure is a *result*, never a
crash. Anything that talks to a router, an ISP or the internet raises one of
these, and every caller in Lodestone turns it into a line of text the user can
act on. `str(e)` is always safe to show in the UI.
"""


class NetworkingError(Exception):
    """Base for everything in this package. Carries a short user-facing message
    and, where we know one, a concrete next step."""

    def __init__(self, message, hint=""):
        super().__init__(message)
        self.message = message
        self.hint = hint

    def as_dict(self):
        return {"error": self.message, "hint": self.hint,
                "kind": type(self).__name__}


class ProviderUnavailable(NetworkingError):
    """No router on this network speaks the protocol this provider implements."""


class DiscoveryFailed(ProviderUnavailable):
    """The search ran but nothing answered — no IGD, no NAT-PMP responder."""


class MappingRejected(NetworkingError):
    """The router understood the request and said no."""


class MappingConflict(MappingRejected):
    """That external port is already mapped, by somebody who is not us."""


class NotAuthorized(MappingRejected):
    """The router has UPnP/PCP switched off, or restricted to a whitelist."""


class UnsupportedOperation(NetworkingError):
    """The router speaks the protocol but not this part of it."""


class PublicIpUnavailable(NetworkingError):
    """Could not establish the public address."""


class ConnectivityError(NetworkingError):
    """The external reachability probe could not be completed. This is *not*
    the same as 'not reachable' — it means we do not know."""


class FirewallError(NetworkingError):
    """A host firewall operation failed."""


class FirewallPermissionDenied(FirewallError):
    """Needed administrator rights and did not get them."""


class PortInvalid(NetworkingError):
    """The requested port is out of range, reserved, or would collide with a
    management service we refuse to publish."""


class PortInUse(PortInvalid):
    """Something on this machine already holds the port."""
