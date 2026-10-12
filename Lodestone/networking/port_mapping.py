"""PortMappingService — one door, three ways to open it.

Tries each provider in turn and keeps a register of the mappings it made. That
register is the whole point of this class:

  * cleanup only ever touches mappings carrying our own token, so a rule the
    user added in the router's web UI, or one another game made, is never
    deleted by us;
  * the register is serialisable, so a crash mid-session does not orphan a hole
    in the firewall — the next launch finds it written down and can take it
    back down;
  * before deleting a UPnP mapping we re-read what is actually on that port and
    refuse if it no longer looks like ours, because the router may have been
    rebooted and the port reassigned to somebody else in the meantime.

Leases are renewed on a background thread at half-life. NAT-PMP and PCP have no
permanent lease at all, so without renewal an internet-hosted server would go
dark an hour in with no message.
"""

import threading
import time

from .errors import (MappingConflict, NetworkingError, PortInvalid,
                     ProviderUnavailable)
from .interfaces import TCP, PortMapping
from .natpmp import NatPmpProvider
from .pcp import PcpProvider
from .upnp import UpnpProvider

# Requesting an hour and renewing at half-life is the middle ground: short
# enough that a crash cannot leave the port open all week, long enough that the
# renewal thread is not chattering at the router.
DEFAULT_LEASE = 3600
RENEW_MARGIN = 0.5


class PortMappingService:

    def __init__(self, interfaces, providers=None, description="Lodestone Minecraft",
                 log=None):
        self.interfaces = interfaces
        self.description = description
        self.log = log or (lambda msg: None)
        self._lock = threading.RLock()
        self._providers = providers            # None == build them on demand
        self.mappings = []                     # PortMapping, ours only
        self.last_attempt = []                 # per-provider outcome, for the UI
        self._renew_stop = threading.Event()
        self._renew_thread = None

    # -- providers -----------------------------------------------------------
    def providers(self, want=("upnp", "natpmp", "pcp")):
        """Fresh provider instances bound to the current addressing.

        Rebuilt per attempt rather than cached: laptops move between networks,
        and a UpnpProvider still holding yesterday's control URL is worse than
        no provider at all.
        """
        if self._providers is not None:
            return list(self._providers)
        local = self.interfaces.local_ipv4()
        gateways = self.interfaces.gateway_candidates()
        built = []
        if "upnp" in want:
            built.append(UpnpProvider(local_ip=local))
        if "natpmp" in want:
            built.append(NatPmpProvider(gateways=gateways))
        if "pcp" in want:
            built.append(PcpProvider(client_ip=local, gateways=gateways))
        return built

    def probe(self, want=("upnp", "natpmp", "pcp")):
        """Which automatic mapping protocols this network supports. Read-only —
        creates nothing."""
        out = []
        for prov in self.providers(want):
            try:
                ok, detail = prov.available()
            except NetworkingError as e:
                ok, detail = False, str(e)
            except Exception as e:
                ok, detail = False, "unexpected error: %s" % e
            out.append({"provider": prov.name, "protocol": prov.protocol_name,
                        "available": bool(ok), "detail": detail})
        return out

    # -- create --------------------------------------------------------------
    def ensure(self, internal_port, external_port=None, protocol=TCP,
               lifetime=DEFAULT_LEASE, want=("upnp", "natpmp", "pcp"),
               internal_ip=""):
        """Open the port. Returns (mapping_or_None, attempts).

        `attempts` always describes every provider tried and why it failed, so
        the UI can say "UPnP is off on your router, NAT-PMP timed out" rather
        than "failed".
        """
        internal_port = int(internal_port)
        external_port = int(external_port or internal_port)
        internal_ip = internal_ip or self.interfaces.local_ipv4()
        if not internal_ip or internal_ip.startswith("127."):
            raise PortInvalid(
                "This machine has no LAN address, so there is nothing to forward to.",
                "Connect to a network first.")

        attempts = []
        for prov in self.providers(want):
            entry = {"provider": prov.name, "ok": False, "detail": "",
                     "external_ip": ""}
            mapping = None
            try:
                existing = prov.get(external_port, protocol)
                if existing and not self._looks_like_ours(existing, internal_ip,
                                                          internal_port):
                    raise MappingConflict(
                        "External port %d is already forwarded to %s on your router."
                        % (external_port, existing.get("internal_client") or "another device"),
                        "Choose a different external port, or remove that rule "
                        "in the router first.")
                mapping = prov.add(internal_ip, internal_port, external_port,
                                   protocol, lifetime, self.description)
                try:
                    entry["external_ip"] = mapping.external_ip or prov.external_ip()
                except NetworkingError:
                    pass
                mapping.external_ip = mapping.external_ip or entry["external_ip"]
                entry["ok"] = True
                entry["detail"] = mapping.describe()
            except NetworkingError as e:
                entry["detail"] = str(e)
                entry["hint"] = getattr(e, "hint", "")
                entry["kind"] = type(e).__name__
            except Exception as e:
                entry["detail"] = "unexpected error: %s" % e
                entry["kind"] = "Unexpected"
            finally:
                finally_close(prov)
            attempts.append(entry)
            if mapping is not None:
                with self._lock:
                    self._register(mapping)
                    self.last_attempt = attempts
                self.log("port mapping created via %s: %s"
                         % (prov.name, mapping.describe()))
                self._start_renewal()
                return mapping, attempts

        with self._lock:
            self.last_attempt = attempts
        return None, attempts

    @staticmethod
    def _looks_like_ours(existing, internal_ip, internal_port):
        try:
            same_port = int(existing.get("internal_port") or 0) == int(internal_port)
        except (TypeError, ValueError):
            same_port = False
        return existing.get("internal_client") == internal_ip and same_port

    def _register(self, mapping):
        for i, m in enumerate(self.mappings):
            if (m.protocol == mapping.protocol
                    and m.external_port == mapping.external_port):
                self.mappings[i] = mapping
                return
        self.mappings.append(mapping)

    # -- read ----------------------------------------------------------------
    def owned(self):
        with self._lock:
            return list(self.mappings)

    def verify(self, mapping):
        """Ask the router whether the mapping is still there. Returns
        (state, detail) where state is one of present / gone / unknown."""
        for prov in self.providers():
            if prov.name != mapping.provider:
                continue
            try:
                got = prov.get(mapping.external_port, mapping.protocol)
            except NetworkingError as e:
                return "unknown", str(e)
            finally:
                finally_close(prov)
            if got is None:
                # NAT-PMP and PCP have no read-back operation at all.
                return "unknown", "%s cannot be read back." % mapping.provider
            if self._looks_like_ours(got, mapping.internal_ip, mapping.internal_port):
                return "present", got.get("description", "")
            return "gone", "Port %d now points at %s." % (
                mapping.external_port, got.get("internal_client") or "something else")
        return "unknown", "That provider is no longer available."

    def router_table(self):
        """Everything the router has mapped, if it will tell us. Shown so the
        user can see we only added one row."""
        for prov in self.providers(("upnp",)):
            try:
                return prov.list_all()
            except NetworkingError:
                return []
            finally:
                finally_close(prov)
        return []

    # -- remove --------------------------------------------------------------
    def release(self, mapping=None, check_owner=True):
        """Take down our mappings. Never touches anything not in our register."""
        results = []
        with self._lock:
            targets = [mapping] if mapping else list(self.mappings)
        for m in targets:
            if check_owner and m.owner != PortMapping.OWNER:
                results.append({"mapping": m.describe(), "ok": False,
                                "detail": "Not created by Lodestone — left alone."})
                continue
            done = False
            detail = "No provider could remove it."
            for prov in self.providers():
                if prov.name != m.provider:
                    continue
                try:
                    # Re-read first. If the router rebooted and gave this port to
                    # somebody else, deleting it would break *their* game.
                    current = prov.get(m.external_port, m.protocol)
                    if (current is not None
                            and not self._looks_like_ours(current, m.internal_ip,
                                                          m.internal_port)):
                        done, detail = False, (
                            "Port %d no longer points at this machine — left alone."
                            % m.external_port)
                        break
                    done, detail = prov.remove(m)
                except NetworkingError as e:
                    done, detail = False, str(e)
                except Exception as e:
                    done, detail = False, "unexpected error: %s" % e
                finally:
                    finally_close(prov)
                break
            results.append({"mapping": m.describe(), "ok": bool(done),
                            "detail": detail, "provider": m.provider})
            self.log("port mapping removed (%s): %s" % (m.describe(), detail))
            if done:
                with self._lock:
                    self.mappings = [x for x in self.mappings if x is not m]
        if not self.owned():
            self._stop_renewal()
        return results

    # -- renewal -------------------------------------------------------------
    def _start_renewal(self):
        with self._lock:
            if self._renew_thread and self._renew_thread.is_alive():
                return
            self._renew_stop.clear()
            self._renew_thread = threading.Thread(
                target=self._renew_loop, name="lodestone-portmap-renew", daemon=True)
            self._renew_thread.start()

    def _stop_renewal(self):
        self._renew_stop.set()

    def _renew_loop(self):
        while not self._renew_stop.wait(30):
            try:
                self.renew_due()
            except Exception:
                # A renewal failure must never take the panel down with it; the
                # next tick tries again and the UI shows the mapping expiring.
                pass
            if not self.owned():
                return

    def renew_due(self):
        """Renew every lease past half-life. Returns what it did."""
        done = []
        for m in self.owned():
            if not m.needs_renewal(RENEW_MARGIN):
                continue
            for prov in self.providers():
                if prov.name != m.provider:
                    continue
                try:
                    prov.renew(m)
                    done.append({"mapping": m.describe(), "ok": True})
                    self.log("port mapping renewed: %s" % m.describe())
                except NetworkingError as e:
                    done.append({"mapping": m.describe(), "ok": False,
                                 "detail": str(e)})
                    self.log("port mapping renewal failed: %s" % e)
                finally:
                    finally_close(prov)
                break
        return done

    # -- persistence ---------------------------------------------------------
    def export(self):
        return [m.as_dict() for m in self.owned()]

    def adopt(self, records):
        """Take back ownership of mappings written down by a previous run, so
        they can be cleaned up even though this process never made them."""
        taken = []
        for rec in records or []:
            # This comes out of a JSON file a user can edit and a crash can
            # truncate, so nothing about its shape is assumed.
            if not isinstance(rec, dict):
                continue
            try:
                if rec.get("owner", PortMapping.OWNER) != PortMapping.OWNER:
                    continue
                m = PortMapping.from_dict(rec)
            except (TypeError, ValueError, KeyError, AttributeError):
                continue
            if not m.external_port or not m.internal_port:
                continue
            with self._lock:
                self._register(m)
            taken.append(m)
        return taken

    def close(self):
        self._stop_renewal()


def finally_close(prov):
    try:
        prov.close()
    except Exception:
        pass
