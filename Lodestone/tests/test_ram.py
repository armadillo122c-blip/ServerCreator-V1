"""The memory tools, and mostly the things they refuse to do.

This is the one part of the panel that can close somebody else's application,
so the tests are weighted heavily towards what must never happen: Windows
components, Java processes, the panel itself and any running server have to
survive every route into `close()`, including being asked for by name.

Nothing here closes a real application. The refusal paths are exercised against
live process names; the success path is exercised against a name that is not
running, which returns cleanly without touching anything.
"""

import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

import ramtools as rt

fails = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name +
          (("  " + str(extra)[:300]) if not cond else ""))
    if not cond:
        fails.append(name)


def section(title):
    print("\n-- %s" % title)


WINDOWS = sys.platform.startswith("win")

section("reading memory")
m = rt.memory()
if WINDOWS:
    check("total memory is a real number", m["total_mb"] > 512, m)
    check("free is not more than total", m["free_mb"] <= m["total_mb"], m)
    check("used adds up", m["used_mb"] == m["total_mb"] - m["free_mb"], m)
    check("percent is a percentage", 0 <= m["percent"] <= 100, m)
else:
    check("non-Windows returns zeros rather than raising", m["total_mb"] == 0)

section("the snapshot")
snap = rt.snapshot()
check("it reports whether it is supported", snap["supported"] is WINDOWS)
if WINDOWS:
    rows = snap["processes"]
    check("it found some processes", len(rows) > 3, len(rows))
    check("they are sorted heaviest first",
          all(rows[i]["mb"] >= rows[i + 1]["mb"] for i in range(len(rows) - 1)))
    check("nothing tiny is listed", all(r["mb"] >= 25 for r in rows))
    check("processes are grouped by application",
          all(isinstance(r["pids"], list) and r["pids"] for r in rows))
    check("reclaimable counts only closeable rows",
          snap["reclaimable_mb"] == sum(r["mb"] for r in rows if r["closeable"]),
          snap["reclaimable_mb"])
    check("  and is never more than what is in use",
          snap["reclaimable_mb"] <= snap["used_mb"], (snap["reclaimable_mb"], snap["used_mb"]))

section("what must never be offered")
if WINDOWS:
    by_name = {r["name"].lower(): r for r in snap["processes"]}
    for nm in ("svchost.exe", "explorer.exe", "memory compression",
               "secure system", "system"):
        row = by_name.get(nm)
        if row:
            check("%s is not closeable" % nm, not row["closeable"], row)
            check("  and is labelled a system process", row["category"] == "system",
                  row["category"])
    for nm in ("java.exe", "javaw.exe"):
        row = by_name.get(nm)
        if row:
            check("%s is not closeable" % nm, not row["closeable"], row)
            check("  and is called out as Java", row["category"] == "java",
                  row["category"])

section("classify, directly")
cases = [
    ("svchost.exe", "system", False),
    ("explorer.exe", "system", False),
    ("Secure System", "system", False),
    ("java.exe", "java", False),
    ("javaw.exe", "java", False),
    ("Lodestone.exe", "panel", False),
    ("claude.exe", "panel", False),
    ("Discord.exe", "background", True),
    ("spotify.exe", "background", True),
    ("SomeRandomThing.exe", "other", True),
]
for nm, want_kind, want_close in cases:
    kind, _note, closeable = rt.classify(nm, 4242)
    check("%-22s -> %s" % (nm, want_kind), kind == want_kind, kind)
    check("  closeable=%s" % want_close, closeable is want_close)

check("a pid we own is protected whatever it is called",
      rt.classify("Discord.exe", 999, protected_pids={999})[2] is False)
check("a server pid is protected whatever it is called",
      rt.classify("Discord.exe", 998, server_pids={998})[2] is False)
check("  and is labelled a server",
      rt.classify("Discord.exe", 998, server_pids={998})[0] == "server")

section("close() refuses everything it should")
for nm in ("svchost.exe", "explorer.exe", "System", "Secure System",
           "lsass.exe", "csrss.exe", "winlogon.exe"):
    try:
        rt.close(nm)
        check("close(%s) refused" % nm, False, "it was allowed")
    except RuntimeError as e:
        check("close(%s) refused" % nm, True)
        check("  saying it is part of Windows", "Windows" in str(e), str(e)[:70])

for nm in ("java.exe", "javaw.exe"):
    try:
        rt.close(nm)
        check("close(%s) refused" % nm, False, "it was allowed")
    except RuntimeError as e:
        check("close(%s) refused" % nm, True)
        check("  pointing at the Stop button instead",
              "Stop button" in str(e), str(e)[:80])

for nm in ("Lodestone.exe", "claude.exe"):
    try:
        rt.close(nm)
        check("close(%s) refused" % nm, False, "it was allowed")
    except RuntimeError as e:
        check("close(%s) refused" % nm, True, str(e)[:60])

try:
    rt.close("")
    check("close() with no name is refused", False, "it was allowed")
except RuntimeError:
    check("close() with no name is refused", True)

section("a protected pid protects its whole group")
# Two processes share a name; one of them is a running server. Neither may be
# closed, because close() works by name and would take both.
fake = [("shared.exe", 111, 100000), ("shared.exe", 222, 100000)]
_real = rt._tasklist
try:
    rt._tasklist = lambda: fake
    s = rt.snapshot(server_pids={222}, min_mb=0)
    row = next(r for r in s["processes"] if r["name"] == "shared.exe")
    check("the group is not closeable", not row["closeable"], row)
    check("  and both pids are in it", sorted(row["pids"]) == [111, 222], row["pids"])
    try:
        rt.close("shared.exe", server_pids={222})
        check("close() refuses the whole group", False, "it was allowed")
    except RuntimeError as e:
        check("close() refuses the whole group", True, str(e)[:60])
finally:
    rt._tasklist = _real

section("closing something that is not running")
res = rt.close("definitely-not-running-%d.exe" % os.getpid())
check("it reports nothing to do", res["closed"] == 0, res)
check("  and says so plainly", "not running" in res["msg"], res["msg"])
check("  without forcing anything", res.get("pids") == [], res)

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
