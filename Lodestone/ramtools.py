"""Finding memory to give the server, without breaking the machine to get it.

A modded server wants 6-10 GB and a laptop with 16 GB is usually already
spending most of it on a browser, a chat client and three game launchers. This
module answers "what could I close, and how much would that give me" and then
closes exactly what it is told to close.

The whole design is shaped by one fact: **killing a process can lose someone's
work.** So:

* Nothing is ever closed automatically. This reports; the user decides.
* Anything Windows needs to keep running is on a deny-list and cannot be closed
  through here at all, however it is asked for.
* Lodestone itself, and any Minecraft server Lodestone is running, are
  protected the same way - freeing memory by killing the server you are
  freeing it for would be a poor trade.
* Closing is *graceful* by default: a close request the application can refuse,
  so an editor with unsaved work gets to prompt. Force is a separate, explicit
  step, and it is the one that loses data.

Processes are grouped by name, because "Opera: 900 MB across 14 processes" is
the useful unit, not fourteen rows of 60 MB.

Windows only, standard library only.
"""

import ctypes
import os
import subprocess
import sys

# Killing any of these either bluescreens Windows, logs the user out, or breaks
# the desktop. They are never offered and never closed, whatever is asked.
NEVER_CLOSE = {
    "system", "system idle process", "registry", "memory compression",
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "lsm.exe", "svchost.exe", "dwm.exe", "fontdrvhost.exe",
    "sihost.exe", "ctfmon.exe", "spoolsv.exe", "conhost.exe", "wudfhost.exe",
    "audiodg.exe", "wininet.exe", "taskhostw.exe", "searchindexer.exe",
    "securityhealthservice.exe", "msmpeng.exe", "nissrv.exe",
    "securityhealthsystray.exe", "sgrmbroker.exe", "wmiprvse.exe",
    "runtimebroker.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe",
    "textinputhost.exe", "applicationframehost.exe", "explorer.exe",
    "python.exe", "py.exe", "pythonw.exe",
    # Kernel-side entries tasklist reports without an extension.
    "secure system", "registry",
    # Defender, servicing and the firewall host. Closing these either fails
    # outright or leaves the machine unprotected mid-session.
    "mc-fw-host.exe", "mpdefendercoreservice.exe", "smartscreen.exe",
    "trustedinstaller.exe", "sppsvc.exe", "usocoreworker.exe", "dllhost.exe",
    "dashost.exe", "lsaiso.exe", "backgroundtaskhost.exe", "wsappx",
    "csrss", "wininit", "winlogon", "services", "lsass", "smss",
}

# Java is never closed from here. A java process on this machine is either a
# Minecraft server - the thing memory is being freed *for* - or the game the
# user is playing, and neither should die because someone wanted RAM back.
# Stopping a server has its own button, which shuts it down cleanly.
NEVER_JAVA = {"java.exe", "javaw.exe"}

# The tools driving this session. Closing the panel mid-click, or the terminal
# running it, would be a memorable bug.
NEVER_TOOLING = {"lodestone.exe", "claude.exe", "node.exe"}

# Things people usually do not mind closing while a server runs, with a note
# saying what it is so the list is not a wall of executable names.
KNOWN = {
    "discord.exe": ("Discord", "chat — reopens with your servers intact"),
    "discordcanary.exe": ("Discord Canary", "chat"),
    "spotify.exe": ("Spotify", "music player"),
    "spotifylauncher.exe": ("Spotify Launcher", "updater for Spotify"),
    "steam.exe": ("Steam", "game launcher — not needed to play Minecraft"),
    "steamwebhelper.exe": ("Steam (browser)", "Steam's built-in browser, often the biggest part"),
    "epicgameslauncher.exe": ("Epic Games Launcher", "game launcher"),
    "epicwebhelper.exe": ("Epic (browser)", "Epic's built-in browser"),
    "riotclientservices.exe": ("Riot Client", "game launcher"),
    "medal.exe": ("Medal", "clip recorder — will stop capturing"),
    "ollama.exe": ("Ollama", "local AI models — frees a lot, models reload on next use"),
    "ollama app.exe": ("Ollama", "local AI models"),
    "adobe desktop service.exe": ("Adobe Desktop Service", "Adobe updater"),
    "adobeipcbroker.exe": ("Adobe IPC Broker", "Adobe background service"),
    "creative cloud.exe": ("Adobe Creative Cloud", "Adobe launcher"),
    "jottad.exe": ("Jottacloud", "cloud backup — pauses syncing"),
    "opera.exe": ("Opera", "browser — check for unsaved tabs first"),
    "chrome.exe": ("Chrome", "browser — check for unsaved tabs first"),
    "msedge.exe": ("Edge", "browser — check for unsaved tabs first"),
    "firefox.exe": ("Firefox", "browser — check for unsaved tabs first"),
    "teams.exe": ("Teams", "chat"),
    "slack.exe": ("Slack", "chat"),
    "onedrive.exe": ("OneDrive", "cloud sync — pauses syncing"),
    "gamebar.exe": ("Xbox Game Bar", "overlay"),
    "xboxpcapp.exe": ("Xbox", "game launcher"),
    "nvcontainer.exe": ("NVIDIA Container", "driver service — may restart itself"),
    "playitd-tray.exe": ("playit tray", "tunnel tray icon — the tunnel keeps running"),
}

# Apps that hold documents. Force-closing these is how work gets lost, so they
# are flagged even though they are perfectly closeable.
HOLDS_WORK = {"opera.exe", "chrome.exe", "msedge.exe", "firefox.exe",
              "code.exe", "notepad.exe", "notepad++.exe", "sublime_text.exe",
              "photoshop.exe", "illustrator.exe", "blender.exe", "obs64.exe",
              "winword.exe", "excel.exe", "powerpnt.exe"}


class MemoryStatusEx(ctypes.Structure):
    _fields_ = [("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def supported():
    return sys.platform.startswith("win")


def memory():
    """Physical memory totals in MB. Zeros when it cannot be read."""
    if not supported():
        return {"total_mb": 0, "free_mb": 0, "used_mb": 0, "percent": 0}
    st = MemoryStatusEx()
    st.dwLength = ctypes.sizeof(MemoryStatusEx)
    try:
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
    except (AttributeError, OSError):
        return {"total_mb": 0, "free_mb": 0, "used_mb": 0, "percent": 0}
    mb = 1024 * 1024
    total = st.ullTotalPhys // mb
    free = st.ullAvailPhys // mb
    return {"total_mb": total, "free_mb": free, "used_mb": total - free,
            "percent": int(st.dwMemoryLoad)}


def _no_window():
    if not sys.platform.startswith("win"):
        return {}
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}


def _tasklist():
    """[(name, pid, working_set_kb)] from tasklist. Empty on any failure."""
    try:
        r = subprocess.run(["tasklist", "/fo", "csv", "/nh"],
                           capture_output=True, text=True, timeout=20,
                           errors="replace", **_no_window())
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in (r.stdout or "").splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) < 5:
            continue
        name = parts[0].strip('"').strip()
        try:
            pid = int(parts[1])
        except ValueError:
            continue
        mem = parts[4].strip('"').replace(" ", " ")
        digits = "".join(c for c in mem if c.isdigit())
        if not digits:
            continue
        out.append((name, pid, int(digits)))
    return out


def classify(name, pid, protected_pids=(), server_pids=()):
    """What kind of process this is, and whether it may be closed."""
    low = (name or "").lower()
    if pid in server_pids:
        return "server", "a Minecraft server this panel is running", False
    if pid in protected_pids:
        return "panel", "Lodestone itself", False
    if low in NEVER_CLOSE:
        return "system", "Windows needs this", False
    if low in NEVER_JAVA:
        return "java", ("a Java process — probably a Minecraft server or the "
                        "game. Stop servers with the Stop button instead"), False
    if low in NEVER_TOOLING:
        return "panel", "part of the tooling running this panel", False
    label, note = KNOWN.get(low, ("", ""))
    if label:
        return "background", note, True
    return "other", "", True


def snapshot(protected_pids=(), server_pids=(), min_mb=25):
    """Memory totals plus what is using it, grouped by application.

    Only groups above min_mb are worth showing - a list of 200 processes using
    4 MB each is noise, and closing them would free nothing.
    """
    protected_pids = set(protected_pids or ())
    server_pids = set(server_pids or ())
    groups = {}
    for name, pid, kb in _tasklist():
        low = (name or "").lower()
        kind, note, closeable = classify(name, pid, protected_pids, server_pids)
        g = groups.setdefault(low, {
            "name": name,
            "label": KNOWN.get(low, (name.rsplit(".", 1)[0], ""))[0],
            "note": note, "category": kind, "closeable": closeable,
            "pids": [], "mb": 0,
            "holds_work": low in HOLDS_WORK,
        })
        # One protected pid protects the whole group: a java.exe running a
        # server must not be closeable just because another java.exe is not.
        if not closeable:
            g["closeable"] = False
            g["category"] = kind
            g["note"] = note
        g["pids"].append(pid)
        g["mb"] += kb // 1024

    rows = [g for g in groups.values() if g["mb"] >= min_mb]
    rows.sort(key=lambda g: g["mb"], reverse=True)
    mem = memory()
    mem["processes"] = rows
    mem["reclaimable_mb"] = sum(g["mb"] for g in rows if g["closeable"])
    mem["supported"] = supported()
    return mem


def close(name, protected_pids=(), server_pids=(), force=False):
    """Close every process of one application by name.

    Graceful by default: taskkill without /F asks the window to close, which an
    application with unsaved work is free to refuse. Force is what actually
    loses data, so it is never the default and never implicit.
    """
    if not supported():
        raise RuntimeError("Closing applications is Windows-only.")
    low = (name or "").strip().lower()
    if not low:
        raise RuntimeError("No application named.")
    if low in NEVER_CLOSE:
        raise RuntimeError("%s is part of Windows and will not be closed from "
                           "here." % name)
    if low in NEVER_JAVA:
        raise RuntimeError(
            "%s is a Java process, which on this machine means a Minecraft "
            "server or the game itself. Stop a server with its Stop button so "
            "it saves the world first." % name)
    if low in NEVER_TOOLING:
        raise RuntimeError("%s is part of the tooling running this panel."
                           % name)

    targets = []
    for pname, pid, _kb in _tasklist():
        if (pname or "").lower() != low:
            continue
        kind, _note, closeable = classify(pname, pid, set(protected_pids or ()),
                                          set(server_pids or ()))
        if not closeable:
            raise RuntimeError(
                "%s is protected (%s) and will not be closed." % (name, kind))
        targets.append(pid)

    if not targets:
        return {"closed": 0, "pids": [], "still_running": 0,
                "msg": "%s is not running." % name}

    args = ["taskkill", "/T"]
    if force:
        args.append("/F")
    for pid in targets:
        args += ["/PID", str(pid)]
    try:
        r = subprocess.run(args, capture_output=True, text=True, timeout=30,
                           errors="replace", **_no_window())
    except (OSError, subprocess.SubprocessError) as e:
        raise RuntimeError("Could not close %s: %s" % (name, e))

    still = [pid for pname, pid, _ in _tasklist()
             if (pname or "").lower() == low]
    closed = len(targets) - len(still)
    detail = (r.stdout or r.stderr or "").strip().splitlines()
    return {"closed": closed, "pids": targets, "still_running": len(still),
            "forced": bool(force),
            "msg": ("Closed %s." % name if not still else
                    "%s asked to close; %d process(es) still running — it may "
                    "be waiting on unsaved work." % (name, len(still))),
            "detail": detail[:4]}
