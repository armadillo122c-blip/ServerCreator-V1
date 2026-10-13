#!/usr/bin/env python3
"""Build the Lodestone desktop app.

    py desktop/build.py                    Windows: one self-contained Lodestone.exe
    py desktop/build.py --rid linux-x64    or osx-arm64 / osx-x64 / win-arm64
    py desktop/build.py --dll              a Lodestone.dll run by the installed
                                           .NET 8 runtime, plus a no-console
                                           launcher - for PCs whose antivirus
                                           refuses freshly built .exe files
    py desktop/build.py --engine-exe       also freeze the engine with PyInstaller
                                           (Windows), so Python is not needed

Every build puts the engine beside the app in engine/: lodestone.py and the
modules it imports. The app finds it there and runs it with --headless. Without
--engine-exe that needs Python 3.8+ on the machine; paramiko only for remote
servers, exactly as before.
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent          # desktop/
ROOT = HERE.parent                              # the engine lives here
PROJECT = HERE / "Lodestone.Desktop.csproj"
ENGINE_FILES = ["lodestone.py", "ramtools.py", "viainstaller.py", "creator.py", "content.py", "art.py", "textures.py",
                "updater.py", "agent.py", "ollama.py", "devtools.py", "skripttools.py", "perfstats.py", "websearch.py", "skills.py", "toolchain.py",
                "Lodestone.ico"]
ENGINE_DIRS = ["networking", "builtin_skills"]


def dotnet():
    exe = shutil.which("dotnet")
    if exe:
        return exe
    for p in (Path(r"C:\Program Files\dotnet\dotnet.exe"), Path("/usr/local/share/dotnet/dotnet"),
              Path("/usr/share/dotnet/dotnet")):
        if p.is_file():
            return str(p)
    sys.exit("The .NET 8 SDK is needed: https://dotnet.microsoft.com/download/dotnet/8.0")


def copy_engine(dest):
    eng = dest / "engine"
    if eng.exists():
        shutil.rmtree(eng)
    eng.mkdir(parents=True)
    for f in ENGINE_FILES:
        if (ROOT / f).is_file():
            shutil.copy2(ROOT / f, eng / f)
    for d in ENGINE_DIRS:
        if (ROOT / d).is_dir():
            shutil.copytree(ROOT / d, eng / d, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return eng


def freeze_engine(eng):
    """PyInstaller build of the engine, named so the app finds it first."""
    r = subprocess.run([sys.executable, str(ROOT / "build_app.py"), "--name", "Lodestone-Engine"], cwd=ROOT)
    if r.returncode != 0:
        sys.exit("The engine build failed - see above.")
    built = ROOT / "dist" / ("Lodestone-Engine.exe" if sys.platform.startswith("win") else "Lodestone-Engine")
    shutil.copy2(built, eng / built.name)
    print("engine:  %s" % (eng / built.name))


LAUNCHER_VBS = r"""' Starts the Lodestone desktop app with no console window.
' The app is a .dll run by the installed .NET 8 runtime (dotnet.exe is signed
' by Microsoft), for PCs that refuse to run a freshly built .exe.
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
sh.CurrentDirectory = here
dotnet = sh.ExpandEnvironmentStrings("%ProgramFiles%") & "\dotnet\dotnet.exe"
If Not fso.FileExists(dotnet) Then dotnet = "dotnet"
On Error Resume Next
sh.Run Chr(34) & dotnet & Chr(34) & " " & Chr(34) & here & "\Lodestone.dll" & Chr(34), 0, False
If Err.Number <> 0 Then
  MsgBox "Lodestone needs the .NET 8 runtime: https://dotnet.microsoft.com/download/dotnet/8.0", 16, "Lodestone"
End If
"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rid", default="win-x64", help="runtime id: win-x64, win-arm64, linux-x64, osx-arm64, osx-x64")
    ap.add_argument("--dll", action="store_true", help="framework-dependent Lodestone.dll plus a launcher")
    ap.add_argument("--engine-exe", action="store_true", help="also freeze the engine with PyInstaller")
    a = ap.parse_args()

    out = HERE / "dist" / ("dll" if a.dll else a.rid)
    if out.exists():
        # Move the old build aside before deleting it: Windows will not move a
        # folder while a program is running from it, so a Lodestone window that
        # is still open stops the build here - before anything is deleted -
        # instead of half way through with a PermissionError.
        trash = out.with_name(out.name + ".old")
        shutil.rmtree(trash, ignore_errors=True)
        try:
            out.rename(trash)
        except OSError:
            print("Lodestone is still running from %s, so Windows has its files locked.\n"
                  "Close it first - Settings > Quit Lodestone, or close the window and choose\n"
                  "\"Stop server and quit\" - then run this again." % out)
            return 2
        shutil.rmtree(trash, ignore_errors=True)
    args = [dotnet(), "publish", str(PROJECT), "-c", "Release", "-o", str(out), "-nologo"]
    if a.dll:
        args += ["--self-contained", "false", "-p:UseAppHost=false"]
    else:
        args += ["-r", a.rid, "--self-contained", "true", "-p:PublishSingleFile=true",
                 "-p:IncludeNativeLibrariesForSelfExtract=true", "-p:DebugType=none"]
    print("[1/3] dotnet publish (%s)" % ("framework-dependent dll" if a.dll else a.rid))
    r = subprocess.run(args, cwd=HERE)
    if r.returncode != 0:
        return r.returncode

    print("[2/3] engine sources -> engine/")
    eng = copy_engine(out)
    if a.engine_exe:
        print("[3/3] freezing the engine")
        freeze_engine(eng)
    else:
        print("[3/3] engine stays as source (needs Python 3.8+); --engine-exe freezes it")

    if a.dll:
        # newline="\r\n": Windows scripts, Windows line endings
        (out / "Lodestone.vbs").write_text(LAUNCHER_VBS, encoding="ascii", newline="\r\n")
        (out / "Lodestone.cmd").write_text(
            '@echo off\ncd /d "%~dp0"\nset "DOTNET=%ProgramFiles%\\dotnet\\dotnet.exe"\n'
            'if not exist "%DOTNET%" set "DOTNET=dotnet"\n"%DOTNET%" Lodestone.dll %*\n',
            encoding="ascii", newline="\r\n")
        print("\nbuilt: %s  (start it with Lodestone.vbs)" % out)
    else:
        exe = out / ("Lodestone.exe" if a.rid.startswith("win") else "Lodestone")
        print("\nbuilt: %s" % exe)
        if exe.is_file():
            print("size:  %.1f MB" % (exe.stat().st_size / 1048576))
    return 0


if __name__ == "__main__":
    sys.exit(main())
