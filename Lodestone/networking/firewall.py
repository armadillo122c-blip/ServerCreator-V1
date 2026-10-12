"""FirewallManager — the Windows Firewall half of "why can nobody connect".

A router mapping delivers the packet to this machine; Windows Firewall decides
whether the machine answers. Both have to be right, and when only the second one
is wrong the symptom is identical, so this module exists to tell them apart.

Rules of engagement, all enforced here rather than left to the caller:

  * the firewall is never disabled, and no existing rule is ever modified;
  * the only rule we create is inbound / allow / one protocol / one local port,
    scoped to the network profile actually in use — no program rules, no
    "allow everything from this machine";
  * the rule carries a deterministic name of our own, and that name is the only
    thing removal will ever match, so nothing anybody else made can be deleted
    by us;
  * elevation is requested through the normal UAC prompt, and a refusal is a
    reported outcome, not an error.

Every value that reaches a command line here is either an int we parsed or a
member of a fixed whitelist, so the netsh invocation cannot be steered by
anything a user types.
"""

import ctypes
import re
import sys

from .errors import FirewallError, FirewallPermissionDenied
from .net_interfaces import run_quiet

RULE_PREFIX = "Lodestone-Minecraft"
PROTOCOLS = ("TCP", "UDP")
PROFILES = ("domain", "private", "public", "any")

# netsh output is localised. These cover the common cases; anything unrecognised
# is treated as "cannot tell", never as a positive answer.
_YES = ("yes", "ja", "oui", "si", "sí", "sim", "da", "tak")
_ALLOW = ("allow", "zulassen", "autoriser", "permitir", "permettere", "consentir")

# `show rule name=all` on a busy machine is a few thousand lines.
SCAN_TIMEOUT = 20
UAC_CANCELLED = 1223


def is_windows():
    return sys.platform.startswith("win")


def rule_name(port, protocol="TCP"):
    """Deterministic, space-free, and unmistakably ours. No spaces because the
    name is passed through PowerShell to netsh and quoting it three times over
    is how firewall helpers acquire their bugs."""
    return "%s-%s-%d" % (RULE_PREFIX, _clean_proto(protocol), int(port))


def _clean_proto(protocol):
    proto = str(protocol or "TCP").upper()
    if proto not in PROTOCOLS:
        raise FirewallError("Unsupported protocol %r." % protocol)
    return proto


def _clean_port(port):
    try:
        value = int(port)
    except (TypeError, ValueError):
        raise FirewallError("%r is not a port number." % port)
    if not 1 <= value <= 65535:
        raise FirewallError("Port %d is out of range." % value)
    return value


class FirewallManager:
    """Windows only. On every other platform it reports that it is not
    supported and does nothing, so callers need no platform branch."""

    def __init__(self, log=None):
        self.log = log or (lambda msg: None)
        self.supported = is_windows()

    # -- privileges -----------------------------------------------------------
    @staticmethod
    def is_admin():
        if not is_windows():
            try:
                import os
                return os.geteuid() == 0
            except AttributeError:
                return False
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    # -- state ----------------------------------------------------------------
    def profile_state(self):
        """Which profiles the firewall is switched on for, and which one the
        current network is using."""
        out = {"supported": self.supported, "on": None, "current": "",
               "profiles": {}, "detail": ""}
        if not self.supported:
            out["detail"] = ("Not Windows — manage the host firewall with your "
                             "own tools (ufw, firewalld, pf).")
            return out
        code, text = run_quiet(["netsh", "advfirewall", "show", "allprofiles", "state"],
                               timeout=8)
        if code != 0 and not text.strip():
            out["detail"] = "Could not read the firewall state (%s)." % text.strip()
            return out
        current_profile = ""
        for block in re.split(r"\r?\n\s*\r?\n", text):
            head = block.strip().splitlines()
            if not head:
                continue
            m = re.match(r"(Domain|Private|Public)\s+Profile\s+Settings", head[0].strip(), re.I)
            if not m:
                continue
            name = m.group(1).lower()
            state = re.search(r"^\s*State\s+(\w+)", block, re.I | re.M)
            out["profiles"][name] = (state.group(1).upper() == "ON") if state else None
        code, cur = run_quiet(["netsh", "advfirewall", "show", "currentprofile"], timeout=8)
        m = re.search(r"(Domain|Private|Public)\s+Profile\s+Settings", cur, re.I)
        if m:
            current_profile = m.group(1).lower()
        out["current"] = current_profile
        if current_profile and current_profile in out["profiles"]:
            out["on"] = out["profiles"][current_profile]
        elif out["profiles"]:
            out["on"] = any(v for v in out["profiles"].values() if v is not None)
        if out["on"] is None:
            out["detail"] = ("The firewall state could not be read — this build of "
                             "Windows may report it in another language.")
        return out

    def current_profile_arg(self):
        """Scope new rules to the profile actually in use. Falls back to
        private+public rather than `any`, because a domain-joined work laptop
        should not get a hole punched in its domain profile by a game panel."""
        state = self.profile_state()
        cur = state.get("current")
        if cur in ("domain", "private", "public"):
            return cur
        return "private,public"

    # -- our rule -------------------------------------------------------------
    def rule_exists(self, port, protocol="TCP"):
        """(allows_traffic, detail). Matches on our own rule name only.

        A rule that exists but is switched off allows nothing, so it reports
        False — otherwise the panel would say "the rule is in place" about a
        disabled rule and refuse to create a working one.
        """
        present, enabled, detail = self.rule_status(port, protocol)
        return (present and enabled is not False), detail

    def rule_status(self, port, protocol="TCP"):
        """(present, enabled, detail). `enabled` is None when it cannot be read."""
        if not self.supported:
            return False, None, "Not Windows."
        name = rule_name(_clean_port(port), protocol)
        code, text = run_quiet(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=" + name],
            timeout=10)
        if "No rules match" in text or code != 0:
            return False, None, "No Lodestone rule for that port."
        if not re.search(r"^\s*Rule Name:", text, re.M):
            return False, None, "No Lodestone rule for that port."
        got = re.search(r"^\s*Enabled:\s+(\w+)", text, re.M)
        if not got:
            return True, None, "%s exists; could not read whether it is enabled." % name
        enabled = got.group(1).strip().lower() in _YES
        return True, enabled, ("%s is present and enabled." % name if enabled
                               else "%s exists but is switched off, so it allows "
                                    "nothing." % name)

    def scan_allow_rules(self, port, protocol="TCP"):
        """Every enabled inbound allow rule that could let this port through.

        Two different things get found and they are NOT equivalent:

          * a **port rule** names the port and opens it for anything — that
            genuinely answers "is the port allowed";
          * a **program rule** says LocalPort: Any but is scoped to one
            executable, so it opens the port for that program only. A typical
            machine has hundreds of these (every game you have ever launched),
            and counting them as "the port is allowed" would make the whole
            firewall diagnosis meaningless.

        `verbose` is needed because the plain output omits Program: entirely,
        which makes LocalPort: Any impossible to interpret.

        Dumps the whole table, so it is only run on an explicit check, never on
        the status poll.
        """
        result = {"scanned": False, "rules": [], "program_rules": [],
                  "java_rules": [], "detail": ""}
        if not self.supported:
            result["detail"] = "Not Windows."
            return result
        port = _clean_port(port)
        proto = _clean_proto(protocol)
        code, text = run_quiet(
            ["netsh", "advfirewall", "firewall", "show", "rule", "name=all",
             "dir=in", "verbose"],
            timeout=SCAN_TIMEOUT)
        if not text.strip() or "Rule Name" not in text:
            result["detail"] = ("Could not read the rule list — it may be "
                                "localised, or blocked by policy.")
            return result
        result["scanned"] = True
        # netsh prints the dashed line immediately AFTER each "Rule Name:", not
        # between rules, so splitting on the dashes pairs every rule's fields
        # with the NEXT rule's name and loses the last rule entirely. Split on
        # the name line itself.
        for block in re.split(r"(?m)^(?=\s*Rule Name:)", text):
            name = re.search(r"^\s*Rule Name:\s+(.+?)\s*$", block, re.M)
            if not name:
                continue

            def field(label, _b=block):
                m = re.search(r"^\s*%s:\s+(.+?)\s*$" % label, _b, re.M)
                return m.group(1).strip() if m else ""

            if field("Enabled").lower() not in _YES:
                continue
            if field("Action").lower() not in _ALLOW:
                continue
            rule_proto = field("Protocol").upper()
            if rule_proto not in (proto, "ANY"):
                continue
            local_port = field("LocalPort")
            if not _port_matches(local_port, port):
                continue
            program = field("Program")
            row = {"name": name.group(1), "protocol": rule_proto,
                   "local_port": local_port, "profiles": field("Profiles"),
                   "program": program}
            if _is_any(local_port) and program and not _is_any(program):
                # Scoped to one executable. Only interesting if that executable
                # plausibly *is* the Minecraft server.
                if _looks_like_java(program):
                    result["java_rules"].append(row)
                else:
                    result["program_rules"].append(row)
            else:
                result["rules"].append(row)

        opens = len(result["rules"])
        java = len(result["java_rules"])
        if opens:
            result["detail"] = ("%d inbound rule(s) open %s port %d for anything."
                                % (opens, proto, port))
        elif java:
            result["detail"] = ("No rule opens %s port %d outright, but %d rule(s) "
                                "allow a Java executable through, which is probably "
                                "your server." % (proto, port, java))
        else:
            result["detail"] = ("Nothing in the inbound rules allows %s port %d."
                                % (proto, port))
        if result["program_rules"]:
            result["detail"] += (" (%d other rules match only because they are "
                                 "scoped to a different program.)"
                                 % len(result["program_rules"]))
        return result

    # -- change ---------------------------------------------------------------
    def _netsh_elevated(self, args, what):
        """Run one netsh command, elevating through UAC if we are not admin."""
        if self.is_admin():
            code, text = run_quiet(["netsh"] + args, timeout=20)
            if code != 0:
                raise FirewallError("%s failed: %s" % (what, text.strip()[:300]))
            return text
        if not is_windows():
            raise FirewallError("%s needs administrator rights." % what)

        # Everything in `args` is derived from validated ints and whitelisted
        # words, so single-quoting for PowerShell is sufficient and there is
        # nothing user-controlled to escape.
        for a in args:
            if "'" in a or '"' in a or "\n" in a or "\r" in a:
                raise FirewallError("Refusing to run a firewall command "
                                    "containing quoting characters.")
        # Windows PowerShell 5.1 joins -ArgumentList with plain spaces and adds
        # no quoting of its own, so an element containing a space arrives at
        # netsh as several arguments and the whole command is rejected. Elements
        # therefore carry their own double quotes; the CRT strips them again on
        # the far side, so netsh sees one argument. (Measured, not assumed.)
        arg_list = ",".join("'%s'" % (('"%s"' % a) if " " in a else a) for a in args)
        script = (
            "$ErrorActionPreference='Stop'; "
            "try { $p = Start-Process -FilePath 'netsh' -ArgumentList @(%s) "
            "-Verb RunAs -WindowStyle Hidden -PassThru -Wait; exit $p.ExitCode } "
            # Cancelling UAC surfaces as a Win32Exception with NativeErrorCode
            # 1223, but Start-Process rethrows it wrapped in an
            # InvalidOperationException, so the code is one level down. Check
            # both, or a refusal reads as a hard failure.
            "catch { $e = $_.Exception; "
            "if ($e.NativeErrorCode -eq 1223 -or "
            "$e.InnerException.NativeErrorCode -eq 1223 -or "
            "$e.Message -match '1223|cancell?ed') { exit 1223 } exit 1 }"
            % arg_list)
        code, text = run_quiet(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy",
             "Bypass", "-Command", script], timeout=120)
        if code == UAC_CANCELLED:
            raise FirewallPermissionDenied(
                "%s was cancelled at the Windows permission prompt." % what,
                "Nothing was changed. Approve the prompt, or add the rule "
                "yourself in Windows Defender Firewall.")
        if code != 0:
            raise FirewallError("%s failed (exit %s). %s"
                                % (what, code, text.strip()[:300]))
        return text

    def add_rule(self, port, protocol="TCP", profile=None):
        """Create the narrowest possible inbound allow rule. Idempotent."""
        if not self.supported:
            raise FirewallError(
                "Host firewall management is Windows-only.",
                "On Linux or macOS, allow the port with your own firewall tool.")
        port = _clean_port(port)
        proto = _clean_proto(protocol)
        name = rule_name(port, proto)
        present, enabled, _ = self.rule_status(port, proto)
        if present and enabled is not False:
            return {"ok": True, "created": False, "rule": name,
                    "detail": "That rule already exists."}
        if present and enabled is False:
            # A disabled rule of ours blocks re-adding under the same name, and
            # re-enabling is the narrower change anyway.
            self._netsh_elevated(["advfirewall", "firewall", "set", "rule",
                                  "name=" + name, "dir=in", "new", "enable=yes"],
                                 "Re-enabling the firewall rule")
            self.log("firewall rule re-enabled: %s" % name)
            return {"ok": True, "created": False, "rule": name,
                    "detail": "The rule existed but was switched off — re-enabled it."}
        prof = profile or self.current_profile_arg()
        for part in prof.split(","):
            if part.strip() not in PROFILES:
                raise FirewallError("Unknown firewall profile %r." % part)
        args = ["advfirewall", "firewall", "add", "rule",
                "name=" + name,
                "dir=in", "action=allow",
                "protocol=" + proto,
                "localport=%d" % port,
                "profile=" + prof,
                "edge=no",
                "description=Created by Lodestone for a Minecraft server. "
                "Safe to delete when you stop hosting."]
        self._netsh_elevated(args, "Adding the firewall rule")
        # netsh exits 0 on some malformed commands, and the elevated child's
        # exit code has travelled through PowerShell to get here. Read the rule
        # back rather than trusting either: a firewall rule that silently was
        # not created is exactly the failure this feature must not have.
        if not self.rule_exists(port, proto)[0]:
            raise FirewallError(
                "Windows reported success but the rule is not there.",
                "Add it by hand in Windows Defender Firewall: inbound, allow, "
                "%s port %d." % (proto, port))
        self.log("firewall rule added: %s (%s %d, profile %s)" % (name, proto, port, prof))
        return {"ok": True, "created": True, "rule": name, "profile": prof,
                "detail": "Allowed inbound %s %d on the %s profile." % (proto, port, prof)}

    def remove_rule(self, port, protocol="TCP"):
        """Delete only the rule we created, matched by our own name."""
        if not self.supported:
            return {"ok": True, "removed": False, "detail": "Not Windows."}
        port = _clean_port(port)
        proto = _clean_proto(protocol)
        name = rule_name(port, proto)
        # Removal keys off presence, not enabled-ness: a disabled rule of ours
        # is still ours and still wants cleaning up.
        if not self.rule_status(port, proto)[0]:
            return {"ok": True, "removed": False, "rule": name,
                    "detail": "There was no Lodestone rule to remove."}
        self._netsh_elevated(["advfirewall", "firewall", "delete", "rule",
                              "name=" + name, "dir=in", "protocol=" + proto,
                              "localport=%d" % port],
                             "Removing the firewall rule")
        self.log("firewall rule removed: %s" % name)
        return {"ok": True, "removed": True, "rule": name,
                "detail": "Removed %s." % name}

    # -- summary --------------------------------------------------------------
    def snapshot(self, port, protocol="TCP", deep=False):
        """What the UI shows. `deep` runs the full rule scan."""
        out = {"supported": self.supported, "admin": self.is_admin(),
               "rule": "", "rule_exists": False, "state": None,
               "current_profile": "", "likely_blocking": None,
               "allowing_rules": [], "scanned": False, "detail": ""}
        if not self.supported:
            out["detail"] = ("Host firewall checks are Windows-only. Make sure your "
                             "own firewall allows inbound TCP on this port.")
            return out
        try:
            port = _clean_port(port)
            proto = _clean_proto(protocol)
        except FirewallError as e:
            out["detail"] = str(e)
            return out
        out["rule"] = rule_name(port, proto)
        state = self.profile_state()
        out["state"] = state.get("on")
        out["current_profile"] = state.get("current", "")
        present, enabled, rule_detail = self.rule_status(port, proto)
        out["rule_exists"] = present and enabled is not False
        out["rule_present"] = present
        out["rule_enabled"] = enabled
        if deep:
            scan = self.scan_allow_rules(port, proto)
            out["scanned"] = scan["scanned"]
            out["allowing_rules"] = scan["rules"]
            out["detail"] = scan["detail"]
            out["program_rules"] = scan.get("program_rules", [])
            out["java_rules"] = scan.get("java_rules", [])
            if scan["scanned"]:
                allowed = bool(scan["rules"]) or bool(scan.get("java_rules"))
                on = state.get("on")
                if on is None:
                    # The rule table parsed but the on/off state did not. We do
                    # not know whether those rules are being enforced, and
                    # "unknown" must not collapse into "not blocking".
                    out["likely_blocking"] = None
                    out["detail"] += (" Whether the firewall is switched on could "
                                      "not be read, so this is not conclusive.")
                else:
                    out["likely_blocking"] = bool(on) and not allowed
            if present and enabled is False:
                out["detail"] += " " + rule_detail
        elif out["state"] is False:
            out["likely_blocking"] = False
            out["detail"] = "The firewall is off for the current profile."
        elif out["rule_exists"]:
            out["likely_blocking"] = False
            out["detail"] = "Lodestone's rule for this port is in place."
        elif present and enabled is False:
            out["detail"] = rule_detail + " Use 'Allow through firewall' to switch it on."
        else:
            out["detail"] = ("No Lodestone rule for this port. Windows may still be "
                             "allowing it through a rule for java.exe — run the full "
                             "check to find out.")
        return out


_ANY = ("any", "alle", "tout", "toute", "cualquiera", "qualsiasi", "beliebig")

# Paths that plausibly host a Minecraft server. A program rule for one of these
# is the rule Windows created the first time the server was launched.
_JAVA_RE = re.compile(r"(?:^|[\\/])(?:java|javaw|jre|jdk)[^\\/]*\.exe$|"
                      r"[\\/](?:jre|jdk|graalvm|zulu|temurin|adoptium|"
                      r"microsoft-jdk|corretto)[^\\/]*[\\/]", re.I)


def _is_any(field):
    return (field or "").strip().lower() in _ANY


def _looks_like_java(program):
    return bool(_JAVA_RE.search(program or ""))


def _port_matches(local_port_field, port):
    """netsh writes LocalPort as `Any`, `25565`, or `25565,25566` / `100-200`."""
    field = (local_port_field or "").strip()
    if not field:
        return False
    if _is_any(field):
        return True
    for part in field.split(","):
        part = part.strip()
        if "-" in part:
            lo, _, hi = part.partition("-")
            try:
                if int(lo) <= port <= int(hi):
                    return True
            except ValueError:
                continue
        else:
            try:
                if int(part) == port:
                    return True
            except ValueError:
                continue
    return False
