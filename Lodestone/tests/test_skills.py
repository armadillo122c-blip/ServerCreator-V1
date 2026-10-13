"""Skills: reading SKILL.md headers the ways people write them, where skills
are found and which wins, the built-in ones, making, importing (folders, zips,
.skill files - and zips that try to write outside their folder) and deleting,
and the assistant using them: listed by description, loaded with use_skill,
started with /name, written with save_skill - against a fake Ollama."""
import io
import json
import os
import sys
import tempfile
import time
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import agent as ag
import skills as sk
from fake_ollama import FakeOllama

fails = []


def check(n, c, e=""):
    print(("  ok   " if c else "  FAIL ") + n + (("  " + str(e)[:300]) if not c else ""))
    if not c:
        fails.append(n)


def section(t):
    print("\n-- " + t)


TMP = tempfile.mkdtemp(prefix="skills_test_")
HOME = os.path.join(TMP, "home")
DATA = os.path.join(TMP, "data")
os.makedirs(HOME)


def make(root, name, header, body="Do the thing.\n", files=None):
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8", newline="") as f:
        f.write(header + body)
    for rel, text in (files or {}).items():
        p = os.path.join(d, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
    return d


# ---------------------------------------------------------------------------
section("reading SKILL.md headers")
f, body = sk._frontmatter('---\nname: x\ndescription: "Says \\"hi\\": politely"\n---\nBody\n')
check("double-quoted values, escapes undone", f["description"] == 'Says "hi": politely', f)
f, _ = sk._frontmatter("---\nname: q\ndescription: 'It''s single-quoted: fine'\n---\n")
check("single-quoted values", f["description"] == "It's single-quoted: fine", f)
f, body = sk._frontmatter("﻿---\r\nname: crlf\r\ndescription: Windows line endings\r\n---\r\nBody")
check("a BOM and CRLF", f == {"name": "crlf", "description": "Windows line endings"} and body.strip() == "Body", (f, body))
f, _ = sk._frontmatter("---\nname: y\ndescription: >\n  Folded over\n  two lines\nallowed-tools: Read, Grep\n---\nx")
check("a folded block", f["description"] == "Folded over two lines" and f["allowed-tools"] == "Read, Grep", f)
f, _ = sk._frontmatter("---\nname: z\ndescription: starts here\n  and carries on\nmetadata:\n  author: me\n---\nx")
check("a plain value over two lines, and a nested map ignored", f["description"] == "starts here and carries on", f)
f, body = sk._frontmatter("# Just markdown\nNo header at all")
check("no header: all body", f == {} and body.startswith("# Just"), f)

# ---------------------------------------------------------------------------
section("where skills come from")
WS = os.path.join(TMP, "ws")
make(os.path.join(WS, ".lodestone", "skills"), "deploy", "---\nname: deploy\ndescription: Workspace deploy steps\n---\n")
make(os.path.join(WS, ".claude", "skills"), "review", "---\nname: review\ndescription: Workspace review\n---\n")
make(os.path.join(DATA, "skills"), "deploy", "---\nname: deploy\ndescription: My own deploy\n---\n")
make(os.path.join(DATA, "skills"), "backups", "---\nname: backups\ndescription: How I back up\n---\n")
make(os.path.join(DATA, "skills"), "nodesc", "---\nname: nodesc\n---\n", "# Restart politely\nSay it first.\n")
make(os.path.join(HOME, ".claude", "skills"), "pdf", "---\nname: pdf\ndescription: Claude Code skill\n"
                                                    "disable-model-invocation: true\n---\n")
make(os.path.join(DATA, "skills"), "skript", "---\nname: skript\ndescription: My Skript rules\n---\n")
found = sk.discover(DATA, WS, disabled=["backups"], home=HOME)
by = {s["name"]: s for s in found}
check("the workspace's skill wins over yours of the same name", by["deploy"]["source"] == "workspace"
      and by["deploy"]["description"] == "Workspace deploy steps", by.get("deploy"))
check(".claude/skills in the workspace counts", by["review"]["source"] == "workspace")
check("Claude Code's own ~/.claude/skills are found", by["pdf"]["source"] == "claude" and by["pdf"]["user_only"])
check("yours win over the built-in of the same name", by["skript"]["source"] == "yours")
check("the built-in ones are there", {"paper-plugin", "fabric-mod", "fix-server-crash", "skill-creator"} <= set(by), list(by))
check("switched off is marked, not hidden", by["backups"]["enabled"] is False and by["deploy"]["enabled"])
check("no description: the first line stands in", by["nodesc"]["description"] == "Restart politely", by["nodesc"])
check("each name once", len(found) == len(by))

section("the built-in skills")
for s in sk.discover(os.path.join(TMP, "none"), None, home=os.path.join(TMP, "nohome")):
    body, files = sk.load(s)
    check("%s: a valid name matching its folder, a description, instructions" % s["name"],
          s["valid_name"] and os.path.basename(s["dir"]) == s["name"] and 40 < len(s["description"]) <= 1024
          and len(body) > 300, s)

# ---------------------------------------------------------------------------
section("files inside a skill")
d = make(os.path.join(DATA, "skills"), "withfiles", "---\nname: withfiles\ndescription: Has files\n---\n",
         files={"reference.md": "ref text", "templates/a.txt": "A"})
s = sk.read_skill(d, "yours")
body, files = sk.load(s)
check("the files beside SKILL.md are listed", files == ["reference.md", "templates/a.txt"], files)
check("and read", sk.read_file(s, "templates/a.txt") == "A")
for bad in ("../deploy/SKILL.md", "/etc/passwd", "../../x"):
    try:
        sk.read_file(s, bad)
        check("%s is refused" % bad, False)
    except sk.SkillError:
        check("%s is refused" % bad, True)
open(os.path.join(d, "bin.dat"), "wb").write(b"\0\1\2")
try:
    sk.read_file(s, "bin.dat")
    check("a binary file is refused", False)
except sk.SkillError:
    check("a binary file is refused", True)

# ---------------------------------------------------------------------------
section("making and deleting")
for bad in ("My Skill", "UPPER", "-lead", "a" * 65, "", "dots.no"):
    try:
        sk.save(DATA, bad, "d", "b")
        check("name %r is refused" % bad, False)
    except sk.SkillError:
        check("name %r is refused" % bad, True)
try:
    sk.save(DATA, "nodescr", "", "body")
    check("a description is required", False)
except sk.SkillError:
    check("a description is required", True)
s = sk.save(DATA, "rank-chat", 'Format chat as "RANK Name » msg": for chat plugins', "1. Check the plugin.\n")
f, body = sk._frontmatter(open(os.path.join(s["dir"], "SKILL.md"), encoding="utf-8").read())
check("a saved skill reads back the same", f["name"] == "rank-chat"
      and s["description"] == 'Format chat as "RANK Name » msg": for chat plugins'
      and body.strip() == "1. Check the plugin.", (f, body))
try:
    sk.save(DATA, "rank-chat", "again", "x", overwrite=False)
    check("not overwritten unless asked", False)
except sk.SkillError:
    check("not overwritten unless asked", True)
check("deleting yours works", sk.delete(DATA, "rank-chat") and not os.path.exists(s["dir"]))
try:
    sk.delete(DATA, "paper-plugin")
    check("a built-in cannot be deleted", False)
except sk.SkillError as e:
    check("a built-in cannot be deleted", "not one of your skills" in str(e), e)

# ---------------------------------------------------------------------------
section("importing")
src = make(os.path.join(TMP, "elsewhere"), "folder-skill", "---\nname: folder-skill\ndescription: From a folder\n---\n",
           files={"notes.md": "n"})
os.makedirs(os.path.join(src, ".git"))
s = sk.import_skill(DATA, src)
check("a folder is copied in, without .git", s["source"] == "yours" and os.path.isfile(os.path.join(s["dir"], "notes.md"))
      and not os.path.exists(os.path.join(s["dir"], ".git")), s)
try:
    sk.import_skill(DATA, src)
    check("importing twice asks first", False)
except sk.SkillError as e:
    check("importing twice asks first", "already have" in str(e), e)
check("and replaces when told to", sk.import_skill(DATA, src, overwrite=True)["name"] == "folder-skill")


def zipped(path, entries):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return path


p = zipped(os.path.join(TMP, "top.zip"), {"SKILL.md": "---\nname: zip-top\ndescription: At the top\n---\nx",
                                          "ref/a.md": "a"})
s = sk.import_skill(DATA, p)
check("a zip with SKILL.md at the top", s["name"] == "zip-top" and os.path.isfile(os.path.join(s["dir"], "ref", "a.md")))
p = zipped(os.path.join(TMP, "x.skill"), {"one-down/SKILL.md": "---\nname: one-down\ndescription: In a folder\n---\nx",
                                          "one-down/b.txt": "b", "stray.txt": "not part of it"})
s = sk.import_skill(DATA, p)
check("a .skill with the skill one folder down", s["name"] == "one-down" and os.path.isfile(os.path.join(s["dir"], "b.txt"))
      and not os.path.exists(os.path.join(s["dir"], "stray.txt")), s)
p = zipped(os.path.join(TMP, "evil.zip"), {"SKILL.md": "---\nname: evil\ndescription: Escapes\n---\nx",
                                           "../../outside.txt": "gotcha"})
try:
    sk.import_skill(DATA, p)
    check("a zip that writes outside its folder is refused", False)
except sk.SkillError as e:
    check("a zip that writes outside its folder is refused", "outside" in str(e)
          and not os.path.exists(os.path.join(DATA, "outside.txt")) and not os.path.exists(os.path.join(DATA, "skills", "evil")), e)
p = zipped(os.path.join(TMP, "none.zip"), {"readme.md": "no skill here"})
try:
    sk.import_skill(DATA, p)
    check("a zip with no SKILL.md is refused", False)
except sk.SkillError as e:
    check("a zip with no SKILL.md is refused", "no SKILL.md" in str(e), e)
open(os.path.join(TMP, "notzip.skill"), "w").write("hello")
try:
    sk.import_skill(DATA, os.path.join(TMP, "notzip.skill"))
    check("a file that is not a zip is refused", False)
except sk.SkillError:
    check("a file that is not a zip is refused", True)

# ---------------------------------------------------------------------------
section("the assistant using skills")
fake = FakeOllama()
fake.add_model("coder:7b", tools=True, ctx=32768)
SETTINGS = {"base_url": fake.base, "model": "coder:7b", "num_ctx": 8192, "skills_disabled": ["backups"]}
A = ag.Agent(os.path.join(DATA, "agent"), lambda: SETTINGS, save=SETTINGS.update, skills_data=DATA, skills_home=HOME)
ws = A.add_workspace(WS, "ws")


def run_until(run, approve=None, timeout=20):
    seen, seq, end = [], 0, time.time() + timeout
    while time.time() < end:
        for ev in run.since(seq, wait=1.0):
            seq = ev["seq"]
            seen.append(ev)
            if ev["type"] == "approval" and approve:
                A.approve(run.id, ev["call_id"], *approve(ev))
            if ev["type"] == "done":
                return seen
    return seen


fake.say("", tool_calls=[("use_skill", {"name": "deploy"}), ("read_skill_file", {"name": "withfiles", "path": "reference.md"}),
                         ("use_skill", {"name": "backups"})])
fake.say("Followed the deploy skill.")
run = A.start(ws, "deploy it")
evs = run_until(run)
sysmsg = fake.requests[-1]["messages"][0]["content"]
tools = {t["function"]["name"] for t in fake.requests[-1]["tools"]}
check("the skills tools are offered", {"use_skill", "read_skill_file", "save_skill"} <= tools, tools)
check("the system prompt lists skills by description", "- deploy: Workspace deploy steps" in sysmsg
      and "- paper-plugin:" in sysmsg, sysmsg[-1500:])
check("but not switched-off ones, or ones only the user may start", "- backups:" not in sysmsg and "- pdf:" not in sysmsg)
res = [e for e in evs if e["type"] == "tool_result"]
check("use_skill hands over the instructions", res[0]["ok"] and "Do the thing." in res[0]["output"], res[0])
check("and says so in the chat", any(e["type"] == "skill" and e["name"] == "deploy" and e["how"] == "assistant" for e in evs))
check("read_skill_file reads a file of the skill", res[1]["ok"] and res[1]["output"] == "ref text", res[1])
check("a switched-off skill is refused", not res[2]["ok"] and "turned off" in res[2]["summary"], res[2])

fake.say("Done what the pdf skill says.")
run = A.start(ws, "/pdf make a report of the server")
evs = run_until(run)
user = fake.requests[-1]["messages"][-1]["content"]
check("/name starts a skill, even one only the user may start", "started the skill 'pdf'" in user and "<skill>" in user
      and "make a report of the server" in user, user[:400])
check("the chat shows who started it", any(e["type"] == "skill" and e["how"] == "you" for e in evs))
fake.say("ok")
run = A.start(ws, "/nosuchskill hello")
run_until(run)
check("/ with no such skill is just a message", fake.requests[-1]["messages"][-1]["content"] == "/nosuchskill hello")

fake.say("", tool_calls=[("save_skill", {"name": "welcome-msg", "description": "Write the server's welcome message",
                                          "instructions": "1. Read the MOTD.\n2. Keep it short."})])
fake.say("Saved.")
run = A.start(ws, "make a skill for welcome messages")
asked = []
evs = run_until(run, approve=lambda ev: (asked.append(ev), ("approve",))[1])
saved = os.path.join(DATA, "skills", "welcome-msg", "SKILL.md")
check("save_skill shows the new SKILL.md for approval", asked and asked[0]["kind"] == "write"
      and "+name: welcome-msg" in asked[0]["diff"], asked)
check("and writes it to your skills", os.path.isfile(saved) and "Keep it short." in open(saved, encoding="utf-8").read())
check("it is usable straight away", sk.find(A.skills(ws), "welcome-msg") is not None)
fake.say("", tool_calls=[("save_skill", {"name": "Bad Name", "description": "x", "instructions": "y"})])
fake.say("ok")
run = A.start(ws, "bad skill")
evs = run_until(run, approve=lambda ev: ("approve",))
r = [e for e in evs if e["type"] == "tool_result"][0]
check("a bad name is refused before anyone is asked", not r["ok"] and "lowercase" in r["summary"], r)

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
