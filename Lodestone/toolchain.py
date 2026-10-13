"""What building a plugin or mod needs, fetched when the user (or the assistant,
with their approval) asks: a JDK, Gradle, and a new project to start from.

* **JDK** - Eclipse Temurin from Adoptium, through creator.install_java (the
  same verified download the server wizard uses), as a full JDK in
  ``~/.lodestone/java/temurin-<major>-jdk``: servers only need a runtime,
  compiling needs javac.
* **Gradle** - the official distribution from services.gradle.org. Its version
  API (``/versions/current``) names the current release with its SHA-256; the
  zip is checked against that before it is unpacked into
  ``~/.lodestone/tools/gradle-<version>``.
* **A Paper plugin project** - settings.gradle.kts, build.gradle.kts,
  plugin.yml and a main class, for the server's Minecraft version and the Java
  it needs; then Gradle's own ``gradle wrapper`` adds gradlew, so the project
  builds anywhere without Gradle installed.

Standard library plus creator.py.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

import creator as createmod

GRADLE_CURRENT = "https://services.gradle.org/versions/current"
GRADLE_DIST = "https://services.gradle.org/distributions/gradle-%s-bin.zip"
EXE = ".exe" if os.name == "nt" else ""


class ToolError(RuntimeError):
    """Something to tell the user in a sentence."""


# ---------------------------------------------------------------------------
#  JDK
# ---------------------------------------------------------------------------

def javac_of(java_path):
    p = Path(java_path).with_name("javac" + EXE)
    return str(p) if p.is_file() else ""


def java_home(java_path):
    """The folder JAVA_HOME should point at for this java binary."""
    return str(Path(java_path).resolve().parent.parent)


def find_jdk(java_root, at_least=17, extra=()):
    """A JDK (java with javac beside it) of at least this major, newest first."""
    try:
        javas = createmod.find_javas(str(java_root), extra)
    except Exception:
        return None
    for j in javas:
        if j["major"] >= at_least and javac_of(j["path"]):
            return j
    return None


def ensure_jdk(java_root, major, log=None):
    """A JDK of exactly this major: one already here, or Temurin downloaded."""
    log = log or (lambda m: None)
    have = find_jdk(java_root, major)
    if have and have["major"] == major:
        log("JDK %d is already here: %s" % (major, have["path"]))
        return have["path"]
    try:
        path = createmod.install_java(major, str(java_root), log=log, jdk=True)
    except createmod.CreateError as e:
        raise ToolError(str(e))
    if not javac_of(path):
        raise ToolError("Java %d was installed but has no javac - Adoptium only had a runtime for this computer."
                        % major)
    return path


def build_env(java_root, base=None, tools_root=None):
    """The environment a build runs in: JAVA_HOME and PATH pointing at a JDK
    Lodestone installed (unless the user already has JAVA_HOME set), and the
    Gradle Lodestone installed on PATH, so `gradle build` works without one."""
    env = dict(base if base is not None else os.environ)
    if tools_root:
        have = installed_gradles(tools_root)
        if have:
            env["PATH"] = os.path.dirname(have[0][1]) + os.pathsep + env.get("PATH", "")
    if env.get("JAVA_HOME") and os.path.isdir(env["JAVA_HOME"]):
        return env
    j = find_jdk(java_root, 8)
    if j:
        home = java_home(j["path"])
        env["JAVA_HOME"] = home
        env["PATH"] = os.path.join(home, "bin") + os.pathsep + env.get("PATH", "")
    return env


# ---------------------------------------------------------------------------
#  Gradle
# ---------------------------------------------------------------------------

def gradle_bin(folder):
    p = Path(folder) / "bin" / ("gradle.bat" if os.name == "nt" else "gradle")
    return str(p) if p.is_file() else ""


def installed_gradles(tools_root):
    out = []
    root = Path(tools_root)
    if root.is_dir():
        for d in root.iterdir():
            m = re.match(r"^gradle-(\d+(?:\.\d+)*)$", d.name)
            if m and gradle_bin(d):
                out.append((tuple(int(x) for x in m.group(1).split(".")), m.group(1), gradle_bin(d)))
    out.sort(reverse=True)
    return [(v, path) for _key, v, path in out]


def gradle_current(fetch=None):
    """(version, url, sha256) of Gradle's current release, from its version API."""
    try:
        data = (fetch or (lambda u: createmod.get_json(u, cache=False)))(GRADLE_CURRENT)
    except Exception as e:
        raise ToolError("Could not ask services.gradle.org for the current Gradle: %s" % e)
    ver, url, sha = data.get("version"), data.get("downloadUrl"), (data.get("checksum") or "").lower()
    if not ver or not url or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise ToolError("services.gradle.org answered without a version, download or checksum.")
    if not url.startswith("https://services.gradle.org/distributions/"):
        raise ToolError("Gradle's download address is not on services.gradle.org (%s) - not using it." % url)
    return ver, url, sha


def ensure_gradle(tools_root, log=None, fetch=None, download=None):
    """Gradle's bin/gradle: one Lodestone already unpacked, else the current
    release, verified against the checksum Gradle publishes for it."""
    log = log or (lambda m: None)
    have = installed_gradles(tools_root)
    if have:
        log("Gradle %s is already here: %s" % have[0])
        return have[0][1]
    ver, url, sha = gradle_current(fetch)
    os.makedirs(tools_root, exist_ok=True)
    work = tempfile.mkdtemp(prefix=".gradle-", dir=str(tools_root))
    try:
        archive = os.path.join(work, "gradle-%s-bin.zip" % ver)
        log("Downloading Gradle %s from services.gradle.org" % ver)
        try:
            (download or createmod.download)(url, archive, {"sha256": sha}, 0, log=log, label="Gradle %s" % ver)
        except createmod.CreateError as e:
            raise ToolError(str(e))
        log("Verified (SHA-256 %s...), unpacking" % sha[:12])
        unpacked = os.path.join(work, "x")
        os.makedirs(unpacked)
        with zipfile.ZipFile(archive) as z:
            createmod._safe_extract_zip(z, unpacked)
        top = os.path.join(unpacked, "gradle-%s" % ver)
        if not os.path.isdir(top):
            raise ToolError("The Gradle zip did not hold gradle-%s/." % ver)
        target = os.path.join(str(tools_root), "gradle-%s" % ver)
        if os.path.isdir(target):
            shutil.rmtree(target, ignore_errors=True)
        shutil.move(top, target)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    path = gradle_bin(target)
    if not path:
        raise ToolError("Gradle %s was unpacked but bin/gradle is missing." % ver)
    if os.name != "nt":
        os.chmod(path, 0o755)
    log("Gradle %s ready: %s" % (ver, path))
    return path


def make_wrapper(project, gradle, env, log=None, timeout=600):
    """Run Gradle's own `gradle wrapper` in the project, so it gets gradlew."""
    log = log or (lambda m: None)
    try:
        r = subprocess.run([gradle, "wrapper", "--no-daemon"], cwd=str(project), env=env, capture_output=True,
                           text=True, errors="replace", timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
    except (OSError, subprocess.SubprocessError) as e:
        raise ToolError("Could not run Gradle: %s" % e)
    for line in (r.stdout + r.stderr).splitlines()[-30:]:
        if line.strip():
            log(line)
    if r.returncode != 0:
        raise ToolError("gradle wrapper failed (exit %d): %s" % (r.returncode, (r.stderr or r.stdout).strip()[-400:]))
    if not (Path(project) / ("gradlew.bat" if os.name == "nt" else "gradlew")).is_file():
        raise ToolError("gradle wrapper ran but made no gradlew.")
    return True


# ---------------------------------------------------------------------------
#  a new Paper plugin
# ---------------------------------------------------------------------------

def _ident(name):
    words = re.findall(r"[A-Za-z0-9]+", name or "")
    cls = "".join(w[:1].upper() + w[1:] for w in words) or "MyPlugin"
    if cls[0].isdigit():
        cls = "P" + cls
    return cls


def paper_api_version(mc):
    """'1.21.4' -> '1.21' for plugin.yml's api-version; newer two-part
    versions ('26.1') stay as they are."""
    parts = (mc or "").split(".")
    if parts and parts[0] == "1" and len(parts) >= 2:
        return "%s.%s" % (parts[0], parts[1])
    return ".".join(parts[:2]) if len(parts) >= 2 else mc


def scaffold_paper(dest, name, group, mc, java_release):
    """Write a Paper plugin project into dest (which must be new or empty).
    Returns the files written, relative to dest."""
    dest = Path(dest)
    m = re.match(r"^1\.(\d+)", mc or "")
    if m and int(m.group(1)) < 17:
        raise ToolError("Paper's API for Minecraft before 1.17 lives elsewhere (com.destroystokyo.paper) - "
                        "this template starts at 1.17.")
    if not re.match(r"^\d+\.\d+", mc or ""):
        raise ToolError("Say which Minecraft version the plugin is for, like 1.21.4.")
    if dest.exists() and any(dest.iterdir()):
        raise ToolError("%s already has files in it - pick a new or empty folder." % dest)
    cls = _ident(name)
    group = (group or "").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+", group):
        raise ToolError("The package must look like com.example (lowercase words separated by dots).")
    pkg = group + "." + cls.lower()
    artifact = re.sub(r"[^a-z0-9-]+", "-", (name or cls).lower()).strip("-") or cls.lower()
    files = {
        "settings.gradle.kts": 'rootProject.name = "%s"\n' % artifact,
        "build.gradle.kts": """plugins {
    java
}

group = "%(group)s"
version = "1.0.0"

repositories {
    mavenCentral()
    maven("https://repo.papermc.io/repository/maven-public/")
}

dependencies {
    compileOnly("io.papermc.paper:paper-api:%(mc)s-R0.1-SNAPSHOT")
}

tasks.withType<JavaCompile>().configureEach {
    options.encoding = "UTF-8"
    options.release.set(%(java)d)
}

tasks.processResources {
    filesMatching("plugin.yml") {
        expand("version" to project.version)
    }
}
""" % {"group": group, "mc": mc, "java": java_release},
        "src/main/resources/plugin.yml": """name: %(cls)s
version: '${version}'
main: %(pkg)s.%(cls)s
api-version: '%(api)s'
commands:
  %(cmd)s:
    description: Shows that %(cls)s is running
    usage: /%(cmd)s
""" % {"cls": cls, "pkg": pkg, "api": paper_api_version(mc), "cmd": artifact.replace("-", "")},
        "src/main/java/%s/%s.java" % (pkg.replace(".", "/"), cls): """package %(pkg)s;

import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.format.NamedTextColor;
import org.bukkit.command.Command;
import org.bukkit.command.CommandSender;
import org.bukkit.plugin.java.JavaPlugin;

public final class %(cls)s extends JavaPlugin {

    @Override
    public void onEnable() {
        getLogger().info("%(cls)s is enabled");
    }

    @Override
    public void onDisable() {
        getLogger().info("%(cls)s is disabled");
    }

    @Override
    public boolean onCommand(CommandSender sender, Command command, String label, String[] args) {
        sender.sendMessage(Component.text("%(cls)s is running.", NamedTextColor.GREEN));
        return true;
    }
}
""" % {"pkg": pkg, "cls": cls},
        ".gitignore": ".gradle/\nbuild/\n.idea/\n*.iml\nout/\nrun/\n",
    }
    dest.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        p = dest / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return sorted(files)
