#!/usr/bin/env python3
"""Draws the Lodestone icon and writes Lodestone.ico / Lodestone.png.

Pure standard library — no Pillow. The art is a 32x32 pixel grid so it stays
crisp at every size, matching the in-app SVG exactly. Re-run after editing
RECTS if you want to tweak the mark.
"""
import struct, zlib

SIZE = 32
# x, y, w, h, #rrggbb  — a lodestone block seen head-on, compass core lit
RECTS = [
    (2, 2, 28, 28, "2f3140"),    # outer stone
    (4, 4, 24, 24, "4a4d63"),    # lighter face
    (6, 6, 20, 20, "23242f"),    # dark inset
    (4, 4, 24, 2, "6b6f8a"),     # top bevel
    (4, 26, 24, 2, "1b1c25"),    # bottom bevel
    (8, 8, 2, 2, "3a3c4d"),      # corner studs, like the block texture
    (22, 8, 2, 2, "3a3c4d"),
    (8, 22, 2, 2, "3a3c4d"),
    (22, 22, 2, 2, "3a3c4d"),
    (14, 9, 4, 14, "123524"),    # needle glow, vertical
    (9, 14, 14, 4, "123524"),    # needle glow, horizontal
    (15, 10, 2, 12, "22c55e"),   # needle, vertical
    (10, 15, 12, 2, "22c55e"),   # needle, horizontal
    (15, 10, 2, 4, "e8fff2"),    # north tip, lit — this is a compass, not a cross
    (14, 14, 4, 4, "4ade80"),    # hub
    (15, 15, 2, 2, "eafff3"),    # hub highlight
]



def grid():
    px = [[(0, 0, 0, 0)] * SIZE for _ in range(SIZE)]
    for x, y, w, h, col in RECTS:
        r, g, b = int(col[0:2], 16), int(col[2:4], 16), int(col[4:6], 16)
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                if 0 <= xx < SIZE and 0 <= yy < SIZE:
                    px[yy][xx] = (r, g, b, 255)
    return px


def scale(px, size):
    if size >= SIZE:                      # integer nearest-neighbour, stays sharp
        k = size / SIZE
        return [[px[min(SIZE - 1, int(y / k))][min(SIZE - 1, int(x / k))]
                 for x in range(size)] for y in range(size)]
    step = SIZE / size                    # box average, so 16px stays readable
    out = []
    for y in range(size):
        row = []
        for x in range(size):
            acc = [0, 0, 0, 0]
            n = 0
            for yy in range(int(y * step), int((y + 1) * step)):
                for xx in range(int(x * step), int((x + 1) * step)):
                    p = px[yy][xx]
                    for i in range(4):
                        acc[i] += p[i]
                    n += 1
            row.append(tuple(v // max(1, n) for v in acc))
        out.append(row)
    return out


def png_bytes(px):
    size = len(px)
    raw = b"".join(b"\x00" + b"".join(struct.pack("BBBB", *p) for p in row) for row in px)

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9))
            + chunk(b"IEND", b""))


def ico_bytes(pngs):
    head = struct.pack("<HHH", 0, 1, len(pngs))
    entries, blobs, offset = b"", b"", 6 + 16 * len(pngs)
    for size, data in pngs:
        entries += struct.pack("<BBBBHHII", size if size < 256 else 0,
                               size if size < 256 else 0, 0, 0, 1, 32, len(data), offset)
        blobs += data
        offset += len(data)
    return head + entries + blobs


def main():
    base = grid()
    sizes = [16, 24, 32, 48, 64, 128, 256]
    pngs = [(s, png_bytes(scale(base, s))) for s in sizes]
    with open("Lodestone.ico", "wb") as f:
        f.write(ico_bytes(pngs))
    with open("Lodestone.png", "wb") as f:
        f.write(dict(pngs)[256])
    print("wrote Lodestone.ico (%s) and Lodestone.png"
          % ", ".join("%dpx" % s for s in sizes))


if __name__ == "__main__":
    main()
