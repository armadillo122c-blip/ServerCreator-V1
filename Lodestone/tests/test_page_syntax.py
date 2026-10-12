"""The whole UI is one big JavaScript string inside a Python file, so a stray
quote is a Python-valid file that serves a dead page. This parses the JS and
CSS that actually get sent to the browser.

Uses node when it is on PATH for a real parse; falls back to structural checks
so the suite still means something without it.
"""
import importlib.util
import os, sys, tempfile
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
_TESTHOME = os.environ.get("LODESTONE_TEST_HOME")
if not _TESTHOME:
    _TESTHOME = tempfile.mkdtemp(prefix="lodestone_testhome_")
    os.environ["LODESTONE_TEST_HOME"] = _TESTHOME
os.environ["HOME"] = _TESTHOME
os.environ["USERPROFILE"] = _TESTHOME
os.environ["HOMEDRIVE"] = os.path.splitdrive(_TESTHOME)[0] or "C:"
os.environ["HOMEPATH"] = os.path.splitdrive(_TESTHOME)[1]

import io
import re
import shutil
import subprocess

spec = importlib.util.spec_from_file_location("lodestone", "lodestone.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

fails = []


def check(n, c, e=""):
    print(("  ok   " if c else "  FAIL ") + n + (("  " + str(e)[:400]) if not c else ""))
    if not c:
        fails.append(n)


page = m.PAGE
check("the page is assembled", len(page) > 50_000, len(page))
for token in ("__CSS__", "__ICON__", "__VER__", "__EVENTS__"):
    check("no unreplaced %s" % token, token not in page)

js = page.split("<script>", 1)[1].rsplit("</script>", 1)[0]
css = page.split("<style>", 1)[1].split("</style>", 1)[0]
check("the script block was found", len(js) > 20_000, len(js))
check("the style block was found", len(css) > 5_000, len(css))

node = shutil.which("node")
if node:
    tmp = os.path.join(tempfile.mkdtemp(), "page.js")
    io.open(tmp, "w", encoding="utf-8", newline="\n").write(js)
    r = subprocess.run([node, "--check", tmp], capture_output=True, text=True)
    check("the page JavaScript parses", r.returncode == 0,
          (r.stderr or r.stdout).strip()[:600])
else:
    print("  note   node is not on PATH — using structural checks only")
    check("quotes look balanced per line",
          all(l.count('"') % 2 == 0 or "'" in l for l in js.splitlines()), "")

# Counting brackets is only a fallback: it cannot tell a real paren from one
# inside a string literal, so it is skipped when node has already parsed the file.
if not node:
    check("braces balance in the JS", js.count("{") == js.count("}"),
          (js.count("{"), js.count("}")))
check("braces balance in the CSS", css.count("{") == css.count("}"),
      (css.count("{"), css.count("}")))

# every onclick="fn(...)" in the markup should name a function the script defines
defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", js))
defined |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(", js))
defined |= {"act", "api", "showView", "toast", "esc"}
BUILTIN = {"this", "if", "return", "event", "parseInt", "parseFloat", "encodeURIComponent",
           "decodeURIComponent", "JSON", "Math", "Number", "String", "Array", "Object",
           "setTimeout", "alert", "confirm", "fetch"}
called = set()
for handler in re.findall(r'on(?:click|change|input|keydown)="([^"]+)"', page):
    # (?<![.$\w]) so obj.method(...) and this.x(...) are not read as bare calls
    for name in re.findall(r"(?<![.$\w])([A-Za-z_$][\w$]*)\s*\(", handler):
        if name not in BUILTIN:
            called.add(name)
missing = sorted(n for n in called if n not in defined)
check("every inline handler names a function that exists", not missing, missing)

# and every view in the rail has a matching section
views = set(re.findall(r'data-view="([a-z]+)"', page))
sections = set(re.findall(r'id="view-([a-z]+)"', page))
check("every rail entry has a view", views <= sections, sorted(views - sections))
check("every view has a rail entry", sections <= views, sorted(sections - views))
print("  note   %d views: %s" % (len(views), ", ".join(sorted(views))))

print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
