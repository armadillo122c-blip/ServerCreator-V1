import importlib.util, io, json, os, shutil, sys, tempfile
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


spec = importlib.util.spec_from_file_location("lodestone", "lodestone.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

fails = []


def check(n, c, e=""):
    print(("  ok   " if c else "  FAIL ") + n + (("  " + str(e)[:240]) if not c else ""))
    if not c:
        fails.append(n)


P = m.PANEL
P.save = lambda *a, **k: True          # never touch the real config in tests
keep_cfg = json.loads(json.dumps(P.cfg))

# ---------- a throwaway server for the assistant to operate ----------------
root = tempfile.mkdtemp(prefix="lds_ai_")
os.makedirs(os.path.join(root, "plugins", "Skript", "scripts"))
open(os.path.join(root, "eula.txt"), "w").write("eula=true\n")
open(os.path.join(root, "server.properties"), "w").write(
    "#props\ndifficulty=easy\npvp=true\nmotd=Before\n")
srv = m.blank_server("local", "AI test")
srv["local"]["cwd"] = root
P.cfg["servers"].append(srv)
P.cfg["active"] = srv["id"]
P.cfg["ai"].update({"api_key": "test-key", "allow_commands": True, "allow_files": True,
                    "provider": "openai"})

# ---------- settings surface ----------------------------------------------
got = P.ai_get()
check("ai_get never returns the key", "api_key" not in got and got["has_key"] is True, got)
P.ai_save({"model": "some-model", "allow_files": False})
check("ai_save stores fields", P.cfg["ai"]["model"] == "some-model" and P.cfg["ai"]["allow_files"] is False)
P.ai_save({"model": "some-model"})
check("blank key does not wipe the stored key", P.cfg["ai"]["api_key"] == "test-key")
P.ai_save({"api_key": "new-key"})
check("a supplied key replaces it", P.cfg["ai"]["api_key"] == "new-key")
P.cfg["ai"]["allow_files"] = True

# ---------- the agent loop, driven by a scripted fake model ----------------
SCRIPT = [
    '{"action":"read_file","path":"server.properties"}',
    '{"action":"set_property","key":"difficulty","value":"hard"}',
    '{"action":"say","message":"difficulty is now hard"}',
    '{"action":"write_file","path":"plugins/Skript/scripts/greet.sk",'
    '"content":"on join:\\n\\tsend \\"hi\\" to player"}',
    '{"action":"notify","title":"Assistant done","description":"tuned the server"}',
    '{"action":"done","reply":"Difficulty is hard and greet.sk is in place."}',
]
calls = []


def fake_chat(system, messages, force_json=False):
    calls.append(messages[-1]["content"])
    assert force_json, "the agent loop must ask for strict JSON"
    return SCRIPT[len(calls) - 1], {"in": 10, "out": 5}


P._ai_chat = fake_chat
ok, reply, log, usage = P.ai_run("make it harder and add a greeting script")
check("agent loop runs to completion", ok and "hard" in reply, (ok, reply))
check("every scripted step executed", [l["action"] for l in log] ==
      ["read_file", "set_property", "say", "write_file", "notify"], [l["action"] for l in log])
check("token usage is accumulated over every call",
      usage["in"] == 10 * len(SCRIPT) and usage["out"] == 5 * len(SCRIPT), usage)
raw = open(os.path.join(root, "server.properties")).read()
check("set_property really changed the file", "difficulty=hard" in raw and "motd=Before" in raw, raw)
check("write_file really wrote the script",
      os.path.exists(os.path.join(root, "plugins", "Skript", "scripts", "greet.sk")))
check("system prompt reached the model", "Skript" in P.AI_SYSTEM)
check("context tells the model where it is",
      "AI test" in calls[0] and "local" in calls[0], calls[0][:160])
check("history remembered for next turn", len(P.ai_history) == 2)

# ---------- permissions are actually enforced -----------------------------
P.cfg["ai"]["allow_files"] = False
out, note = P._ai_do("write_file", {"path": "x.txt", "content": "nope"})
check("file writes refused when the permission is off", "denied" in out and "(denied)" in note, (out, note))
check("and nothing was written", not os.path.exists(os.path.join(root, "x.txt")))
P.cfg["ai"]["allow_commands"] = False
out, note = P._ai_do("run", {"command": "stop"})
check("commands refused when the permission is off", "denied" in out, out)
P.cfg["ai"]["allow_commands"] = True
P.cfg["ai"]["allow_files"] = True

# a failing action must be reported, not raise
out, note = P._ai_do("read_file", {"path": "does/not/exist.txt"})
check("a failed action is reported, not raised", "failed" in out.lower() or "not a file" in out.lower(), out)
out, note = P._ai_do("nonsense", {})
check("unknown actions are handled", out == "unknown action", out)

# ---------- automod --------------------------------------------------------
P.cfg["ai"].update({"automod": True, "automod_auto": False})
P._automod_q.clear()
P.ingest("[10:00:00] [Server thread/INFO]: <Griefer> buy ranks at otherserver.net")
check("chat is queued for automod", len(P._automod_q) == 1, list(P._automod_q))
P._ai_chat = lambda sys_, msgs, force_json=False: (
    '{"violations":[{"player":"Griefer","reason":"advertising","action":"kick"}]}', {"in": 1, "out": 1})
P._automod_last = 0
sent = []
P.command = lambda c, quiet=False, who="panel": (sent.append(c), (True, ""))[1]
P._automod_tick()
check("automod flags a violation", any("automod flag" in t for _s, t in P.recent(20)),
      [t for _s, t in P.recent(5)])
check("review-only mode does not act", not sent, sent)
P.cfg["ai"]["automod_auto"] = True
P._automod_q.append(("Griefer", "buy ranks at otherserver.net"))
P._automod_last = 0
P._automod_tick()
check("auto mode issues the kick", any("kick Griefer" in c for c in sent), sent)

# ---------- migration from the old apps ------------------------------------
# Written into the isolated HOME so this exercises the real reader without
# depending on what happens to be on the machine.
home = os.environ["LODESTONE_TEST_HOME"]
legacy_cwd = os.path.join(home, "OldServer")
os.makedirs(legacy_cwd, exist_ok=True)
io.open(os.path.join(home, ".mc_control_panel.json"), "w", encoding="utf-8").write(json.dumps({
    "java": "java", "jar": os.path.join(legacy_cwd, "paper.jar"), "cwd": legacy_cwd,
    "min_ram": "2G", "max_ram": "8G", "server_type": "paper",
    "features": {"auto_restart": True,
                 "webhook": {"enabled": True, "url": "https://discord.com/api/webhooks/1/legacy",
                             "join_leave": True, "start_stop": False}},
    "ai": {"provider": "anthropic", "api_key": "legacy-key", "automod": True,
           "rules": "No advertising."},
}))
io.open(os.path.join(home, ".mc_sftp_panel.json"), "w", encoding="utf-8").write(json.dumps({
    "host": "ftp.example.com", "port": 2223, "user": "abc123", "password": "pw",
    "save_password": True, "root": "/server", "display_name": "CurrentBox",
}))
io.open(os.path.join(home, ".remotehost_profiles.json"), "w", encoding="utf-8").write(json.dumps({
    "profiles": [
        {"name": "Alpha", "host": "a.example.com", "port": 22, "user": "u1",
         "password": "p1", "root": "/srv", "id": "aaa"},
        {"name": "Beta", "host": "b.example.com", "port": 22, "user": "u2",
         "password": "p2", "root": "/srv", "id": "bbb"},
    ],
}))

P.cfg = json.loads(json.dumps(keep_cfg))
P.cfg["servers"] = [m.blank_server("local", "My server")]
P.cfg["active"] = P.cfg["servers"][0]["id"]
P.cfg["ai"] = dict(P.cfg.get("ai", {}), api_key="")
avail = P.legacy_available()
print("    legacy found:", [(a["kind"], a["name"], a["new"]) for a in avail])
check("both old apps are detected", len(avail) == 4, avail)
check("a missing folder is flagged", any(a["kind"] == "local" for a in avail), avail)
added = P.import_legacy()
print("    imported:", added)
check("import brings across local + all three remotes + assistant settings",
      len(added) == 5 and "assistant settings" in added, added)
names = [x["name"] for x in P.cfg["servers"]]
check("the empty starter row was dropped", "My server" not in names, names)
local = [x for x in P.cfg["servers"] if x["kind"] == "local"]
check("local server kept its jar and memory settings",
      local and local[0]["local"]["max_ram"] == "8G" and local[0]["local"]["cwd"] == legacy_cwd,
      local[0]["local"] if local else None)
check("the old single webhook came across",
      local and local[0]["discord"]["hooks"] and
      local[0]["discord"]["hooks"][0]["url"].endswith("/legacy"),
      local[0]["discord"]["hooks"] if local else None)
check("webhook event choices were honoured",
      local and "join_leave" in local[0]["discord"]["hooks"][0]["events"]
      and "start_stop" not in local[0]["discord"]["hooks"][0]["events"],
      local[0]["discord"]["hooks"][0]["events"] if local else None)
check("automation settings came across", local and local[0]["features"]["auto_restart"] is True)
check("assistant key and rules came across",
      P.cfg["ai"]["api_key"] == "legacy-key" and "advertising" in P.cfg["ai"]["rules"],
      P.cfg["ai"].get("rules"))
remotes = sorted(x["sftp"]["host"] for x in P.cfg["servers"] if x["kind"] == "sftp")
check("all three remote profiles landed",
      remotes == ["a.example.com", "b.example.com", "ftp.example.com"], remotes)
again = P.import_legacy()
check("importing twice adds nothing new", again == [], again)
check("no duplicate servers after a second import",
      len(P.cfg["servers"]) == len(set(
          (x["kind"], x["local"]["cwd"], x["sftp"]["host"], x["sftp"]["user"]) for x in P.cfg["servers"])),
      [x["name"] for x in P.cfg["servers"]])

P.cfg = keep_cfg
P.backends.clear()
shutil.rmtree(root, ignore_errors=True)
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
