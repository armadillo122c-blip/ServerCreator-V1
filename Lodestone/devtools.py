"""Checks a server developer can trust: config files, Java projects, build
output and stack traces.

Nothing in here asks a model anything. Every answer comes from the file, the
build tool's own output or the JVM's own stack trace, which is what lets the
assistant build on it: it is handed facts, not guesses, and the UI can show
the same facts without an assistant at all.

* **Config files** - YAML, JSON, TOML and ``.properties``, with line numbers.
  YAML has no parser in the standard library, so :func:`check_yaml` is a
  structural checker aimed at the mistakes that actually break plugin
  configs: tabs, indentation, ``key: value: more``, ``&a`` read as an anchor,
  ``#`` read as a comment, unclosed quotes and brackets, duplicate keys.
* **Java projects** - Gradle and Maven build files, ``plugin.yml``,
  ``fabric.mod.json`` and ``mods.toml``, and the mismatches between them.
* **Build output** - javac, Gradle, Maven and Kotlin error lines.
* **Stack traces** - the exception chain, the frames that are in this
  project, and what the usual causes of that exception are.

Standard library only.
"""

import json
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path

try:
    import tomllib
except ImportError:          # Python < 3.11
    tomllib = None


def problem(line, message, severity="error", col=0, hint=""):
    out = {"line": int(line or 0), "col": int(col or 0), "severity": severity, "message": message}
    if hint:
        out["hint"] = hint
    return out


def _result(fmt, problems, extra=None):
    errs = sum(1 for p in problems if p["severity"] == "error")
    warns = sum(1 for p in problems if p["severity"] == "warning")
    problems.sort(key=lambda p: (p["line"], {"error": 0, "warning": 1}.get(p["severity"], 2)))
    out = {"format": fmt, "ok": errs == 0, "errors": errs, "warnings": warns, "problems": problems,
           "summary": ("no problems found" if not problems else
                       "%d error%s, %d warning%s" % (errs, "" if errs == 1 else "s",
                                                      warns, "" if warns == 1 else "s"))}
    if extra:
        out.update(extra)
    return out


# ---------------------------------------------------------------------------
#  which checker
# ---------------------------------------------------------------------------

CONFIG_FORMATS = {".yml": "yaml", ".yaml": "yaml", ".json": "json", ".mcmeta": "json",
                  ".toml": "toml", ".properties": "properties", ".sk": "skript"}


def format_for(name):
    return CONFIG_FORMATS.get(os.path.splitext(name or "")[1].lower(), "")


def validate(name, text, bool_keys=(), enum_keys=None):
    """Check one file by its extension. Skript files go to skripttools."""
    fmt = format_for(name)
    base = os.path.basename(name or "").lower()
    if fmt == "yaml":
        res = check_yaml(text)
        if base in ("plugin.yml", "paper-plugin.yml", "bungee.yml") and res["ok"]:
            res["problems"] += check_plugin_yml(text, base)
            res = _result("yaml", res["problems"])
        return res
    if fmt == "json":
        return check_json(text)
    if fmt == "toml":
        return check_toml(text)
    if fmt == "properties":
        return check_properties(text, server=(base == "server.properties"),
                                bool_keys=bool_keys, enum_keys=enum_keys or {})
    if fmt == "skript":
        try:
            import skripttools
        except ImportError as e:
            return _result("skript", [problem(0, "The Skript checker could not be loaded: %s" % e)])
        if base in ("config.sk", "features.sk") or "/aliases/" in (name or "").replace("\\", "/"):
            # Skript's own settings files: same reader, but they are not scripts
            _nodes, probs, _unit = skripttools.read_nodes(text)
            return _result("skript-config", probs)
        return skripttools.check(text, name)
    return {"format": "", "ok": True, "errors": 0, "warnings": 0, "problems": [],
            "summary": "no checker for this kind of file"}


# ---------------------------------------------------------------------------
#  JSON
# ---------------------------------------------------------------------------

def check_json(text):
    problems = []
    dupes = []

    def pairs(items):
        seen = set()
        for k, _v in items:
            if k in seen:
                dupes.append(k)
            seen.add(k)
        return dict(items)

    if (text or "").startswith("﻿"):
        text = text[1:]
    try:
        json.loads(text or "", object_pairs_hook=pairs)
    except json.JSONDecodeError as e:
        hint = ""
        if "Expecting property name enclosed in double quotes" in e.msg:
            hint = "JSON needs double quotes around keys, and no comma after the last item."
        elif "Expecting ',' delimiter" in e.msg:
            hint = "A comma is missing between two items, or a quote inside a string is not escaped (\\\")."
        problems.append(problem(e.lineno, e.msg, col=e.colno, hint=hint))
        return _result("json", problems)
    for k in sorted(set(dupes)):
        problems.append(problem(_find_line(text, '"%s"' % k), "Key \"%s\" appears more than once; "
                                "only the last one counts." % k, "warning"))
    return _result("json", problems)


def _find_line(text, needle, after=0):
    i = (text or "").find(needle)
    return text.count("\n", 0, i) + 1 if i >= 0 else after


# ---------------------------------------------------------------------------
#  TOML
# ---------------------------------------------------------------------------

def check_toml(text):
    if tomllib is None:
        return _result("toml", [problem(0, "TOML needs Python 3.11 or newer to check.", "info")])
    try:
        tomllib.loads(text or "")
    except tomllib.TOMLDecodeError as e:
        msg = str(e)
        m = re.search(r"\(at line (\d+), column (\d+)\)", msg)
        line, col = (int(m.group(1)), int(m.group(2))) if m else (0, 0)
        return _result("toml", [problem(line, re.sub(r"\s*\(at line \d+, column \d+\)", "", msg), col=col)])
    return _result("toml", [])


# ---------------------------------------------------------------------------
#  .properties
# ---------------------------------------------------------------------------

INT_PROPS = {"server-port": (1, 65535), "query.port": (1, 65535), "rcon.port": (1, 65535),
             "max-players": (0, 2147483647), "view-distance": (2, 32), "simulation-distance": (2, 32),
             "spawn-protection": (0, 2147483647), "max-world-size": (1, 29999984),
             "network-compression-threshold": (-1, 2147483647), "op-permission-level": (0, 4),
             "function-permission-level": (1, 4), "entity-broadcast-range-percentage": (10, 1000),
             "max-tick-time": (-1, 9223372036854775807), "rate-limit": (0, 2147483647),
             "player-idle-timeout": (0, 2147483647), "max-chained-neighbor-updates": (-1, 2147483647)}
LEGACY_NUMERIC = {"gamemode": 3, "difficulty": 3}


def parse_properties(text):
    """[(line, key, value)] the way java.util.Properties reads them: '=' ':' or
    whitespace separates, '#' and '!' start comments, a trailing backslash
    continues the line."""
    out = []
    lines = (text or "").replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines):
        start = i + 1
        raw = lines[i].lstrip(" \t\f")
        i += 1
        if not raw or raw[0] in "#!":
            continue
        while _continues(raw) and i < len(lines):
            raw = raw[:-1] + lines[i].lstrip(" \t\f")
            i += 1
        key, j = [], 0
        while j < len(raw):
            c = raw[j]
            if c == "\\" and j + 1 < len(raw):
                key.append(raw[j:j + 2])
                j += 2
                continue
            if c in "=: \t\f":
                break
            key.append(c)
            j += 1
        rest = raw[j:].lstrip(" \t\f")
        if rest[:1] in ("=", ":"):
            rest = rest[1:].lstrip(" \t\f")
        out.append((start, "".join(key), rest))
    return out


def _continues(line):
    n = len(line) - len(line.rstrip("\\"))
    return n % 2 == 1


def check_properties(text, server=False, bool_keys=(), enum_keys=None):
    problems = []
    seen = {}
    for line, key, value in parse_properties(text):
        for m in re.finditer(r"\\u(.{0,4})", key + "=" + value):
            if not re.fullmatch(r"[0-9a-fA-F]{4}", m.group(1)):
                problems.append(problem(line, "Malformed \\uXXXX escape: Java will refuse to load this file."))
        if key in seen:
            problems.append(problem(line, "%s is set again (first on line %d); the last one wins."
                                    % (key, seen[key]), "warning"))
        seen[key] = line
        if not server:
            continue
        v = value.strip()
        if key in bool_keys and v.lower() not in ("true", "false"):
            problems.append(problem(line, "%s must be true or false, not '%s'." % (key, v)))
        elif key in INT_PROPS:
            lo, hi = INT_PROPS[key]
            try:
                n = int(v)
                if not lo <= n <= hi:
                    problems.append(problem(line, "%s is %d; Minecraft accepts %d to %d." % (key, n, lo, hi),
                                            "warning" if key in ("view-distance", "simulation-distance") else "error"))
            except ValueError:
                problems.append(problem(line, "%s must be a whole number, not '%s'." % (key, v)))
        elif key in (enum_keys or {}) and key in LEGACY_NUMERIC:
            opts = enum_keys[key]
            if v.lower() not in opts:
                if v.isdigit() and int(v) <= LEGACY_NUMERIC[key]:
                    problems.append(problem(line, "%s=%s is the old numeric form; current versions use "
                                            "%s." % (key, v, " / ".join(opts)), "info"))
                else:
                    problems.append(problem(line, "%s must be one of %s, not '%s'." % (key, ", ".join(opts), v)))
    return _result("properties", problems)


# ---------------------------------------------------------------------------
#  YAML
# ---------------------------------------------------------------------------

COLOUR_ANCHOR = re.compile(r"&(?:[0-9a-fk-orA-FK-OR]|#[0-9a-fA-F]{6}|x)")
KEY_RE = re.compile(r"^(?:\"(?:[^\"\\]|\\.)*\"|'(?:[^']|'')*'|[^\s#'\"\[\]{},&*!|>@`%-][^#]*?|-[^\s][^#]*?)\s*$")


def _scan(s, start=0, quote=None, depth=0):
    """Walk ``s`` tracking quotes and flow brackets. Returns
    (comment_at, quote, depth, colon_at) where colon_at is the first ': ' or
    trailing ':' outside quotes and brackets."""
    i, colon = start, -1
    n = len(s)
    while i < n:
        c = s[i]
        if quote == '"':
            if c == "\\":
                i += 2
                continue
            if c == '"':
                quote = None
        elif quote == "'":
            if c == "'":
                if i + 1 < n and s[i + 1] == "'":
                    i += 2
                    continue
                quote = None
        else:
            if c == "#" and (i == 0 or s[i - 1] in " \t"):
                return i, quote, depth, colon
            if c in "\"'" and (i == 0 or s[i - 1] in " \t[{,:" or depth > 0):
                quote = c
            elif c in "[{":
                depth += 1
            elif c in "]}":
                depth = max(0, depth - 1)
            elif c == ":" and depth == 0 and colon < 0 and (i + 1 == n or s[i + 1] in " \t"):
                colon = i
        i += 1
    return -1, quote, depth, colon


def check_yaml(text):
    problems = []
    text = (text or "")
    if text.startswith("﻿"):
        text = text[1:]
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    # open blocks: indent, keys seen in it, and whether it is a sequence that
    # sits at its parent key's indent (YAML allows "key:\n- a")
    root = {"indent": -1, "keys": {}, "compact": False}
    stack = [root]
    expect_child = None       # (indent, line) of a key or "-" with nothing after it
    plain_open = None         # indent of a line whose plain value may continue below
    block_scalar = None       # indent of the line that opened | or >
    multi = None              # ('quote', ch, line) or ('flow', depth, line)
    anchors, aliases = set(), set()
    bare_anchors = []         # (line, name, value) of 'key: &name' with nothing after
    top = {}

    for idx, raw in enumerate(lines):
        ln = idx + 1
        if block_scalar is not None:
            if not raw.strip():
                continue
            ind = len(raw) - len(raw.lstrip(" "))
            if ind > block_scalar:
                continue
            block_scalar = None

        if multi is not None:
            if multi[0] == "quote":
                _c, q, _d, _k = _scan(raw, 0, multi[1], 0)
                if q is None:
                    multi = None
                continue
            _c, q, d, _k = _scan(raw, 0, None, multi[1])
            if q is not None:
                multi = ("quote", q, multi[2])
            elif d == 0:
                multi = None
            else:
                multi = ("flow", d, multi[2])
            continue

        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if (raw.startswith("%") and not top) or stripped in ("---", "...") or stripped.startswith("--- "):
            stack = [root]
            expect_child = plain_open = None
            continue

        lead = raw[:len(raw) - len(raw.lstrip(" \t"))]
        if "\t" in lead:
            problems.append(problem(ln, "Tab used for indentation. YAML only allows spaces.",
                                    hint="Replace the tab with spaces (two per level is usual)."))
            expect_child = plain_open = None
            continue
        indent = len(lead)
        body = raw[indent:]
        is_item = body == "-" or body.startswith("- ")

        # a deeper line under a plain value is that value continuing
        if plain_open is not None and indent > plain_open and expect_child is None:
            continue
        plain_open = None

        popped = False
        while len(stack) > 1 and (stack[-1]["indent"] > indent or
                                  (stack[-1]["compact"] and stack[-1]["indent"] == indent and not is_item)):
            stack.pop()
            popped = True

        if expect_child is not None:
            e_ind, _e_line = expect_child
            expect_child = None
            if bare_anchors and bare_anchors[-1][3] == e_ind and indent > e_ind:
                bare_anchors.pop()            # the anchor names a block: a real anchor
            if indent > e_ind:
                stack.append({"indent": indent, "keys": {}, "compact": False})
            elif indent == e_ind and is_item:
                stack.append({"indent": indent, "keys": {}, "compact": True})
        if stack[-1]["indent"] != indent:
            if len(stack) == 1 and not root["keys"]:
                stack.append({"indent": indent, "keys": {}, "compact": False})
            elif indent > stack[-1]["indent"] and not popped:
                problems.append(problem(ln, "This line is indented deeper than the one above it, which "
                                        "does not open a section.",
                                        hint="Line it up with its neighbours, or end the line above with ':' "
                                             "to make this part of it."))
                continue
            else:
                problems.append(problem(ln, "The indentation does not match any level above it.",
                                        hint="Use the same number of spaces as the other keys at this level."))
                continue
        root["keys"].setdefault("\0", ln)
        block = stack[-1]
        at_top = len(stack) == 2 and stack[1]["indent"] == indent and not stack[1]["compact"]

        # "- item", possibly "- - item", possibly "- key: value"
        item = False
        while body == "-" or body.startswith("- "):
            item = True
            rest = body[1:].lstrip(" ")
            if not rest or rest.startswith("#"):
                expect_child = (indent, ln)
                body = ""
                break
            indent += len(body) - len(rest)
            body = rest
        if not body:
            continue

        comment_at, q, depth, colon = _scan(body)
        content = body if comment_at < 0 else body[:comment_at].rstrip()
        if colon >= 0:
            key = content[:colon].strip()
            value = content[colon + 1:].strip()
            if key.startswith("? "):
                continue
            if item:
                stack.append({"indent": indent, "keys": {}, "compact": False})
                block = stack[-1]
                at_top = False
            if not KEY_RE.match(key):
                problems.append(problem(ln, "'%s' is not a valid key." % key[:60],
                                        hint="Quote keys that start with a special character."))
            kname = key.strip("'\"")
            if kname in block["keys"]:
                problems.append(problem(ln, "Key '%s' is already defined on line %d in this section; "
                                        "the later value replaces it." % (kname, block["keys"][kname]),
                                        "warning"))
            else:
                block["keys"][kname] = ln
            if at_top:
                top[kname] = value
            anchor_only = re.fullmatch(r"&(\S+)", value)
            if not value or anchor_only or re.fullmatch(r"!\S*", value):
                if not value and comment_at >= 0 and re.match(r"#[0-9a-fA-F]{3,8}\b", body[comment_at:]):
                    colour = body[comment_at:].split()[0]
                    problems.append(problem(ln, "The value is empty: '#' starts a comment, so '%s' is not "
                                            "read." % colour, "warning",
                                            hint="Quote it: %s: '%s'" % (kname, colour)))
                if anchor_only:
                    anchors.add(anchor_only.group(1))
                    bare_anchors.append((ln, anchor_only.group(1), value, indent))
                expect_child = (indent, ln)
                continue
            _check_value(value, ln, problems, anchors, aliases, q, depth)
        else:
            if q is None and depth == 0 and not item:
                m = re.match(r"^([A-Za-z0-9_.\-]+):(\S.*)$", content)
                if m and not re.match(r"^[a-z]+://", content):
                    problems.append(problem(ln, "'%s' has no space after the colon, so YAML reads the whole "
                                            "line as text, not as a key and value." % content[:60],
                                            hint="Write it as '%s: %s'" % (m.group(1), m.group(2))))
                elif len(stack) == 2 and len(root["keys"]) <= 1 and ln == root["keys"].get("\0"):
                    pass   # a document that is just a scalar
                else:
                    problems.append(problem(ln, "Expected 'key: value' or '- item' here.",
                                            hint="If this is meant to continue the value above, indent it "
                                                 "further."))
                continue
            value = content
            if item and value:
                _check_value(value, ln, problems, anchors, aliases, q, depth)
        if q is not None:
            multi = ("quote", q, ln)
        elif depth > 0:
            multi = ("flow", depth, ln)
        elif value and value[0] in "|>":
            block_scalar = indent - (2 if item and colon < 0 else 0)
        elif value and value[0] not in "\"'[{&*!":
            plain_open = indent

    if multi is not None:
        what = "quote" if multi[0] == "quote" else "bracket"
        problems.append(problem(multi[2], "This %s is never closed." % what))
    for ln, name, value, _ind in bare_anchors:
        if name not in aliases and COLOUR_ANCHOR.match(value):
            problems.append(problem(ln, "'%s' is read as a YAML anchor, not as text with a colour code, so the "
                                    "value is empty." % value, "warning",
                                    hint="Quote the text: '%s'" % value))
    return _result("yaml", problems, {"top": top})


PLACEHOLDER = re.compile(r"^(?:@[\w.\-]+@|\$\{[\w.\-]+\})$")


def _check_value(value, ln, problems, anchors, aliases, q, depth):
    c = value[0]
    if PLACEHOLDER.match(value):
        problems.append(problem(ln, "%s is a build placeholder; the build replaces it, so this file is a "
                                "template rather than the final config." % value, "info"))
        return
    if c in "@`":
        problems.append(problem(ln, "A value cannot start with '%s' unless it is quoted." % c,
                                hint="Put quotes around it: '%s'" % value[:40]))
        return
    if c == "&":
        m = re.match(r"&(\S*)\s*(.*)$", value)
        anchors.add(m.group(1))
        if m.group(2) and (COLOUR_ANCHOR.match(value) or re.search(r"&[0-9a-fk-or]", m.group(2), re.I)):
            problems.append(problem(ln, "'%s' is read as a YAML anchor, not as text with a colour code - the "
                                    "'&%s' part disappears." % (value[:40], m.group(1)), "warning",
                                    hint="Quote the text: '%s'" % value.replace("'", "''")[:60]))
        return
    if c == "*":
        name = re.match(r"\*(\S*)", value).group(1)
        aliases.add(name)
        if name not in anchors:
            problems.append(problem(ln, "'*%s' is read as a reference to an anchor that does not exist." % name,
                                    hint="If it is text, quote it: '%s'" % value.replace("'", "''")[:60]))
        return
    if c in "\"'":
        if q is None:
            end = _closing_quote(value)
            if end > 0 and value[end + 1:].strip():
                problems.append(problem(ln, "Text after the closing quote: %s" % value[end + 1:].strip()[:40],
                                        hint="Put the whole value inside the quotes."))
        return
    if c in "[{|>!":
        return
    _cm, _q, _d, colon = _scan(value)
    if colon >= 0:
        problems.append(problem(ln, "A ': ' inside an unquoted value starts another key, which YAML does not "
                                "allow here.", hint="Quote the value: '%s'" % value.replace("'", "''")[:60]))


def _closing_quote(value):
    q = value[0]
    i = 1
    while i < len(value):
        if q == '"' and value[i] == "\\":
            i += 2
            continue
        if value[i] == q:
            if q == "'" and i + 1 < len(value) and value[i + 1] == "'":
                i += 2
                continue
            return i
        i += 1
    return -1


def yaml_scalar(v):
    v = (v or "").strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def yaml_list(text, key):
    """A top-level list, written either [a, b] or as '- a' lines."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    for i, ln in enumerate(lines):
        m = re.match(r"^%s\s*:\s*(.*)$" % re.escape(key), ln)
        if not m:
            continue
        v = m.group(1).split(" #")[0].strip()
        if v.startswith("["):
            return [yaml_scalar(x) for x in v.strip("[]").split(",") if x.strip()]
        if v:
            return [yaml_scalar(v)]
        out = []
        for nxt in lines[i + 1:]:
            s = nxt.strip()
            if not s or s.startswith("#"):
                continue
            if s.startswith("- "):
                out.append(yaml_scalar(s[2:].split(" #")[0]))
                continue
            if nxt[:1] not in (" ", "\t", "-"):
                break
        return out
    return []


def check_plugin_yml(text, base="plugin.yml"):
    problems = []
    top = check_yaml(text).get("top", {})
    for k in ("name", "version", "main"):
        if k not in top:
            problems.append(problem(1, "%s has no '%s', which every plugin needs." % (base, k)))
    if base in ("plugin.yml", "paper-plugin.yml") and "api-version" not in top:
        problems.append(problem(1, "No api-version: the server will load this as a legacy plugin, with "
                                "old material names and a warning on every start.", "warning",
                                hint="Add api-version matching the oldest Minecraft version you support, "
                                     "e.g. api-version: '1.21'"))
    name = yaml_scalar(top.get("name", ""))
    if name and not PLACEHOLDER.match(name) and not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
        problems.append(problem(_find_line(text, "name:"), "Plugin names may only use letters, digits, "
                                "'_', '.' and '-'; '%s' will be refused." % name))
    return problems


# ---------------------------------------------------------------------------
#  Java projects
# ---------------------------------------------------------------------------

def _read(p, limit=1_000_000):
    try:
        if p.stat().st_size > limit:
            return ""
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


GRADLE_DEP = re.compile(
    r"""(?m)(?:^|[{;])\s*(implementation|api|compileOnly|runtimeOnly|testImplementation|annotationProcessor|"""
    r"""modImplementation|modCompileOnly|modApi|include|shadow|minecraft|mappings|compileOnlyApi)"""
    r"""\s*\(?\s*(?:\w+\s*\()?\s*["']([^"']+)["']""")
GRADLE_PLUGIN = re.compile(r"""(?m)^\s*id\s*\(?\s*["']([^"']+)["']\s*\)?(?:\s*version\s*\(?\s*["']([^"']+)["'])?""")
GRADLE_REPO = re.compile(r"""(?:url\s*=?\s*(?:uri\s*\()?\s*["']([^"']+)["']|\b(mavenCentral|mavenLocal|google|gradlePluginPortal)\s*\(\s*\))""")
TOOLCHAIN = re.compile(r"""(?:languageVersion\s*(?:=|\.set\s*\()\s*JavaLanguageVersion\.of\s*\(\s*(\d+)|"""
                       r"""(?:source|target)Compatibility\s*=\s*(?:JavaVersion\.VERSION_)?['"]?(\d+)|"""
                       r"""options\.release\s*(?:=|\.set\s*\()\s*(\d+))""")
DEV_BUNDLE = re.compile(r"""paperweight\.(?:paperDevBundle|devBundle)\s*\(\s*["']([^"']+)["']""")

LOADER_PLUGINS = {
    "fabric-loom": "fabric", "net.fabricmc.fabric-loom": "fabric", "net.fabricmc.fabric-loom-remap": "fabric",
    "net.neoforged.moddev": "neoforge", "net.neoforged.gradle.userdev": "neoforge",
    "net.minecraftforge.gradle": "forge", "net.neoforged.moddev.legacyforge": "forge",
    "io.papermc.paperweight.userdev": "paper", "xyz.jpenilla.run-paper": "paper",
    "com.gradleup.shadow": "", "com.github.johnrengelman.shadow": "", "io.github.goooler.shadow": "",
}
SERVER_APIS = ("io.papermc.paper:paper-api", "org.spigotmc:spigot-api", "org.bukkit:bukkit",
               "com.destroystokyo.paper:paper-api", "dev.folia:folia-api", "org.purpurmc.purpur:purpur-api")


def _mc_of(version):
    m = re.match(r"(\d+\.\d+(?:\.\d+)?)", version or "")
    return m.group(1) if m else ""


def _mc_tuple(v):
    try:
        return tuple(int(x) for x in (v or "0").split("."))
    except ValueError:
        return (0,)


def java_needed_for(mc):
    """The Java a Minecraft version's server needs, from Mojang's own version files."""
    t = _mc_tuple(mc)
    if t[:1] and t[0] >= 26:
        return 25
    if t >= (1, 20, 5):
        return 21
    if t >= (1, 18):
        return 17
    if t >= (1, 17):
        return 16
    return 8


def java_project(root):
    """What kind of project this is and what it is built with. ``kind`` is
    'gradle', 'maven' or '' when neither is there."""
    root = Path(root)
    out = {"root": str(root), "kind": "", "name": root.name, "wrapper": False, "build_files": [],
           "plugins": [], "dependencies": [], "repositories": [], "java": 0, "loader": "",
           "mc": "", "descriptors": [], "problems": [], "build_command": [], "test_command": []}
    gradle = [f for f in ("build.gradle.kts", "build.gradle") if (root / f).is_file()]
    if gradle:
        out["kind"] = "gradle"
        out["build_files"] = gradle + [f for f in ("settings.gradle.kts", "settings.gradle", "gradle.properties")
                                       if (root / f).is_file()]
        text = "\n".join(_read(root / f) for f in out["build_files"])
        props = {}
        for line in _read(root / "gradle.properties").splitlines():
            m = re.match(r"^\s*([\w.\-]+)\s*[=:]\s*(.*?)\s*$", line)
            if m and not line.lstrip().startswith("#"):
                props[m.group(1)] = m.group(2)
        for m in GRADLE_PLUGIN.finditer(text):
            out["plugins"].append({"id": m.group(1), "version": m.group(2) or ""})
            if LOADER_PLUGINS.get(m.group(1)):
                out["loader"] = LOADER_PLUGINS[m.group(1)]
        for m in GRADLE_DEP.finditer(text):
            coord = _expand(m.group(2), props)
            out["dependencies"].append({"scope": m.group(1), "coord": coord,
                                        "line": text.count("\n", 0, m.start()) + 1})
        db = DEV_BUNDLE.search(text)
        if db:
            out["dependencies"].append({"scope": "paperweight", "coord": "io.papermc.paper:dev-bundle:" + _expand(db.group(1), props),
                                        "line": text.count("\n", 0, db.start()) + 1})
            out["loader"] = out["loader"] or "paper"
        for m in GRADLE_REPO.finditer(text):
            out["repositories"].append(m.group(1) or m.group(2) + "()")
        tc = TOOLCHAIN.search(text)
        if tc:
            out["java"] = int(next(g for g in tc.groups() if g))
        out["mc"] = props.get("minecraft_version") or props.get("mc_version") or props.get("minecraftVersion") or ""
        out["wrapper"] = (root / "gradlew").is_file() or (root / "gradlew.bat").is_file()
        wprops = _read(root / "gradle" / "wrapper" / "gradle-wrapper.properties")
        wm = re.search(r"gradle-([\d.]+(?:-rc-\d+)?)-(?:bin|all)\.zip", wprops)
        out["gradle_version"] = wm.group(1) if wm else ""
        exe = ("gradlew.bat" if os.name == "nt" else "./gradlew") if out["wrapper"] else "gradle"
        out["build_command"] = [exe, "build"]
        out["test_command"] = [exe, "test"]
    elif (root / "pom.xml").is_file():
        out["kind"] = "maven"
        out["build_files"] = ["pom.xml"]
        _maven(root / "pom.xml", out)
        out["wrapper"] = (root / "mvnw").is_file() or (root / "mvnw.cmd").is_file()
        exe = ("mvnw.cmd" if os.name == "nt" else "./mvnw") if out["wrapper"] else "mvn"
        out["build_command"] = [exe, "-B", "package"]
        out["test_command"] = [exe, "-B", "test"]

    _descriptors(root, out)
    for d in out["dependencies"]:
        ga = ":".join(d["coord"].split(":")[:2])
        if ga in SERVER_APIS or ga.endswith(":dev-bundle"):
            out["loader"] = out["loader"] or "paper"
            out["mc"] = out["mc"] or _mc_of(d["coord"].split(":")[2] if d["coord"].count(":") >= 2 else "")
    _project_problems(root, out)
    return out


def _expand(s, props):
    def sub(m):
        return props.get(m.group(1) or m.group(2), m.group(0))
    return re.sub(r"\$\{?(?:project\.)?([\w.]+)\}?|\$\{(\w+)\}", sub, s)


def _maven(path, out):
    try:
        tree = ET.parse(str(path))
    except (ET.ParseError, OSError) as e:
        out["problems"].append(problem(getattr(e, "position", (0, 0))[0], "pom.xml is not valid XML: %s" % e))
        return
    r = tree.getroot()
    ns = {"m": r.tag[1:r.tag.index("}")]} if r.tag.startswith("{") else {}
    p = "m:" if ns else ""

    def q(path):
        return "/".join(p + s for s in path.split("/")) if p else path

    def t(el, path):
        x = el.find(path, ns) if el is not None else None
        return (x.text or "").strip() if x is not None and x.text else ""

    out["name"] = t(r, q("artifactId")) or out["name"]
    props = {}
    pe = r.find(q("properties"), ns)
    if pe is not None:
        for child in pe:
            props[child.tag.split("}")[-1]] = (child.text or "").strip()
    for dep in r.findall(q("dependencies/dependency"), ns):
        g, a, v = t(dep, q("groupId")), t(dep, q("artifactId")), t(dep, q("version"))
        out["dependencies"].append({"scope": t(dep, q("scope")) or "compile",
                                    "coord": _expand("%s:%s:%s" % (g, a, v), props), "line": 0})
    for rep in r.findall(q("repositories/repository"), ns):
        out["repositories"].append(t(rep, q("url")))
    for pl in r.findall(q("build/plugins/plugin"), ns):
        out["plugins"].append({"id": t(pl, q("artifactId")), "version": t(pl, q("version"))})
    for key in ("maven.compiler.release", "maven.compiler.target", "maven.compiler.source", "java.version"):
        v = _expand(props.get(key, ""), props)
        if v:
            try:
                out["java"] = int(v.replace("1.", "", 1) if v.startswith("1.") else v)
                break
            except ValueError:
                pass


def _descriptors(root, out):
    res = root / "src" / "main" / "resources"
    for name, kind in (("plugin.yml", "bukkit"), ("paper-plugin.yml", "paper"), ("fabric.mod.json", "fabric"),
                       ("META-INF/mods.toml", "forge"), ("META-INF/neoforge.mods.toml", "neoforge")):
        f = res / name
        if not f.is_file():
            continue
        text = _read(f)
        d = {"file": str(f.relative_to(root)).replace("\\", "/"), "kind": kind}
        if kind in ("bukkit", "paper"):
            top = check_yaml(text).get("top", {})
            d.update({"name": yaml_scalar(top.get("name", "")), "main": yaml_scalar(top.get("main", "")),
                      "api_version": yaml_scalar(top.get("api-version", "")),
                      "depend": yaml_list(text, "depend"), "softdepend": yaml_list(text, "softdepend")})
            for pr in check_plugin_yml(text, name):
                pr["file"] = d["file"]
                out["problems"].append(pr)
            out["loader"] = out["loader"] or "paper"
        elif kind == "fabric":
            try:
                j = json.loads(text)
            except ValueError:
                j = {}
            d.update({"name": j.get("id", ""), "entrypoints": j.get("entrypoints", {}),
                      "depends": j.get("depends", {})})
            out["loader"] = out["loader"] or "fabric"
        else:
            m = re.search(r'(?m)^\s*modId\s*=\s*"([^"]+)"', text)
            d["name"] = m.group(1) if m else ""
            out["loader"] = out["loader"] or kind
        out["descriptors"].append(d)


def _project_problems(root, out):
    probs = out["problems"]
    for d in out["dependencies"]:
        ga = ":".join(d["coord"].split(":")[:2])
        if ga in SERVER_APIS and d["scope"] in ("implementation", "api", "compile", "shadow"):
            probs.append(problem(d.get("line", 0), "%s is declared as '%s', so the server API would be packed into "
                                 "your jar. The server already provides it." % (ga, d["scope"]),
                                 hint="Use compileOnly (Gradle) or <scope>provided</scope> (Maven).",
                                 severity="warning"))
    mc = out.get("mc", "")
    need = java_needed_for(mc) if mc else 0
    if need and out["java"] and out["java"] < need:
        probs.append(problem(0, "This project targets Java %d, but Minecraft %s needs Java %d - classes "
                             "from the server API will not compile against it." % (out["java"], mc, need)))
    for d in out["descriptors"]:
        if d["kind"] in ("bukkit", "paper") and d.get("main"):
            rel = Path("src/main/java") / (d["main"].replace(".", "/") + ".java")
            relk = Path("src/main/kotlin") / (d["main"].replace(".", "/") + ".kt")
            if not (root / rel).is_file() and not (root / relk).is_file() and (root / "src/main").is_dir():
                probs.append(problem(0, "%s names main class %s, but there is no %s." % (d["file"], d["main"],
                                     str(rel).replace("\\", "/")), hint="The server would refuse to load the plugin."))
            api = d.get("api_version", "")
            if api and mc and _mc_tuple(api) > _mc_tuple(mc):
                probs.append(problem(0, "api-version %s is newer than the API version %s you compile against."
                                     % (api, mc), "warning"))


# ---------------------------------------------------------------------------
#  build output
# ---------------------------------------------------------------------------

JAVAC_ERR = re.compile(r"^(?P<file>(?:[A-Za-z]:)?[^:\n]+\.java):(?P<line>\d+):\s*(?P<sev>error|warning):\s*(?P<msg>.+)$")
MAVEN_ERR = re.compile(r"^\[(?P<sev>ERROR|WARNING)\]\s+(?P<file>(?:[A-Za-z]:)?[^\[\n]+\.(?:java|kt)):\[(?P<line>\d+),(?P<col>\d+)\]\s*(?P<msg>.+)$")
KOTLIN_ERR = re.compile(r"^(?P<sev>e|w):\s+(?:file://)?(?P<file>(?:/?[A-Za-z]:)?[^:\n]+\.kts?):(?:\s*\()?(?P<line>\d+)[:,]\s*(?P<col>\d+)\)?:?\s*(?P<msg>.+)$")


def parse_build_output(text, root=None):
    """Errors and warnings from javac, Gradle, Maven or kotlinc output."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    errors, warnings, hints = [], [], []
    failed = False
    i = 0
    while i < len(lines):
        ln = lines[i].rstrip()
        m = JAVAC_ERR.match(ln) or MAVEN_ERR.match(ln) or KOTLIN_ERR.match(ln.strip())
        if m:
            d = m.groupdict()
            entry = {"file": _rel(d["file"].strip(), root), "line": int(d["line"]),
                     "col": int(d.get("col") or 0), "message": d["msg"].strip()}
            # javac prints the source line, a caret, then symbol/location lines
            extra = []
            j = i + 1
            while j < len(lines) and j < i + 6:
                nxt = lines[j].rstrip()
                if re.match(r"^\s+(symbol|location|required|found|reason)\s*:", nxt):
                    extra.append(nxt.strip())
                elif JAVAC_ERR.match(nxt) or MAVEN_ERR.match(nxt) or not nxt.strip():
                    break
                j += 1
            if extra:
                entry["detail"] = "; ".join(extra)
            sev = d["sev"].lower()
            (errors if sev in ("error", "e") else warnings).append(entry)
        if re.search(r"BUILD FAILED|FAILURE: Build failed|\[ERROR\] BUILD FAILURE|COMPILATION ERROR", ln):
            failed = True
        cm = re.search(r"Could not find ([\w.\-]+:[\w.\-]+:[\w.\-+]+)", ln)
        if cm:
            hints.append("Gradle could not download %s - the repository that hosts it is probably missing "
                         "from repositories { }, or the version does not exist." % cm.group(1))
        if "Unsupported class file major version" in ln:
            mm = re.search(r"major version (\d+)", ln)
            if mm:
                hints.append("The Gradle in use is too old for Java %d. Update the wrapper "
                             "(gradle/wrapper/gradle-wrapper.properties)." % (int(mm.group(1)) - 44))
        if "invalid source release" in ln or "invalid target release" in ln:
            hints.append("The JDK running the build is older than the Java version the project asks for.")
        if "JAVA_HOME is not set" in ln or "no java command could be found" in ln.lower():
            hints.append("No JDK was found. Set JAVA_HOME to a JDK, or let the project use a Gradle toolchain.")
        i += 1
    for e in errors:
        h = _compile_hint(e["message"], e.get("detail", ""))
        if h and h not in hints:
            hints.append(h)
    failed = failed or bool(errors)
    summary = ("%d error%s, %d warning%s" % (len(errors), "" if len(errors) == 1 else "s",
                                             len(warnings), "" if len(warnings) == 1 else "s"))
    return {"failed": failed, "errors": errors, "warnings": warnings, "hints": hints,
            "summary": ("build failed: " if failed else "") + summary}


def _rel(path, root):
    if not root:
        return path.replace("\\", "/")
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve())).replace("\\", "/")
    except (ValueError, OSError):
        return path.replace("\\", "/")


def _compile_hint(msg, detail):
    m = msg.lower()
    if "cannot find symbol" in m:
        if "class" in detail and "symbol" in detail:
            return ("'cannot find symbol' for a class usually means a missing import or a dependency that "
                    "is not on the classpath.")
        return "'cannot find symbol' means the name is misspelled, not imported, or not in the API version used."
    if "package" in m and "does not exist" in m:
        return "A package that does not exist means a dependency is missing or has a different version."
    if "unreported exception" in m:
        return "A checked exception must be caught or declared with 'throws'."
    if "incompatible types" in m:
        return "Incompatible types: the value's type does not match where it is used - check the API's return type."
    if "has been deprecated and marked for removal" in m:
        return "Deprecated-for-removal API: it still compiles now but will break in a later server version."
    return ""


# ---------------------------------------------------------------------------
#  stack traces
# ---------------------------------------------------------------------------

EXC_LINE = re.compile(r"^(?:.*?\b(?:Exception in thread \"[^\"]*\"\s+|Caused by:\s+))?"
                      r"(?P<cls>(?:[a-zA-Z_$][\w$]*\.)+[A-Z][\w$]*(?:Exception|Error|Throwable|Failure|Exceptions\.\w+)?)"
                      r"(?::\s*(?P<msg>.*))?$")
FRAME = re.compile(r"^\s*at\s+(?:[\w.]+//)?(?P<cls>[\w$.]+)\.(?P<meth>[\w$<>]+)\((?P<src>[^)]*)\)(?:\s*~?\[(?P<jar>[^\]]*)\])?")
CLASS_VERSIONS = {52: 8, 53: 9, 54: 10, 55: 11, 56: 12, 57: 13, 58: 14, 59: 15, 60: 16, 61: 17, 62: 18,
                  63: 19, 64: 20, 65: 21, 66: 22, 67: 23, 68: 24, 69: 25, 70: 26}
LOG_PREFIX = re.compile(r"^\s*(?:\[[^\]]*\]:?\s*)+")


def parse_stacktraces(text, root=None, packages=None):
    """Every exception chain in a log. Frames inside this project (by package,
    or by a matching source file under ``root``) are marked ``own``."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    traces, cur, chain_head = [], None, None
    context = ""
    for raw in lines:
        line = raw.rstrip()
        bare = LOG_PREFIX.sub("", line) if line.lstrip().startswith("[") else line
        fm = FRAME.match(bare)
        if fm and cur is not None:
            src = fm.group("src")
            f, ln = (src.rsplit(":", 1) + [""])[:2] if ":" in src else (src, "")
            frame = {"cls": fm.group("cls"), "method": fm.group("meth"), "file": f,
                     "line": int(ln) if ln.isdigit() else 0, "jar": fm.group("jar") or ""}
            _own(frame, root, packages)
            cur["frames"].append(frame)
            continue
        if re.match(r"^\s*\.\.\.\s+\d+\s+more", bare):
            continue
        em = EXC_LINE.match(bare.strip())
        caused = "Caused by:" in bare
        is_head = em and (caused or "Exception in thread" in bare or
                          re.search(r"(Exception|Error|Throwable)$", em.group("cls") or ""))
        if is_head:
            exc = {"exception": em.group("cls"), "message": (em.group("msg") or "").strip(), "frames": []}
            if caused and chain_head is not None:
                chain_head["caused_by"].append(exc)
            else:
                exc["caused_by"] = []
                exc["context"] = context
                chain_head = exc
                traces.append(exc)
            cur = exc
            continue
        if bare.strip():
            if cur is not None and not cur["frames"] and not caused:
                # a multi-line message
                cur["message"] = (cur["message"] + "\n" + bare.strip()).strip()
                continue
            cur = None
            chain_head = None
            context = bare.strip()
    for t in traces:
        t["hints"] = _trace_hints(t)
        root_cause = t["caused_by"][-1] if t["caused_by"] else t
        t["root_cause"] = {"exception": root_cause["exception"], "message": root_cause["message"]}
        own = [f for e in [t] + t["caused_by"] for f in e["frames"] if f.get("own")]
        t["own_frames"] = own[:8]
    return traces


def _own(frame, root, packages):
    cls = frame["cls"].split("$")[0]
    if packages and any(cls.startswith(p + ".") or cls == p for p in packages):
        frame["own"] = True
    if root:
        for base in ("src/main/java", "src/main/kotlin", "src/client/java", ""):
            rel = Path(base) / (cls.replace(".", "/") + (".kt" if frame["file"].endswith(".kt") else ".java"))
            if (Path(root) / rel).is_file():
                frame["own"] = True
                frame["source"] = str(rel).replace("\\", "/")
                break


def _trace_hints(t):
    hints = []
    every = [t] + t.get("caused_by", [])
    for e in every:
        cls, msg = e["exception"], e["message"] or ""
        short = cls.rsplit(".", 1)[-1]
        if short in ("NoClassDefFoundError", "ClassNotFoundException"):
            hints.append("A class (%s) was missing at runtime. Usually a library that was compiled against but "
                         "not shaded into the jar, a plugin dependency missing from depend: in plugin.yml, or "
                         "code written for a different server type." % (msg.split()[0] if msg else "?"))
        elif short in ("NoSuchMethodError", "NoSuchFieldError", "AbstractMethodError", "IncompatibleClassChangeError"):
            hints.append("The code was compiled against a different version of an API than the one running - "
                         "update the plugin/mod, or build it against this server's version.")
        elif short == "UnsupportedClassVersionError":
            vs = [int(float(x)) for x in re.findall(r"class file versions? (?:up to )?(\d+(?:\.\d+)?)", msg)]
            if len(vs) >= 2:
                hints.append("It was compiled for Java %s but the server runs Java %s. Run the server on Java %s "
                             "or newer." % (CLASS_VERSIONS.get(vs[0], "?"), CLASS_VERSIONS.get(vs[1], "?"),
                                            CLASS_VERSIONS.get(vs[0], "?")))
            else:
                hints.append("It was compiled for a newer Java than the one running the server.")
        elif short == "IllegalStateException" and re.search(r"Asynchronous .+!|must be called on|main thread", msg, re.I):
            hints.append("Bukkit API used off the main thread. Move that call into Bukkit.getScheduler().runTask(...).")
        elif short == "IllegalPluginAccessException":
            hints.append("A plugin scheduled a task or registered a listener while disabled - often from "
                         "onDisable, or after an error during onEnable.")
        elif short in ("InvalidPluginException", "InvalidDescriptionException"):
            hints.append("The server refused the plugin: check plugin.yml (name, version, main) and that main "
                         "names a class in the jar.")
        elif short == "ConcurrentModificationException":
            hints.append("A collection was changed while it was being looped over. Loop over a copy, or use an "
                         "Iterator's remove().")
        elif short == "NullPointerException":
            hints.append("Something was null. Java's message says what: %s" % msg if msg else
                         "Something was null - the first frame in this project is where.")
        elif short == "StackOverflowError":
            hints.append("Endless recursion: a method (or an event handler firing its own event) calls itself.")
        elif short == "OutOfMemoryError":
            hints.append("The JVM ran out of memory (%s). Give the server more with -Xmx, or look for a leak if "
                         "it grows over time." % (msg or "heap"))
        elif "mixin" in cls.lower() or "Mixin" in msg:
            m = re.search(r"([\w.\-]+\.mixins?\.json)", msg)
            hints.append("A mixin failed to apply%s - usually two mods changing the same code, or a mod for a "
                         "different Minecraft/loader version." % (" (from %s)" % m.group(1) if m else ""))
        elif short == "ModLoadingException" or "Missing or unsupported mandatory dependencies" in msg:
            hints.append("A mod's required dependency is missing or the wrong version.")
    out = []
    for h in hints:
        if h not in out:
            out.append(h)
    return out


# ---------------------------------------------------------------------------
#  Java source heuristics
# ---------------------------------------------------------------------------

BLOCKING = [(re.compile(r"\bThread\.sleep\s*\("), "Thread.sleep blocks the server thread"),
            (re.compile(r"\.openConnection\s*\(|\bHttpClient\b[^;]*\.send\s*\("), "a network call blocks the server thread"),
            (re.compile(r"\bDriverManager\.getConnection\s*\(|\.executeQuery\s*\(|\.executeUpdate\s*\("),
             "a database call blocks the server thread"),
            (re.compile(r"\.join\s*\(\s*\)|\.get\s*\(\s*\)\s*;"), "waiting on a future blocks the server thread")]


def _strip_java(text):
    """Blank out comments and string contents, keeping line numbers."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(re.sub(r"[^\n]", " ", text[i:j]))
            i = j
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            out.append(c + " " * max(0, j - i - 1) + (c if j < n else ""))
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


def java_lint(path, text):
    """Patterns that hurt a server: blocking calls inside event handlers,
    every-tick timers, threads. Heuristics, and labelled as such."""
    problems = []
    src = _strip_java(text or "")
    for m in re.finditer(r"@EventHandler[^{;]*?\)\s*(?:throws[^{]+)?\{", src):
        start = m.end() - 1
        depth, j = 0, start
        while j < len(src):
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        body = src[start:j]
        for rx, why in BLOCKING:
            for bm in rx.finditer(body):
                line = src.count("\n", 0, start + bm.start()) + 1
                problems.append(problem(line, "Inside an event handler: %s, which freezes every player until it "
                                        "returns." % why, "warning",
                                        hint="Do the slow part in runTaskAsynchronously and come back with runTask."))
    for m in re.finditer(r"\brunTaskTimer(?:Asynchronously)?\s*\([^;]*?,\s*\d+L?\s*,\s*1L?\s*\)", src):
        problems.append(problem(src.count("\n", 0, m.start()) + 1, "A timer that runs every tick - check it "
                                "really needs to.", "info"))
    for m in re.finditer(r"\bnew\s+Thread\s*\(", src):
        problems.append(problem(src.count("\n", 0, m.start()) + 1, "A raw Thread: Bukkit's scheduler (or an "
                                "executor shut down in onDisable) is easier to stop cleanly on reload.", "info"))
    legacy_colour = (re.search(r"\bimport\s+org\.bukkit\.ChatColor\b", src)
                     or re.search(r"\bimport\s+net\.md_5\.bungee\.api\.ChatColor\b", src))
    if legacy_colour and re.search(r"\bimport\s+(?:io\.papermc|net\.kyori)\.", src):
        problems.append(problem(1, "ChatColor is the legacy colour API; on Paper the Adventure API "
                                "(Component, NamedTextColor) is the supported one.", "info"))
    return _result("java", problems, {"file": path})
