"""The development assistant: a local model that works inside a project.

It reads a whole project rather than one file, edits files, runs the build and
tries again - and every change and every command is shown to the person first.

**Workspaces.** Every local server is one, and any folder can be added as
another (a plugin or mod being written). Tools are confined to the
workspace's folder; nothing outside it can be read or written.

**The loop.** One message from the user is a *run*. A run asks the model what
to do, carries out the tool calls it makes, hands back the results, and
repeats until the model answers in words, asks a question, hits the step
limit, or is cancelled. With a model that can call tools Ollama's native tool
calling is used; with one that cannot, the same tools are offered through a
JSON reply format instead.

**Nothing changes unseen.** A write or edit becomes an approval card with the
diff and the result of checking the new content; a command shows its exact
argument list. Commands must also be on the allowlist - build tools and
read-only git by default. Every file a run changes is backed up first and can
be put back.

**Memory.** A project index (files, languages, symbols, build system) is kept
up to date in the background and can be paused. Notes the model is asked to
remember are kept per workspace and given to it at the start of every run.

**Events.** A run is a list of numbered events the UI reads with
``events(run, since, wait)`` - status changes, streamed text, tool calls and
results, approvals, plans, file changes - so progress is visible as it happens.

Standard library only; talks to the model through :mod:`ollama`.
"""

import difflib
import fnmatch
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
import uuid
from collections import deque
from pathlib import Path

import devtools
import ollama as ol
import skripttools

MAX_READ_LINES = 400
MAX_READ_BYTES = 48_000
MAX_TOOL_RESULT = 12_000
MAX_EVENTS = 4000

SKIP_DIRS = {".git", ".gradle", ".idea", ".vscode", ".svn", ".hg", "node_modules", "build", "out", "target",
             "bin", "obj", "dist", "__pycache__", ".lodestone", "libraries", "versions", "cache", ".cache",
             "logs", "crash-reports", "backups", "world", "world_nether", "world_the_end", "bundler",
             ".fabric", ".quilt", "run", ".lodestone-update", "debug", "journeymap", "playerdata", "region"}
TEXT_EXT = {".java", ".kt", ".kts", ".gradle", ".xml", ".yml", ".yaml", ".json", ".toml", ".properties",
            ".sk", ".md", ".txt", ".cfg", ".conf", ".ini", ".sh", ".bat", ".cmd", ".mcmeta", ".lang",
            ".js", ".ts", ".py", ".groovy", ".mcfunction", ".csv", ".html", ".css", ".snbt", ".accesswidener",
            ".cs", ".rs", ".go", ".sql", ".env", ""}
LANG = {".java": "Java", ".kt": "Kotlin", ".kts": "Kotlin", ".gradle": "Gradle", ".xml": "XML", ".yml": "YAML",
        ".yaml": "YAML", ".json": "JSON", ".toml": "TOML", ".properties": "Properties", ".sk": "Skript",
        ".md": "Markdown", ".js": "JavaScript", ".ts": "TypeScript", ".py": "Python", ".mcfunction": "mcfunction",
        ".groovy": "Groovy", ".cs": "C#", ".sh": "Shell", ".bat": "Batch"}
KEY_FILES = ("build.gradle.kts", "build.gradle", "settings.gradle.kts", "settings.gradle", "pom.xml",
             "gradle.properties", "src/main/resources/plugin.yml", "src/main/resources/paper-plugin.yml",
             "src/main/resources/fabric.mod.json", "src/main/resources/META-INF/mods.toml",
             "src/main/resources/META-INF/neoforge.mods.toml", "server.properties", "eula.txt",
             "bukkit.yml", "spigot.yml", "config/paper-global.yml", "purpur.yml", "plugins/Skript/config.sk")

# executables a run may use without the user adding anything (still approved one by one)
DEFAULT_COMMANDS = {"gradlew": None, "gradle": None, "mvn": None, "mvnw": None, "java": ["-version", "--version"],
                    "javac": ["-version", "--version"], "git": ["status", "diff", "log", "show", "branch",
                                                                "rev-parse", "ls-files", "blame"]}
BUILD_TOOLS = {"gradlew", "gradle", "mvn", "mvnw"}
BAT_UNSAFE = re.compile(r"[&|<>^%!\"\r\n`]")


class AgentError(RuntimeError):
    """Something to tell the user in a sentence."""


def now():
    return time.time()


def new_id():
    return uuid.uuid4().hex[:12]


def _read_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(str(tmp), str(path))


def _clip(text, n=MAX_TOOL_RESULT):
    text = text or ""
    if len(text) <= n:
        return text
    head = n * 2 // 3
    return text[:head] + "\n... [%d characters cut] ...\n" % (len(text) - n) + text[-(n - head):]


# ---------------------------------------------------------------------------
#  workspace paths
# ---------------------------------------------------------------------------

class Sandbox:
    """Resolves paths inside one folder and refuses anything that leaves it,
    symlinks included."""

    def __init__(self, root):
        self.root = Path(root).resolve()

    def resolve(self, rel, must_exist=False):
        rel = (rel or ".").strip().replace("\\", "/")
        if rel.startswith("~"):
            raise AgentError("Paths are relative to the workspace folder.")
        p = Path(rel)
        p = (p if p.is_absolute() else self.root / p).resolve()
        try:
            p.relative_to(self.root)
        except ValueError:
            raise AgentError("'%s' is outside the workspace folder." % rel)
        if must_exist and not p.exists():
            raise AgentError("'%s' does not exist." % rel)
        return p

    def rel(self, p):
        try:
            return str(Path(p).resolve().relative_to(self.root)).replace("\\", "/") or "."
        except ValueError:
            return str(p)


def _is_text_file(p, limit=2_000_000):
    try:
        if p.stat().st_size > limit:
            return False
        if p.suffix.lower() not in TEXT_EXT:
            return False
        with open(p, "rb") as f:
            return b"\0" not in f.read(4096)
    except OSError:
        return False


def _walk(root, cancel=None, limit=8000):
    """Text-ish files under root, skipping build output, worlds and VCS."""
    root = Path(root)
    out = []
    stack = [root]
    while stack and len(out) < limit:
        if cancel is not None and cancel.cancelled:
            break
        d = stack.pop()
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            continue
        if any(e.name == "level.dat" for e in entries) and d != root:
            continue                      # a world folder
        for e in entries:
            if e.is_dir(follow_symlinks=False):
                if e.name not in SKIP_DIRS and not e.name.startswith("."):
                    stack.append(Path(e.path))
            elif e.is_file(follow_symlinks=False):
                out.append(Path(e.path))
                if len(out) >= limit:
                    break
    return out


# ---------------------------------------------------------------------------
#  the project index
# ---------------------------------------------------------------------------

SYMBOL_RES = {
    "Java": [re.compile(r"(?m)^\s*(?:public|protected|private|abstract|final|static|sealed|\s)*"
                        r"(?:class|interface|enum|record|@interface)\s+(\w+)"),
             re.compile(r"(?m)^\s*(?:@\w+(?:\([^)]*\))?\s*)*(?:public|protected|private)\s+(?:static\s+)?(?:final\s+)?"
                        r"(?:<[^>]+>\s+)?[\w<>\[\],.? ]+\s+(\w+)\s*\(")],
    "Kotlin": [re.compile(r"(?m)^\s*(?:\w+\s+)*(?:class|object|interface)\s+(\w+)"),
               re.compile(r"(?m)^\s*(?:\w+\s+)*fun\s+(?:<[^>]+>\s+)?(?:\w+\.)?(\w+)\s*\(")],
    "Skript": [re.compile(r"(?mi)^command\s+/?(\S+?)(?:\s|:)"), re.compile(r"(?mi)^(?:local\s+)?function\s+(\w+)"),
               re.compile(r"(?mi)^on\s+([^:]+):")],
    "YAML": [re.compile(r"(?m)^([A-Za-z0-9_.\-]+):")],
}


class ProjectIndex:
    """Files, languages and symbols of one workspace, refreshed by mtime."""

    def __init__(self, root, path):
        self.root = Path(root)
        self.path = Path(path)
        self.data = _read_json(self.path, {"files": {}, "built": 0})
        self.lock = threading.Lock()
        self.busy = False

    def build(self, cancel=None, pause=None, budget_s=None):
        """Bring the index up to date. ``pause`` is a callable returning True
        while indexing should wait (Development Performance Mode)."""
        with self.lock:
            if self.busy:
                return self.stats()
            self.busy = True
        started = now()
        try:
            files = _walk(self.root, cancel)
            seen = {}
            old = self.data.get("files", {})
            changed = 0
            stopped = False
            for p in files:
                if (pause is not None and pause()) or (cancel is not None and cancel.cancelled):
                    stopped = True
                    break
                if budget_s and now() - started > budget_s:
                    break
                try:
                    st = p.stat()
                except OSError:
                    continue
                rel = str(p.relative_to(self.root)).replace("\\", "/")
                prev = old.get(rel)
                if prev and prev.get("mtime") == int(st.st_mtime) and prev.get("size") == st.st_size:
                    seen[rel] = prev
                    continue
                changed += 1
                seen[rel] = self._describe(p, st)
            if stopped:
                # keep what was there for the files not reached yet
                for rel, ent in old.items():
                    seen.setdefault(rel, ent)
            self.data = {"files": seen, "built": now() if not stopped else self.data.get("built", 0),
                         "root": str(self.root), "truncated": len(files) >= 8000}
            try:
                _write_json(self.path, self.data)
            except OSError:
                pass
            return dict(self.stats(), changed=changed, paused=stopped)
        finally:
            with self.lock:
                self.busy = False

    def _describe(self, p, st):
        ext = p.suffix.lower()
        ent = {"size": st.st_size, "mtime": int(st.st_mtime), "lang": LANG.get(ext, ext.lstrip(".") or "file")}
        if _is_text_file(p, 600_000):
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ent
            ent["lines"] = text.count("\n") + 1
            syms = []
            for rx in SYMBOL_RES.get(ent["lang"], []):
                syms += [m.group(1).strip() for m in rx.finditer(text)][:40]
            if ent["lang"] == "Java":
                m = re.search(r"(?m)^\s*package\s+([\w.]+)\s*;", text)
                if m:
                    ent["package"] = m.group(1)
                if "@EventHandler" in text:
                    syms.append("@EventHandler")
            ent["symbols"] = sorted(set(s for s in syms if s and len(s) < 60))[:60]
        return ent

    def stats(self):
        files = self.data.get("files", {})
        langs = {}
        for f in files.values():
            langs[f["lang"]] = langs.get(f["lang"], 0) + 1
        return {"files": len(files), "bytes": sum(f.get("size", 0) for f in files.values()),
                "languages": dict(sorted(langs.items(), key=lambda kv: -kv[1])[:12]),
                "built": self.data.get("built", 0), "busy": self.busy,
                "truncated": bool(self.data.get("truncated"))}

    def packages(self):
        pk = set()
        for f in self.data.get("files", {}).values():
            if f.get("package"):
                parts = f["package"].split(".")
                pk.add(".".join(parts[:3]))
        return sorted(pk)

    def find(self, query, k=10):
        words = [w.lower() for w in re.findall(r"[A-Za-z][a-z0-9]*|[0-9]+", query or "") if len(w) > 1]
        if not words:
            return []
        scored = []
        for rel, f in self.data.get("files", {}).items():
            path_l = rel.lower()
            syms = [s.lower() for s in f.get("symbols", [])]
            score, why = 0, []
            for w in words:
                if w in path_l:
                    score += 3
                hits = [s for s in syms if w in s]
                if hits:
                    score += 2
                    why += hits[:2]
            if score:
                scored.append((score, rel, why))
        scored.sort(key=lambda x: (-x[0], len(x[1])))
        return [{"path": r, "score": s, "matches": sorted(set(w))[:4]} for s, r, w in scored[:k]]


# ---------------------------------------------------------------------------
#  per-workspace storage: memory, chat, backups
# ---------------------------------------------------------------------------

class Store:
    def __init__(self, base, wsid):
        self.dir = Path(base) / wsid
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    # memory
    def memory(self):
        return _read_json(self.dir / "memory.json", [])

    def remember(self, text, source="assistant"):
        text = " ".join((text or "").split())[:400]
        if not text:
            raise AgentError("Nothing to remember.")
        with self.lock:
            notes = self.memory()
            if any(n["text"].lower() == text.lower() for n in notes):
                return notes
            notes.append({"id": new_id(), "text": text, "t": now(), "source": source})
            notes = notes[-60:]
            _write_json(self.dir / "memory.json", notes)
            return notes

    def forget(self, nid):
        with self.lock:
            notes = [n for n in self.memory() if n["id"] != nid]
            _write_json(self.dir / "memory.json", notes)
            return notes

    # chat
    def history(self):
        return _read_json(self.dir / "chat.json", [])

    def append_history(self, entries):
        with self.lock:
            h = self.history() + entries
            _write_json(self.dir / "chat.json", h[-200:])

    def clear_history(self):
        with self.lock:
            _write_json(self.dir / "chat.json", [])

    # changes
    def changes(self):
        return _read_json(self.dir / "changes.json", [])

    def record_change(self, ch):
        with self.lock:
            lst = self.changes() + [ch]
            for old in lst[:-300]:
                if old.get("backup"):
                    try:
                        os.remove(old["backup"])
                    except OSError:
                        pass
            _write_json(self.dir / "changes.json", lst[-300:])

    def mark_reverted(self, cid):
        with self.lock:
            lst = self.changes()
            for c in lst:
                if c["id"] == cid:
                    c["reverted"] = now()
            _write_json(self.dir / "changes.json", lst)

    def backup_path(self, cid):
        d = self.dir / "backups"
        d.mkdir(exist_ok=True)
        return d / cid


# ---------------------------------------------------------------------------
#  commands
# ---------------------------------------------------------------------------

def command_policy(argv, root, extra_allowed=()):
    """(ok, why). The first word must be an allowed program; git only for
    reading; batch files only with arguments cmd.exe cannot misread."""
    if not argv:
        return False, "Empty command."
    prog = os.path.basename(argv[0]).lower()
    for suffix in (".exe", ".bat", ".cmd"):
        if prog.endswith(suffix):
            prog = prog[:-len(suffix)]
    if prog.startswith("./"):
        prog = prog[2:]
    joined = " ".join(argv)
    for pat in extra_allowed or ():
        pat = (pat or "").strip()
        if pat and (joined == pat or joined.startswith(pat + " ") or fnmatch.fnmatch(joined, pat)):
            return True, "allowed by your list"
    if prog not in DEFAULT_COMMANDS:
        return False, ("'%s' is not on the allowed list. Build tools (gradle, gradlew, mvn, mvnw), java/javac "
                       "-version and read-only git are; add others in AI settings." % argv[0])
    subs = DEFAULT_COMMANDS[prog]
    if subs is not None and (len(argv) < 2 or argv[1] not in subs):
        return False, "'%s' may only be run as: %s" % (prog, ", ".join("%s %s" % (prog, s) for s in subs))
    if prog in ("gradlew", "mvnw"):
        local = [n for n in os.listdir(root) if n.lower().startswith(prog)] if os.path.isdir(root) else []
        if not local:
            return False, "There is no %s in the workspace folder." % prog
    return True, ""


def resolve_argv(argv, root):
    """Turn './gradlew' into something this OS can start, inside root."""
    prog = argv[0]
    base = os.path.basename(prog).lower()
    if base in ("gradlew", "./gradlew", "gradlew.bat", "mvnw", "./mvnw", "mvnw.cmd"):
        stem = "gradlew" if "gradlew" in base else "mvnw"
        if os.name == "nt":
            exe = os.path.join(root, stem + (".bat" if stem == "gradlew" else ".cmd"))
            return [exe] + argv[1:]
        exe = os.path.join(root, stem)
        if not os.access(exe, os.X_OK):
            return ["sh", exe] + argv[1:]
        return [exe] + argv[1:]
    found = shutil.which(prog)
    if not found:
        raise AgentError("'%s' is not installed or not on PATH." % prog)
    return [found] + argv[1:]


def split_command(cmd):
    try:
        parts = shlex.split(cmd, posix=(os.name != "nt"))
    except ValueError as e:
        raise AgentError("Could not read that command: %s" % e)
    if os.name == "nt":
        parts = [p[1:-1] if len(p) >= 2 and p[0] == p[-1] == '"' else p for p in parts]
    return parts


class ProcessRunner:
    """Runs one command we started, and can stop exactly that process tree."""

    def __init__(self, argv, cwd, on_line=None, timeout=900, cancel=None, env=None):
        self.argv, self.cwd, self.on_line = argv, cwd, on_line
        self.timeout, self.cancel, self.env = timeout, cancel, env
        self.proc = None
        self.killed = ""

    def kill(self, why="cancelled"):
        self.killed = self.killed or why
        p = self.proc
        if not p or p.poll() is not None:
            return
        try:
            if os.name == "nt":
                # only this process and its children - never anything else
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                os.killpg(p.pid, signal.SIGTERM)
                for _ in range(30):
                    if p.poll() is not None:
                        return
                    time.sleep(0.1)
                os.killpg(p.pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            try:
                p.kill()
            except OSError:
                pass

    def run(self):
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kw["start_new_session"] = True
        if os.name == "nt" and self.argv[0].lower().endswith((".bat", ".cmd")):
            bad = [a for a in self.argv[1:] if BAT_UNSAFE.search(a)]
            if bad:
                raise AgentError("Refusing to pass %r to a batch file: cmd.exe would read those characters "
                                 "as commands." % bad[0])
        self.proc = subprocess.Popen(self.argv, cwd=self.cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
                                     bufsize=1, env=self.env, **kw)
        remove = self.cancel.on_cancel(lambda: self.kill("cancelled")) if self.cancel is not None else (lambda: None)
        timer = threading.Timer(self.timeout, lambda: self.kill("timed out after %ds" % self.timeout))
        timer.daemon = True
        timer.start()
        tail = deque(maxlen=4000)
        try:
            for line in self.proc.stdout:
                line = line.rstrip("\n")
                tail.append(line)
                if self.on_line:
                    self.on_line(line)
            code = self.proc.wait()
        finally:
            timer.cancel()
            remove()
        return code, "\n".join(tail)


# ---------------------------------------------------------------------------
#  tools
# ---------------------------------------------------------------------------

def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


S = {"type": "string"}
I = {"type": "integer"}
B = {"type": "boolean"}

TOOLS = [
    ("project_overview", "Summary of the project: type, build system, main files, languages, the linked server, "
                         "and saved notes. Call this first in a new conversation.", _schema({}), "read"),
    ("list_files", "List files and folders under a path in the workspace.",
     _schema({"path": dict(S, description="folder, relative to the workspace ('.' for the top)"),
              "depth": dict(I, description="how many levels down, 1-4")}), "read"),
    ("read_file", "Read a text file with line numbers. Read a file before editing it.",
     _schema({"path": S, "start_line": I, "end_line": I}, ["path"]), "read"),
    ("find_files", "Find files by name, class, command, function or other symbol, using the project index.",
     _schema({"query": S}, ["query"]), "read"),
    ("search_code", "Search the contents of every text file for a word or regular expression.",
     _schema({"query": S, "regex": B, "path": dict(S, description="limit to this folder")}, ["query"]), "read"),
    ("check_file", "Validate a file: YAML/JSON/TOML/properties syntax, Skript structure, or Java heuristics.",
     _schema({"path": S}, ["path"]), "read"),
    ("explain_error", "Analyse a stack trace or build output: the exception chain, frames in this project, "
                      "compile errors with file and line, and usual causes.",
     _schema({"text": dict(S, description="the trace or output; leave empty to use the server console")}), "read"),
    ("write_file", "Create a file or replace its whole content. Shown to the user as a diff for approval.",
     _schema({"path": S, "content": dict(S, description="the complete new file content")}, ["path", "content"]),
     "write"),
    ("edit_file", "Replace an exact piece of text in a file. old_text must appear exactly once unless "
                  "replace_all is true. Shown to the user as a diff for approval.",
     _schema({"path": S, "old_text": S, "new_text": S, "replace_all": B}, ["path", "old_text", "new_text"]),
     "write"),
    ("run_build", "Build or test the Java project with its own Gradle or Maven, and get the errors parsed.",
     _schema({"task": dict(S, enum=["build", "test"])}), "command"),
    ("run_command", "Run an allowed command in the workspace folder (build tools, java -version, read-only "
                    "git, and anything the user allowed). Needs the user's approval.",
     _schema({"command": S}, ["command"]), "command"),
    ("remember", "Save a lasting fact about this project for future conversations (a convention, a build "
                 "quirk, a decision). Keep it short.", _schema({"note": S}, ["note"]), "memory"),
    ("ask_user", "Ask the user a question when the request is unclear or needs their decision. Ends your turn.",
     _schema({"question": S}, ["question"]), "ask"),
    ("set_plan", "Show the user your plan for a multi-step task: the steps and the files you expect to change.",
     _schema({"steps": {"type": "array", "items": S}, "files": {"type": "array", "items": S},
              "done": dict(I, description="how many steps are finished")}, ["steps"]), "plan"),
]
SERVER_TOOLS = [
    ("server_status", "The linked server's state: running or not, software, version, players, uptime.",
     _schema({}), "read"),
    ("read_console", "Recent lines of the server console.",
     _schema({"lines": I, "filter": dict(S, description="only lines containing this text")}), "read"),
    ("server_command", "Run a console command on the running server (no leading slash). Needs approval.",
     _schema({"command": S}, ["command"]), "server"),
    ("skript_environment", "The installed Skript version, its addons, the scripts, and problems found across "
                           "them. Check this before writing Skript.", _schema({}), "read"),
    ("skript_reload", "Reload a Skript script on the running server and get Skript's own parser errors. This "
                      "is how to check that Skript syntax is valid.",
     _schema({"script": dict(S, description="file name under plugins/Skript/scripts, e.g. shop.sk")}, ["script"]),
     "server"),
]


def tool_schemas(with_server):
    out = []
    for name, desc, params, _kind in TOOLS + (SERVER_TOOLS if with_server else []):
        out.append({"type": "function", "function": {"name": name, "description": desc, "parameters": params}})
    return out


TOOL_KIND = {t[0]: t[3] for t in TOOLS + SERVER_TOOLS}


# ---------------------------------------------------------------------------
#  a run
# ---------------------------------------------------------------------------

class Run:
    def __init__(self, ws, message):
        self.id = new_id()
        self.ws = ws
        self.message = message
        self.state = "queued"
        self.events = []
        self.seq = 0
        self.cond = threading.Condition()
        self.cancel = ol.CancelToken()
        self.pending = {}               # call_id -> {"ev": Event, "decision", "note"}
        self.always = set()             # tool kinds approved for the rest of this run
        self.changes = []
        self.started = now()
        self.finished = 0
        self.usage = {"in": 0, "out": 0}
        self.final = ""
        self.tools_used = []
        self._buf = {"text": [], "thinking": []}
        self._last_flush = 0.0

    def emit(self, type_, **data):
        with self.cond:
            self.seq += 1
            ev = {"seq": self.seq, "type": type_, "t": round(now(), 3)}
            ev.update(data)
            self.events.append(ev)
            if len(self.events) > MAX_EVENTS:
                del self.events[:len(self.events) - MAX_EVENTS]
            self.cond.notify_all()
            return ev

    def stream(self, kind, text):
        self._buf[kind].append(text)
        t = now()
        if t - self._last_flush > 0.08 or sum(len(x) for x in self._buf[kind]) > 160:
            self.flush()

    def flush(self):
        self._last_flush = now()
        for kind in ("thinking", "text"):
            if self._buf[kind]:
                self.emit(kind, text="".join(self._buf[kind]))
                self._buf[kind] = []

    def set_state(self, state, detail=""):
        self.state = state
        self.emit("status", state=state, detail=detail)

    def since(self, seq, wait=0.0):
        end = now() + max(0.0, min(30.0, wait))
        with self.cond:
            while True:
                out = [e for e in self.events if e["seq"] > seq]
                if out or self.finished or now() >= end:
                    return out
                self.cond.wait(max(0.05, end - now()))

    def summary(self):
        return {"id": self.id, "ws": self.ws["id"], "state": self.state, "message": self.message[:200],
                "started": self.started, "finished": self.finished, "usage": self.usage,
                "pending": [dict(call_id=k, **{x: v[x] for x in ("kind", "title")}) for k, v in self.pending.items()],
                "changes": self.changes}


SYSTEM = """You are the development assistant inside Lodestone, a Minecraft server creator and manager. \
You work inside the user's project through tools, on their own computer, with a local model.

How to work:
- Look before you answer: use the tools to read the project. Never guess file contents, APIs, versions or \
Skript syntax - check them.
- Read a file before you edit it. Make complete, working changes - no placeholders, no "TODO", no \
"rest of the code here". Prefer edit_file for changes to existing files and write_file for new files.
- After a change, verify it: check_file for configs and scripts, run_build for Java projects, \
skript_reload for Skript on a running server. If it fails, read the errors, fix them and check again.
- Every write and command is shown to the user for approval. If they reject one, read their note and adapt.
- When the request is unclear, or needs a decision only the user can make, call ask_user instead of inventing \
requirements.
- For a task with several steps, call set_plan first with the steps and the files you expect to change, and \
call it again as steps finish.
- Use remember for lasting facts about this project worth knowing next time.
- Finish with a short summary in Markdown: what you changed (as path:line references), how you verified it, \
and anything the user still has to do. Put code in fenced blocks with a language.
"""

SKRIPT_RULES = """Skript: only use syntax that exists in the installed Skript version and installed addons \
(see skript_environment). Scripts live in plugins/Skript/scripts/. Indentation is significant and must be \
consistent. After writing a script, run skript_reload on it and fix every error Skript reports."""

JSON_PROTOCOL = """You cannot call tools directly, so reply with ONE JSON object and nothing else:
  {"tool": "<tool name>", "args": {...}}   to use a tool - you will get its result back, or
  {"answer": "<your reply to the user, in Markdown>"}   when you are finished.
Tools:
%s"""


class Agent:
    def __init__(self, data_dir, settings, bridge_for=None, client_for=None, save=None, props_meta=None):
        """``settings()`` returns the current dev settings dict and ``save(dict)``
        merges changes into it. ``bridge_for(server_id)`` returns a server
        bridge or None. ``client_for(base)`` makes an Ollama client (tests pass
        a fake's). ``props_meta`` is (boolean keys, enum keys) of
        server.properties, for checking it."""
        self.base = Path(data_dir)
        self.save = save or (lambda changes: None)
        self.props_meta = props_meta or ((), {})
        self.base.mkdir(parents=True, exist_ok=True)
        self.settings = settings
        self.bridge_for = bridge_for or (lambda sid: None)
        self.client_for = client_for or (lambda base: ol.Ollama(base))
        self.runs = {}
        self.indexes = {}
        self.lock = threading.Lock()
        self._slots_n = 0
        self._active = 0
        self._slot_cond = threading.Condition()
        self._caps = {}
        self._idx_queue = deque()
        self._idx_ev = threading.Event()
        threading.Thread(target=self._index_loop, daemon=True, name="agent-index").start()

    # -- settings -----------------------------------------------------------
    def cfg(self):
        s = dict(self.settings() or {})
        s.setdefault("base_url", ol.DEFAULT_BASE)
        s.setdefault("model", "")
        # a coding session needs room for tools and a few files; capped at the
        # model's own maximum when a run starts
        s.setdefault("num_ctx", 8192)
        s.setdefault("trusted_commands", [])
        s.setdefault("max_steps", 24)
        s.setdefault("max_parallel", 1)
        s.setdefault("auto_approve_edits", False)
        s.setdefault("allowed_commands", [])
        s.setdefault("command_timeout", 900)
        s.setdefault("index_enabled", True)
        s.setdefault("perf_mode", False)
        s.setdefault("tool_mode", {})
        s.setdefault("think", False)
        return s

    def client(self):
        return self.client_for(self.cfg().get("base_url") or ol.DEFAULT_BASE)

    def options(self, model=None):
        c = self.cfg()
        opts = {k: c.get(k) for k in ("num_ctx", "temperature", "num_thread", "num_gpu", "top_p", "seed")}
        if model and opts.get("num_ctx"):
            try:
                top = int(self.model_info(model).get("context_length") or 0)
            except (ol.OllamaError, ValueError, TypeError):
                top = 0
            if top and int(opts["num_ctx"]) > top:
                opts["num_ctx"] = top
        if opts.get("temperature") in (None, ""):
            opts["temperature"] = 0.2
        ka = c.get("keep_alive")
        if c.get("perf_mode") and ka in (None, "", "default"):
            ka = "30m"                    # keep the model in memory while coding
        opts["keep_alive"] = ka
        return ol.ModelOptions(opts)

    def paused(self):
        c = self.cfg()
        return bool(c.get("perf_mode")) or not c.get("index_enabled", True)

    # -- workspaces ---------------------------------------------------------
    def _ws_file(self):
        return self.base / "workspaces.json"

    def project_workspaces(self):
        return _read_json(self._ws_file(), [])

    def add_workspace(self, root, name="", server_id=""):
        root = os.path.abspath(os.path.expanduser(root or ""))
        if not os.path.isdir(root):
            raise AgentError("That folder does not exist.")
        lst = self.project_workspaces()
        for w in lst:
            if os.path.normcase(w["root"]) == os.path.normcase(root):
                return w
        w = {"id": "p-" + new_id(), "name": name or os.path.basename(root) or root, "root": root,
             "kind": "project", "server_id": server_id or "", "added": now()}
        lst.append(w)
        _write_json(self._ws_file(), lst)
        self.queue_index(w)
        return w

    def remove_workspace(self, wsid):
        lst = [w for w in self.project_workspaces() if w["id"] != wsid]
        _write_json(self._ws_file(), lst)
        shutil.rmtree(self.base / wsid, ignore_errors=True)
        self.indexes.pop(wsid, None)

    def store(self, ws):
        return Store(self.base, ws["id"])

    def index(self, ws):
        with self.lock:
            idx = self.indexes.get(ws["id"])
            if idx is None or str(idx.root) != str(Path(ws["root"])):
                idx = self.indexes[ws["id"]] = ProjectIndex(ws["root"], self.base / ws["id"] / "index.json")
            return idx

    def queue_index(self, ws):
        self._idx_queue.append(ws)
        self._idx_ev.set()

    def _index_loop(self):
        while True:
            self._idx_ev.wait(30)
            self._idx_ev.clear()
            later = []
            while self._idx_queue:
                ws = self._idx_queue.popleft()
                if not os.path.isdir(ws.get("root", "")):
                    continue
                if self.paused():
                    later.append(ws)      # Development Performance Mode: not now
                    continue
                try:
                    res = self.index(ws).build(pause=self.paused)
                    if res.get("paused"):
                        later.append(ws)
                except Exception:
                    pass
            for ws in later:
                if all(w["id"] != ws["id"] for w in self._idx_queue):
                    self._idx_queue.append(ws)

    # -- model capabilities -------------------------------------------------
    def model_info(self, model, fresh=False):
        key = (self.cfg().get("base_url"), model)
        if not fresh and key in self._caps:
            return self._caps[key]
        d = self.client().capabilities(model, fresh=fresh)
        self._caps[key] = d
        return d

    def tool_mode(self, model):
        """'native' or 'json', from the per-model override or what Ollama says."""
        forced = (self.cfg().get("tool_mode") or {}).get(model, "auto")
        if forced in ("native", "json"):
            return forced, "set in AI settings"
        try:
            d = self.model_info(model)
        except ol.OllamaError:
            return "json", "Ollama did not describe the model"
        if d.get("tools"):
            return "native", "the model supports tools (%s)" % d.get("source")
        return "json", "the model does not support tool calling (%s)" % d.get("source")

    def models(self):
        c = self.client()
        out = []
        for m in c.tags():
            try:
                d = self.model_info(m["name"])
            except ol.OllamaError:
                d = {}
            if d.get("embedding_only"):
                continue
            mode, why = self.tool_mode(m["name"])
            out.append(dict(m, capabilities=d.get("capabilities", []), tools=d.get("tools"),
                            thinking=d.get("thinking", False), context_length=d.get("context_length", 0),
                            mode=mode, mode_why=why, kv=d.get("kv", {})))
        return out

    # -- runs ---------------------------------------------------------------
    def start(self, ws, message, attachments=None):
        message = (message or "").strip()
        if not message:
            raise AgentError("Type a message first.")
        if not os.path.isdir(ws.get("root", "")):
            raise AgentError("The workspace folder %s does not exist." % ws.get("root"))
        if not self.cfg().get("model"):
            raise AgentError("Choose a model in the assistant's settings first.")
        with self.lock:
            busy = [r for r in self.runs.values() if r.ws["id"] == ws["id"] and not r.finished]
        if busy:
            raise AgentError("This workspace is already working on something - wait or cancel it.")
        run = Run(ws, message)
        run.attachments = list(attachments or [])[:6]
        with self.lock:
            done = sorted((r for r in self.runs.values() if r.finished), key=lambda r: r.finished)
            for old in done[:-20]:
                self.runs.pop(old.id, None)
            self.runs[run.id] = run
        run.emit("status", state="queued", detail="")
        threading.Thread(target=self._run, args=(run,), daemon=True, name="agent-run-" + run.id).start()
        return run

    def get(self, rid):
        with self.lock:
            r = self.runs.get(rid)
        if not r:
            raise AgentError("No such run - it may have finished before Lodestone restarted.")
        return r

    def cancel(self, rid):
        r = self.get(rid)
        if r.finished:
            return False
        r.cancel.cancel("Stopped by you.")
        for p in list(r.pending.values()):
            p["decision"] = "reject"
            p["note"] = "cancelled"
            p["ev"].set()
        with self._slot_cond:
            self._slot_cond.notify_all()
        return True

    def approve(self, rid, call_id, decision, note=""):
        r = self.get(rid)
        p = r.pending.get(call_id)
        if not p:
            raise AgentError("That approval is no longer waiting.")
        if decision not in ("approve", "reject", "always"):
            raise AgentError("Unknown decision.")
        p["decision"], p["note"] = decision, (note or "")[:1000]
        p["ev"].set()
        return True

    def active(self):
        with self.lock:
            runs = list(self.runs.values())
        return [r.summary() for r in runs if not r.finished]

    def _acquire(self, run):
        with self._slot_cond:
            while True:
                limit = 1 if self.cfg().get("perf_mode") else max(1, int(self.cfg().get("max_parallel") or 1))
                if self._active < limit:
                    self._active += 1
                    return True
                if run.cancel.cancelled:
                    return False
                if run.state != "queued":
                    run.set_state("queued", "waiting for another assistant task to finish")
                self._slot_cond.wait(0.5)

    def _release(self):
        with self._slot_cond:
            self._active = max(0, self._active - 1)
            self._slot_cond.notify_all()

    def _run(self, run):
        store = self.store(run.ws)
        ok = self._acquire(run)
        try:
            if not ok:
                raise ol.Cancelled("Stopped by you.")
            self._loop(run, store)
            end_state = "question" if run.state == "question" else "done"
        except ol.Cancelled as e:
            run.flush()
            run.emit("error", text=str(e) or "Cancelled.", cancelled=True)
            end_state = "cancelled"
        except (ol.OllamaError, AgentError) as e:
            run.flush()
            run.emit("error", text=str(e))
            end_state = "error"
        except Exception as e:     # a bug - say so rather than hang the UI
            run.flush()
            run.emit("error", text="The assistant hit an internal error: %s: %s" % (type(e).__name__, e))
            end_state = "error"
        finally:
            if ok:
                self._release()
        run.state = end_state
        run.finished = now()
        try:
            store.append_history([
                {"role": "user", "content": run.message, "t": run.started, "run": run.id},
                {"role": "assistant", "content": run.final or "", "t": run.finished, "run": run.id,
                 "state": end_state, "tools": run.tools_used[-40:], "changes": run.changes,
                 "usage": run.usage}])
        except OSError:
            pass
        run.emit("done", state=end_state, usage=run.usage)
        with run.cond:
            run.cond.notify_all()

    # -- context ------------------------------------------------------------
    def overview(self, ws, refresh=True):
        idx = self.index(ws)
        if refresh and not idx.busy:
            idx.build(pause=None, budget_s=8)
        st = idx.stats()
        root = Path(ws["root"])
        out = {"name": ws["name"], "root": ws["root"], "kind": ws.get("kind", "project"), "index": st,
               "key_files": [k for k in KEY_FILES if (root / k).exists()]}
        jp = devtools.java_project(root)
        if jp["kind"] or jp["descriptors"]:
            out["java"] = {k: jp[k] for k in ("kind", "name", "loader", "mc", "java", "build_command",
                                              "descriptors", "problems") if jp.get(k)}
            out["java"]["dependencies"] = [d["coord"] for d in jp["dependencies"]][:25]
        if (root / "plugins" / "Skript").is_dir():
            env = skripttools.environment(str(root))
            out["skript"] = {"version": (env["skript"] or {}).get("version", "not installed"),
                             "addons": ["%s %s" % (a["name"], a["version"]) for a in env["addons"]],
                             "scripts": len(env["scripts"])}
        b = self._bridge(ws)
        if b is not None:
            try:
                out["server"] = b.info()
            except Exception:
                pass
        out["memory"] = [n["text"] for n in self.store(ws).memory()]
        return out

    def _bridge(self, ws):
        sid = ws.get("server_id")
        if not sid:
            return None
        try:
            return self.bridge_for(sid)
        except Exception:
            return None

    def _system(self, ws, mode, with_server):
        ov = self.overview(ws, refresh=True)
        lines = [SYSTEM, "\nThe workspace:"]
        lines.append("- %s at %s (%s)" % (ov["name"], ov["root"], ov["kind"]))
        if ov.get("java"):
            j = ov["java"]
            lines.append("- Java project: %s, %s%s%s" % (j.get("kind", "?"), j.get("loader", "") or "plain Java",
                                                         ", Minecraft %s" % j["mc"] if j.get("mc") else "",
                                                         ", Java %s" % j["java"] if j.get("java") else ""))
        if ov.get("server"):
            s = ov["server"]
            lines.append("- Linked server: %s, %s %s, %s" % (s.get("name"), s.get("software") or "unknown software",
                                                           s.get("mc") or "", "running" if s.get("running")
                                                           else "stopped"))
        if ov.get("skript"):
            sk = ov["skript"]
            lines.append("- Skript %s; addons: %s" % (sk["version"], ", ".join(sk["addons"]) or "none"))
        langs = ov["index"]["languages"]
        if langs:
            lines.append("- Files: %s" % ", ".join("%s %d" % kv for kv in list(langs.items())[:6]))
        if ov["key_files"]:
            lines.append("- Key files: %s" % ", ".join(ov["key_files"][:12]))
        if ov["memory"]:
            lines.append("\nNotes saved for this project:")
            lines += ["- " + m for m in ov["memory"][-20:]]
        if ov.get("skript") or with_server:
            lines.append("\n" + SKRIPT_RULES)
        if mode == "json":
            desc = []
            for name, d, params, _k in TOOLS + (SERVER_TOOLS if with_server else []):
                args = ", ".join("%s%s" % (k, "" if k in params.get("required", []) else "?")
                                 for k in params["properties"])
                desc.append("  %s(%s): %s" % (name, args, d))
            lines.append("\n" + JSON_PROTOCOL % "\n".join(desc))
        return "\n".join(lines)

    def _history_messages(self, store):
        msgs = []
        for h in store.history()[-12:]:
            if h["role"] in ("user", "assistant") and h.get("content"):
                msgs.append({"role": h["role"], "content": _clip(h["content"], 4000)})
        return msgs

    @staticmethod
    def _tokens(messages):
        return sum(len(m.get("content") or "") + len(json.dumps(m.get("tool_calls", ""))) for m in messages) // 3

    def _fit(self, messages, num_ctx):
        """Keep the conversation inside the context: old tool results are the
        first to shrink, then the oldest turns go. The system message and the
        current request stay."""
        budget = int((num_ctx or 8192) * 0.8)
        if self._tokens(messages) <= budget:
            return messages
        for m in messages[1:-6]:
            if m["role"] == "tool" or (m["role"] == "user" and m["content"].startswith("RESULT of ")):
                if len(m["content"]) > 400:
                    m["content"] = m["content"][:300] + "\n[trimmed to save context - call the tool again if needed]"
            if self._tokens(messages) <= budget:
                return messages
        while len(messages) > 8 and self._tokens(messages) > budget:
            del messages[1]
            while len(messages) > 2 and messages[1]["role"] == "tool":
                del messages[1]
        return messages

    # -- the loop -----------------------------------------------------------
    def _loop(self, run, store):
        c = self.cfg()
        model = c["model"]
        client = self.client()
        opts = self.options(model)
        bridge = self._bridge(run.ws)
        with_server = bridge is not None
        run.set_state("running", "reading the project")
        mode, why = self.tool_mode(model)
        run.emit("model", model=model, mode=mode, why=why)
        think = None
        if c.get("think"):
            try:
                if self.model_info(model).get("thinking"):
                    think = True
            except ol.OllamaError:
                pass
        system = self._system(run.ws, mode, with_server)
        user = run.message
        for a in getattr(run, "attachments", []):
            user += "\n\n[attached: %s]\n%s\n[end of %s]" % (a.get("name", "file"), _clip(a.get("content", ""), 20000),
                                                            a.get("name", "file"))
        messages = [{"role": "system", "content": system}] + self._history_messages(store) + \
                   [{"role": "user", "content": user}]
        schemas = tool_schemas(with_server)
        num_ctx = int(opts.options.get("num_ctx") or 0)
        if not num_ctx:
            try:
                num_ctx = min(int(self.model_info(model).get("context_length") or 4096), 4096)
            except ol.OllamaError:
                num_ctx = 4096
        steps = max(2, min(60, int(c.get("max_steps") or 24)))
        for step in range(steps):
            run.cancel.check()
            run.set_state("thinking", "step %d" % (step + 1))
            self._fit(messages, num_ctx)
            res = client.chat(model, messages, tools=schemas if mode == "native" else None, opts=opts,
                              fmt="json" if mode == "json" else None, think=think,
                              on_delta=(lambda t: run.stream("text", t)) if mode == "native" else None,
                              on_thinking=lambda t: run.stream("thinking", t), cancel=run.cancel)
            run.flush()
            run.usage["in"] += res["usage"]["in"]
            run.usage["out"] += res["usage"]["out"]
            run.emit("usage", step=step + 1, **res["usage"])
            if mode == "native":
                calls = res["tool_calls"]
                text = res["content"]
                if not calls:
                    parsed = text_tool_call(text, {t[0] for t in TOOLS + (SERVER_TOOLS if with_server else [])})
                    if parsed:
                        calls, text = [parsed], ""
                msg = {"role": "assistant", "content": text}
                if calls:
                    msg["tool_calls"] = [{"function": {"name": x["name"], "arguments": x["arguments"]}}
                                         for x in calls]
                messages.append(msg)
                if not calls:
                    run.final = text.strip() or "(the model returned an empty answer)"
                    run.emit("message", text=run.final)
                    return
                for call in calls:
                    result = self._call(run, store, call, bridge)
                    messages.append({"role": "tool", "content": result, "tool_name": call["name"]})
                    if run.state == "question":
                        return
            else:
                obj = _extract_json(res["content"])
                messages.append({"role": "assistant", "content": res["content"]})
                if not obj or ("tool" not in obj and "answer" not in obj):
                    if obj is None and res["content"].strip():
                        run.final = res["content"].strip()
                        run.emit("message", text=run.final)
                        return
                    messages.append({"role": "user", "content": "Reply with one JSON object: {\"tool\": ..., "
                                                                "\"args\": {...}} or {\"answer\": ...}."})
                    continue
                if "answer" in obj and not obj.get("tool"):
                    run.final = str(obj.get("answer") or "").strip() or "(empty answer)"
                    run.emit("text", text=run.final)
                    run.emit("message", text=run.final)
                    return
                call = {"name": str(obj.get("tool")), "arguments": obj.get("args") if isinstance(obj.get("args"), dict)
                        else {k: v for k, v in obj.items() if k != "tool"}}
                result = self._call(run, store, call, bridge)
                messages.append({"role": "user", "content": "RESULT of %s:\n%s" % (call["name"], result)})
                if run.state == "question":
                    return
        run.final = ("I stopped after %d steps without finishing. Tell me to continue, or break the task into "
                     "smaller parts." % steps)
        run.emit("message", text=run.final)

    # -- tool calls ---------------------------------------------------------
    def _call(self, run, store, call, bridge):
        name, args = call["name"], call.get("arguments") or {}
        cid = new_id()
        run.set_state("tool", name)
        run.emit("tool", call_id=cid, name=name, args=_short_args(args))
        t0 = now()
        try:
            run.cancel.check()
            text, summary, data = self._do(run, store, cid, name, args, bridge)
            ok = True
        except ol.Cancelled:
            raise
        except AgentError as e:
            text, summary, data, ok = "ERROR: %s" % e, str(e), None, False
        except Exception as e:
            text, summary, data, ok = "ERROR: %s: %s" % (type(e).__name__, e), "failed: %s" % e, None, False
        run.tools_used.append({"name": name, "summary": summary[:200], "ok": ok})
        ev = {"call_id": cid, "name": name, "ok": ok, "summary": summary[:300], "ms": int((now() - t0) * 1000),
              "output": _clip(text, 4000)}
        if data is not None:
            ev["data"] = data
        run.emit("tool_result", **ev)
        if run.state not in ("question",):
            run.set_state("running", "")
        return _clip(text)

    def _wait_approval(self, run, cid, kind, title, persist=False, **detail):
        if kind in run.always or (kind == "write" and self.cfg().get("auto_approve_edits")):
            run.emit("approval_done", call_id=cid, decision="auto")
            return "approve", ""
        p = {"ev": threading.Event(), "decision": None, "note": "", "kind": kind, "title": title}
        run.pending[cid] = p
        run.set_state("waiting", title)
        run.emit("approval", call_id=cid, kind=kind, title=title, always=("Always allow this command" if persist
                                                                          else "Allow all for this task"), **detail)
        try:
            while not p["ev"].wait(0.25):
                run.cancel.check()
        finally:
            run.pending.pop(cid, None)
        run.cancel.check()
        decision = p["decision"]
        run.emit("approval_done", call_id=cid, decision=decision, note=p["note"])
        if decision == "always":
            if persist:
                return "trust", p["note"]
            run.always.add(kind)
            decision = "approve"
        run.set_state("running", "")
        return decision, p["note"]

    def _do(self, run, store, cid, name, args, bridge):
        ws = run.ws
        box = Sandbox(ws["root"])
        idx = self.index(ws)

        if name == "project_overview":
            ov = self.overview(ws)
            return json.dumps(ov, indent=1), "%d files indexed" % ov["index"]["files"], {"overview": ov}

        if name == "list_files":
            base = box.resolve(args.get("path") or ".", must_exist=True)
            depth = max(1, min(4, int(args.get("depth") or 2)))
            lines = []
            for p in _walk_tree(base, depth):
                rel = box.rel(p)
                lines.append(rel + ("/" if p.is_dir() else "  (%s)" % _size(p)))
                if len(lines) >= 400:
                    lines.append("... more not shown")
                    break
            return "\n".join(lines) or "(empty)", "%d entries in %s" % (len(lines), box.rel(base)), None

        if name == "read_file":
            p = box.resolve(args.get("path"), must_exist=True)
            if p.is_dir():
                raise AgentError("'%s' is a folder - use list_files." % args.get("path"))
            if not _is_text_file(p, 5_000_000):
                raise AgentError("'%s' is not a text file (or is too big to read)." % args.get("path"))
            lines = p.read_text(encoding="utf-8", errors="replace").split("\n")
            start = max(1, int(args.get("start_line") or 1))
            end = int(args.get("end_line") or 0) or min(len(lines), start + MAX_READ_LINES - 1)
            end = min(len(lines), end, start + MAX_READ_LINES - 1)
            out, size = [], 0
            for i in range(start, end + 1):
                s = "%5d| %s" % (i, lines[i - 1])
                size += len(s)
                if size > MAX_READ_BYTES:
                    end = i - 1
                    break
                out.append(s)
            note = "" if end >= len(lines) else "\n[lines %d-%d of %d - read on with start_line=%d]" % (
                start, end, len(lines), end + 1)
            return "\n".join(out) + note, "%s lines %d-%d of %d" % (box.rel(p), start, end, len(lines)), \
                {"path": box.rel(p), "start": start, "end": end, "total": len(lines)}

        if name == "find_files":
            if not idx.data.get("files"):
                idx.build(budget_s=8)
            hits = idx.find(args.get("query", ""))
            text = "\n".join("%s%s" % (h["path"], "  [%s]" % ", ".join(h["matches"]) if h["matches"] else "")
                             for h in hits) or "nothing matched"
            return text, "%d matches" % len(hits), None

        if name == "search_code":
            return self._search(box, args)

        if name == "check_file":
            p = box.resolve(args.get("path"), must_exist=True)
            if p.is_dir() or not _is_text_file(p, 5_000_000):
                raise AgentError("'%s' is not a text file." % args.get("path"))
            text = p.read_text(encoding="utf-8", errors="replace")
            res = check_text(box.rel(p), text, self.props_meta)
            return _fmt_check(res), "%s: %s" % (box.rel(p), res["summary"]), {"check": res, "path": box.rel(p)}

        if name == "explain_error":
            text = args.get("text") or ""
            if not text and bridge is not None:
                text = "\n".join(bridge.console(400))
            traces = devtools.parse_stacktraces(text, ws["root"], idx.packages())
            build = devtools.parse_build_output(text, ws["root"])
            out = {"traces": traces[-5:], "build": build if (build["errors"] or build["failed"]) else None}
            n = len(traces) + len(build["errors"])
            return json.dumps(out, indent=1), ("%d trace(s), %d compile error(s)" % (len(traces), len(build["errors"]))
                                               if n else "no stack trace or compile error found"), out

        if name in ("write_file", "edit_file"):
            return self._write(run, store, cid, box, name, args, bridge)

        if name == "run_build":
            jp = devtools.java_project(ws["root"])
            if not jp["kind"]:
                raise AgentError("There is no build.gradle(.kts) or pom.xml in the workspace.")
            argv = jp["test_command"] if args.get("task") == "test" else jp["build_command"]
            return self._command(run, cid, box, argv, build=True)

        if name == "run_command":
            argv = split_command(args.get("command") or "")
            return self._command(run, cid, box, argv, build=os.path.basename(argv[0]).lower().split(".")[0]
                                 in BUILD_TOOLS if argv else False)

        if name == "remember":
            notes = store.remember(args.get("note", ""))
            run.emit("memory", notes=len(notes), text=notes[-1]["text"])
            return "Saved.", "saved: " + notes[-1]["text"][:80], None

        if name == "ask_user":
            q = (args.get("question") or "").strip() or "Could you tell me more?"
            run.final = q
            run.emit("question", text=q)
            run.emit("message", text=q)
            run.state = "question"
            return "The question was shown to the user; their answer will come as the next message.", "asked", None

        if name == "set_plan":
            steps = [str(s)[:200] for s in (args.get("steps") or [])][:20]
            files = [str(f)[:200] for f in (args.get("files") or [])][:30]
            done = max(0, min(len(steps), int(args.get("done") or 0)))
            run.emit("plan", steps=steps, files=files, done=done)
            return "Plan shown to the user.", "%d steps, %d files" % (len(steps), len(files)), None

        # -- server tools
        if name in ("server_status", "read_console", "server_command", "skript_environment", "skript_reload"):
            if bridge is None:
                raise AgentError("This workspace has no server linked.")
            if name == "server_status":
                info = bridge.info()
                return json.dumps(info, indent=1), "running" if info.get("running") else "stopped", None
            if name == "read_console":
                n = max(10, min(400, int(args.get("lines") or 80)))
                lines = bridge.console(n * (4 if args.get("filter") else 1))
                f = (args.get("filter") or "").lower()
                if f:
                    lines = [x for x in lines if f in x.lower()][-n:]
                return "\n".join(lines) or "(the console is empty)", "%d lines" % len(lines), None
            if name == "skript_environment":
                env = skripttools.environment(ws["root"])
                scripts = {}
                sd = Path(ws["root"]) / "plugins" / "Skript" / "scripts"
                for s in env["scripts"][:200]:
                    try:
                        scripts[s["name"]] = (sd / s["name"]).read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        pass
                rep = skripttools.workspace_report(scripts, env)
                brief = {"skript": env["skript"], "addons": env["addons"], "scripts": env["scripts"][:100],
                         "commands": rep["commands"][:100],
                         "functions": sorted(rep["functions"])[:100],
                         "findings": rep["cross"][:40], "missing_addons": rep["missing_addons"],
                         "file_problems": {k: v["problems"][:10] for k, v in rep["files"].items() if v["problems"]}}
                return json.dumps(brief, indent=1), "Skript %s, %d addons, %d scripts" % (
                    (env["skript"] or {}).get("version", "not installed"), len(env["addons"]), len(env["scripts"])), None
            if name == "server_command":
                cmd = (args.get("command") or "").strip().lstrip("/")
                if not cmd:
                    raise AgentError("No command given.")
                decision, note = self._wait_approval(run, cid, "server", "Run on the server: /%s" % cmd, command=cmd)
                if decision != "approve":
                    return "The user declined to run this command.%s" % (" Their note: " + note if note else ""), \
                        "declined", None
                seq = bridge.last_seq()
                ok, out = bridge.command(cmd)
                if not ok:
                    raise AgentError(out or "The server did not take the command.")
                run.cancel.wait(1.5)
                lines = [t for _s, t in bridge.since(seq)]
                return (out or "") + "\n" + "\n".join(lines[-60:]), "ran /%s" % cmd, None
            if name == "skript_reload":
                return self._skript_reload(run, cid, box, args, bridge)
        raise AgentError("There is no tool called '%s'." % name)

    def _search(self, box, args):
        q = args.get("query") or ""
        if not q:
            raise AgentError("Nothing to search for.")
        try:
            rx = re.compile(q if args.get("regex") else re.escape(q), re.I)
        except re.error as e:
            raise AgentError("Bad regular expression: %s" % e)
        base = box.resolve(args.get("path") or ".", must_exist=True)
        hits, files = [], 0
        for p in _walk(base):
            if not _is_text_file(p, 1_000_000):
                continue
            try:
                text = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            found = False
            for i, line in enumerate(text.split("\n"), 1):
                if rx.search(line):
                    hits.append("%s:%d: %s" % (box.rel(p), i, line.strip()[:200]))
                    found = True
                    if len(hits) >= 80:
                        break
            files += found
            if len(hits) >= 80:
                hits.append("... stopped at 80 matches - narrow the search")
                break
        return "\n".join(hits) or "no matches", "%d matches in %d files" % (len(hits), files), None

    def _write(self, run, store, cid, box, name, args, bridge):
        p = box.resolve(args.get("path"))
        rel = box.rel(p)
        if any(part in (".git",) for part in Path(rel).parts):
            raise AgentError("Refusing to write inside .git.")
        if p.suffix.lower() in (".jar", ".zip", ".exe", ".dll", ".class", ".dat", ".mca", ".png"):
            raise AgentError("Refusing to write a binary file (%s)." % p.suffix)
        exists = p.exists()
        if exists and p.is_dir():
            raise AgentError("'%s' is a folder." % rel)
        old = p.read_text(encoding="utf-8", errors="replace") if exists else ""
        if exists and not _is_text_file(p, 5_000_000):
            raise AgentError("'%s' is not a text file." % rel)
        if name == "write_file":
            new = args.get("content")
            if not isinstance(new, str):
                raise AgentError("content must be the whole file as text.")
        else:
            if not exists:
                raise AgentError("'%s' does not exist - use write_file to create it." % rel)
            old_t, new_t = args.get("old_text"), args.get("new_text")
            if not isinstance(old_t, str) or not old_t:
                raise AgentError("old_text must be the exact text to replace.")
            if not isinstance(new_t, str):
                raise AgentError("new_text must be text.")
            n = old.count(old_t)
            if n == 0:
                # line endings are the usual reason
                if old.count(old_t.replace("\n", "\r\n")):
                    old_t, new_t = old_t.replace("\n", "\r\n"), new_t.replace("\n", "\r\n")
                    n = old.count(old_t)
            if n == 0:
                raise AgentError("old_text was not found in %s. Read the file again and copy the text exactly, "
                                 "including indentation." % rel)
            if n > 1 and not args.get("replace_all"):
                raise AgentError("old_text appears %d times in %s. Include more surrounding lines so it is unique, "
                                 "or set replace_all." % (n, rel))
            new = old.replace(old_t, new_t) if args.get("replace_all") else old.replace(old_t, new_t, 1)
        if exists and new == old:
            return "No change: the file already has that content.", "no change", None
        if "\r\n" in old and "\r\n" not in new:
            new = new.replace("\n", "\r\n")
        diff = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                            "a/" + rel, "b/" + rel, n=3))
        added = sum(1 for ln in diff.splitlines() if ln.startswith("+") and not ln.startswith("+++"))
        removed = sum(1 for ln in diff.splitlines() if ln.startswith("-") and not ln.startswith("---"))
        checks = check_text(rel, new, self.props_meta)
        title = ("Create %s" if not exists else "Edit %s") % rel
        decision, note = self._wait_approval(run, cid, "write", title, path=rel, diff=_clip(diff, 60000),
                                             added=added, removed=removed, created=not exists,
                                             checks={k: checks.get(k) for k in ("format", "ok", "summary", "problems")})
        if decision != "approve":
            return ("The user rejected this change to %s.%s" % (rel, " Their note: " + note if note else "")), \
                "rejected", None
        chid = new_id()
        backup = ""
        if exists:
            bp = store.backup_path(chid)
            shutil.copy2(str(p), str(bp))
            backup = str(bp)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".lodestone-tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(new)
        os.replace(str(tmp), str(p))
        ch = {"id": chid, "run": run.id, "path": rel, "created": not exists, "backup": backup,
              "added": added, "removed": removed, "t": now()}
        store.record_change(ch)
        run.changes.append({k: ch[k] for k in ("id", "path", "created", "added", "removed")})
        run.emit("change", **{k: ch[k] for k in ("id", "path", "created", "added", "removed")})
        self.index(run.ws).data.get("files", {}).pop(rel, None)
        msg = "%s %s (+%d -%d)." % ("Created" if not exists else "Updated", rel, added, removed)
        if checks.get("problems"):
            msg += "\nChecks on the new content: %s\n%s" % (checks["summary"], _fmt_check(checks))
        return msg, "%s %s +%d -%d" % ("created" if not exists else "edited", rel, added, removed), None

    def _command(self, run, cid, box, argv, build=False):
        c = self.cfg()
        root = str(box.root)
        ok, why = command_policy(argv, root, c.get("allowed_commands") or [])
        if not ok:
            raise AgentError(why)
        shown = " ".join(shlex.quote(a) for a in argv)
        if shown in (c.get("trusted_commands") or []):
            run.emit("approval_done", call_id=cid, decision="auto")
        else:
            decision, note = self._wait_approval(run, cid, "command", "Run: %s" % shown, command=shown, cwd=root,
                                                 persist=True)
            if decision == "trust":
                trusted = list(c.get("trusted_commands") or [])
                if shown not in trusted:
                    self.save({"trusted_commands": trusted + [shown]})
                decision = "approve"
            if decision != "approve":
                return "The user declined to run '%s'.%s" % (shown, " Their note: " + note if note else ""), \
                    "declined", None
        real = resolve_argv(argv, root)
        run.set_state("tool", "running " + shown)
        buf, last = [], [now()]

        def on_line(line):
            buf.append(line)
            if now() - last[0] > 0.25 or len(buf) > 40:
                run.emit("output", call_id=cid, text="\n".join(buf))
                buf.clear()
                last[0] = now()

        runner = ProcessRunner(real, root, on_line, timeout=int(c.get("command_timeout") or 900), cancel=run.cancel)
        code, tail = runner.run()
        if buf:
            run.emit("output", call_id=cid, text="\n".join(buf))
        run.cancel.check()
        if runner.killed:
            raise AgentError("The command was stopped: %s." % runner.killed)
        head = "$ %s\nexit code %d\n" % (shown, code)
        data = None
        if build:
            parsed = devtools.parse_build_output(tail, root)
            data = {"build": parsed}
            lines = [head + parsed["summary"]]
            for e in parsed["errors"][:30]:
                lines.append("ERROR %s:%d: %s%s" % (e["file"], e["line"], e["message"],
                                                     " (%s)" % e["detail"] if e.get("detail") else ""))
            for h in parsed["hints"]:
                lines.append("HINT " + h)
            lines.append("\nlast output:\n" + _clip(tail, 5000))
            return "\n".join(lines), ("exit %d - %s" % (code, parsed["summary"])), data
        return head + _clip(tail, 8000), "exit %d" % code, data

    def _skript_reload(self, run, cid, box, args, bridge):
        name = (args.get("script") or "").strip().replace("\\", "/")
        if name.startswith("plugins/Skript/scripts/"):
            name = name[len("plugins/Skript/scripts/"):]
        if not name or ".." in name.split("/"):
            raise AgentError("Name a script under plugins/Skript/scripts.")
        if not name.endswith(".sk"):
            name += ".sk"
        if not (box.root / "plugins" / "Skript" / "scripts" / name).is_file():
            raise AgentError("There is no plugins/Skript/scripts/%s." % name)
        info = bridge.info()
        if not info.get("running") and not info.get("can_command"):
            raise AgentError("The server is not running, so Skript cannot check the script. Start it first, "
                             "or use check_file for the structure.")
        stem = name[:-3]
        decision, note = self._wait_approval(run, cid, "server", "Reload Skript script %s" % name,
                                             command="sk reload %s" % stem)
        if decision != "approve":
            return "The user declined the reload.%s" % (" Their note: " + note if note else ""), "declined", None
        return skript_reload(bridge, stem, run.cancel)

    # -- reading back -------------------------------------------------------
    def revert(self, ws, change_id):
        store = self.store(ws)
        ch = next((c for c in store.changes() if c["id"] == change_id), None)
        if not ch:
            raise AgentError("No such change.")
        if ch.get("reverted"):
            raise AgentError("That change was already put back.")
        box = Sandbox(ws["root"])
        p = box.resolve(ch["path"])
        later = [c for c in store.changes() if c["path"] == ch["path"] and c["t"] > ch["t"] and not c.get("reverted")]
        if later:
            raise AgentError("%s was changed again afterwards - put back the later change first." % ch["path"])
        if ch["created"]:
            if p.exists():
                p.unlink()
        else:
            if not ch.get("backup") or not os.path.exists(ch["backup"]):
                raise AgentError("The backup of %s is gone." % ch["path"])
            shutil.copy2(ch["backup"], str(p))
        store.mark_reverted(change_id)
        return {"path": ch["path"], "restored": not ch["created"], "deleted": ch["created"]}


def skript_reload(bridge, stem, cancel=None, timeout=20):
    """Send 'sk reload <stem>' and read Skript's own verdict from the console."""
    seq = bridge.last_seq()
    ok, out = bridge.command("sk reload %s" % stem)
    if not ok:
        raise AgentError(out or "The server did not take the command.")
    lines = []
    end = now() + timeout
    res = {"status": "no-output", "entries": []}
    while now() < end:
        if cancel is not None and cancel.wait(0.4):
            raise ol.Cancelled(cancel.reason)
        elif cancel is None:
            time.sleep(0.4)
        lines = [t for _s, t in bridge.since(seq)]
        res = skripttools.parse_reload(lines, stem)
        if res["status"] in ("ok", "errors", "warnings"):
            break
    if res["status"] == "no-output":
        text = ("Skript printed nothing recognisable within %ds. Is Skript installed and the script name right? "
                "Console:\n%s" % (timeout, "\n".join(lines[-30:])))
        return text, "no answer from Skript", {"reload": res}
    parts = ["Skript says: %s%s" % (res["status"], " (%d errors)" % res["error_count"] if res["error_count"] else "")]
    for e in res["entries"]:
        parts.append("line %d%s: %s%s" % (e["line"], " [%s]" % e["severity"] if e["severity"] else "",
                                          e["message"], "\n    code: " + e["code"] if e["code"] else ""))
    summary = ("reloaded cleanly" if res["status"] == "ok" else
               "%d error(s)" % res["error_count"] if res["status"] == "errors" else
               "%d warning(s)" % len(res["entries"]))
    return "\n".join(parts), summary, {"reload": res}


def check_text(name, text, props_meta=((), {})):
    try:
        if name.endswith(".java"):
            return devtools.java_lint(name, text)
        bk, ek = props_meta or ((), {})
        return devtools.validate(name, text, bool_keys=bk, enum_keys=ek)
    except Exception as e:
        return {"format": "", "ok": True, "summary": "could not check: %s" % e, "problems": []}


def _fmt_check(res):
    if not res.get("problems"):
        return res.get("summary", "no problems")
    out = [res["summary"]]
    for p in res["problems"][:40]:
        out.append("line %d [%s] %s%s" % (p["line"], p["severity"], p["message"],
                                         " - " + p["hint"] if p.get("hint") else ""))
    if res.get("note"):
        out.append(res["note"])
    return "\n".join(out)


def _walk_tree(base, depth):
    out = []

    def rec(d, level):
        try:
            entries = sorted(os.scandir(d), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return
        for e in entries:
            if e.name in SKIP_DIRS or e.name.startswith("."):
                if e.is_dir():
                    out.append(Path(e.path))
                continue
            out.append(Path(e.path))
            if e.is_dir(follow_symlinks=False) and level < depth:
                rec(e.path, level + 1)
            if len(out) > 400:
                return
    rec(base, 1)
    return out


def _size(p):
    try:
        return ol.fmt_bytes(p.stat().st_size)
    except OSError:
        return "?"


def _short_args(args):
    out = {}
    for k, v in (args or {}).items():
        if isinstance(v, str) and len(v) > 300:
            out[k] = v[:300] + "... (%d chars)" % len(v)
        else:
            out[k] = v
    return out


def _extract_json(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`")
        text = text[text.find("\n") + 1:] if "\n" in text else text
    i, j = text.find("{"), text.rfind("}")
    if i >= 0 and j > i:
        try:
            v = json.loads(text[i:j + 1])
            return v if isinstance(v, dict) else None
        except ValueError:
            return None
    return None


def text_tool_call(text, names):
    """A model with tool support that wrote its call as text instead:
    '<tool_call>{...}</tool_call>', or a bare {"name": ..., "arguments": ...}."""
    t = (text or "").strip()
    m = re.search(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", t, re.S)
    cand = m.group(1) if m else (t if t.startswith("{") and t.endswith("}") else "")
    if not cand:
        return None
    try:
        obj = json.loads(cand)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    name = obj.get("name") or obj.get("tool")
    args = obj.get("arguments", obj.get("parameters", obj.get("args", {})))
    if name in names and isinstance(args, (dict, str)):
        return ol.normalise_call({"function": {"name": name, "arguments": args}})
    return None
