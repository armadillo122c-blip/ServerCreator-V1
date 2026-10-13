"""Every module the engine imports from its own folder must ship in both
builds: the engine copy beside the desktop app (desktop/build.py) and the
PyInstaller exe (build_app.py). The imports are optional, so a module left
out does not fail the build - the feature just quietly disappears."""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)

fails = []


def check(n, c, e=""):
    print(("  ok   " if c else "  FAIL ") + n + (("  " + str(e)[:300]) if not c else ""))
    if not c:
        fails.append(n)


src = open("lodestone.py", encoding="utf-8").read()
local = sorted({m for m in re.findall(r"(?m)^\s*import\s+(\w+)", src) if os.path.isfile(m + ".py")})
check("the engine imports its own modules", "agent" in local and "creator" in local, local)

build = open(os.path.join("desktop", "build.py"), encoding="utf-8").read()
files = re.search(r"ENGINE_FILES\s*=\s*\[(.*?)\]", build, re.S).group(1)
for m in local:
    check("desktop/build.py copies %s.py beside the app" % m, '"%s.py"' % m in files)

exe = open("build_app.py", encoding="utf-8").read()
for m in local:
    named = ('"--hidden-import", "%s"' % m) in exe or ('"%s"' % m) in exe
    check("build_app.py bundles %s into the exe" % m, named)

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
