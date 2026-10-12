"""Chat parsing (the bug the user reported), log search, plugin management,
the ops/whitelist lists, and the local-model providers."""
import importlib.util
import os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
# lodestone.py reads Path.home() at import time, so redirect it BEFORE loading
# the module: every test then runs against a throwaway config, never the real one.
_TESTHOME = os.environ.get("LODESTONE_TEST_HOME")
if not _TESTHOME:
    _TESTHOME = tempfile.mkdtemp(prefix="lodestone_testhome_")
    os.environ["LODESTONE_TEST_HOME"] = _TESTHOME
os.environ["HOME"] = _TESTHOME
os.environ["USERPROFILE"] = _TESTHOME
os.environ["HOMEDRIVE"] = os.path.splitdrive(_TESTHOME)[0] or "C:"
os.environ["HOMEPATH"] = os.path.splitdrive(_TESTHOME)[1]

import gzip
import json
import re
import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

spec = importlib.util.spec_from_file_location("lodestone", "lodestone.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

fails = []


def check(n, c, e=""):
    print(("  ok   " if c else "  FAIL ") + n + (("  " + str(e)[:230]) if not c else ""))
    if not c:
        fails.append(n)


# =========================================================== chat parsing ====
# The exact shape from the user's own logs, which the old regex could not read.
REAL = "[08:13:27] [Async Chat Thread - #0/INFO]: <#BF0000>O<#C70D09>W<#D01A13>N" \
       "<#D8261C>E<#E03325>R <#E03325>Armadillo122: fuh"
OLD_RE = re.compile(r"]:\s+<([A-Za-z0-9_]{2,16})>\s+(.*)$")
check("the format that was broken now parses", m.parse_chat(REAL) == ("Armadillo122", "fuh"),
      m.parse_chat(REAL))
check("and it really was broken before", OLD_RE.search(REAL) is None)

CASES = [
    ("vanilla", "[1] [Server thread/INFO]: <Steve> hello world", ("Steve", "hello world")),
    ("paper async, plain", "[1] [Async Chat Thread - #1/INFO]: Steve: hey there", ("Steve", "hey there")),
    ("skript arrow format",
     "[1] [Async Chat Thread - #0/INFO]: §f§aOWNER Steve §8» §fhi all", ("Steve", "hi all")),
    ("prestige, tick and a glued rank",
     "[1] [Async Chat Thread - #3/INFO]: §a✔ §f12 §cADMIN§fNotch §8» §fgg",
     ("Notch", "gg")),
    ("essentials brackets", "[1] [Async Chat Thread - #1/INFO]: [Member] Alex: yo", ("Alex", "yo")),
    ("minimessage tags",
     "[1] [Async Chat Thread - #2/INFO]: <gradient:#ff0000:#00ff00>MVP</gradient> <white>Dinnerbone</white>: test",
     ("Dinnerbone", "test")),
    ("ampersand hex", "[1] [Async Chat Thread - #1/INFO]: &x&F&F&0&0&0&0VIP &fjeb_: wow", ("jeb_", "wow")),
    ("a colon inside the message",
     "[1] [Async Chat Thread - #1/INFO]: Steve: check this: it works", ("Steve", "check this: it works")),
    ("underscores in the name",
     "[1] [Async Chat Thread - #1/INFO]: [VIP] some_player_1 » hi", ("some_player_1", "hi")),
    ("rank glued with a colour code",
     "[1] [Async Chat Thread - #1/INFO]: §c[MVP]§fSteve: hey", ("Steve", "hey")),
    ("bar separated rank", "[1] [Async Chat Thread - #1/INFO]: Owner | Armadillo122 » yo",
     ("Armadillo122", "yo")),
]
for name, line, want in CASES:
    check("chat: " + name, m.parse_chat(line) == want, m.parse_chat(line))

NEGATIVE = [
    ("a join line", "[1] [Server thread/INFO]: Steve joined the game"),
    ("the startup banner", "[1] [Server thread/INFO]: Done (6.1s)! For help, type help"),
    ("a plugin loading", "[1] [Server thread/INFO]: [LiteBans] Loading: h2 1.4.197"),
    ("a lag warning", "[1] [Server thread/WARN]: Can't keep up! Is the server overloaded?"),
    ("a punishment", "[1] [Server thread/INFO]: PUNISHMENT » Steve was banned by Console for x"),
    ("a command on the chat thread", "[1] [Async Chat Thread - #1/INFO]: Steve issued server command: /home"),
    ("a disconnect on the chat thread", "[1] [Async Chat Thread - #1/INFO]: Steve lost connection: Disconnected"),
    ("a coloured broadcast on the main thread",
     "[1] [Server thread/INFO]: <#FF0000>Broadcast<#FFFFFF>: server restarting"),
]
for name, line in NEGATIVE:
    check("not chat: " + name, m.parse_chat(line) is None, m.parse_chat(line))

cus = re.compile(r"\[CHAT\]\s*(?P<player>\w+)\s*~\s*(?P<message>.*)$")
check("a custom pattern overrides detection",
      m.parse_chat("[1] [Server thread/INFO]: [CHAT] Steve ~ custom", cus) == ("Steve", "custom"))
check("a custom pattern that does not match yields nothing",
      m.parse_chat("[1] [Async Chat Thread - #1/INFO]: Steve: hi", cus) is None)

# =============================================== events read out of the log ===
GOT = []


class Hook(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n)
        GOT.append(raw.decode("utf-8", "replace"))
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.end_headers()


hook_srv = ThreadingHTTPServer(("127.0.0.1", 8794), Hook)
hook_srv.daemon_threads = True
threading.Thread(target=hook_srv.serve_forever, daemon=True).start()

root = tempfile.mkdtemp(prefix="lds_tools_")
os.makedirs(os.path.join(root, "plugins"))
os.makedirs(os.path.join(root, "logs"))
open(os.path.join(root, "eula.txt"), "w").write("eula=true\n")
open(os.path.join(root, "server.properties"), "w").write("#p\nmotd=x\n")
open(os.path.join(root, "ops.json"), "w").write('[{"uuid":"1","name":"Armadillo122","level":4}]')
for jar in ("Skript-2.16.0.jar", "DiSky.jar", "Old.jar.disabled"):
    open(os.path.join(root, "plugins", jar), "wb").write(b"PK\x03\x04" + b"x" * 200)
open(os.path.join(root, "logs", "latest.log"), "w", encoding="utf-8").write(
    "[1] [Server thread/ERROR]: java.lang.NullPointerException: boom\n"
    "[2] [Async Chat Thread - #1/INFO]: Steve: hello\n")
with gzip.open(os.path.join(root, "logs", "2026-08-20-1.log.gz"), "wt", encoding="utf-8") as f:
    f.write("[3] [Server thread/ERROR]: java.lang.OutOfMemoryError: heap\n"
            "[4] [Server thread/INFO]: nothing interesting\n")

P = m.PANEL
P.save = lambda *a, **k: True
srv = m.blank_server("local", "Tools test")
srv["local"]["cwd"] = root
srv["discord"]["hooks"] = [dict(m.Discord.blank_hook(), url="http://127.0.0.1:8794/h",
                                enabled=True, embeds=True,
                                events=["punish", "ingame_cmd", "lag", "first_join", "chat"])]
P.cfg["servers"].append(srv)
P.cfg["active"] = srv["id"]
P.backends.clear()

GOT.clear()
P.ingest("[1] [Server thread/INFO]: PUNISHMENT » Griefer was banned by Armadillo122 for griefing")
P.ingest("[2] [Server thread/INFO]: Kicked Steve: spamming")
P.ingest("[3] [Server thread/INFO]: Steve issued server command: /tp 0 64 0")
P.ingest("[4] [Server thread/WARN]: Can't keep up! Is the server overloaded?")
P.ingest("[5] [Server thread/INFO]: Newbie[/1.2.3.4:1234] logged in with entity id 42")
import time
time.sleep(2.5)
blob = " ".join(GOT)
check("a LiteBans-style ban is picked up", "Griefer" in blob and "banned" in blob.lower(), blob[:200])
check("a vanilla kick is picked up", "Steve" in blob and "kick" in blob.lower(), blob[:200])
check("an in-game command is picked up", "/tp 0 64 0" in blob, blob[:200])
check("a lag warning is picked up", "lagging" in blob.lower(), blob[:200])
check("a first-time join is picked up", "Newbie" in blob and "first" in blob.lower(), blob[:200])

GOT.clear()
P.ingest("[6] [Server thread/INFO]: Newbie[/1.2.3.4:1234] logged in with entity id 43")
time.sleep(1.5)
check("the same player is only 'first join' once", not any("first" in g.lower() for g in GOT), GOT)

# ================================================================ log search ===
r = P.log_search("java.lang")
check("log search reads both plain and gzipped logs",
      r["ok"] and len(r["hits"]) == 2
      and {h["file"] for h in r["hits"]} == {"latest.log", "2026-08-20-1.log.gz"}, r.get("hits"))
check("log search reports which files it read", len(r["scanned"]) == 2, r.get("scanned"))
r2 = P.log_search("OutOfMemory")
check("log search finds text only inside the gzip",
      r2["ok"] and len(r2["hits"]) == 1 and r2["hits"][0]["file"].endswith(".gz"), r2.get("hits"))
r3 = P.log_search("NULLPOINTER")
check("search is case-insensitive by default", len(r3["hits"]) == 1, r3.get("hits"))
r4 = P.log_search("NULLPOINTER", case=True)
check("match-case is honoured", len(r4["hits"]) == 0, r4.get("hits"))
r5 = P.log_search(r"java\.lang\.\w+Error", regex=True)
check("regex mode works", len(r5["hits"]) == 1, r5.get("hits"))
r6 = P.log_search("[unclosed", regex=True)
check("a bad regex is reported, not raised", r6["ok"] is False and "regex" in r6["error"].lower(), r6)
check("an empty query is refused", P.log_search("")["ok"] is False)

# ================================================================== plugins ===
pl = P.plugins_list()
names = sorted(x["title"] for x in pl["items"])
check("plugins are listed", pl["ok"] and names == ["DiSky.jar", "Old.jar", "Skript-2.16.0.jar"], names)
check("a disabled jar is shown as disabled",
      [x for x in pl["items"] if x["title"] == "Old.jar"][0]["enabled"] is False)
tog = P.plugin_toggle("plugins/DiSky.jar")
check("disabling renames rather than deletes",
      tog["name"] == "DiSky.jar.disabled"
      and os.path.exists(os.path.join(root, "plugins", "DiSky.jar.disabled"))
      and not os.path.exists(os.path.join(root, "plugins", "DiSky.jar")), tog)
P.plugin_toggle("plugins/DiSky.jar.disabled")
check("and enabling puts it back", os.path.exists(os.path.join(root, "plugins", "DiSky.jar")))
try:
    P.plugin_toggle("plugins/notajar.txt")
    check("non-jars are refused", False)
except RuntimeError:
    check("non-jars are refused", True)

# ==================================================================== lists ===
lst = P.list_get("ops")
check("ops.json is read", lst["ok"] and lst["items"][0]["name"] == "Armadillo122", lst)
ok, out = P.list_edit("ops", "Notch", True)
check("editing a list without a reachable server explains itself",
      not ok and ("running" in out.lower() or "rcon" in out.lower()), out)
try:
    P.list_edit("ops", "not a name!", True)
    check("a bad player name is refused", False)
except RuntimeError:
    check("a bad player name is refused", True)
try:
    P.list_edit("banned-ips", "1.2.3.4", True)
    check("an IP is accepted for the ip list", True)
except RuntimeError as e:
    check("an IP is accepted for the ip list", "not an IP" not in str(e), e)

# ============================================================ local AI shape ===
check("ollama is a known provider", "ollama" in m.Panel.PROVIDERS)
check("local providers need no key", set(m.Panel.LOCAL_PROVIDERS) >= {"ollama", "lmstudio", "llamacpp"})
P.cfg["ai"].update({"provider": "ollama", "base_url": "", "api_key": "", "model": ""})
check("ollama gets a sane default base",
      P._base_for(P.cfg["ai"]) == "http://localhost:11434", P._base_for(P.cfg["ai"]))
info = P.ai_get()
check("the UI is told no key is needed", info["local"] is True and info["needs_key"] is False, info)
# point at a port nothing is on, so this does not depend on whether the machine
# happens to be running Ollama
P.cfg["ai"]["base_url"] = "http://127.0.0.1:9"
ok2, msg2 = P.ai_test()
check("an unreachable local server fails clearly instead of hanging",
      ok2 is False and ("reach" in msg2.lower() or "running" in msg2.lower()
                        or "refused" in msg2.lower()), msg2)
ok3, models, base = P.ai_models()
check("model detection reports that failure too", ok3 is False and ":9" in base, (ok3, base))
P.cfg["ai"]["base_url"] = ""

captured = {}


def fake_json(url, body, headers, who="API", timeout=60):
    """Answers in whichever shape the caller expects, so one stub covers both
    the native Ollama branch and the OpenAI-compatible one."""
    captured["url"], captured["body"] = url, body
    return {"message": {"content": '{"action":"done","reply":"ok"}'},
            "prompt_eval_count": 7, "eval_count": 3,
            "choices": [{"message": {"role": "assistant",
                                     "content": '{"action":"done","reply":"ok"}'}}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3}}


P._http_json = fake_json
txt, usage = P._ai_chat("sys", [{"role": "user", "content": "hi"}], force_json=True)
check("ollama uses its native chat endpoint", captured["url"].endswith("/api/chat"), captured.get("url"))
check("streaming is turned off", captured["body"].get("stream") is False, captured.get("body"))
check("json mode is requested for the agent loop", captured["body"].get("format") == "json", captured.get("body"))
check("the system prompt is sent as a message",
      captured["body"]["messages"][0]["role"] == "system", captured["body"]["messages"][0])
check("the reply is read from message.content", txt.startswith('{"action"'), txt)
check("ollama token counts are mapped", usage == {"in": 7, "out": 3}, usage)

P.cfg["ai"].update({"provider": "lmstudio", "base_url": "", "model": "local-model"})
txt2, usage2 = P._ai_chat("sys", [{"role": "user", "content": "hi"}], force_json=True)
check("lm studio uses the openai shape", captured["url"].endswith("/chat/completions"), captured.get("url"))
check("and asks for a json object",
      captured["body"].get("response_format", {}).get("type") == "json_object", captured.get("body"))
check("lm studio needs no Authorization header", txt2.startswith('{"action"'), txt2)
check("openai-shaped usage is mapped", usage2 == {"in": 7, "out": 3}, usage2)
P.cfg["ai"].update({"provider": "openai", "model": "", "base_url": ""})

hook_srv.shutdown()
shutil.rmtree(root, ignore_errors=True)
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
