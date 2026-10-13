#!/usr/bin/env python3
"""Build Lodestone into a single self-contained exe.

    py build_app.py

Regenerates the icon first so the exe never ships stale art, then hands
everything to PyInstaller. paramiko is bundled when it is importable here;
without it the exe still runs local servers and says what is missing for
remote ones.
"""
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
# --name Lodestone-Engine is how desktop/build.py freezes the engine for the
# desktop app; the default is the classic browser-page exe.
NAME = sys.argv[sys.argv.index("--name") + 1] if "--name" in sys.argv[:-1] else "Lodestone"


def main():
    if not (HERE / "lodestone.py").is_file():
        print("lodestone.py is not next to this script.")
        return 1

    print("[1/3] drawing the icon")
    subprocess.run([sys.executable, str(HERE / "make_icon.py")], cwd=HERE, check=True)

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("PyInstaller is missing. Install it with:  py -m pip install pyinstaller")
        return 1

    hidden = []
    try:
        import paramiko  # noqa: F401
        hidden += ["--hidden-import", "paramiko"]
        print("[2/3] paramiko found — remote servers will work in the exe")
    except ImportError:
        print("[2/3] paramiko missing — the exe will handle local servers only")

    # The networking package (internet hosting) is imported inside a try/except,
    # which PyInstaller's static analysis is entitled to skip. Collect it
    # explicitly, or the exe silently loses the Network tab.
    if (HERE / "ramtools.py").is_file():
        hidden += ["--hidden-import", "ramtools"]
        print("      memory tools found — the Free memory card is included")

    if (HERE / "viainstaller.py").is_file():
        hidden += ["--hidden-import", "viainstaller"]
        print("      Via plugin installer found — the ViaVersion button is included")

    if (HERE / "creator.py").is_file():
        hidden += ["--hidden-import", "creator"]
        print("      server creator found — the Create server wizard is included")

    if (HERE / "updater.py").is_file():
        hidden += ["--hidden-import", "updater"]

    if (HERE / "art.py").is_file():
        hidden += ["--hidden-import", "art"]

    if (HERE / "textures.py").is_file():
        hidden += ["--hidden-import", "textures"]

    if (HERE / "content.py").is_file():
        hidden += ["--hidden-import", "content"]
        print("      mod and plugin browser found — the Mods & plugins tab is included")

    # The AI Assistant, the code tools and the performance numbers - each is
    # imported inside a try/except too, so name them or the exe loses them.
    want = ("agent", "ollama", "devtools", "skripttools", "perfstats", "websearch", "skills", "toolchain", "gittools", "githubapi")
    dev = [m for m in want if (HERE / (m + ".py")).is_file()]
    for m in dev:
        hidden += ["--hidden-import", m]
    if len(dev) == len(want):
        print("      AI Assistant, code tools and performance found - those pages are included")

    if (HERE / "networking" / "__init__.py").is_file():
        hidden += ["--collect-submodules", "networking"]
        print("      networking package found — internet hosting is included")
    else:
        print("      networking package missing — the exe will have Local and "
              "LAN hosting only")

    # Ship the .ico alongside the code so /favicon.ico can serve the real thing.
    sep = ";" if sys.platform.startswith("win") else ":"
    args = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--noconsole",
        "--name", NAME,
        "--icon", str(HERE / "Lodestone.ico"),
        "--add-data", "%s%s." % (HERE / "Lodestone.ico", sep),
        # the assistant's built-in skills (SKILL.md folders), read from sys._MEIPASS/builtin_skills
        "--add-data", "%s%sbuiltin_skills" % (HERE / "builtin_skills", sep),
        "--clean", "--noconfirm",
    ] + hidden + [str(HERE / "lodestone.py")]

    print("[3/3] building")
    r = subprocess.run(args, cwd=HERE)
    if r.returncode != 0:
        return r.returncode

    out = HERE / "dist" / (NAME + (".exe" if sys.platform.startswith("win") else ""))
    print("\nbuilt: %s" % out)
    if out.is_file():
        print("size:  %.1f MB" % (out.stat().st_size / 1048576))
    for junk in ("build", NAME + ".spec"):
        p = HERE / junk
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        elif p.is_file():
            p.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
