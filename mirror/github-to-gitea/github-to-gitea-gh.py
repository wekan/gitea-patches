#!/usr/bin/env python3
"""gh for Gitea Actions: the subset of GitHub CLI that WeKan's workflows and
releases/*.sh run, answered by the Gitea API of the server the job runs on.

github-to-gitea.py puts this file in the gitea-actions branch as .gitea/gh, and
the first step of every converted job puts it first on PATH. The repository the job
runs for (GITHUB_REPOSITORY) is read from and written to on THIS Gitea; any other
repository (wekan/FerretDB, wekan/node-patches, ...) is read from github.com, and
writing to one of those is refused - a local build never publishes to GitHub.

  gh release view [TAG] [--json F,..] [--jq|-q EXPR]
  gh release list [--limit N] [--exclude-drafts] [--exclude-pre-releases] [--json F,..] [--jq EXPR]
  gh release download TAG [--pattern GLOB].. [--dir D] [--output F] [--clobber] [--skip-existing]
  gh release upload TAG FILE[#label].. [--clobber]
  gh release create TAG [--verify-tag] [--title T] [--notes N|--notes-file F] [--prerelease] [--draft] [--target REF]
  gh release edit TAG [--title T] [--notes N|--notes-file F] [--prerelease[=B]] [--draft[=B]]
  gh workflow run FILE [--ref REF] [-f KEY=VALUE]..
  gh auth status

Every command also takes --repo/-R OWNER/REPO. The local token is LOCAL_GITEA_TOKEN
(the converted workflows set it to github.token); GH_TOKEN is used only for github.com.
"""

import fnmatch
import json
import mimetypes
import os
import shutil
import subprocess
import sys
import time
import threading
import urllib.error
import urllib.parse
import urllib.request
import uuid

FIELDS = ("tagName", "name", "body", "isDraft", "isPrerelease", "url", "publishedAt",
          "createdAt", "targetCommitish", "assets", "id")


def die(msg, code=1):
    print(f"gh (Gitea): {msg}", file=sys.stderr)
    sys.exit(code)


class Target:
    def __init__(self, repo):
        here = os.environ.get("GITHUB_REPOSITORY", "")
        self.repo = repo or here
        if not self.repo:
            die("no repository: give --repo or set GITHUB_REPOSITORY")
        extra = [r.strip().lower() for r in os.environ.get("GH_SHIM_LOCAL_REPOS", "").split(",") if r.strip()]
        self.local = self.repo.lower() in [here.lower()] + extra
        if self.local:
            server = os.environ.get("GITHUB_SERVER_URL", "").rstrip("/")
            self.server = server
            self.api = (os.environ.get("GITHUB_API_URL") or server + "/api/v1").rstrip("/")
            self.token = os.environ.get("LOCAL_GITEA_TOKEN") or os.environ.get("GITEA_TOKEN") or ""
        else:
            self.server = "https://github.com"
            self.api = "https://api.github.com"
            self.token = os.environ.get("GH_TOKEN") or ""
            if self.token and self.token == os.environ.get("LOCAL_GITEA_TOKEN"):
                self.token = ""  # never send the Gitea token to GitHub: it answers 401

    def headers(self, extra=None):
        h = {"Accept": "application/json", "User-Agent": "gh-gitea-shim"}
        if self.token:
            h["Authorization"] = f"token {self.token}"
        h.update(extra or {})
        return h

    def call(self, method, path, body=None, data=None, ctype=None, length=None, ok=(200, 201, 204)):
        url = path if path.startswith("http") else f"{self.api}/repos/{self.repo}{path}"
        extra = {}
        if body is not None:
            data = json.dumps(body).encode()
            extra["Content-Type"] = "application/json"
        elif ctype:
            extra["Content-Type"] = ctype
        if length is not None:
            extra["Content-Length"] = str(length)
        req = urllib.request.Request(url, data=data, method=method, headers=self.headers(extra))
        try:
            with urllib.request.urlopen(req, timeout=None) as r:
                text = r.read()
                return r.status, (json.loads(text) if text.strip() else None)
        except urllib.error.HTTPError as e:
            text = e.read()
            if e.code in ok:
                return e.code, None
            try:
                return e.code, json.loads(text)
            except ValueError:
                return e.code, text.decode(errors="replace")[:300]

    def need_write(self):
        if not self.local:
            die(f"refusing to write to {self.repo}: only {os.environ.get('GITHUB_REPOSITORY')} "
                f"on this Gitea is written by a local build")


def normalise(r, t):
    """A Gitea or GitHub release as gh --json names it."""
    return {
        "id": r.get("id"),
        "tagName": r.get("tag_name"),
        "name": r.get("name") or "",
        "body": r.get("body") or "",
        "isDraft": bool(r.get("draft")),
        "isPrerelease": bool(r.get("prerelease")),
        "url": r.get("html_url") or "",
        "publishedAt": r.get("published_at") or "",
        "createdAt": r.get("created_at") or "",
        "targetCommitish": r.get("target_commitish") or "",
        "assets": [{"name": a.get("name"), "size": a.get("size"), "id": a.get("id"),
                    "url": a.get("browser_download_url"),
                    "downloadCount": a.get("download_count", 0)} for a in r.get("assets") or []],
    }


def get_release(t, tag):
    path = "/releases/latest" if not tag else f"/releases/tags/{urllib.parse.quote(tag, safe='')}"
    s, r = t.call("GET", path)
    if s != 200:
        die(f"release not found: {tag or 'latest'} in {t.repo}")
    return r


def list_releases(t, limit):
    out, page = [], 1
    per = 50 if t.local else 100
    while len(out) < limit:
        q = "limit" if t.local else "per_page"
        s, d = t.call("GET", f"/releases?{q}={per}&page={page}")
        if s != 200 or not d:
            break
        out += d
        if len(d) < per:
            break
        page += 1
    return out[:limit]


def jq(value, expr):
    text = json.dumps(value)
    if shutil.which("jq"):
        r = subprocess.run(["jq", "-r", expr], input=text, text=True, capture_output=True)
        if r.returncode:
            die(f"jq: {r.stderr.strip()}")
        sys.stdout.write(r.stdout)
        return
    # The filters WeKan uses, for a runner without jq.
    if expr == ".assets[].name":
        print("\n".join(a["name"] for a in value.get("assets", [])))
    elif expr == ".assets[] | [.name, .size] | @tsv":
        print("\n".join(f"{a['name']}\t{a['size']}" for a in value.get("assets", [])))
    elif expr.startswith(".[0].") and isinstance(value, list):
        print(value[0].get(expr[5:], "") if value else "")
    elif expr.startswith(".") and "." not in expr[1:] and isinstance(value, dict):
        v = value.get(expr[1:])
        print("" if v is None else (json.dumps(v) if isinstance(v, (dict, list)) else v))
    else:
        die(f"jq is not installed, and the filter {expr!r} needs it")


def emit(value, fields, expr):
    if fields:
        keep = [f for f in fields.split(",") if f]
        for f in keep:
            if f not in FIELDS:
                die(f"unknown JSON field: {f}")
        value = [{k: v[k] for k in keep} for v in value] if isinstance(value, list) else {k: value[k] for k in keep}
    if expr:
        jq(value, expr)
    elif fields:
        print(json.dumps(value, indent=2))
    elif isinstance(value, list):
        for r in value:
            print("\t".join([r["name"] or r["tagName"], "Draft" if r["isDraft"] else ("Pre-release" if r["isPrerelease"] else ""),
                             r["tagName"], r["publishedAt"]]))
    else:
        print(f"title:\t{value['name']}\ntag:\t{value['tagName']}\ndraft:\t{str(value['isDraft']).lower()}\n"
              f"prerelease:\t{str(value['isPrerelease']).lower()}\nurl:\t{value['url']}\n--")
        print(value["body"])
        for a in value["assets"]:
            print(f"asset:\t{a['name']}")


def download_url(t, asset_url):
    """Gitea writes asset URLs with its ROOT_URL, which a job container may not reach;
    the job's GITHUB_SERVER_URL is the address that works from where it runs."""
    if t.local and t.server and "/releases/download/" in (asset_url or ""):
        return t.server + "/" + t.repo + "/releases/download/" + asset_url.split("/releases/download/", 1)[1]
    return asset_url


def fetch(t, url, dest):
    req = urllib.request.Request(url, headers=t.headers({"Accept": "application/octet-stream"}))
    tmp = dest + ".part"
    with urllib.request.urlopen(req, timeout=None) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1 << 20)
    os.replace(tmp, dest)


class Multipart:
    def __init__(self, path, name):
        b = uuid.uuid4().hex
        self.ctype = f"multipart/form-data; boundary={b}"
        head = (f"--{b}\r\nContent-Disposition: form-data; name=\"attachment\"; filename=\"{name}\"\r\n"
                f"Content-Type: {mimetypes.guess_type(name)[0] or 'application/octet-stream'}\r\n\r\n").encode()
        tail = f"\r\n--{b}--\r\n".encode()
        self.parts = [head, open(path, "rb"), tail]
        self.length = len(head) + os.path.getsize(path) + len(tail)

    def read(self, n=-1):
        while self.parts:
            p = self.parts[0]
            if isinstance(p, bytes):
                self.parts.pop(0)
                if p:
                    return p
                continue
            chunk = p.read(n if n and n > 0 else 1 << 20)
            if chunk:
                return chunk
            p.close()
            self.parts.pop(0)
        return b""


def parse(argv, flags_with_value, multi=()):
    opts, pos, i = {k: [] for k in multi}, [], 0
    while i < len(argv):
        a = argv[i]
        if a.startswith("-") and a != "-":
            key, eq, val = a.partition("=")
            key = {"-R": "--repo", "-q": "--jq", "-f": "--field", "-F": "--field", "-n": "--notes",
                   "-t": "--title", "-D": "--dir", "-O": "--output",
                   "-L": "--limit"}.get(key, key)
            if key in flags_with_value:
                if not eq:
                    i += 1
                    if i >= len(argv):
                        die(f"{key} needs a value")
                    val = argv[i]
                if key in multi:
                    opts[key].append(val)
                else:
                    opts[key] = val
            else:
                opts[key] = val if eq else True
        else:
            pos.append(a)
        i += 1
    return opts, pos


def boolish(v):
    return v is True or str(v).lower() in ("true", "1", "yes")


def release(cmd, argv):
    valued = {"--repo", "--json", "--jq", "--limit", "--pattern", "--dir", "--output", "--title",
              "--notes", "--notes-file", "--target", "--template"}
    o, pos = parse(argv, valued, multi=("--pattern",))
    t = Target(o.get("--repo"))
    tag = pos[0] if pos else None

    if cmd == "view":
        emit(normalise(get_release(t, tag), t), o.get("--json"), o.get("--jq"))
    elif cmd == "list":
        rels = [normalise(r, t) for r in list_releases(t, int(o.get("--limit", 30)) * 2 + 10)]
        if o.get("--exclude-drafts"):
            rels = [r for r in rels if not r["isDraft"]]
        if o.get("--exclude-pre-releases"):
            rels = [r for r in rels if not r["isPrerelease"]]
        emit(rels[:int(o.get("--limit", 30))], o.get("--json"), o.get("--jq"))
    elif cmd == "download":
        r = normalise(get_release(t, tag), t)
        pats = o["--pattern"] or ["*"]
        chosen = [a for a in r["assets"] if any(fnmatch.fnmatchcase(a["name"], p) for p in pats)]
        if not chosen:
            die("no assets match the file pattern")
        out_dir = o.get("--dir", ".")
        os.makedirs(out_dir, exist_ok=True)
        for a in chosen:
            dest = o["--output"] if o.get("--output") and len(chosen) == 1 else os.path.join(out_dir, a["name"])
            if os.path.exists(dest):
                if o.get("--skip-existing"):
                    continue
                if not o.get("--clobber"):
                    die(f"{dest} already exists (use --clobber to overwrite)")
            fetch(t, download_url(t, a["url"]), dest)
    elif cmd == "upload":
        t.need_write()
        if not pos[1:]:
            die("upload needs a tag and at least one file")
        r = get_release(t, tag)
        have = {a["name"]: a["id"] for a in r.get("assets") or []}
        for spec in pos[1:]:
            path = spec.split("#", 1)[0]
            name = os.path.basename(path)
            if not os.path.isfile(path):
                die(f"{path}: no such file")
            if name in have:
                if not o.get("--clobber"):
                    die(f"asset under the same name already exists: [{name}]")
                t.call("DELETE", f"/releases/{r['id']}/assets/{have[name]}")
            body = Multipart(path, name)
            s, d = t.call("POST", f"/releases/{r['id']}/assets?name={urllib.parse.quote(name)}",
                          data=body, ctype=body.ctype, length=body.length)
            if s != 201:
                die(f"upload of {name} failed: HTTP {s} {d}")
            print(f"Uploaded {name}", file=sys.stderr)
    elif cmd in ("create", "edit"):
        t.need_write()
        if not tag:
            die(f"{cmd} needs a tag")
        notes = o.get("--notes")
        if o.get("--notes-file"):
            notes = sys.stdin.read() if o["--notes-file"] == "-" else open(o["--notes-file"], encoding="utf-8").read()
        body = {}
        if o.get("--title") is not None:
            body["name"] = o["--title"]
        if notes is not None:
            body["body"] = notes
        for flag, key in (("--prerelease", "prerelease"), ("--draft", "draft")):
            if flag in o:
                body[key] = boolish(o[flag])
        if cmd == "create":
            s, _ = t.call("GET", f"/tags/{urllib.parse.quote(tag, safe='')}")
            if o.get("--verify-tag") and s != 200:
                die(f"tag {tag} doesn't exist in the repo, aborting due to --verify-tag flag")
            body.setdefault("name", tag)
            body.update(tag_name=tag, target_commitish=o.get("--target") or "main")
            s, d = t.call("POST", "/releases", body)
            if s != 201:
                die(f"create failed: HTTP {s} {d}")
            print(d.get("html_url", ""))
        else:
            r = get_release(t, tag)
            s, d = t.call("PATCH", f"/releases/{r['id']}", body)
            if s != 200:
                die(f"edit failed: HTTP {s} {d}")
            print(d.get("html_url", ""))
    else:
        die(f"gh release {cmd} is not supported on Gitea")


def workflow(cmd, argv):
    if cmd != "run":
        die(f"gh workflow {cmd} is not supported on Gitea")
    o, pos = parse(argv, {"--repo", "--ref", "--field", "--raw-field"}, multi=("--field", "--raw-field"))
    t = Target(o.get("--repo"))
    t.need_write()
    if not pos:
        die("workflow run needs a workflow file name")
    inputs = dict(f.split("=", 1) for f in o["--field"] + o["--raw-field"])
    ref = o.get("--ref") or os.environ.get("GITHUB_REF_NAME") or "main"
    s, d = t.call("POST", f"/actions/workflows/{urllib.parse.quote(pos[0])}/dispatches",
                  {"ref": ref, "inputs": inputs})
    if s not in (200, 201, 204):
        die(f"dispatch failed: HTTP {s} {d}")
    print(f"Created workflow_dispatch event for {pos[0]} at {ref}", file=sys.stderr)



# --- the screen's log ---------------------------------------------------------------------

def mirror_log_dir():
    """<WeKan>/.tools/log/mirror: the WeKan checkout is at a fixed place on each operating
    system - ~/repos/wekan on Linux, ~/Documents/repos/wekan on macOS and
    Downloads/repos/wekan under the user's profile on Windows. MIRROR_LOG_DIR overrides it."""
    if os.environ.get("MIRROR_LOG_DIR"):
        return os.environ["MIRROR_LOG_DIR"]
    home = os.path.expanduser("~")
    if sys.platform.startswith("win"):
        wekan = os.path.join(os.environ.get("USERPROFILE", home), "Downloads", "repos", "wekan")
    elif sys.platform == "darwin":
        wekan = os.path.join(home, "Documents", "repos", "wekan")
    else:
        wekan = os.path.join(home, "repos", "wekan")
    return os.path.join(wekan, ".tools", "log", "mirror")


def start_screen_log(here, name):
    """Everything this run shows on the screen - its own lines, its prompts and the
    output of the git and other programs it runs - also goes to
    <WeKan>/.tools/log/mirror/<name>/YYYY-MM-DD_HH-MM-SS.txt (mirror_log_dir()). Copied
    at the file descriptors, so a child process's messages are in it too, and flushed
    to the file before the program ends."""
    import atexit
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    d = os.path.join(mirror_log_dir(), name)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{stamp}.txt")
    n = 0
    while os.path.exists(path):
        n += 1
        path = os.path.join(d, f"{stamp}-{n}.txt")
    out = open(path, "ab", buffering=0)
    lock = threading.Lock()
    saved, threads = [], []
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
            fd = stream.fileno()
        except (AttributeError, OSError, ValueError):
            continue
        original = os.dup(fd)
        r, w = os.pipe()
        os.dup2(w, fd)
        os.close(w)

        def copy(r=r, original=original):
            while True:
                try:
                    data = os.read(r, 65536)
                except OSError:
                    break
                if not data:
                    break
                with lock:
                    try:
                        os.write(original, data)
                    except OSError:
                        pass
                    out.write(data)
            os.close(r)

        try:  # a pipe is block-buffered: keep Python's lines in step with its children's
            stream.reconfigure(line_buffering=True)
        except (AttributeError, ValueError):
            pass
        t = threading.Thread(target=copy, daemon=True)
        t.start()
        saved.append((fd, original))
        threads.append(t)

    def finish():
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (OSError, ValueError):
                pass
        for fd, original in saved:
            os.dup2(original, fd)  # closes the pipe's last writer: the copier sees the end
        for t in threads:
            t.join(timeout=5)
        out.close()

    atexit.register(finish)
    print(f"Log: {path}", flush=True)
    return path


def main(argv):
    start_screen_log(os.path.dirname(os.path.abspath(__file__)), "github-to-gitea-gh")
    if len(argv) < 2:
        die(__doc__.strip(), 0)
    group, cmd, rest = argv[0], argv[1], argv[2:]
    if group == "release":
        release(cmd, rest)
    elif group == "workflow":
        workflow(cmd, rest)
    elif group == "auth" and cmd == "status":
        t = Target(None)
        s, d = t.call("GET", f"{t.api}/user")
        if s != 200 and not (t.local and s in (401, 403) and t.token):
            die(f"not logged in to {t.server}: HTTP {s}")
        print(f"Logged in to {t.server} (Gitea Actions token)", file=sys.stderr)
    else:
        die(f"gh {' '.join(argv[:2])} is not supported on Gitea (the shim covers gh release, "
            f"gh workflow run and gh auth status)")


if __name__ == "__main__":
    main(sys.argv[1:])
