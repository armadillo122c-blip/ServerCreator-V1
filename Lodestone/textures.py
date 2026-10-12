"""Minecraft's own item and block pictures, for instance icons.

Lodestone does not ship Mojang's textures - they are Mojang's, and they change
with every release. Instead, the first time they are wanted, it reads them out
of the official game download, the client jar on piston-data.mojang.com, the
same file every launcher fetches. Not the whole 40 MB: a jar is a zip, a zip
keeps its index at the end, and Mojang's CDN answers HTTP range requests, so
Lodestone reads the index, then the one stretch of the file where the block and
item textures sit - about half a megabyte in four requests - and keeps those
PNGs in a small zip of its own under ~/.lodestone/textures.

From there every icon is drawn here, standard library only:

* **Items** are flat sprites, scaled up by a whole number so every pixel stays
  square - exactly how the game draws them in an inventory.
* **Blocks** are cubes in the inventory's three-quarter view: the top face and
  two sides, each a texture mapped onto a parallelogram and shaded like the
  game shades them. Slabs and other short blocks (a daylight detector, a sculk
  sensor) keep their height.

The PNG decoder handles what Mojang actually ships - mostly palette images at
1, 2, 4 or 8 bits, with some greyscale and RGBA - and nothing more.
"""

import io
import json
import os
import re
import struct
import threading
import time
import urllib.error
import urllib.request
import zipfile
import zlib

MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
UA = "Lodestone/1.0 (Minecraft server panel; instance icons)"
PREFIX = "assets/minecraft/textures/"
TIMEOUT = 30
KEY_RE = re.compile(r"^(block|item)/[a-z0-9_]{1,64}$")


class TextureError(Exception):
    pass


# ---------------------------------------------------------------------------
#  PNG in and out
# ---------------------------------------------------------------------------

def decode_png(data):
    """(width, height, rows of (r, g, b, a)) for the PNGs Minecraft ships."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise TextureError("not a PNG")
    pos, idat, plte, trns = 8, [], None, None
    w = h = depth = ctype = 0
    while pos + 8 <= len(data):
        n, tag = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        pos += 12 + n
        if tag == b"IHDR":
            w, h, depth, ctype, _c, _f, lace = struct.unpack(">IIBBBBB", body)
            if lace:
                raise TextureError("interlaced PNGs are not handled")
            if depth == 16:
                raise TextureError("16-bit PNGs are not handled")
        elif tag == b"PLTE":
            plte = [tuple(body[i:i + 3]) for i in range(0, len(body) - 2, 3)]
        elif tag == b"tRNS":
            trns = body
        elif tag == b"IDAT":
            idat.append(body)
        elif tag == b"IEND":
            break
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if channels is None or not w or not h:
        raise TextureError("unsupported PNG")
    key_grey = struct.unpack(">H", trns[:2])[0] if ctype == 0 and trns and len(trns) >= 2 else None
    key_rgb = tuple(struct.unpack(">HHH", trns[:6])) if ctype == 2 and trns and len(trns) >= 6 else None
    raw = zlib.decompress(b"".join(idat))
    bpp = max(1, channels * depth // 8)          # bytes per pixel, for the filters
    stride = (w * channels * depth + 7) // 8
    prev = bytearray(stride)
    rows, i = [], 0
    for _y in range(h):
        ft = raw[i]
        line = bytearray(raw[i + 1:i + 1 + stride])
        i += 1 + stride
        for x in range(stride):
            a = line[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if ft == 1:
                line[x] = (line[x] + a) & 255
            elif ft == 2:
                line[x] = (line[x] + b) & 255
            elif ft == 3:
                line[x] = (line[x] + ((a + b) >> 1)) & 255
            elif ft == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[x] = (line[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        prev = line
        # unpack samples
        if depth == 8:
            samples = list(line)
        else:
            per = 8 // depth
            mask = (1 << depth) - 1
            samples = []
            for byte in line:
                for k in range(per):
                    samples.append((byte >> (8 - depth * (k + 1))) & mask)
        row = []
        for x in range(w):
            if ctype == 3:
                idx = samples[x]
                r, g, bb = plte[idx] if plte and idx < len(plte) else (0, 0, 0)
                al = trns[idx] if trns and idx < len(trns) else 255
                row.append((r, g, bb, al))
            elif ctype == 0:
                v = samples[x] * 255 // ((1 << depth) - 1)
                # greyscale marks one grey level as see-through
                row.append((v, v, v, 0 if key_grey is not None and samples[x] == key_grey else 255))
            elif ctype == 4:
                row.append((samples[2 * x], samples[2 * x], samples[2 * x], samples[2 * x + 1]))
            elif ctype == 2:
                rgb = tuple(samples[3 * x:3 * x + 3])
                row.append(rgb + (0 if rgb == key_rgb else 255,))
            else:
                row.append(tuple(samples[4 * x:4 * x + 4]))
        rows.append(row)
    return w, h, rows


def encode_png(rows):
    """RGBA rows to a PNG."""
    h, w = len(rows), len(rows[0])
    raw = b"".join(b"\x00" + bytes(c for p in row for c in p) for row in rows)

    def chunk(tag, data):
        c = tag + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


# ---------------------------------------------------------------------------
#  fetching: a few ranges of the official client jar
# ---------------------------------------------------------------------------

def _get(url, headers=None):
    req = urllib.request.Request(url, headers=dict({"User-Agent": UA}, **(headers or {})))
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.status, r.read()


class _RangeFile(io.RawIOBase):
    """A remote file as a seekable stream, read with HTTP Range requests."""

    def __init__(self, url, size):
        self.url, self.size, self.pos, self.requests = url, size, 0, 0

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.size + off
        return self.pos

    def readinto(self, b):
        n = min(len(b), self.size - self.pos)
        if n <= 0:
            return 0
        status, data = _get(self.url, {"Range": "bytes=%d-%d" % (self.pos, self.pos + n - 1)})
        self.requests += 1
        if status != 206:
            # A server that ignores Range sent the whole file: take our slice of it.
            data = data[self.pos:self.pos + n]
        b[:len(data)] = data
        self.pos += len(data)
        return len(data)


def latest_client(manifest_url=MANIFEST, version=None):
    """(version id, client jar url, size) for the newest release, or the one named."""
    try:
        _s, body = _get(manifest_url)
        man = json.loads(body)
        want = version or man["latest"]["release"]
        entry = next(v for v in man["versions"] if v["id"] == want)
        _s, body = _get(entry["url"])
        client = json.loads(body)["downloads"]["client"]
        return want, client["url"], int(client["size"])
    except (urllib.error.URLError, OSError, ValueError, KeyError, StopIteration) as e:
        raise TextureError("Could not ask Mojang for the game files: %s" % e)


def pack_path(root):
    return os.path.join(root, "textures.zip")


def meta(root):
    try:
        with open(os.path.join(root, "meta.json"), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def fetch(root, log=None, manifest_url=MANIFEST, version=None):
    """Read the block and item textures out of the client jar into root/textures.zip."""
    log = log or (lambda m: None)
    os.makedirs(root, exist_ok=True)
    ver, url, size = latest_client(manifest_url, version)
    log("reading the texture list from Minecraft %s" % ver)
    started = time.time()
    raw = _RangeFile(url, size)
    try:
        jar = zipfile.ZipFile(io.BufferedReader(raw, buffer_size=4 * 1024 * 1024))
        wanted = [i for i in jar.infolist()
                  if i.filename.endswith(".png") and (i.filename.startswith(PREFIX + "block/")
                                                      or i.filename.startswith(PREFIX + "item/"))]
        if not wanted:
            raise TextureError("Minecraft %s has no textures where Lodestone expected them." % ver)
        # In file order, so the buffered reader walks the stretch front to back.
        wanted.sort(key=lambda i: i.header_offset)
        tmp = pack_path(root) + ".part"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED) as out:
            for info in wanted:
                out.writestr(info.filename[len(PREFIX):], jar.read(info))   # CRC-checked by zipfile
    except (zipfile.BadZipFile, urllib.error.URLError, OSError) as e:
        raise TextureError("Could not read the textures: %s" % e)
    os.replace(tmp, pack_path(root))
    info = {"version": ver, "count": len(wanted), "fetched": int(time.time()),
            "requests": raw.requests, "seconds": round(time.time() - started, 1)}
    with open(os.path.join(root, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(info, fh)
    _PACKS.pop(root, None)
    log("kept %d textures from Minecraft %s (%d requests, %.1fs)"
        % (len(wanted), ver, raw.requests, time.time() - started))
    return info


# ---------------------------------------------------------------------------
#  the pack on disk
# ---------------------------------------------------------------------------

_PACKS = {}
_LOCK = threading.Lock()


class Pack:
    def __init__(self, path):
        self.zip = zipfile.ZipFile(path)
        self.names = set(self.zip.namelist())
        self._tex = {}
        self._lock = threading.Lock()

    def has(self, rel):
        return rel + ".png" in self.names

    def texture(self, rel):
        """A texture's first frame as (size, rows); None when missing."""
        with self._lock:
            if rel in self._tex:
                return self._tex[rel]
            t = None
            if rel + ".png" in self.names:
                try:
                    w, h, rows = decode_png(self.zip.read(rel + ".png"))
                    # animated textures are a vertical strip of square frames
                    t = (w, rows[:w]) if h > w else (w, rows)
                except (TextureError, zlib.error, KeyError, IndexError, struct.error):
                    t = None
            self._tex[rel] = t
            return t


def pack(root):
    path = pack_path(root)
    if not os.path.isfile(path):
        return None
    with _LOCK:
        p = _PACKS.get(root)
        if p is None:
            try:
                p = _PACKS[root] = Pack(path)
            except (OSError, zipfile.BadZipFile):
                return None
        return p


def ready(root):
    return pack(root) is not None


# ---------------------------------------------------------------------------
#  what can be an icon
# ---------------------------------------------------------------------------

def _hex(c):
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


GRASS = _hex("91bd59")      # plains grass
FOLIAGE = _hex("59ae30")
LEATHER = _hex("a06540")

# Blocks whose faces differ, or that are not full height. (top, front, side, height)
# "front" is the face on the left, "side" the one on the right.
CUBES = {
    # redstone & tech
    "piston": ("Redstone & tech", "piston_top", "piston_side", "piston_side", 1),
    "sticky_piston": ("Redstone & tech", "piston_top_sticky", "piston_side", "piston_side", 1),
    "observer": ("Redstone & tech", "observer_top", "observer_front", "observer_side", 1),
    "dispenser": ("Redstone & tech", "furnace_top", "dispenser_front", "furnace_side", 1),
    "dropper": ("Redstone & tech", "furnace_top", "dropper_front", "furnace_side", 1),
    "crafter": ("Redstone & tech", "crafter_top", "crafter_north", "crafter_east", 1),
    "target": ("Redstone & tech", "target_top", "target_side", "target_side", 1),
    "tnt": ("Redstone & tech", "tnt_top", "tnt_side", "tnt_side", 1),
    "redstone_lamp": ("Redstone & tech", "redstone_lamp_on", "redstone_lamp_on", "redstone_lamp_on", 1),
    "redstone_block": ("Redstone & tech", "redstone_block", "redstone_block", "redstone_block", 1),
    "note_block": ("Redstone & tech", "note_block", "note_block", "note_block", 1),
    "jukebox": ("Redstone & tech", "jukebox_top", "jukebox_side", "jukebox_side", 1),
    "daylight_detector": ("Redstone & tech", "daylight_detector_top", "daylight_detector_side",
                          "daylight_detector_side", 6 / 16),
    "sculk_sensor": ("Redstone & tech", "sculk_sensor_top", "sculk_sensor_side", "sculk_sensor_side", 8 / 16),
    "copper_bulb": ("Redstone & tech", "copper_bulb_lit", "copper_bulb_lit", "copper_bulb_lit", 1),
    "oxidized_copper_bulb": ("Redstone & tech", "oxidized_copper_bulb_lit", "oxidized_copper_bulb_lit",
                             "oxidized_copper_bulb_lit", 1),
    "command_block": ("Redstone & tech", "command_block_side", "command_block_front", "command_block_side", 1),
    "chain_command_block": ("Redstone & tech", "chain_command_block_side", "chain_command_block_front",
                            "chain_command_block_side", 1),
    "repeating_command_block": ("Redstone & tech", "repeating_command_block_side",
                                "repeating_command_block_front", "repeating_command_block_side", 1),
    "structure_block": ("Redstone & tech", "structure_block", "structure_block", "structure_block", 1),
    "jigsaw": ("Redstone & tech", "jigsaw_top", "jigsaw_side", "jigsaw_side", 1),
    "lodestone": ("Redstone & tech", "lodestone_top", "lodestone_side", "lodestone_side", 1),
    "respawn_anchor": ("Redstone & tech", "respawn_anchor_top", "respawn_anchor_side4", "respawn_anchor_side4", 1),
    "spawner": ("Redstone & tech", "spawner", "spawner", "spawner", 1),
    "trial_spawner": ("Redstone & tech", "trial_spawner_top_active", "trial_spawner_side_active",
                      "trial_spawner_side_active", 1),
    "vault": ("Redstone & tech", "vault_top", "vault_front_on", "vault_side_on", 1),
    "slime_block": ("Redstone & tech", "slime_block", "slime_block", "slime_block", 1),
    "honey_block": ("Redstone & tech", "honey_block_top", "honey_block_side", "honey_block_side", 1),
    # workstations
    "crafting_table": ("Workstations", "crafting_table_top", "crafting_table_front", "crafting_table_side", 1),
    "furnace": ("Workstations", "furnace_top", "furnace_front_on", "furnace_side", 1),
    "smoker": ("Workstations", "smoker_top", "smoker_front_on", "smoker_side", 1),
    "blast_furnace": ("Workstations", "blast_furnace_top", "blast_furnace_front_on", "blast_furnace_side", 1),
    "loom": ("Workstations", "loom_top", "loom_front", "loom_side", 1),
    "smithing_table": ("Workstations", "smithing_table_top", "smithing_table_front", "smithing_table_side", 1),
    "fletching_table": ("Workstations", "fletching_table_top", "fletching_table_front", "fletching_table_side", 1),
    "cartography_table": ("Workstations", "cartography_table_top", "cartography_table_side3",
                          "cartography_table_side1", 1),
    "enchanting_table": ("Workstations", "enchanting_table_top", "enchanting_table_side",
                         "enchanting_table_side", 12 / 16),
    "bookshelf": ("Workstations", "oak_planks", "bookshelf", "bookshelf", 1),
    "chiseled_bookshelf": ("Workstations", "chiseled_bookshelf_top", "chiseled_bookshelf_occupied",
                           "chiseled_bookshelf_side", 1),
    "barrel": ("Workstations", "barrel_top", "barrel_side", "barrel_side", 1),
    "beehive": ("Workstations", "beehive_end", "beehive_front_honey", "beehive_side", 1),
    "bee_nest": ("Workstations", "bee_nest_top", "bee_nest_front_honey", "bee_nest_side", 1),
    "stonecutter": ("Workstations", "stonecutter_top", "stonecutter_side", "stonecutter_side", 9 / 16),
    # blocks
    "grass_block": ("Blocks", "grass_block_top", "grass_block_side", "grass_block_side", 1),
    "snowy_grass_block": ("Blocks", "snow", "grass_block_snow", "grass_block_snow", 1),
    "dirt_path": ("Blocks", "dirt_path_top", "dirt_path_side", "dirt_path_side", 15 / 16),
    "pumpkin": ("Blocks", "pumpkin_top", "pumpkin_side", "pumpkin_side", 1),
    "carved_pumpkin": ("Blocks", "pumpkin_top", "carved_pumpkin", "pumpkin_side", 1),
    "jack_o_lantern": ("Blocks", "pumpkin_top", "jack_o_lantern", "pumpkin_side", 1),
    "melon": ("Blocks", "melon_top", "melon_side", "melon_side", 1),
    "cake": ("Food", "cake_top", "cake_side", "cake_side", 8 / 16),
    "sandstone": ("Blocks", "sandstone_top", "sandstone", "sandstone", 1),
    "red_sandstone": ("Blocks", "red_sandstone_top", "red_sandstone", "red_sandstone", 1),
    "magma_block": ("Blocks", "magma", "magma", "magma", 1),
    "end_portal_frame": ("Blocks", "end_portal_frame_top", "end_portal_frame_side", "end_portal_frame_side", 13 / 16),
    "sculk_shrieker": ("Blocks", "sculk_shrieker_top", "sculk_shrieker_side", "sculk_shrieker_side", 8 / 16),
}

# Blocks drawn as flat sprites, the way their item is.
FLAT_BLOCKS = {
    "redstone_torch": ("Redstone & tech", "redstone_torch"),
    "lever": ("Redstone & tech", "lever"),
    "rail": ("Redstone & tech", "rail"),
    "powered_rail": ("Redstone & tech", "powered_rail_on"),
    "detector_rail": ("Redstone & tech", "detector_rail"),
    "activator_rail": ("Redstone & tech", "activator_rail"),
    "tripwire_hook": ("Redstone & tech", "tripwire_hook"),
    "torch": ("Blocks", "torch"),
    "soul_torch": ("Blocks", "soul_torch"),
    "ladder": ("Blocks", "ladder"),
    "cobweb": ("Blocks", "cobweb"),
    "poppy": ("Blocks", "poppy"),
    "dandelion": ("Blocks", "dandelion"),
    "cornflower": ("Blocks", "cornflower"),
    "sunflower": ("Blocks", "sunflower_front"),
    "red_mushroom": ("Blocks", "red_mushroom"),
    "oak_sapling": ("Blocks", "oak_sapling"),
    "cherry_sapling": ("Blocks", "cherry_sapling"),
}

# Single-texture cubes found by name.
_CUBE_SUFFIX = ("_ore", "_wool", "_concrete", "_concrete_powder", "_terracotta", "_planks", "_bricks",
                "_block", "_leaves", "_stained_glass", "_copper")
_CUBE_NAMES = {"stone", "cobblestone", "mossy_cobblestone", "dirt", "coarse_dirt", "rooted_dirt", "mud",
               "clay", "gravel", "sand", "red_sand", "glass", "ice", "packed_ice", "blue_ice", "obsidian",
               "crying_obsidian", "netherrack", "soul_sand", "soul_soil", "glowstone", "sea_lantern",
               "shroomlight", "sponge", "wet_sponge", "end_stone", "calcite", "tuff", "bedrock",
               "prismarine", "dark_prismarine", "terracotta", "bricks", "deepslate", "cobbled_deepslate",
               "smooth_stone", "andesite", "diorite", "granite", "polished_andesite", "polished_diorite",
               "polished_granite", "blackstone", "gilded_blackstone", "snow", "copper_block", "moss_block",
               "pale_moss_block", "budding_amethyst", "packed_mud", "dripstone_block", "chiseled_stone_bricks",
               "chiseled_deepslate", "reinforced_deepslate", "chiseled_tuff", "chiseled_copper", "honeycomb_block",
               "ochre_froglight", "pearlescent_froglight", "verdant_froglight", "resin_block", "creaking_heart"}
_NOT_CUBES = {"redstone_dust_dot", "barrier", "structure_void", "light_block"}
_ORE_HINT = ("_ore", "diamond_block", "emerald_block", "gold_block", "iron_block", "netherite_block",
             "lapis_block", "coal_block", "copper_block", "raw_", "amethyst", "ancient_debris", "quartz_block")
_LEAF_TINT = {"oak_leaves": FOLIAGE, "jungle_leaves": FOLIAGE, "acacia_leaves": FOLIAGE,
              "dark_oak_leaves": FOLIAGE, "mangrove_leaves": _hex("92c648"), "spruce_leaves": _hex("619961"),
              "birch_leaves": _hex("80a755")}

# Items: animation frames, overlays and in-between states are not icons.
_ITEM_SKIP = re.compile(r"(_overlay|_pulling_\d+|_cast|_base|_head|_arrow|_firework|_filled|_latch|_animated)$"
                        r"|^(empty_|light_\d\d$|broken_|bundle_open|debug_stick$)"
                        r"|^(compass|clock|recovery_compass)_\d\d$")
# One frame stands for the animated ones.
_ITEM_FRAMES = {"compass": "compass_16", "clock": "clock_00", "recovery_compass": "recovery_compass_16"}
# The potion family is a tinted liquid layer under the bottle.
_POTIONS = {"potion": ("potion", "potion_overlay", _hex("f82423")),
            "splash_potion": ("splash_potion", "potion_overlay", _hex("3f76e4")),
            "lingering_potion": ("lingering_potion", "potion_overlay", _hex("8890f8"))}

_FOOD = {"apple", "bread", "cookie", "carrot", "potato", "baked_potato", "poisonous_potato", "beetroot",
         "melon_slice", "pumpkin_pie", "sweet_berries", "glow_berries", "honey_bottle", "mushroom_stew",
         "rabbit_stew", "beetroot_soup", "suspicious_stew", "golden_carrot", "golden_apple",
         "enchanted_golden_apple", "dried_kelp", "chorus_fruit", "popped_chorus_fruit", "tropical_fish",
         "pufferfish", "cod", "salmon", "cooked_cod", "cooked_salmon", "rotten_flesh", "spider_eye", "beef",
         "cooked_beef", "porkchop", "cooked_porkchop", "chicken", "cooked_chicken", "mutton", "cooked_mutton",
         "rabbit", "cooked_rabbit", "milk_bucket", "cake", "egg", "sugar", "wheat"}
_MINERALS = {"diamond", "emerald", "gold_ingot", "iron_ingot", "copper_ingot", "netherite_ingot",
             "netherite_scrap", "raw_iron", "raw_gold", "raw_copper", "coal", "charcoal", "lapis_lazuli", "quartz",
             "amethyst_shard", "gold_nugget", "iron_nugget", "copper_nugget", "echo_shard", "prismarine_shard",
             "prismarine_crystals", "redstone", "glowstone_dust", "resin_brick", "resin_clump"}
_MAGIC = {"ender_pearl", "ender_eye", "nether_star", "blaze_rod", "blaze_powder", "enchanted_book",
          "experience_bottle", "totem_of_undying", "heart_of_the_sea", "nautilus_shell", "dragon_breath",
          "end_crystal", "ghast_tear", "phantom_membrane", "magma_cream", "slime_ball", "potion", "splash_potion",
          "lingering_potion", "breeze_rod", "trial_key", "ominous_trial_key", "ominous_bottle", "wind_charge",
          "firework_rocket", "book", "writable_book", "written_book", "knowledge_book", "name_tag", "armadillo_scute",
          "turtle_scute", "rabbit_foot", "goat_horn", "echo_shard", "disc_fragment_5"}
_TECH_ITEMS = {"redstone", "repeater", "comparator", "hopper", "minecart", "chest_minecart", "furnace_minecart",
               "hopper_minecart", "tnt_minecart", "command_block_minecart", "observer", "lever", "redstone_torch",
               "tripwire_hook", "daylight_detector", "lightning_rod", "crafter", "copper_bulb", "target",
               "sculk_sensor", "calibrated_sculk_sensor", "hopper_minecart", "bundle", "spyglass",
               "compass", "clock", "recovery_compass", "map", "filled_map", "lead", "string"}
_TOOL_WORDS = ("_sword", "_pickaxe", "_axe", "_shovel", "_hoe", "bow", "trident", "mace", "fishing_rod",
               "shears", "flint_and_steel", "brush", "_on_a_stick", "spectral_arrow", "arrow", "shield")
_ARMOR_WORDS = ("_helmet", "_chestplate", "_leggings", "_boots", "elytra", "horse_armor", "wolf_armor")

GROUPS = ("Blocks", "Ores & minerals", "Redstone & tech", "Workstations", "Tools & combat", "Armor",
          "Food", "Magic & rare", "Mobs", "Music", "Items")


def _label(name):
    words = name.replace("_", " ").split()
    small = {"of", "on", "a", "the", "and"}
    return " ".join(w if (i and w in small) else w.capitalize() for i, w in enumerate(words))


def _item_group(name):
    if name.endswith("_spawn_egg"):
        return "Mobs"
    if name.startswith("music_disc_") or name.startswith("disc_fragment"):
        return "Music"
    if name in _TECH_ITEMS:
        return "Redstone & tech"
    if name in _MINERALS:
        return "Ores & minerals"
    if name in _FOOD:
        return "Food"
    if name in _MAGIC:
        return "Magic & rare"
    if any(w in name for w in _ARMOR_WORDS):
        return "Armor"
    if any(w in name for w in _TOOL_WORDS):
        return "Tools & combat"
    return "Items"


_CATALOG = {}


def catalog(root):
    """Every icon the downloaded textures can draw: [{"key", "label", "group"}], grouped and sorted."""
    p = pack(root)
    if p is None:
        return []
    cached = _CATALOG.get(root)
    if cached and cached[0] is p:
        return cached[1]
    out = {}
    blocks = {n[len("block/"):-4] for n in p.names if n.startswith("block/") and n.endswith(".png")}
    items = {n[len("item/"):-4] for n in p.names if n.startswith("item/") and n.endswith(".png")}
    for name, (group, top, front, side, _h) in CUBES.items():
        if all(t in blocks for t in (top, front, side)):
            out["block/" + name] = (_label(name), group)
    for name in sorted(blocks):
        key = "block/" + name
        if key in out or name in _NOT_CUBES or name in CUBES:
            continue
        cube = name in _CUBE_NAMES or name.endswith(_CUBE_SUFFIX)
        if name.endswith(("_log", "_stem", "_wood")) and name + "_top" in blocks:
            cube = True
        if not cube and name + "_top" in blocks and (name + "_side" in blocks or name.endswith("_block")):
            cube = True
        if cube and not re.search(r"_(top|side|bottom|front|back|inner|on|off|lit|end|overlay|\d)$", name):
            group = "Ores & minerals" if any(h in name for h in _ORE_HINT) else "Blocks"
            out[key] = (_label(name), group)
    for name in sorted(items):
        if _ITEM_SKIP.search(name):
            continue
        out["item/" + name] = (_label(name), _item_group(name))
    for name, frame in _ITEM_FRAMES.items():
        if frame in items:
            out["item/" + name] = (_label(name), _item_group(name))
    for name, (base, overlay, _tint) in _POTIONS.items():
        if base in items and overlay in items:
            out["item/" + name] = (_label(name), "Magic & rare")
    for name, (group, tex) in FLAT_BLOCKS.items():
        if tex in blocks and "item/" + name not in out:
            out["item/" + name] = (_label(name), group)
    order = {g: i for i, g in enumerate(GROUPS)}
    result = [{"key": k, "label": lab, "group": grp} for k, (lab, grp) in out.items()]
    result.sort(key=lambda x: (order.get(x["group"], 99), x["label"]))
    _CATALOG[root] = (p, result)
    return result


def known(root, key):
    return any(x["key"] == key for x in catalog(root))


# What a server runs decides the icon it gets until someone picks one.
SOFTWARE_ICON = {
    "vanilla": "block/grass_block", "snapshot": "block/grass_block",
    "paper": "item/paper", "purpur": "block/purpur_block",
    "spigot": "item/water_bucket", "bukkit": "item/water_bucket", "craftbukkit": "item/water_bucket",
    "fabric": "block/loom", "quilt": "block/magenta_glazed_terracotta",
    "forge": "block/smithing_table", "neoforge": "block/blast_furnace",
    "velocity": "item/ender_pearl", "waterfall": "item/ender_pearl", "bungeecord": "item/ender_pearl",
}


def auto_icon(software="", kind="local"):
    return SOFTWARE_ICON.get((software or "").lower()) or ("item/compass" if kind == "sftp" else "block/lodestone")


def resolve(chosen, software="", kind="local"):
    """The icon an instance shows: the one picked, or the one its software suggests."""
    if chosen and KEY_RE.match(chosen):
        return chosen
    return auto_icon(software, kind)


# ---------------------------------------------------------------------------
#  drawing
# ---------------------------------------------------------------------------

def _tint(rows, rgb):
    return [[(r * rgb[0] // 255, g * rgb[1] // 255, b * rgb[2] // 255, a) for r, g, b, a in row] for row in rows]


def _over(dst, src):
    """src over dst, same size."""
    out = []
    for drow, srow in zip(dst, src):
        row = []
        for (dr, dg, db, da), (sr, sg, sb, sa) in zip(drow, srow):
            if sa == 255 or da == 0:
                row.append((sr, sg, sb, sa) if sa else (dr, dg, db, da))
            elif sa == 0:
                row.append((dr, dg, db, da))
            else:
                k = sa / 255
                row.append((int(sr * k + dr * (1 - k)), int(sg * k + dg * (1 - k)), int(sb * k + db * (1 - k)),
                            max(sa, da)))
        out.append(row)
    return out


def _grey(c):
    return c[0] == c[1] == c[2]


def _face(p, name, block_name=""):
    """A block texture with the game's colouring applied where it is grey in the file."""
    t = p.texture("block/" + name)
    if t is None:
        return None
    size, rows = t
    if name == "grass_block_top":
        rows = _tint(rows, GRASS)
    elif name == "grass_block_side" and _grey(rows[0][0]):
        # older packs left the side's grass grey too, with the colour in an overlay
        over = p.texture("block/grass_block_side_overlay")
        if over and over[0] == size:
            rows = _over(rows, _tint(over[1], GRASS))
    elif block_name in _LEAF_TINT and name == block_name:
        rows = _tint(rows, _LEAF_TINT[block_name])
    return size, rows


def _sprite(p, key):
    """An item's flat picture as (size, rows)."""
    kind, name = key.split("/", 1)
    if kind != "item":
        return None
    if name in _POTIONS:
        base, overlay, tint = _POTIONS[name]
        b, o = p.texture("item/" + base), p.texture("item/" + overlay)
        if b and o and b[0] == o[0]:
            return b[0], _over(_tint(o[1], tint), b[1])
    t = p.texture("item/" + _ITEM_FRAMES.get(name, name))
    if t is None and name in FLAT_BLOCKS:
        t = p.texture("block/" + FLAT_BLOCKS[name][1])
    if t is None:
        return None
    size, rows = t
    if name.startswith("leather_"):
        rows = _tint(rows, LEATHER)
    return size, rows


_MAPS = {}
SHADE = (1.0, 0.82, 0.64)       # top, left, right - the inventory's lighting


def _cube_map(size, height):
    """For each output pixel, which face and texel each of its sub-samples lands on."""
    key = (size, round(height, 4))
    m = _MAPS.get(key)
    if m is not None:
        return m
    ss = 3
    w = size * 0.94 / 1.155          # widest at the corners, full cube 1.155x as tall
    e = w / 3 ** 0.5                 # an edge, foreshortened
    cx = size / 2
    y_full = (size - w * 1.155) / 2
    yt = y_full + e * (1 - height)   # short blocks sit at the bottom of the cube's space
    hs = e * height
    T, L, R, F = (cx, yt), (cx - w / 2, yt + e / 2), (cx + w / 2, yt + e / 2), (cx, yt + e)
    faces = (
        (L, (T[0] - L[0], T[1] - L[1]), (F[0] - L[0], F[1] - L[1]), False),   # top
        (L, (F[0] - L[0], F[1] - L[1]), (0.0, hs), True),                       # left
        (F, (R[0] - F[0], R[1] - F[1]), (0.0, hs), True),                       # right
    )
    inv = []
    for o, u, v, side in faces:
        det = u[0] * v[1] - u[1] * v[0]
        inv.append((o, u, v, det, side))
    out = []
    for y in range(size):
        row = []
        for x in range(size):
            hits = {}
            for sy in range(ss):
                for sx in range(ss):
                    px, py = x + (sx + 0.5) / ss, y + (sy + 0.5) / ss
                    for fi, (o, u, v, det, side) in enumerate(inv):
                        dx, dy = px - o[0], py - o[1]
                        a = (dx * v[1] - dy * v[0]) / det
                        b = (u[0] * dy - u[1] * dx) / det
                        if 0 <= a < 1 and 0 <= b < 1:
                            # sides of a short block show the bottom of their texture
                            tb = 1 - height + b * height if side else b
                            k = (fi, int(a * 16), int(tb * 16))
                            hits[k] = hits.get(k, 0) + 1
                            break
            row.append(tuple(hits.items()))
        out.append(row)
    _MAPS[key] = (out, ss * ss)
    return _MAPS[key]


def _render_cube(p, name, size):
    spec = CUBES.get(name)
    if spec:
        _group, top, front, side, height = spec
    else:
        top = name + "_top" if p.has("block/" + name + "_top") else name
        front = side = name
        if name == "magma_block":
            top = front = side = "magma"
        height = 1
    faces = [_face(p, t, name) for t in (top, front, side)]
    if any(f is None for f in faces):
        return None
    mp, n = _cube_map(size, height)
    out = []
    for row in mp:
        orow = []
        for hits in row:
            if not hits:
                orow.append((0, 0, 0, 0))
                continue
            r = g = b = a = 0.0
            for (fi, tx, ty), cnt in hits:
                fsize, frows = faces[fi]
                c = frows[min(fsize - 1, ty * fsize // 16)][min(fsize - 1, tx * fsize // 16)]
                k = c[3] / 255 * cnt
                s = SHADE[fi]
                r += c[0] * s * k
                g += c[1] * s * k
                b += c[2] * s * k
                a += k
            if a <= 0:
                orow.append((0, 0, 0, 0))
            else:
                orow.append((min(255, int(r / a)), min(255, int(g / a)), min(255, int(b / a)),
                             min(255, int(255 * a / n))))
        out.append(orow)
    return out


def _render_sprite(p, key, size):
    s = _sprite(p, key)
    if s is None:
        return None
    tsize, rows = s
    k = max(1, size // tsize)
    pad = (size - tsize * k) // 2
    blank = (0, 0, 0, 0)
    out = [[blank] * size for _ in range(size)]
    for y in range(tsize):
        for x in range(tsize):
            c = rows[y][x]
            if c[3] == 0:
                continue
            for yy in range(pad + y * k, pad + y * k + k):
                line = out[yy]
                for xx in range(pad + x * k, pad + x * k + k):
                    line[xx] = c
    return out


_ICONS = {}


def pixels(root, key, size=64):
    """An icon as RGBA rows, or None when it cannot be drawn."""
    size = max(16, min(256, int(size)))
    p = pack(root)
    if p is None or not KEY_RE.match(key or ""):
        return None
    kind, name = key.split("/", 1)
    return _render_cube(p, name, size) if kind == "block" else _render_sprite(p, key, size)


def png(root, key, size=64):
    """An icon as PNG bytes, kept once drawn; None when it cannot be drawn."""
    size = max(16, min(256, int(size)))
    ck = (root, key, size)
    if ck in _ICONS:
        return _ICONS[ck]
    rows = pixels(root, key, size)
    data = encode_png(rows) if rows else None
    if len(_ICONS) > 4000:
        _ICONS.clear()
    _ICONS[ck] = data
    return data


def sheet(root, keys, cell=48, columns=16):
    """Many icons on one PNG, left to right, top to bottom - one request for a whole picker."""
    keys = list(keys)
    if not keys:
        return None
    rows_n = (len(keys) + columns - 1) // columns
    blank = (0, 0, 0, 0)
    out = [[blank] * (cell * columns) for _ in range(cell * rows_n)]
    for i, key in enumerate(keys):
        px = pixels(root, key, cell)
        if not px:
            continue
        ox, oy = (i % columns) * cell, (i // columns) * cell
        for y in range(cell):
            out[oy + y][ox:ox + cell] = px[y]
    return encode_png(out)


# ---------------------------------------------------------------------------
#  fetching in the background, once
# ---------------------------------------------------------------------------

STATE = {"busy": False, "error": "", "log": []}
_FETCH_LOCK = threading.Lock()


def ensure_async(root, log=None, force=False, manifest_url=MANIFEST):
    """Fetch the textures on a thread unless they are here already."""
    if (ready(root) and not force) or STATE["busy"]:
        return False

    def run():
        with _FETCH_LOCK:
            STATE.update(busy=True, error="")
            try:
                fetch(root, log=lambda m: (STATE["log"].append(m), log and log(m)), manifest_url=manifest_url)
                _ICONS.clear()
                _CATALOG.pop(root, None)
            except TextureError as e:
                STATE["error"] = str(e)
                if log:
                    log("textures: %s" % e)
            except Exception as e:                       # pragma: no cover - never take the engine down
                STATE["error"] = "Could not fetch the textures: %s" % e
            finally:
                STATE["busy"] = False

    STATE["busy"] = True
    threading.Thread(target=run, daemon=True).start()
    return True


def status(root):
    m = meta(root)
    return {"ready": ready(root), "busy": STATE["busy"], "error": STATE["error"],
            "version": m.get("version", ""), "count": m.get("count", 0)}
