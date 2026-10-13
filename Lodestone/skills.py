"""Skills for the AI Assistant: packaged instructions it loads when a task needs them.

A skill is a folder with a ``SKILL.md`` in it - the open Agent Skills format
(the same shape Claude Code uses, so its skill folders work here unchanged)::

    my-skill/
      SKILL.md          ---
                        name: my-skill
                        description: What it does, and when to use it.
                        ---
                        Instructions, in Markdown...
      reference.md      anything else the instructions point to
      templates/...

Only the name and description go into the model's context up front. When a
request matches, the model calls ``use_skill`` and gets the instructions; files
next to SKILL.md are read on demand with ``read_skill_file``. A person can also
start one by typing ``/name`` at the start of a message. A skill whose header
says ``disable-model-invocation: true`` is only ever started that way.

Where skills come from, first match of a name wins:

* the workspace: ``<workspace>/.lodestone/skills`` and ``<workspace>/.claude/skills``
* yours: ``~/.lodestone/skills`` (created, imported and deleted from the app)
  and ``~/.claude/skills``
* built in: ``builtin_skills/`` shipped with Lodestone

Skills are instructions, not programs: nothing in a skill folder is ever run by
loading it. A skill that tells the model to run something still goes through
the assistant's normal tools and approvals.

Standard library only.
"""

import io
import os
import re
import shutil
import sys
import zipfile
from pathlib import Path

NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_NAME = 64
MAX_DESC = 1024
MAX_BODY = 200_000
MAX_FILE = 400_000
MAX_ZIP = 20_000_000
MAX_ZIP_FILES = 500


class SkillError(RuntimeError):
    """Something to tell the user in a sentence."""


def builtin_dir():
    base = Path(getattr(sys, "_MEIPASS", "")) if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    return base / "builtin_skills"


def _frontmatter(text):
    """(fields, body) of a SKILL.md. The header is simple YAML - key: value
    lines, quoted or not, with '|' / '>' blocks - read without a YAML library."""
    text = text.lstrip("﻿")
    m = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.S)
    if not m:
        return {}, text
    fields, key, block = {}, None, []

    def flush():
        if key is not None and block is not None and key in fields and fields[key] in ("|", ">", "|-", ">-"):
            joiner = "\n" if fields[key].startswith("|") else " "
            fields[key] = joiner.join(block).strip()

    for line in m.group(1).splitlines():
        km = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if km and not line.startswith((" ", "\t")):
            flush()
            key, val = km.group(1).strip().lower(), km.group(2).strip()
            if len(val) >= 2 and val[0] == val[-1] == '"':
                val = re.sub(r'\\(["\\/])', r"\1", val[1:-1]).replace("\\n", "\n").replace("\\t", "\t")
            elif len(val) >= 2 and val[0] == val[-1] == "'":
                val = val[1:-1].replace("''", "'")
            fields[key] = val
            block = []
        elif key is not None and fields.get(key) in ("|", ">", "|-", ">-"):
            block.append(line.strip())
        elif key is not None and line.strip() and fields.get(key) and not line.strip().startswith("- "):
            fields[key] = (fields[key] + " " + line.strip()).strip()    # a folded plain scalar
    flush()
    return fields, text[m.end():]


def _truthy(v):
    return str(v or "").strip().lower() in ("true", "yes", "1", "on")


def read_skill(folder, source):
    """The skill in ``folder``, or None if it has no usable SKILL.md."""
    folder = Path(folder)
    md = folder / "SKILL.md"
    if not md.is_file():
        return None
    try:
        text = md.read_text(encoding="utf-8", errors="replace")[:MAX_BODY]
    except OSError:
        return None
    fields, body = _frontmatter(text)
    name = (fields.get("name") or folder.name).strip()
    desc = " ".join((fields.get("description") or "").split())[:MAX_DESC]
    if not desc:
        # no description, nothing for the model to choose it by - take the first sentence of the body
        first = next((ln.strip("# ").strip() for ln in body.splitlines() if ln.strip()), "")
        desc = first[:200]
    return {"name": name, "description": desc, "dir": str(folder), "source": source,
            "user_only": _truthy(fields.get("disable-model-invocation")),
            "valid_name": bool(NAME_RE.match(name)) and len(name) <= MAX_NAME}


def roots(data_dir, workspace_root=None, home=None):
    """(folder, source) in the order a name is looked up."""
    home = Path(home or os.path.expanduser("~"))
    out = []
    if workspace_root:
        w = Path(workspace_root)
        out += [(w / ".lodestone" / "skills", "workspace"), (w / ".claude" / "skills", "workspace")]
    out += [(Path(data_dir) / "skills", "yours"), (home / ".claude" / "skills", "claude"),
            (builtin_dir(), "built-in")]
    return out


def discover(data_dir, workspace_root=None, disabled=(), home=None):
    """Every skill that can be used here, the first of each name winning."""
    seen, out = set(), []
    for root, source in roots(data_dir, workspace_root, home):
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue
        for folder in entries:
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            sk = read_skill(folder, source)
            if not sk or sk["name"] in seen:
                continue
            seen.add(sk["name"])
            sk["enabled"] = sk["name"] not in set(disabled or ())
            out.append(sk)
    return out


def find(skills, name):
    name = (name or "").strip().lstrip("/").lower()
    for s in skills:
        if s["name"].lower() == name:
            return s
    return None


def files_of(skill, limit=80):
    """The files next to SKILL.md, relative to the skill's folder."""
    base = Path(skill["dir"])
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d != "__pycache__")
        for f in sorted(filenames):
            rel = os.path.relpath(os.path.join(dirpath, f), base).replace("\\", "/")
            if rel != "SKILL.md":
                out.append(rel)
            if len(out) >= limit:
                return out
    return out


def load(skill):
    """The instructions (SKILL.md without its header) and the list of files."""
    try:
        text = (Path(skill["dir"]) / "SKILL.md").read_text(encoding="utf-8", errors="replace")[:MAX_BODY]
    except OSError as e:
        raise SkillError("Could not read %s: %s" % (skill["name"], e))
    _fields, body = _frontmatter(text)
    return body.strip(), files_of(skill)


def read_file(skill, rel):
    """A file inside the skill's folder - never outside it."""
    base = Path(skill["dir"]).resolve()
    p = (base / (rel or "").replace("\\", "/")).resolve()
    try:
        p.relative_to(base)
    except ValueError:
        raise SkillError("'%s' is outside the skill's folder." % rel)
    if not p.is_file():
        raise SkillError("The skill %s has no file '%s'. It has: %s" % (
            skill["name"], rel, ", ".join(files_of(skill)[:30]) or "only SKILL.md"))
    if p.stat().st_size > MAX_FILE:
        raise SkillError("'%s' is too big to read (%d bytes)." % (rel, p.stat().st_size))
    with open(p, "rb") as f:
        raw = f.read()
    if b"\0" in raw[:4096]:
        raise SkillError("'%s' is not a text file." % rel)
    return raw.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
#  making, importing, removing - only ever in the user's own folder
# ---------------------------------------------------------------------------

def _check_name(name):
    name = (name or "").strip()
    if not NAME_RE.match(name) or len(name) > MAX_NAME:
        raise SkillError("A skill's name is lowercase letters, digits and hyphens, up to %d characters - "
                         "like paper-plugin or fix-crashes." % MAX_NAME)
    return name


def _yaml_str(s):
    s = " ".join((s or "").split())
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def save(data_dir, name, description, body, overwrite=True):
    """Write ~/.lodestone/skills/<name>/SKILL.md. Returns the skill."""
    name = _check_name(name)
    description = " ".join((description or "").split())
    if not description:
        raise SkillError("Say what the skill does and when to use it - that is how the assistant picks it.")
    if len(description) > MAX_DESC:
        raise SkillError("Keep the description under %d characters." % MAX_DESC)
    body = (body or "").strip()
    if not body:
        raise SkillError("Write the instructions the assistant should follow.")
    folder = Path(data_dir) / "skills" / name
    if folder.exists() and not overwrite:
        raise SkillError("There is already a skill called %s." % name)
    folder.mkdir(parents=True, exist_ok=True)
    text = "---\nname: %s\ndescription: %s\n---\n\n%s\n" % (name, _yaml_str(description), body)
    tmp = folder / "SKILL.md.tmp"
    tmp.write_text(text, encoding="utf-8")
    os.replace(str(tmp), str(folder / "SKILL.md"))
    return read_skill(folder, "yours")


def delete(data_dir, name):
    name = _check_name(name)
    folder = (Path(data_dir) / "skills" / name)
    if not (folder / "SKILL.md").is_file():
        raise SkillError("%s is not one of your skills (built-in and workspace skills are not deleted from "
                         "here)." % name)
    shutil.rmtree(folder)
    return True


def _skill_root_in_zip(names):
    """The folder inside a zip that holds SKILL.md: the top, or one folder down."""
    mds = [n for n in names if n.rstrip("/").split("/")[-1] == "SKILL.md"]
    if not mds:
        raise SkillError("There is no SKILL.md in that file.")
    mds.sort(key=lambda n: n.count("/"))
    top = mds[0]
    if top.count("/") > 1:
        raise SkillError("SKILL.md is buried too deep in that file - it should be at the top or in one folder.")
    return top[:-len("SKILL.md")]


def import_skill(data_dir, path, overwrite=False):
    """Copy a skill in from a folder, a .zip or a .skill file into your skills."""
    src = Path(os.path.expanduser(path or "")).resolve()
    dest_root = Path(data_dir) / "skills"
    dest_root.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        sk = read_skill(src, "import")
        if not sk:
            raise SkillError("That folder has no SKILL.md.")
        name = _check_name(sk["name"] if sk["valid_name"] else src.name.lower())
        dest = dest_root / name
        if dest.exists():
            if not overwrite:
                raise SkillError("You already have a skill called %s." % name)
            shutil.rmtree(dest)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns(".git", "__pycache__", "node_modules"))
        return read_skill(dest, "yours")
    if not src.is_file():
        raise SkillError("There is nothing at %s." % path)
    if src.stat().st_size > MAX_ZIP:
        raise SkillError("That file is too big for a skill (over %d MB)." % (MAX_ZIP // 1_000_000))
    if src.name == "SKILL.md":
        return import_skill(data_dir, str(src.parent), overwrite)
    try:
        z = zipfile.ZipFile(io.BytesIO(src.read_bytes()))
    except zipfile.BadZipFile:
        raise SkillError("That is not a zip, .skill file or skill folder.")
    with z:
        infos = [i for i in z.infolist() if not i.is_dir()]
        if len(infos) > MAX_ZIP_FILES:
            raise SkillError("That file has too many files in it for a skill.")
        if sum(i.file_size for i in infos) > MAX_ZIP * 3:
            raise SkillError("That file unpacks to too much for a skill.")
        prefix = _skill_root_in_zip([i.filename for i in infos])
        fields, _body = _frontmatter(z.read(prefix + "SKILL.md").decode("utf-8", "replace"))
        name = (fields.get("name") or prefix.rstrip("/").split("/")[-1] or src.stem).strip().lower()
        name = _check_name(name)
        dest = dest_root / name
        if dest.exists():
            if not overwrite:
                raise SkillError("You already have a skill called %s." % name)
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        base = dest.resolve()
        for i in infos:
            if not i.filename.startswith(prefix):
                continue
            rel = i.filename[len(prefix):]
            target = (base / rel).resolve()
            try:
                target.relative_to(base)           # no ../ or absolute paths out of the folder
            except ValueError:
                shutil.rmtree(dest, ignore_errors=True)
                raise SkillError("That file tries to write outside its folder (%s) - not importing it." % i.filename)
            if (i.external_attr >> 16) & 0o170000 == 0o120000:
                continue                           # symlinks are skipped
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(i) as fsrc, open(target, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst)
    return read_skill(dest, "yours")
