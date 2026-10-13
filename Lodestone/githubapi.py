"""Reading GitHub for the assistant: finding repositories (and code), and reading
a repository's README, folders, files and releases - through GitHub's public
REST API, which is free.

Without a token GitHub allows 60 requests an hour and no code search; with a
personal access token (any, even one with no scopes - https://github.com/settings/tokens)
it is 5000 an hour and code search works. The token is the user's to give and
is only ever sent to api.github.com.

What comes back is other people's text, marked so for the model, like web pages.

Standard library only.
"""

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
UA = "Lodestone (+https://github.com/armadillo122c-blip/ServerCreator-V1)"
REPO_RE = re.compile(r"^[A-Za-z0-9-]{1,39}/[A-Za-z0-9._-]{1,100}$")
MAX_FILE = 120_000


class GitHubError(RuntimeError):
    """Something to tell the user in a sentence."""


def _get(path, token="", base=API, params=None, accept="application/vnd.github+json"):
    url = base.rstrip("/") + path + ("?" + urllib.parse.urlencode(params) if params else "")
    h = {"User-Agent": UA, "Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read(4_000_000).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = json.loads(e.read(4000).decode("utf-8", "replace")).get("message", "")
        except (ValueError, AttributeError):
            pass
        if e.code == 403 and "rate limit" in body.lower():
            raise GitHubError("GitHub's limit for requests without a token is used up for this hour. Add a GitHub "
                              "token in AI Assistant > Model & resources (any token works, no scopes needed).")
        if e.code == 401:
            raise GitHubError("GitHub did not accept the token - check it in AI Assistant > Model & resources.")
        if e.code == 404:
            raise GitHubError("GitHub has nothing at %s (or it is private)." % path)
        raise GitHubError("GitHub answered %d%s" % (e.code, ": " + body if body else ""))
    except urllib.error.URLError as e:
        raise GitHubError("Could not reach GitHub: %s" % e.reason)
    except (TimeoutError, OSError, ValueError) as e:
        raise GitHubError("Could not read GitHub's answer: %s" % e)


def _repo(repo):
    repo = (repo or "").strip()
    m = re.match(r"^(?:https?://github\.com/)?([^/\s]+/[^/\s#?]+?)(?:\.git)?/?$", repo)
    repo = m.group(1) if m else repo
    if not REPO_RE.match(repo):
        raise GitHubError("Name a repository as owner/name, like PaperMC/Paper.")
    return repo


def search_repos(query, token="", n=8, base=API):
    query = (query or "").strip()
    if not query:
        raise GitHubError("Nothing to search for.")
    data = _get("/search/repositories", token, base, {"q": query, "per_page": max(1, min(20, int(n or 8)))})
    return [{"repo": r.get("full_name"), "description": (r.get("description") or "")[:300],
             "stars": r.get("stargazers_count", 0), "language": r.get("language") or "",
             "updated": (r.get("pushed_at") or "")[:10], "url": r.get("html_url"),
             "license": ((r.get("license") or {}).get("spdx_id") or ""), "archived": bool(r.get("archived"))}
            for r in data.get("items") or []]


def search_code(query, token="", n=10, base=API):
    if not token:
        raise GitHubError("GitHub only searches code for signed-in requests - add a GitHub token in AI Assistant > "
                          "Model & resources (any token works, no scopes needed). Searching repositories works "
                          "without one.")
    data = _get("/search/code", token, base, {"q": query, "per_page": max(1, min(20, int(n or 10)))})
    return [{"repo": (i.get("repository") or {}).get("full_name"), "path": i.get("path"), "url": i.get("html_url")}
            for i in data.get("items") or []]


def read(repo, path="", ref="", token="", base=API):
    """A file's text, a folder's listing, or (no path) the README and the top folder."""
    repo = _repo(repo)
    path = (path or "").strip().strip("/")
    if ".." in path.split("/"):
        raise GitHubError("Paths go down from the repository's top, not up.")
    params = {"ref": ref} if ref else None
    if not path:
        info = _get("/repos/%s" % repo, token, base)
        out = {"repo": repo, "kind": "repo", "description": info.get("description") or "",
               "default_branch": info.get("default_branch"), "stars": info.get("stargazers_count", 0),
               "license": ((info.get("license") or {}).get("spdx_id") or ""), "url": info.get("html_url")}
        try:
            rd = _get("/repos/%s/readme" % repo, token, base, params)
            out["readme"] = _decode(rd)[:MAX_FILE]
        except GitHubError:
            out["readme"] = ""
        out["files"] = _listing(_get("/repos/%s/contents" % repo, token, base, params))
        return out
    data = _get("/repos/%s/contents/%s" % (repo, urllib.parse.quote(path)), token, base, params)
    if isinstance(data, list):
        return {"repo": repo, "kind": "folder", "path": path, "files": _listing(data)}
    if data.get("type") != "file":
        raise GitHubError("%s is a %s, not a file." % (path, data.get("type")))
    if data.get("size", 0) > 1_000_000:
        raise GitHubError("%s is too big to read here (%d bytes)." % (path, data["size"]))
    text = _decode(data)
    return {"repo": repo, "kind": "file", "path": path, "text": text[:MAX_FILE], "truncated": len(text) > MAX_FILE,
            "url": data.get("html_url")}


def releases(repo, token="", n=5, base=API):
    repo = _repo(repo)
    data = _get("/repos/%s/releases" % repo, token, base, {"per_page": max(1, min(20, int(n or 5)))})
    return [{"tag": r.get("tag_name"), "name": r.get("name") or "", "date": (r.get("published_at") or "")[:10],
             "prerelease": bool(r.get("prerelease")), "notes": (r.get("body") or "")[:1500],
             "assets": [{"name": a.get("name"), "size": a.get("size"), "url": a.get("browser_download_url"),
                         "sha256": (a.get("digest") or "").replace("sha256:", "")} for a in r.get("assets") or []]}
            for r in data]


def _decode(item):
    if item.get("encoding") == "base64":
        raw = base64.b64decode(item.get("content") or "")
        if b"\0" in raw[:4096]:
            raise GitHubError("%s is not a text file." % item.get("path", "that"))
        return raw.decode("utf-8", "replace")
    return item.get("content") or ""


def _listing(items):
    return [("%s/" % i["name"]) if i.get("type") == "dir" else i["name"] for i in items or []][:300]
