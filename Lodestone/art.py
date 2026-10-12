"""Pixel art for the kinds of server, drawn the way the Lodestone mark is.

Each picture is a 16x16 grid of palette letters, so it stays crisp at any size
and costs nothing to ship: the engine renders it to PNG on request, and both
the desktop app and the browser page load it from /api/art/<name>.png. One
source of truth, and no image library - the PNG encoder is twenty lines, the
same as make_icon.py.

These are Lodestone's own drawings of what each name suggests - a grass block,
a sheet of paper, a purpur block, a tap, a swatch of fabric, a quilt, an anvil -
not the projects' logos.
"""

import struct
import zlib

SIZE = 16


def _tiles(tile, size=SIZE):
    """Repeat a small tile over the whole grid."""
    th, tw = len(tile), len(tile[0])
    return ["".join(tile[y % th][x % tw] for x in range(size)) for y in range(size)]


def _fabric():
    rows = []
    for y in range(SIZE):
        row = ""
        for x in range(SIZE):
            if x in (0, 15) or y in (0, 15):
                c = "."
            elif x in (1, 14) or y in (1, 14):
                c = "E"
            elif (x in (3, 12) and 3 <= y <= 12) or (y in (3, 12) and 3 <= x <= 12):
                c = "S" if (x + y) % 2 == 0 else ("B" if (x // 1 + y) % 2 else "b")
            else:
                c = "B" if (x + y) % 2 else "b"
            row += c
        rows.append(row)
    return rows


def _quilt():
    rows = []
    for y in range(SIZE):
        row = ""
        for x in range(SIZE):
            if x in (0, 15) or y in (0, 15):
                c = "O"
            elif x in (7, 8) or y in (7, 8):
                # the seams, stitched
                c = "S" if (x + y) % 3 == 0 else "s"
            else:
                quad = ("A" if x < 8 else "B") if y < 8 else ("C" if x < 8 else "D")
                # a lighter diagonal across each patch, like quilting lines
                lx, ly = x % 8, y % 8
                c = quad.lower() if lx == ly else quad
            row += c
        rows.append(row)
    return rows


ART = {
    "vanilla": {
        "palette": {"L": "7cc84f", "G": "5aa83c", "g": "3f8a2a", "D": "8b5a2b",
                    "d": "6b4220", "s": "a87445"},
        "rows": [
            "LLGLLGGLGLLGGLGL",
            "GLGGGLGGLGGGLGGG",
            "GGgGGGgGGGgGGgGG",
            "gGDgGgDgGDgGgDgG",
            "DgDDgDDDgDDDgDDg",
            "DDDsDDdDDDsDDDdD",
            "DdDDDDDDsDDdDDDD",
            "DDDdDsDDDDDDDsDD",
            "DsDDDDDdDDdDDDDD",
            "DDDDdDDDDDDDsDDd",
            "DdDsDDDDsDdDDDDD",
            "DDDDDdDDDDDDDdDD",
            "DsDDDDDsDDsDDDDs",
            "DDdDDDDDDDDDdDDD",
            "DDDDsDDdDDsDDDDD",
            "dddddddddddddddd",
        ]},
    "paper": {
        "palette": {"O": "6b7280", "W": "f3f4f6", "L": "93c5fd", "F": "d1d5db"},
        "rows": [
            "................",
            "..OOOOOOOOOO....",
            "..OWWWWWWWWWO...",
            "..OWWWWWWWWWFO..",
            "..OWWWWWWWWWFFO.",
            "..OWLLLLLLLWOOO.",
            "..OWWWWWWWWWWWO.",
            "..OWLLLLLLLLLWO.",
            "..OWWWWWWWWWWWO.",
            "..OWLLLLLLLLLWO.",
            "..OWWWWWWWWWWWO.",
            "..OWLLLLLLLLLWO.",
            "..OWWWWWWWWWWWO.",
            "..OWLLLLLLWWWWO.",
            "..OOOOOOOOOOOOO.",
            "................",
        ]},
    "purpur": {
        "palette": {"H": "d4acd4", "P": "a97aa9", "D": "6f4a6f", "M": "8c628c"},
        "rows": _tiles(["HHHM",
                        "HPPD",
                        "HPPD",
                        "MDDD"])},
    "spigot": {
        "palette": {"H": "f59e0b", "h": "b45309", "M": "9ca3af", "L": "e5e7eb",
                    "D": "4b5563", "W": "60a5fa", "w": "bfdbfe"},
        "rows": [
            "................",
            "................",
            "......HHHHHH....",
            "......hhHHhh....",
            "........MM......",
            ".......MMMMM....",
            "MMMMMMMLLLLLM...",
            "LLLLLLLLLLLLMD..",
            "MMMMMMMMMMMMMD..",
            "DDDDDDDMMMMMD...",
            ".........MLMD...",
            ".........MMMD...",
            ".........DDDD...",
            "..........w.....",
            "..........W.....",
            ".........WWW....",
        ]},
    "fabric": {
        "palette": {"B": "dbcba5", "b": "c9b78f", "S": "6f5f3e", "E": "a8956b"},
        "rows": _fabric()},
    "quilt": {
        "palette": {"O": "3b2a5a", "A": "9a5be0", "a": "b98af0", "B": "3e7be0",
                    "b": "7aa6f0", "C": "e0559a", "c": "f08cbd", "D": "e0a93e",
                    "d": "f0c779", "S": "f5f3ff", "s": "cbbfe6"},
        "rows": _quilt()},
    "forge": {
        "palette": {"L": "a1a1aa", "A": "52525b", "D": "27272a", "*": "fb923c",
                    "o": "fdba74"},
        "rows": [
            "...........o....",
            ".........*......",
            "............*...",
            "...LLLLLLLLLLLL.",
            ".LLAAAAAAAAAAAAD",
            "..AAAAAAAAAAAAAD",
            "....DAAAAAAAAD..",
            "......AAAAAD....",
            "......AAAAAD....",
            ".....AAAAAAAD...",
            "...LLAAAAAAAAAD.",
            "...AAAAAAAAAAAD.",
            "...DDDDDDDDDDDD.",
            "................",
            "................",
            "................",
        ]},
    "neoforge": {
        "palette": {"L": "fdba74", "A": "ea580c", "D": "9a3412", "*": "fde68a",
                    "o": "fef3c7"},
        "rows": [
            "...........o....",
            ".........*......",
            "............*...",
            "...LLLLLLLLLLLL.",
            ".LLAAAAAAAAAAAAD",
            "..AAAAAAAAAAAAAD",
            "....DAAAAAAAAD..",
            "......AAAAAD....",
            "......AAAAAD....",
            ".....AAAAAAAD...",
            "...LLAAAAAAAAAD.",
            "...AAAAAAAAAAAD.",
            "...DDDDDDDDDDDD.",
            "................",
            "................",
            "................",
        ]},
}
NAMES = tuple(ART)


def pixels(name):
    """The art as rows of RGBA tuples, or None for an unknown name."""
    a = ART.get(name)
    if not a:
        return None
    pal = {k: tuple(int(v[i:i + 2], 16) for i in (0, 2, 4)) + (255,)
           for k, v in a["palette"].items()}
    out = []
    for row in a["rows"]:
        out.append([pal.get(ch, (0, 0, 0, 0)) for ch in row])
    return out


def png(name, size=64):
    """The art as a PNG, scaled up by a whole number so every pixel stays square."""
    px = pixels(name)
    if px is None:
        return None
    k = max(1, min(16, int(size) // SIZE))
    big = [[row[x // k] for x in range(SIZE * k)] for row in px for _ in range(k)]
    return _encode(big)


def _encode(px):
    h, w = len(px), len(px[0])
    raw = b"".join(b"\x00" + b"".join(struct.pack("BBBB", *p) for p in row) for row in px)

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))
