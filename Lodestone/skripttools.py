"""Reading Skript scripts without inventing Skript.

Two kinds of answer come out of here, and they are kept apart:

* **What Skript itself would say about the file's shape.** Comments, block
  comments, indentation and sections follow Skript's own config reader
  (``ch.njol.skript.config.Node.splitLine`` and ``SectionNode.load``), ported
  rule for rule: the indent is fixed by the first indented line, a line deeper
  than its section allows is an error, an empty section is a warning.
* **Static observations** - the commands, events, functions and variables in a
  script, repeating tasks that loop over the whole world, ``while`` loops that
  never wait, functions that are called but defined nowhere. These are labelled
  as observations: whether a line is valid Skript is for Skript to say.

That last part is :func:`parse_reload`: it reads what ``sk reload <script>``
printed in the server console, which is the only authority on syntax - the
installed Skript version plus the installed addons.

Standard library only.
"""

import os
import re
import zipfile

# Skript's built-in functions (DefaultFunctions.java, SkriptLang/Skript master)
BUILTIN_FUNCTIONS = {
    "abs", "acos", "asin", "atan", "atan2", "axisangle", "calcexperience", "ceil", "ceiling", "clamp",
    "combinations", "concat", "cos", "date", "entity", "exp", "factorial", "floor", "formatnumber",
    "frombase", "isnan", "ln", "location", "log", "max", "mean", "median", "min", "mod", "offlineplayer",
    "permutations", "player", "product", "quaternion", "rgb", "root", "round", "sin", "sqrt", "sum", "tan",
    "tobase", "uuid", "vector", "world",
}

# The message Skript gives an empty section (SectionNode.M_EMPTY_SECTION)
EMPTY_SECTION = ("Empty configuration section! You might want to indent one or more of the subsequent lines "
                 "to make them belong to this section or remove the colon at the end of the line if you don't "
                 "want this line to start a section.")

# Phrases that only exist in an addon's syntax. Used only to say "this appears
# to need X, which is not installed" - never to judge whether a line is valid.
ADDON_MARKERS = {
    "skript-reflect": [r"\}\s*\.\s*[a-zA-Z_]\w*\s*\(", r"\bnew\s+[a-z]+(?:\.[a-z]+)+\.[A-Z]\w*\s*\("],
    "SkBee": [r"\bnbt (?:compound|of|from)\b", r"\btext component\b", r"\bvirtual (?:furnace|merchant)\b"],
    "skript-gui": [r"\bcreate a (?:new )?gui\b", r"\b(?:make|format) (?:a )?gui slot\b"],
    "skript-yaml": [r"\byaml (?:value|list|nodes?|path)\b", r"\bload yaml\b"],
}
ADDON_NAMES = {"skript-reflect": ("skript-reflect", "skript-mirror"), "SkBee": ("skbee",),
               "skript-gui": ("skript-gui", "tuske"), "skript-yaml": ("skript-yaml",)}

TICKS = {"tick": 1, "second": 20, "minute": 1200, "hour": 72000, "day": 1728000}


# ---------------------------------------------------------------------------
#  Skript's own line reader
# ---------------------------------------------------------------------------

def split_line(line, in_block):
    """Node.splitLine: (value, comment, in_block). '#' starts a comment unless
    it is inside a string; '##' outside a string is an escaped '#'. The
    states and transitions are Skript's own."""
    trimmed = line.strip()
    if trimmed == "###":
        return "", line, not in_block
    if trimmed.startswith("#"):
        return "", line[line.index("#"):], in_block
    if in_block:
        return "", line, in_block
    out = []
    state, prev = "CODE", "CODE"
    i, n = 0, len(line)
    while i < n:
        c = line[i]
        if c in '%"#':
            if (c != "#" or state != "STRING") and i + 1 < n and line[i + 1] == c:
                out.append(c if c == "#" else c + c)
                i += 2
                continue
            tmp = state
            if c == "%":
                state = prev if state == "CODE" else "CODE"
            elif c == '"':
                state = {"CODE": "STRING", "STRING": "CODE"}.get(state, state)
            elif c == "#":
                if state != "STRING":
                    return "".join(out), line[i:], in_block
            if c == "%" and state == "CODE":
                prev = tmp
        # Skript's state machine knows '{' and '}', but its loop only ever
        # feeds it '%', '"' and '#', so braces never change the state.
        out.append(c)
        i += 1
    return "".join(out), "", in_block


def _readable(ws):
    if re.fullmatch(r" +", ws):
        return "%d space%s" % (len(ws), "" if len(ws) == 1 else "s")
    if re.fullmatch(r"\t+", ws):
        return "%d tab%s" % (len(ws), "" if len(ws) == 1 else "s")
    return "'" + ws.replace("\t", "->").replace(" ", "_") + "' [-> = tab, _ = space]"


def read_nodes(text):
    """The script as Skript's config reader sees it: a list of nodes with their
    level, plus the problems the reader itself would report."""
    problems = []
    nodes = []
    unit = None
    in_block = False
    block_line = 0
    expect = 0          # the level the next line may have at most
    open_sections = []  # node indexes of sections, by level
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for idx, raw in enumerate(lines):
        ln = idx + 1
        was_block = in_block
        value, comment, in_block = split_line(raw, in_block)
        if in_block and not was_block:
            block_line = ln
        if not value.strip():
            continue
        ws = value[:len(value) - len(value.lstrip())]
        if ws and unit is None:
            if re.fullmatch(r" +", ws) or re.fullmatch(r"\t+", ws):
                unit = ws
            else:
                problems.append(_p(ln, "indentation error: indent must only consist of either spaces or tabs, "
                                   "but not mixed (found %s)" % _readable(ws)))
                continue
        if ws:
            k = 0
            rest = ws
            while unit and rest.startswith(unit):
                rest = rest[len(unit):]
                k += 1
            if rest:
                problems.append(_p(ln, "indentation error: expected %d %s, but found %s"
                                   % (expect * len(unit or " "), _unit_name(unit), _readable(ws))))
                continue
        else:
            k = 0
        if k > expect:
            problems.append(_p(ln, "indentation error: expected %d %s, but found %s"
                               % (expect * len(unit or " "), _unit_name(unit), _readable(ws))))
            continue
        # leaving sections: any that never got a line are empty
        while len(open_sections) > k:
            sec = open_sections.pop()
            if not nodes[sec]["children"]:
                problems.append(_p(nodes[sec]["line"], EMPTY_SECTION, "warning"))
        v = value.strip()
        section = v.endswith(":")
        node = {"line": ln, "level": k, "text": v[:-1].rstrip() if section else v, "section": section,
                "comment": comment, "children": [], "parent": open_sections[-1] if open_sections else None}
        nodes.append(node)
        if node["parent"] is not None:
            nodes[node["parent"]]["children"].append(len(nodes) - 1)
        if section:
            open_sections.append(len(nodes) - 1)
            expect = k + 1
        else:
            expect = k
    while open_sections:
        sec = open_sections.pop()
        if not nodes[sec]["children"]:
            problems.append(_p(nodes[sec]["line"], EMPTY_SECTION, "warning"))
    if in_block:
        problems.append(_p(block_line, "A block comment (###) was opened on line %d but never closed." % block_line))
    return nodes, problems, unit


def _unit_name(unit):
    return "tab" + "s" if unit and unit[0] == "\t" else "spaces"


def _p(line, message, severity="error", hint="", source="skript-reader"):
    out = {"line": line, "col": 0, "severity": severity, "message": message, "source": source}
    if hint:
        out["hint"] = hint
    return out


# ---------------------------------------------------------------------------
#  what is in a script
# ---------------------------------------------------------------------------

def _code_parts(line):
    """The code in a line: string contents dropped, but %expressions% inside
    strings kept, since those are code too."""
    out, i, n, in_str = [], 0, len(line), False
    while i < n:
        c = line[i]
        if c == '"':
            if in_str and i + 1 < n and line[i + 1] == '"':
                i += 2
                continue
            in_str = not in_str
            out.append(" ")
            i += 1
            continue
        if in_str:
            if c == "%" and i + 1 < n and line[i + 1] == "%":
                i += 2
                continue
            if c == "%":
                j = line.find("%", i + 1)
                if j < 0:
                    break
                out.append(" " + line[i + 1:j] + " ")
                i = j + 1
                continue
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def variables_in(line):
    """Every {variable} in a line, with %expression% parts shown as *."""
    found, i, n = [], 0, len(line)
    while i < n:
        if line[i] == "{":
            depth, j = 0, i
            while j < n:
                if line[j] == "{":
                    depth += 1
                elif line[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            if j >= n:
                break
            name = re.sub(r"%[^%]*%", "*", line[i + 1:j]).strip()
            if name and not name.startswith("@"):
                found.append(name)
            i = j + 1
        else:
            i += 1
    return found


def _period_ticks(spec):
    """'every 5 seconds' -> 100; None when the period cannot be read."""
    m = re.match(r"^every\s+(?:(\d+(?:\.\d+)?|a|an|one)\s+)?(?:real\s+|minecraft\s+|mc\s+)?"
                 r"(tick|second|minute|hour|day)s?\b", spec.strip(), re.I)
    if not m:
        return None
    n = m.group(1)
    num = 1.0 if n in (None, "a", "an", "one") else float(n)
    return num * TICKS[m.group(2).lower()]


def analyze(text, name=""):
    """Structures, variables and functions in one script, plus observations."""
    nodes, problems, unit = read_nodes(text)
    info = {"file": name, "commands": [], "events": [], "functions": [], "periodic": [], "options": {},
            "structures": 0, "variables": {"global": {}, "list": {}, "local": set()}, "calls": [],
            "indent": _readable(unit) if unit else "", "addon_markers": {}}

    def body_of(i):
        out = []
        stack = list(nodes[i]["children"])
        while stack:
            j = stack.pop(0)
            out.append(j)
            stack[0:0] = nodes[j]["children"]
        return out

    for i, nd in enumerate(nodes):
        t = nd["text"]
        if nd["level"] == 0:
            info["structures"] += 1
            low = t.lower()
            # top-level lines without a colon exist ('using queues'), so
            # which structures are valid is left to Skript
            if not nd["section"]:
                pass
            elif low.startswith("command "):
                cname = t.split(None, 1)[1].split()[0] if len(t.split()) > 1 else ""
                entries = {nodes[c]["text"].split(":", 1)[0].strip().lower(): nodes[c]
                           for c in nd["children"]}
                perm = ""
                for c in nd["children"]:
                    ct = nodes[c]["text"]
                    if ct.lower().startswith("permission:"):
                        perm = ct.split(":", 1)[1].strip()
                info["commands"].append({"name": cname.lstrip("/"), "line": nd["line"], "permission": perm,
                                         "args": t.split(None, 2)[2] if len(t.split(None, 2)) > 2 else ""})
                if "trigger" not in entries:
                    problems.append(_p(nd["line"], "Command %s has no 'trigger:' section, which every command "
                                       "needs." % cname, source="static"))
            elif low.startswith("on ") or low.startswith("on\t"):
                info["events"].append({"event": t[3:].strip(), "line": nd["line"]})
            elif low.startswith("function ") or low.startswith("local function "):
                m = re.match(r"^(?:local\s+)?function\s+([A-Za-z_][\w]*)\s*\((.*?)\)\s*(?:::\s*(.+)|returns\s+(.+))?$",
                             t, re.I)
                if m:
                    info["functions"].append({"name": m.group(1), "params": m.group(2).strip(),
                                              "returns": (m.group(3) or m.group(4) or "").strip(),
                                              "line": nd["line"], "local": low.startswith("local")})
                else:
                    problems.append(_p(nd["line"], "This does not look like 'function name(params) :: type:'.",
                                       "warning", source="static"))
            elif low.startswith("every "):
                ticks = _period_ticks(t)
                body = body_of(i)
                info["periodic"].append({"spec": t, "line": nd["line"], "ticks": ticks})
                _periodic_checks(nd, ticks, [nodes[j] for j in body], problems)
            elif low == "options":
                for c in nd["children"]:
                    ct = nodes[c]["text"]
                    if ":" in ct:
                        k, v = ct.split(":", 1)
                        info["options"][k.strip()] = v.strip()
        code = _code_parts(t)
        for v in variables_in(t):
            if v.startswith("_"):
                info["variables"]["local"].add(v)
                continue
            kind = "list" if "::" in v else "global"
            rec = info["variables"][kind].setdefault(v, {"set": [], "read": []})
            writes = re.match(r"^\s*(set|add|remove|delete|clear|reset)\b", t, re.I)
            (rec["set"] if writes and _target_is(t, v) else rec["read"]).append(nd["line"])
        if not t.lower().startswith(("function ", "local function ")):
            # a call is name( with no space; 'size of (x)' is not one
            for m in re.finditer(r"(?<![\w.\]}])([A-Za-z_]\w*)\(", code):
                if re.search(r"\bnew\s+$", code[:m.start()]):
                    continue
                info["calls"].append({"name": m.group(1), "line": nd["line"]})
        wm = re.match(r"^(?:do\s+)?while\s+(.*)$", t, re.I) if nd["section"] else None
        if wm:
            body = [nodes[j] for j in body_of(i)]
            if not any(re.match(r"^(wait|stop|exit|return|halt)\b", b["text"], re.I) for b in body):
                constant = re.match(r"^(?:true(?: is true)?|1 is 1|1 = 1)\s*$", wm.group(1).strip(), re.I)
                if constant:
                    problems.append(_p(nd["line"], "This while loop's condition never changes and nothing inside "
                                       "waits or exits, so it never ends - the server hangs on it.", "warning",
                                       hint="Add 'exit loop' when done, or 'wait 1 tick' inside it.",
                                       source="static"))
                else:
                    problems.append(_p(nd["line"], "A while loop with no 'wait' runs entirely within one tick. "
                                       "Fine for short loops; if the condition never turns false the server hangs.",
                                       "info", source="static"))
        rm = re.search(r"\bblocks? (?:with)?in (?:a )?radius (\d+)", t, re.I)
        if rm and int(rm.group(1)) >= 16:
            n = int(rm.group(1))
            problems.append(_p(nd["line"], "Looping over blocks in radius %d touches about %d blocks each time."
                               % (n, int(4.19 * n ** 3)), "warning", source="static"))
        if nd["level"] == 0 and nd["section"] and t.lower() == "import":
            info["addon_markers"].setdefault("skript-reflect", []).append(nd["line"])
        for addon, pats in ADDON_MARKERS.items():
            if any(re.search(p, t, re.I) for p in pats):
                info["addon_markers"].setdefault(addon, []).append(nd["line"])
    info["variables"]["local"] = sorted(info["variables"]["local"])
    return info, problems


def _target_is(line, var):
    """True when a set/add/remove/delete line writes to this variable."""
    low = line.lower()
    m = re.match(r"^\s*(set|delete|clear|reset)\s+(.*)$", low)
    if m:
        target = m.group(2).split(" to ")[0] if m.group(1) == "set" else m.group(2)
        return var.lower() in re.sub(r"%[^%]*%", "*", target)
    m = re.match(r"^\s*(add|remove)\s+.*\b(?:to|from)\s+(.*)$", low)
    if m:
        return var.lower() in re.sub(r"%[^%]*%", "*", m.group(2))
    return False


def _periodic_checks(nd, ticks, body, problems):
    texts = [b["text"].lower() for b in body]
    if ticks is not None and ticks <= 1:
        problems.append(_p(nd["line"], "This runs every tick - 20 times a second. Make sure it has to.", "warning",
                           source="static"))
    heavy = [t for t in texts if re.search(r"\bloop all (?:entities|mobs|living entities|blocks|items|"
                                           r"dropped items)\b|\bloop (?:all )?blocks? (?:with)?in radius\b", t)]
    if heavy and (ticks is None or ticks <= 100):
        problems.append(_p(nd["line"], "A repeating task that loops over every entity or block %s. On a busy "
                           "server that costs tick time every run." % ("every tick" if ticks == 1 else
                                                                      "every %s ticks" % int(ticks) if ticks else ""),
                           "warning", hint="Run it less often, or react to an event instead of polling.",
                           source="static"))
    elif ticks is not None and ticks < 20 and any("loop all players" in t for t in texts):
        problems.append(_p(nd["line"], "Loops over all players %d times a second." % int(20 / max(ticks, 1)),
                           "info", source="static"))


def check(text, name=""):
    """The single-file check used by devtools.validate."""
    info, problems = analyze(text, name)
    errs = sum(1 for p in problems if p["severity"] == "error")
    warns = sum(1 for p in problems if p["severity"] == "warning")
    problems.sort(key=lambda p: (p["line"], p["severity"] != "error"))
    return {"format": "skript", "ok": errs == 0, "errors": errs, "warnings": warns, "problems": problems,
            "summary": ("no problems in the file's structure" if not problems else
                        "%d error%s, %d warning%s" % (errs, "" if errs == 1 else "s", warns,
                                                       "" if warns == 1 else "s")),
            "note": "Structure follows Skript's own reader; whether each line is valid syntax is for "
                    "Skript to say - reload it on the server to check.",
            "info": _public(info)}


def _public(info):
    out = dict(info)
    out["variables"] = {"global": sorted(info["variables"]["global"]), "list": sorted(info["variables"]["list"]),
                        "local": info["variables"]["local"]}
    out.pop("calls", None)
    return out


# ---------------------------------------------------------------------------
#  a server's scripts and addons
# ---------------------------------------------------------------------------

def _plugin_yml(jar):
    try:
        with zipfile.ZipFile(jar) as z:
            for n in ("plugin.yml", "paper-plugin.yml"):
                if n in z.namelist():
                    return z.read(n).decode("utf-8", "replace")
    except (zipfile.BadZipFile, OSError, KeyError):
        return ""
    return ""


def _yml_field(text, key):
    m = re.search(r"(?m)^%s:\s*['\"]?([^'\"\n#]+)" % re.escape(key), text)
    return m.group(1).strip() if m else ""


def _yml_names(text, key):
    m = re.search(r"(?m)^%s:\s*\[([^\]]*)\]" % re.escape(key), text)
    if m:
        return [x.strip().strip("'\"") for x in m.group(1).split(",") if x.strip()]
    m = re.search(r"(?m)^%s:\s*\n((?:\s*-\s*.+\n?)+)" % re.escape(key), text)
    if m:
        return [x.strip().lstrip("-").strip().strip("'\"") for x in m.group(1).splitlines() if x.strip()]
    v = _yml_field(text, key)
    return [v] if v else []


def environment(server_root):
    """The installed Skript and its addons, read from the plugin jars."""
    root = os.path.join(server_root or "", "plugins")
    out = {"skript": None, "addons": [], "scripts_dir": "", "scripts": []}
    if not os.path.isdir(root):
        return out
    for fn in sorted(os.listdir(root)):
        if not fn.lower().endswith(".jar"):
            continue
        text = _plugin_yml(os.path.join(root, fn))
        if not text:
            continue
        name = _yml_field(text, "name")
        ver = _yml_field(text, "version")
        deps = [d.lower() for d in _yml_names(text, "depend") + _yml_names(text, "softdepend")]
        if name.lower() == "skript":
            out["skript"] = {"version": ver, "jar": fn}
        elif "skript" in deps:
            out["addons"].append({"name": name, "version": ver, "jar": fn})
    sd = os.path.join(root, "Skript", "scripts")
    if os.path.isdir(sd):
        out["scripts_dir"] = "plugins/Skript/scripts"
        for dp, _dn, files in os.walk(sd):
            for f in files:
                if f.endswith(".sk"):
                    rel = os.path.relpath(os.path.join(dp, f), sd).replace("\\", "/")
                    out["scripts"].append({"name": rel, "disabled": os.path.basename(rel).startswith("-")})
        out["scripts"].sort(key=lambda s: s["name"])
    return out


def workspace_report(scripts, env=None):
    """Across every script: functions called but defined nowhere, global
    variables only ever read or only ever written, duplicate commands, and
    syntax that appears to need an addon that is not installed.

    ``scripts`` is {name: text}.
    """
    env = env or {}
    installed = {a["name"].lower() for a in env.get("addons", [])}
    per_file, defined, calls, cmds = {}, {}, [], {}
    gvars = {}
    markers = {}
    for name, text in scripts.items():
        info, problems = analyze(text, name)
        per_file[name] = {"problems": problems, "info": _public(info)}
        for f in info["functions"]:
            defined.setdefault(f["name"].lower(), []).append((name, f["line"]))
        for c in info["calls"]:
            calls.append((name, c))
        for c in info["commands"]:
            cmds.setdefault(c["name"].lower(), []).append((name, c["line"]))
        for kind in ("global", "list"):
            for v, rec in info["variables"][kind].items():
                g = gvars.setdefault(v, {"set": [], "read": []})
                g["set"] += [(name, ln) for ln in rec["set"]]
                g["read"] += [(name, ln) for ln in rec["read"]]
        for addon, lines in info["addon_markers"].items():
            markers.setdefault(addon, []).extend((name, ln) for ln in lines)

    cross = []
    for name, c in calls:
        low = c["name"].lower()
        if low in BUILTIN_FUNCTIONS or low in defined:
            continue
        cross.append({"file": name, "line": c["line"], "severity": "warning", "source": "static",
                      "message": "%s() is not defined in any script here and is not one of Skript's built-in "
                                 "functions." % c["name"],
                      "hint": "Check the spelling, or that the script defining it is loaded."})
    for cname, where in cmds.items():
        if len(where) > 1:
            for name, ln in where[1:]:
                cross.append({"file": name, "line": ln, "severity": "warning", "source": "static",
                              "message": "/%s is also defined in %s line %d; the scripts will fight over it."
                                         % (cname, where[0][0], where[0][1])})
    for v, rec in gvars.items():
        if "*" in v:
            continue
        if rec["read"] and not rec["set"]:
            name, ln = rec["read"][0]
            cross.append({"file": name, "line": ln, "severity": "info", "source": "static",
                          "message": "{%s} is read but never set in these scripts (it may be set by a command "
                                     "or from saved data)." % v})
    missing = []
    if env.get("skript") is not None or env.get("addons") is not None:
        for addon, where in markers.items():
            if any(n in installed for n in ADDON_NAMES.get(addon, (addon.lower(),))):
                continue
            missing.append({"addon": addon, "where": where[:5]})
            name, ln = where[0]
            cross.append({"file": name, "line": ln, "severity": "warning", "source": "static",
                          "message": "This appears to use %s syntax, and %s is not installed on this server."
                                     % (addon, addon),
                          "hint": "Install %s, or rewrite it with Skript's own syntax." % addon})
    return {"files": per_file, "cross": cross, "functions": {k: v for k, v in defined.items()},
            "commands": sorted(cmds), "missing_addons": missing, "environment": env}


# ---------------------------------------------------------------------------
#  what Skript said
# ---------------------------------------------------------------------------

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
SECTION_CODES = re.compile(r"\u00a7[0-9a-fk-orx]", re.I)
PREFIX = re.compile(r"^\s*(?:\[[^\]]*\]:?\s*)+")


def _clean(line):
    raw = line
    s = ANSI.sub("", line)
    s = SECTION_CODES.sub("", s)
    s = PREFIX.sub("", s) if s.lstrip().startswith("[") else s
    s = re.sub(r"^\[Skript\]\s*", "", s.strip())
    red = bool(re.search(r"\x1b\[[0-9;]*\b(?:31|91)\b[0-9;]*m|\u00a7c", raw))
    gold = bool(re.search(r"\x1b\[[0-9;]*\b(?:33|93)\b[0-9;]*m|\u00a76", raw))
    return s, red, gold


def parse_reload(lines, script=None):
    """Read console lines printed by 'sk reload <script>'.

    Messages come from Skript's english.lang: 'Reloading %s...', a block per
    problem starting 'Line %s: (%s)' and ending '    Line: <code>', then
    'Encountered %s errors while reloading %s! (%sms)' or 'Successfully
    reloaded %s. (%sms)'. Skript prints errors in red and warnings in gold; a
    piped console usually loses the colours, so each entry's severity is only
    known when they survived, and the error count always comes from Skript's
    own summary line.
    """
    entries, cur = [], None
    status, count, ms, seen_start = "unknown", 0, 0, False
    target = (script or "").lower()
    for raw in lines:
        s, red, gold = _clean(raw)
        if not s:
            continue
        m = re.match(r"^Reloading (.+?)\.\.\.$", s)
        if m:
            seen_start = True
            continue
        m = re.match(r"^Line (\d+): \((.+?)\)$", s)
        if m:
            cur = {"line": int(m.group(1)), "file": m.group(2), "message": "", "code": "",
                   "severity": "error" if red else ("warning" if gold else "")}
            entries.append(cur)
            continue
        m = re.match(r"^Encountered (\d+) errors? while (?:reloading|parsing) (.+?)!(?: \((\d+)ms\))?", s)
        if m:
            status, count, ms = "errors", int(m.group(1)), int(m.group(3) or 0)
            cur = None
            continue
        m = re.match(r"^Successfully reloaded (.+?)\.(?: \((\d+)ms\))?$", s)
        if m:
            status, ms = ("ok" if not entries else "warnings"), int(m.group(2) or 0)
            cur = None
            continue
        if re.match(r"^All scripts loaded without errors", s):
            status = "ok"
            continue
        if cur is not None:
            lm = re.match(r"^Line:\s*(.*)$", s)
            if lm:
                cur["code"] = lm.group(1)
                cur = None
            else:
                cur["message"] = (cur["message"] + " " + s).strip()
    if target:
        entries = [e for e in entries if not e["file"] or target in e["file"].lower()
                   or e["file"].lower() in target]
    if status == "ok" and entries:
        status = "warnings"
    if status == "errors":
        unknown = [e for e in entries if not e["severity"]]
        if len(unknown) == len(entries) and count == len(entries):
            for e in entries:
                e["severity"] = "error"
    return {"status": status if (seen_start or status != "unknown") else "no-output",
            "error_count": count, "ms": ms, "entries": entries}
