"""Installing and setting up the playit.gg agent - when the user asks for it.

Tunnel mode used to stop at "download playit, then point Lodestone at it".
Now one button does the official steps the user would otherwise type:

1. **Install.** On Windows through winget (``winget install
   DevelopedMethods.playit``), which is how playit's own README says to install
   it and which checks the installer against the SHA-256 in its manifest before
   running it. Windows may show its administrator prompt - that is the MSI
   installing the playit service. Elsewhere there is no unattended install
   that does not need root, so the official commands are shown instead.
2. **Set up.** playit 1.x runs as a background service with a ``playit`` CLI in
   front of it. ``playit setup`` makes a claim code, prints a
   ``https://playit.gg/claim/<code>`` link and waits until the user approves it
   in their browser - the one step that needs their playit account, so it is
   handed to them, never done for them. The link is passed on as soon as it is
   printed.

Nothing here runs without the user pressing the button, and nothing but
playit's own installer and CLI is run.
"""

import os
import re
import shutil
import subprocess
import sys
import threading
import time

from .tunnel import PLAYIT_CLAIM_RE, PLAYIT_DOWNLOAD, _no_window, find_agent

WINGET_ID = "DevelopedMethods.playit"
WINGET_ARGS = ["install", "--id", WINGET_ID, "--exact", "--source", "winget", "--silent",
               "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"]
# winget's "already installed, nothing newer" - not a failure for us
WINGET_ALREADY = (-1978335189, 0x8A15002B, -1978335135, 0x8A150061)
LINUX_COMMANDS = [
    "curl -SsL https://playit-cloud.github.io/ppa/key.gpg | gpg --dearmor | "
    "sudo tee /etc/apt/trusted.gpg.d/playit.gpg >/dev/null",
    'echo "deb [signed-by=/etc/apt/trusted.gpg.d/playit.gpg] https://playit-cloud.github.io/ppa/data ./" | '
    "sudo tee /etc/apt/sources.list.d/playit-cloud.list",
    "sudo apt update && sudo apt install playit",
]
NOISE = re.compile(r"^[\s\-\\|/█▒░▓#=.%0-9KMGBkmgbs]*$")


class SetupError(RuntimeError):
    """Something to tell the user in a sentence."""


def plan(platform=None, which=shutil.which):
    """How playit would be installed on this computer."""
    platform = platform or sys.platform
    if platform.startswith("win"):
        winget = which("winget")
        if winget:
            return {"method": "winget", "can_auto": True, "winget": winget,
                    "detail": "Installs playit with winget (Windows' package manager), from playit's official "
                              "package. Windows may ask for administrator permission for the playit service."}
        return {"method": "manual", "can_auto": False, "download": PLAYIT_DOWNLOAD,
                "detail": "winget (App Installer) is not on this PC. Install App Installer from the Microsoft Store, "
                          "or download playit from %s." % PLAYIT_DOWNLOAD}
    if platform.startswith("linux"):
        return {"method": "manual", "can_auto": False, "commands": LINUX_COMMANDS, "download": PLAYIT_DOWNLOAD,
                "detail": "Installing needs root, so run playit's official commands in a terminal, then press "
                          "Set up again."}
    return {"method": "manual", "can_auto": False, "download": PLAYIT_DOWNLOAD,
            "detail": "Download playit from %s, then press Set up again." % PLAYIT_DOWNLOAD}


def _stream(argv, log, timeout, cancel=None, on_line=None):
    """Run argv, passing its readable lines to log. Returns (exit code, all text)."""
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                text=True, encoding="utf-8", errors="replace", bufsize=1, **_no_window())
    except OSError as e:
        raise SetupError("Could not run %s: %s" % (os.path.basename(argv[0]), e))
    killed = []

    def stop(why):
        if proc.poll() is None:
            killed.append(why)
            try:
                proc.kill()
            except OSError:
                pass
    timer = threading.Timer(timeout, lambda: stop("timed out after %d minutes" % (timeout // 60)))
    timer.daemon = True
    timer.start()
    watcher = None
    if cancel is not None:
        def watch():
            while proc.poll() is None:
                if cancel.is_set():
                    stop("cancelled")
                    return
                time.sleep(0.3)
        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
    text = []
    try:
        for raw in proc.stdout:
            for line in raw.replace("\r", "\n").split("\n"):
                line = line.strip()
                if not line or NOISE.match(line):
                    continue           # winget's spinner and progress bars
                text.append(line)
                log(line)
                if on_line:
                    on_line(line)
        code = proc.wait()
    finally:
        timer.cancel()
    if killed:
        raise SetupError("%s %s." % (os.path.basename(argv[0]), killed[0]))
    return code, "\n".join(text)


def install(log, cancel=None, which=shutil.which, runner=None):
    """Install playit the official way for this OS. Returns the agent's path."""
    found = find_agent()
    if found:
        log("playit is already installed: %s" % found)
        return found
    p = plan(which=which)
    if not p["can_auto"]:
        raise SetupError(p["detail"])
    log("Installing playit with winget (%s)..." % WINGET_ID)
    code, text = (runner or _stream)([p["winget"]] + WINGET_ARGS, log, 900, cancel)
    low = text.lower()
    if code != 0 and code not in WINGET_ALREADY and "already installed" not in low:
        if "cancel" in low or "1602" in low:
            raise SetupError("The install was cancelled - Windows needs permission to install the playit service.")
        raise SetupError("winget could not install playit (exit code %s). %s" % (code, text.splitlines()[-1]
                                                                                 if text else ""))
    for _ in range(10):                  # the installer can take a moment to finish writing files
        found = find_agent()
        if found:
            log("Installed: %s" % found)
            return found
        time.sleep(1)
    raise SetupError("winget finished, but playit.exe was not found where it installs to. Restart Lodestone, "
                     "or set the agent's path in Network settings.")


def setup(agent, log, on_claim=None, cancel=None, runner=None, retries=3):
    """Run ``playit setup`` and wait for the user to approve the claim link.
    Returns "set up" or "already set up"."""
    claim = {"url": ""}

    def seen(line):
        m = PLAYIT_CLAIM_RE.search(line)
        if m and not claim["url"]:
            claim["url"] = m.group(0).rstrip(".,)")
            if on_claim:
                on_claim(claim["url"])

    for attempt in range(retries):
        code, text = (runner or _stream)([agent, "setup"], log, 1800, cancel, seen)
        low = text.lower()
        if code == 0:
            return "set up"
        if "already has a configured secret" in low:
            log("playit is already set up on this computer.")
            return "already set up"
        if "still starting" in low and attempt < retries - 1:
            log("The playit service is still starting - trying again in a few seconds.")
            time.sleep(4)
            continue
        if "not approved" in low or "rejected" in low:
            raise SetupError("The claim was not approved in the browser. Press Set up again to get a new link.")
        if "invalid secret" in low:
            raise SetupError("playit has an invalid saved key. Run `playit reset` in a terminal, then Set up again.")
        if "over the agent limit" in low:
            raise SetupError("Your playit account has reached its agent limit - remove an old agent at playit.gg.")
        last = [ln for ln in text.splitlines() if ln.strip()]
        raise SetupError("playit setup stopped (exit code %s)%s" % (code, ": " + last[-1] if last else "."))
    raise SetupError("The playit service did not get ready. Try again in a minute.")
