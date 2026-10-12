"""Instance icons drawn from Minecraft's own textures.

A fake Mojang serves the version manifest, a version file and a client jar,
and answers Range requests the way piston-data does - so the fetch reads only
the jar's index and the stretch with the textures in it, exactly as it will
against the real thing. The PNGs inside are written here in the formats
Mojang ships: palette images at 4 and 8 bits with a transparent index, and
greyscale with a transparent grey level.

Then the drawing: an item is its texture scaled by a whole number, a block is
a cube whose top is tinted where the game tints it, and the engine hands every
server an icon - the one picked, or the one its software suggests.
"""

import importlib.util
import io
import json
import os
import struct
import sys
import tempfile
import threading
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.chdir(ROOT)
sys.path.insert(0, ROOT)
_TESTHOME = os.environ.get("LODESTONE_TEST_HOME")
if not _TESTHOME:
    _TESTHOME = tempfile.mkdtemp(prefix="lodestone_tex_")
    os.environ["LODESTONE_TEST_HOME"] = _TESTHOME
os.environ["HOME"] = _TESTHOME
os.environ["USERPROFILE"] = _TESTHOME
os.environ["HOMEDRIVE"] = os.path.splitdrive(_TESTHOME)[0] or "C:"
os.environ["HOMEPATH"] = os.path.splitdrive(_TESTHOME)[1]

import textures as tx

fails = []


def check(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + (("  " + str(extra)[:300]) if not cond else ""))
    if not cond:
        fails.append(name)


def section(title):
    print("\n-- %s" % title)


# ---------------------------------------------------------------------------
#  PNGs the way Mojang writes them
# ---------------------------------------------------------------------------

def _chunk(tag, data):
    c = tag + data
    return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)


def _png(w, h, depth, ctype, lines, plte=None, trns=None):
    out = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, depth, ctype, 0, 0, 0))
    if plte:
        out += _chunk(b"PLTE", b"".join(bytes(c) for c in plte))
    if trns is not None:
        out += _chunk(b"tRNS", trns)
    return out + _chunk(b"IDAT", zlib.compress(b"".join(lines))) + _chunk(b"IEND", b"")


def palette_png(grid, colours, depth=8):
    """grid: rows of palette indices; index 0 is transparent."""
    h, w = len(grid), len(grid[0])
    lines = []
    for row in grid:
        if depth == 8:
            data = bytes(row)
        else:
            per = 8 // depth
            data = bytearray()
            for i in range(0, w, per):
                b = 0
                for k in range(per):
                    v = row[i + k] if i + k < w else 0
                    b |= v << (8 - depth * (k + 1))
                data.append(b)
            data = bytes(data)
        lines.append(b"\x00" + data)
    return _png(w, h, depth, 3, lines, plte=colours, trns=b"\x00")


def grey_png(grid, key=0):
    """8-bit greyscale with one grey level marked see-through."""
    lines = [b"\x00" + bytes(row) for row in grid]
    return _png(len(grid[0]), len(grid), 8, 0, lines, trns=struct.pack(">H", key))


def rgba_png_filtered(rows):
    """RGBA rows written with every filter type in turn, to exercise the unfiltering."""
    w = len(rows[0])
    prev = bytes(w * 4)
    lines = []
    for y, row in enumerate(rows):
        raw = bytes(c for p in row for c in p)
        ft = y % 5
        out = bytearray()
        for x in range(len(raw)):
            a = raw[x - 4] if x >= 4 else 0
            b = prev[x]
            c = prev[x - 4] if x >= 4 else 0
            if ft == 0:
                v = raw[x]
            elif ft == 1:
                v = raw[x] - a
            elif ft == 2:
                v = raw[x] - b
            elif ft == 3:
                v = raw[x] - ((a + b) >> 1)
            else:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                v = raw[x] - (a if pa <= pb and pa <= pc else b if pb <= pc else c)
            out.append(v & 255)
        lines.append(bytes([ft]) + bytes(out))
        prev = raw
    return _png(w, len(rows), 8, 6, lines)


def solid(idx):
    return [[idx] * 16 for _ in range(16)]


GREY = [(0, 0, 0), (150, 150, 150), (200, 200, 200)]
BROWN = [(0, 0, 0), (120, 80, 50), (140, 100, 60)]
CYAN = [(0, 0, 0), (40, 220, 210), (200, 255, 250)]
diamond_grid = [[(1 if 4 <= x <= 11 and 4 <= y <= 11 else 0) for x in range(16)] for y in range(16)]
diamond_grid[5][5] = 2

TEX = {
    "block/grass_block_top.png": palette_png(solid(1), GREY, depth=4),
    "block/grass_block_side.png": palette_png(solid(1), BROWN),
    "block/oak_log.png": palette_png(solid(1), BROWN),
    "block/oak_log_top.png": palette_png(solid(2), BROWN),
    "block/piston_top.png": palette_png(solid(2), GREY),
    "block/piston_side.png": palette_png(solid(1), GREY),
    "block/lodestone_top.png": palette_png(solid(2), GREY),
    "block/lodestone_side.png": palette_png(solid(1), GREY),
    "block/daylight_detector_top.png": palette_png(solid(2), GREY),
    "block/daylight_detector_side.png": palette_png(solid(1), GREY),
    "block/stone.png": palette_png(solid(1), GREY),
    "block/torch.png": palette_png(diamond_grid, BROWN),
    "item/diamond.png": palette_png(diamond_grid, CYAN, depth=4),
    "item/compass_16.png": palette_png(diamond_grid, GREY),
    "item/compass_17.png": palette_png(diamond_grid, GREY),
    "item/paper.png": palette_png(diamond_grid, GREY),
    "item/leather_helmet.png": grey_png([[(180 if 3 <= x <= 12 and y < 8 else 0) for x in range(16)] for y in range(16)]),
    "item/potion.png": palette_png(diamond_grid, GREY),
    "item/potion_overlay.png": palette_png(diamond_grid, GREY),
    "item/bow_pulling_0.png": palette_png(diamond_grid, BROWN),
    "item/creeper_spawn_egg.png": palette_png(diamond_grid, CYAN),
}


def client_jar():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # a jar is mostly classes; the textures sit in one stretch of it
        for i in range(60):
            z.writestr("net/minecraft/C%03d.class" % i, os.urandom(3000))
        for name, data in TEX.items():
            z.writestr("assets/minecraft/textures/" + name, data)
        z.writestr("assets/minecraft/textures/entity/pig.png", TEX["block/stone.png"])
        z.writestr("assets/minecraft/lang/en_us.json", "{}")
        for i in range(40):
            z.writestr("data/minecraft/R%03d.json" % i, os.urandom(2000))
    return buf.getvalue()


JAR = client_jar()
HITS = {"range": 0, "full": 0}
HONOUR_RANGE = [True]


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        if self.path == "/manifest.json":
            body = json.dumps({"latest": {"release": "26.3", "snapshot": "26.4-snapshot-1"},
                               "versions": [{"id": "26.3", "url": base + "/26.3.json"}]}).encode()
        elif self.path == "/26.3.json":
            body = json.dumps({"downloads": {"client": {"url": base + "/client.jar", "size": len(JAR),
                                                        "sha1": "x"}}}).encode()
        elif self.path == "/client.jar":
            rng = self.headers.get("Range")
            if rng and HONOUR_RANGE[0]:
                a, b = rng.split("=", 1)[1].split("-")
                a, b = int(a), min(int(b), len(JAR) - 1)
                body = JAR[a:b + 1]
                HITS["range"] += 1
                self.send_response(206)
                self.send_header("Content-Range", "bytes %d-%d/%d" % (a, b, len(JAR)))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            HITS["full"] += 1
            body = JAR
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
threading.Thread(target=srv.serve_forever, daemon=True).start()
MANIFEST = "http://127.0.0.1:%d/manifest.json" % srv.server_address[1]


# ---------------------------------------------------------------------------
section("reading the PNGs Mojang ships")
w, h, rows = tx.decode_png(TEX["item/diamond.png"])
check("a 4-bit palette image decodes", (w, h) == (16, 16) and rows[4][4] == (40, 220, 210, 255), rows[4][4])
check("its transparent index is see-through", rows[0][0][3] == 0)
check("and the other colours are kept", rows[5][5] == (200, 255, 250, 255))
w, h, rows = tx.decode_png(TEX["item/leather_helmet.png"])
check("greyscale with a transparent grey level", rows[0][0][3] == 0 and rows[0][3] == (180, 180, 180, 255), rows[0][:4])
pic = [[((x * 16) % 256, (y * 16) % 256, (x * y) % 256, 255 if (x + y) % 3 else 128) for x in range(16)] for y in range(16)]
w, h, rows = tx.decode_png(rgba_png_filtered(pic))
check("every row filter undoes exactly (None, Sub, Up, Average, Paeth)", rows == pic)
w, h, rows = tx.decode_png(tx.encode_png(pic))
check("what Lodestone writes reads back the same", rows == pic)

section("fetching only what is needed")
root = os.path.join(_TESTHOME, "tex")
check("nothing is there before the first fetch", not tx.ready(root) and tx.catalog(root) == [])
info = tx.fetch(root, manifest_url=MANIFEST)
check("the newest release is the one read", info["version"] == "26.3", info)
check("every block and item texture is kept", info["count"] == len(TEX), info)
names = zipfile.ZipFile(tx.pack_path(root)).namelist()
check("and nothing else - no entity textures, no classes", all(n.startswith(("block/", "item/")) for n in names)
      and "block/stone.png" in names, names[:5])
check("the jar was read in ranges, never whole", HITS["range"] > 0 and HITS["full"] == 0, HITS)
check("in a handful of requests", info["requests"] <= 6, info["requests"])
check("the pack is ready", tx.ready(root) and tx.meta(root)["version"] == "26.3")
root2 = os.path.join(_TESTHOME, "tex-norange")
HONOUR_RANGE[0] = False
info2 = tx.fetch(root2, manifest_url=MANIFEST)
HONOUR_RANGE[0] = True
check("a server that ignores Range still works", info2["count"] == len(TEX))
try:
    tx.fetch(os.path.join(_TESTHOME, "tex-bad"), manifest_url="http://127.0.0.1:%d/nope.json" % srv.server_address[1])
    check("an unreachable manifest is a readable error", False, "no error")
except tx.TextureError as e:
    check("an unreachable manifest is a readable error", "Mojang" in str(e), e)

section("what can be an icon")
cat = {x["key"]: x for x in tx.catalog(root)}
check("blocks with faces of their own", "block/grass_block" in cat and "block/piston" in cat and "block/lodestone" in cat)
check("logs find their top", "block/oak_log" in cat)
check("plain cubes by name", "block/stone" in cat)
check("items", "item/diamond" in cat and "item/paper" in cat)
check("one compass, not a frame of its animation", "item/compass" in cat and "item/compass_16" not in cat
      and "item/compass_17" not in cat)
check("potions, but not their overlay", "item/potion" in cat and "item/potion_overlay" not in cat)
check("no half-drawn bows", "item/bow_pulling_0" not in cat)
check("block-textured items like the torch", "item/torch" in cat)
check("groups", cat["block/piston"]["group"] == "Redstone & tech" and cat["item/creeper_spawn_egg"]["group"] == "Mobs"
      and cat["item/diamond"]["group"] == "Ores & minerals", cat["item/diamond"])
check("readable names", cat["block/grass_block"]["label"] == "Grass Block")
check("a block whose textures are missing is not offered", "block/observer" not in cat)

section("drawing")
k = 64 // 16
_w, _h, px = tx.decode_png(tx.png(root, "item/diamond", 64))
check("an item is its texture scaled by a whole number", (_w, _h) == (64, 64) and px[5 * k][5 * k] == (200, 255, 250, 255)
      and px[5 * k + 3][5 * k + 3] == (200, 255, 250, 255), px[5 * k][5 * k])
check("with hard edges", px[4 * k][4 * k - 1][3] == 0 and px[4 * k][4 * k] == (40, 220, 210, 255))
_w, _h, px = tx.decode_png(tx.png(root, "item/leather_helmet", 64))
check("leather is dyed its default brown", px[0][3 * k][0] > px[0][3 * k][2], px[0][3 * k])
_w, _h, px = tx.decode_png(tx.png(root, "block/grass_block", 96))
check("a block is a cube with see-through corners", px[0][0][3] == 0 and px[95][0][3] == 0)
top = px[20][48]
check("its top is tinted grass green", top[3] == 255 and top[1] > top[0] and top[1] > top[2], top)
left, right = px[70][25], px[70][71]
check("its sides are shaded, the right darker than the left", left[3] == 255 and right[3] == 255
      and sum(right[:3]) < sum(left[:3]), (left, right))
_w, _h, slab = tx.decode_png(tx.png(root, "block/daylight_detector", 96))
first = next(y for y in range(96) if slab[y][48][3])
check("a short block sits low, at its own height", first > 30, first)
check("unknown icons are not drawn", tx.png(root, "block/not_a_block", 64) is None and tx.png(root, "nonsense", 64) is None)
sheet = tx.sheet(root, ["item/diamond", "block/stone", "item/paper"], 32, 2)
_w, _h, _px = tx.decode_png(sheet)
check("a sheet lays icons out in rows", (_w, _h) == (64, 64))

section("which icon a server gets")
check("Vanilla gets a grass block", tx.auto_icon("vanilla") == "block/grass_block")
check("Paper gets paper", tx.auto_icon("paper") == "item/paper")
check("Forge a smithing table, NeoForge a blast furnace",
      tx.auto_icon("forge") == "block/smithing_table" and tx.auto_icon("neoforge") == "block/blast_furnace")
check("an unknown local server gets a lodestone", tx.auto_icon("", "local") == "block/lodestone")
check("a remote one a compass", tx.auto_icon("", "sftp") == "item/compass")
check("a picked icon wins", tx.resolve("item/diamond", "paper") == "item/diamond")
check("an old mascot name falls back to automatic", tx.resolve("golem", "paper") == "item/paper")

section("the engine")
spec = importlib.util.spec_from_file_location("lodestone", "lodestone.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.PANEL.save = lambda *a, **k: True
tx.fetch(str(m.TEX_ROOT), manifest_url=MANIFEST)
icons = m.PANEL.art_icons()
check("the catalogue is offered, grouped", icons["ready"] and icons["groups"] and
      any(i["key"] == "block/piston" for i in icons["items"]), icons.get("error"))
s = m.PANEL.server()
s["local"]["server_type"], s["local"]["mc_version"] = "paper", "1.21.4"
row = next(x for x in m.PANEL.listing() if x["id"] == s["id"])
check("a server without a pick shows its software's icon", row["icon"] == "item/paper" and row["icon_auto"] == "item/paper", row)
m.PANEL.set_look({"id": s["id"], "look": {"icon": "item/diamond"}})
row = next(x for x in m.PANEL.listing() if x["id"] == s["id"])
check("a picked item is kept", row["icon"] == "item/diamond" and row["icon_auto"] == "item/paper", row)
check("and the status names it, for the header", m.PANEL.status()["server"]["icon"] == "item/diamond")
m.PANEL.set_look({"id": s["id"], "look": {"icon": "block/made_up"}})
check("an icon the textures cannot draw is refused", s["look"]["icon"] == "item/diamond")
m.PANEL.set_look({"id": s["id"], "look": {"icon": "../../etc"}})
check("as is anything that is not an icon name", s["look"]["icon"] == "item/diamond")
m.PANEL.set_look({"id": s["id"], "look": {"icon": ""}})
check("blank goes back to automatic", s["look"]["icon"] == "" and m.PANEL.icon_of(s) == "item/paper")

srv.shutdown()
print("\n%d failure(s)" % len(fails))
sys.exit(1 if fails else 0)
