"""Git for the assistant and the Code page, through the git the user has installed.

Reads (status, diff, log, branches) and the few writes people actually do
from a panel (commit, branch, init, pull, push). Every call:

* runs ``git`` with an argument list - never a shell - in the workspace folder,
* never waits at a prompt: ``GIT_TERMINAL_PROMPT=0`` makes a push that needs a
  password fail with git's own message instead of hanging (git's credential
  manager, if the user set one up, still answers),
* is time-limited, and returns git's output to show as it is.

Pull is fast-forward only, so it never makes a merge commit behind anyone's
back, and nothing here force-pushes or rewrites history.

Standard library only.
"""

import os
import re
import shutil
import subprocess

TIMEOUT = 60
NET_TIMEOUT = 180
MAX_DIFF = 200_000
STATUS = {"M": "modified", "A": "added", "D": "deleted", "R": "renamed", "C": "copied", "U": "conflict",
          "?": "new", "!": "ignored", "T": "type changed"}


class GitError(RuntimeError):
    """Something to tell the user in a sentence."""


def git_exe():
    return shutil.which("git") or ""


def available():
    return bool(git_exe())


def _run(root, args, timeout=TIMEOUT, check=True, input_text=None):
    exe = git_exe()
    if not exe:
        raise GitError("Git is not installed. Get it from https://git-scm.com/downloads (on Windows: winget install "
                       "--id Git.Git), then try again.")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GCM_INTERACTIVE="never")
    try:
        r = subprocess.run([exe] + list(args), cwd=str(root), env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout, input=input_text,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0)
    except subprocess.TimeoutExpired:
        raise GitError("git %s took longer than %ds and was stopped." % (args[0], timeout))
    except OSError as e:
        raise GitError("Could not run git: %s" % e)
    if check and r.returncode != 0:
        msg = (r.stderr or r.stdout).strip()
        if "Please tell me who you are" in msg or "empty ident" in msg:
            msg = ("Git does not know who you are yet. Run once in a terminal:\n"
                   "  git config --global user.name \"Your Name\"\n"
                   "  git config --global user.email \"you@example.com\"")
        elif "terminal prompts disabled" in msg or "could not read Username" in msg:
            msg = ("The remote wants a login, and Lodestone does not type passwords. Sign in once with git in a "
                   "terminal (or set up Git Credential Manager), then try again.\n" + msg)
        raise GitError(msg or "git %s failed (exit %d)." % (args[0], r.returncode))
    return r


def is_repo(root):
    if not available() or not os.path.isdir(root):
        return False
    r = _run(root, ["rev-parse", "--is-inside-work-tree"], check=False)
    return r.returncode == 0 and r.stdout.strip() == "true"


def status(root):
    """{"repo", "branch", "upstream", "ahead", "behind", "files": [{path, index, work, label}], "clean"}"""
    if not is_repo(root):
        return {"repo": False, "git": available(), "files": [], "clean": True}
    r = _run(root, ["status", "--porcelain=v1", "-b", "-z", "--untracked-files=all"])
    parts = r.stdout.split("\0")
    out = {"repo": True, "git": True, "branch": "", "upstream": "", "ahead": 0, "behind": 0, "files": []}
    i = 0
    while i < len(parts):
        e = parts[i]
        i += 1
        if not e:
            continue
        if e.startswith("## "):
            head = e[3:]
            m = re.match(r"^(?:No commits yet on |Initial commit on )?([^.\s]+(?:\.[^.\s]+)*?)(?:\.\.\.(\S+))?"
                         r"(?: \[(.*)\])?$", head)
            if m:
                out["branch"], out["upstream"] = m.group(1), m.group(2) or ""
                for k, n in re.findall(r"(ahead|behind) (\d+)", m.group(3) or ""):
                    out[k] = int(n)
            else:
                out["branch"] = head
            continue
        x, y, path = e[0], e[1], e[3:]
        entry = {"path": path, "index": x.strip(), "work": y.strip()}
        if x in "RC":                         # a rename carries its old name as the next field
            entry["from"] = parts[i] if i < len(parts) else ""
            i += 1
        code = x if x not in " ?" else y
        entry["label"] = STATUS.get(code, code)
        entry["staged"] = x not in " ?!"
        out["files"].append(entry)
    out["clean"] = not out["files"]
    return out


def diff(root, path="", staged=False, limit=MAX_DIFF):
    args = ["diff", "--no-color", "--no-ext-diff"] + (["--cached"] if staged else [])
    if path:
        args += ["--", path]
    text = _run(root, args).stdout
    if not staged and path:
        st = status(root)
        if any(f["path"] == path and f["label"] == "new" for f in st["files"]):
            # an untracked file has no diff yet - show it as all added
            r = _run(root, ["diff", "--no-color", "--no-index", "--", os.devnull, path], check=False)
            text = r.stdout
    return text[:limit] + ("\n... [cut]\n" if len(text) > limit else "")


def log(root, n=20):
    if not is_repo(root):
        return []
    r = _run(root, ["log", "-n", str(max(1, min(200, int(n)))), "--date=iso-strict",
                    "--pretty=format:%H%x1f%h%x1f%an%x1f%ad%x1f%s"], check=False)
    if r.returncode != 0:
        return []          # no commits yet
    out = []
    for line in r.stdout.splitlines():
        p = line.split("\x1f")
        if len(p) == 5:
            out.append({"hash": p[0], "short": p[1], "author": p[2], "date": p[3], "subject": p[4]})
    return out


def branches(root):
    r = _run(root, ["branch", "--format=%(refname:short)%09%(HEAD)"], check=False)
    return [{"name": ln.split("\t")[0], "current": ln.endswith("*")} for ln in r.stdout.splitlines() if ln.strip()]


def _paths(root, files):
    """Paths inside the repository only - no options sneaking in as file names."""
    out = []
    for f in files or []:
        f = str(f).strip().replace("\\", "/")
        if not f:
            continue
        full = os.path.abspath(os.path.join(root, f))
        if os.path.commonpath([full, os.path.abspath(root)]) != os.path.abspath(root):
            raise GitError("'%s' is outside the repository." % f)
        out.append(f)
    return out


def commit(root, message, files=None):
    """Stage files (all changes when none are named) and commit them."""
    message = (message or "").strip()
    if not message:
        raise GitError("A commit needs a message.")
    if not is_repo(root):
        raise GitError("This folder is not a git repository yet - initialise it first.")
    paths = _paths(root, files)
    _run(root, ["add", "--"] + paths if paths else ["add", "-A"])
    if not _run(root, ["diff", "--cached", "--quiet"], check=False).returncode:
        raise GitError("There is nothing to commit.")
    _run(root, ["commit", "-F", "-"], input_text=message + "\n")
    head = log(root, 1)
    return head[0] if head else {}


def init(root):
    if is_repo(root):
        raise GitError("It is already a git repository.")
    _run(root, ["init"])
    gi = os.path.join(root, ".gitignore")
    if not os.path.exists(gi) and (os.path.exists(os.path.join(root, "build.gradle.kts"))
                                   or os.path.exists(os.path.join(root, "build.gradle"))
                                   or os.path.exists(os.path.join(root, "pom.xml"))):
        with open(gi, "w", encoding="utf-8") as f:
            f.write(".gradle/\nbuild/\ntarget/\n.idea/\n*.iml\nout/\nrun/\n")
    return True


def switch(root, name, create=False):
    name = (name or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", name) or name.startswith(("-", "/")) or ".." in name:
        raise GitError("'%s' is not a branch name git accepts." % name)
    _run(root, ["switch", "-c", name] if create else ["switch", name])
    return name


def pull(root):
    return _run(root, ["pull", "--ff-only"], timeout=NET_TIMEOUT).stdout.strip() or "Up to date."


def push(root):
    st = status(root)
    args = ["push"] if st.get("upstream") else ["push", "-u", "origin", st.get("branch") or "HEAD"]
    r = _run(root, args, timeout=NET_TIMEOUT)
    return (r.stdout + r.stderr).strip() or "Pushed."
