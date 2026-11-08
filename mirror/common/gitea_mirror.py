"""Shared engine of gitea-to-gitlab.py and gitea-to-sourceforge.py: mirror repositories
from a local Gitea to another forge - git branches and tags, the wiki, issues and pull
requests with their comments, labels and milestones, and releases with their files.

A target script defines a Target (how to talk to GitLab or SourceForge) and calls
main(). Everything else - the menu, the settings file, sources, state and resume,
rate limits, logging, the read-only checks - is here, so both behave the same way.

WHAT IS COPIED, AND HOW IT STAYS IN STEP
  git       branches and tags, pushed WITHOUT force and without deleting anything: a
            destination branch that has moved its own way is reported, never
            overwritten. The wiki likewise, where the target has a git wiki.
  issues    issues, and pull requests as issues titled "[PR] ...", with their comments,
            labels, milestones and closed state. Each carries the marker
            <!-- wekan-mirror:URL --> that WeKan's releases/mirror.sh writes too, so an
            item already copied - by this script or by WeKan's mirror from GitHub - is
            recognised and never copied twice. For a repository Gitea migrated from
            GitHub, URL is the GitHub address of the issue, exactly what WeKan's mirror
            used. Only what is missing is added; nothing at the destination is removed.
  releases  by default only the NEWEST release - its notes and its files - and the
            files of older releases are deleted from the destination (setting
            release_assets_keep: newest, all, or a number of newest releases). The
            repositories in the menu's "Releases kept in full" list - (all),
            (allpublic), (allprivate), an organization or user, or owner/name - get
            every release, notes and files, and keep them all. A file is downloaded
            alone to WeKan's .tools/tmp/<script>/, copied, and deleted.
  avatars   the avatar of each Gitea organization or user onto its destination, and a
            repository's avatar onto the repository there - uploaded again only when
            the image changes.

STATE AND RESUME
  <name>-state.json remembers which destination item each source item became, when each
  repository's issues were last synced (the next run reads only what changed since), and
  the progress of the current run: an interrupted run (Ctrl-C, crash, reboot) is resumed
  by the next run of the same steps and sources, skipping finished repositories.
  Repositories that failed are tried again.
"""

import argparse
import base64
import configparser
import datetime
import hashlib
import json
import mimetypes
import os
import re
import threading
import shutil
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid

STEPS = ["git", "issues", "releases", "avatars"]
STEP_NAMES = {"git": "git branches and tags, and the wiki (pushed without force)",
              "issues": "issues and pull requests with comments, labels, milestones",
              "releases": "releases, their notes and files",
              "avatars": "avatars of organizations, users and repositories"}
SPECIAL = {"(all)": None, "(allpublic)": False, "(allprivate)": True}
SPECIAL_NAMES = {"(all)": "every private and public repository the Gitea token can read",
                 "(allpublic)": "every public repository the Gitea token can read",
                 "(allprivate)": "every private repository the Gitea token can read"}
ATTRIB = re.compile(r"^> \*(?:From GitHub|GitHubista): @([^,*]+), ([0-9-]+), (\S+?)\*\n\n")


class RepoError(Exception):
    """Processing of one repository stops; the next one goes on."""


def fail(msg):
    raise RepoError(msg)


def log(msg=""):
    print(msg, flush=True)


def now_iso():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_segment(text):
    """A file or directory name that is safe on every file system and in sftp."""
    s = re.sub(r"[^A-Za-z0-9._+-]", "_", str(text)).lstrip(".")
    return s[:200] or "_"


def normalize(text):
    """Text compared without links and whitespace, for recognising a comment that was
    copied without a marker (WeKan's mirror rewrites links)."""
    return " ".join(re.sub(r"https?://\S+", " ", text or "").split())


# --- logging, like WeKan's releases/mirror.sh: everything also goes to a run log ----------

def wekan_tools_dir():
    """<WeKan>/.tools: the WeKan checkout is at a fixed place on each operating system -
    ~/repos/wekan on Linux, ~/Documents/repos/wekan on macOS and Downloads/repos/wekan
    under the user's profile on Windows."""
    home = os.path.expanduser("~")
    if sys.platform.startswith("win"):
        wekan = os.path.join(os.environ.get("USERPROFILE", home), "Downloads", "repos", "wekan")
    elif sys.platform == "darwin":
        wekan = os.path.join(home, "Documents", "repos", "wekan")
    else:
        wekan = os.path.join(home, "repos", "wekan")
    return os.path.join(wekan, ".tools")


def mirror_log_dir():
    """<WeKan>/.tools/log/mirror. MIRROR_LOG_DIR overrides it."""
    if os.environ.get("MIRROR_LOG_DIR"):
        return os.environ["MIRROR_LOG_DIR"]
    return os.path.join(wekan_tools_dir(), "log", "mirror")


def mirror_tmp_dir(name):
    """<WeKan>/.tools/tmp/<script>: where a release file is while it is copied - never
    /tmp, which may be a small tmpfs. MIRROR_TMP_DIR overrides it."""
    return os.environ.get("MIRROR_TMP_DIR") or os.path.join(wekan_tools_dir(), "tmp", name)


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
    shared = {}  # (device, inode) of a destination -> the pipe's write end going to it
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
            fd = stream.fileno()
            dest = os.fstat(fd)
        except (AttributeError, OSError, ValueError):
            continue
        original = os.dup(fd)
        key = (dest.st_dev, dest.st_ino)
        if key in shared:
            # stderr goes where stdout goes - the terminal, usually: through the SAME
            # pipe, so a child's "out" then "err" stay in that order. Two pipes, read by
            # two threads, could swap them.
            os.dup2(shared[key], fd)
            saved.append((fd, original))
            try:
                stream.reconfigure(line_buffering=True)
            except (AttributeError, ValueError):
                pass
            continue
        r, w = os.pipe()
        os.dup2(w, fd)
        os.close(w)
        shared[key] = fd

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


def start_log(here, name):
    return start_screen_log(here, name)


# --- HTTP -------------------------------------------------------------------------------

class FileBody:
    """A request body read from disk in pieces: release files are gigabytes."""

    def __init__(self, path, field=None, name=None):
        self.f = open(path, "rb")
        size = os.path.getsize(path)
        if field is None:  # raw upload
            self.ctype, self.parts, self.length = "application/octet-stream", [self.f], size
            return
        b = uuid.uuid4().hex
        name = name or os.path.basename(path)
        head = (f"--{b}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{name}\"\r\n"
                f"Content-Type: {mimetypes.guess_type(name)[0] or 'application/octet-stream'}\r\n\r\n").encode()
        tail = f"\r\n--{b}--\r\n".encode()
        self.ctype, self.parts, self.length = f"multipart/form-data; boundary={b}", [head, self.f, tail], len(head) + size + len(tail)

    def read(self, n=-1):
        while self.parts:
            p = self.parts[0]
            if isinstance(p, bytes):
                if not p:
                    self.parts.pop(0)
                    continue
                k = len(p) if n is None or n < 0 else n
                self.parts[0] = p[k:]
                return p[:k]
            chunk = p.read(n if n and n > 0 else 1 << 20)
            if chunk:
                return chunk
            p.close()
            self.parts.pop(0)
        return b""


def rate_wait(headers, waits):
    """Seconds to wait when a forge says "too many requests", 0 otherwise."""
    after = headers.get("Retry-After") if headers else None
    if after and after.strip().isdigit():
        return int(after) + 1
    for key in ("RateLimit-Reset", "X-RateLimit-Reset"):
        v = headers.get(key) if headers else None
        if v and v.isdigit():
            v = int(v)
            return max(5, v - int(time.time()) + 5) if v > 10 ** 9 else v + 1
    return min(60 * 2 ** waits, 900)


def multipart_form(form):
    """(body bytes, Content-Type) of a multipart/form-data form of text fields."""
    b = uuid.uuid4().hex
    out = b"".join(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n".encode()
                   + str(v).encode() + b"\r\n" for k, v in form.items())
    return out + f"--{b}--\r\n".encode(), f"multipart/form-data; boundary={b}"


def http(method, url, headers=None, json_body=None, form=None, body=None, timeout=600, retry=None, multipart=False):
    """(status, decoded answer, response headers). A 429 - or a 403 that says it is a rate
    limit - is waited out and does not count as an attempt. Errors of the server or the
    network are retried only for requests that can be repeated safely (GET, HEAD, PUT,
    DELETE): a POST that timed out may already have created something, and the next run
    finds it by its marker instead."""
    retry = method in ("GET", "HEAD", "PUT", "DELETE") if retry is None else retry
    h = {"Accept": "application/json", "User-Agent": "gitea-mirror"}
    h.update(headers or {})
    data = None
    if json_body is not None:
        data = json.dumps(json_body).encode()
        h["Content-Type"] = "application/json"
    elif form is not None and multipart:
        data, h["Content-Type"] = multipart_form(form)
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        h["Content-Type"] = "application/x-www-form-urlencoded"
    elif body is not None:
        data = body
        h["Content-Type"] = body.ctype
        h["Content-Length"] = str(body.length)
    attempt = waits = 0
    while True:
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                text = r.read()
                try:
                    return r.status, (json.loads(text) if text.strip() else None), r.headers
                except ValueError:
                    return r.status, text.decode(errors="replace"), r.headers
        except urllib.error.HTTPError as e:
            text = e.read()
            limited = e.code == 429 or (e.code == 403 and (b"rate limit" in text.lower()
                                                            or e.headers.get("X-RateLimit-Remaining") == "0"))
            if limited and waits < 30 and not isinstance(body, FileBody):
                wait = rate_wait(e.headers, waits)
                waits += 1
                log(f"  {urllib.parse.urlsplit(url).netloc}: rate limit, waiting {wait} s...")
                time.sleep(wait)
                continue
            if e.code >= 500 and retry and attempt < 5 and not isinstance(body, FileBody):
                attempt += 1
                time.sleep(3 * attempt)
                continue
            try:
                return e.code, json.loads(text), e.headers
            except ValueError:
                return e.code, text[:500].decode(errors="replace"), e.headers
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if retry and attempt < 5 and not isinstance(body, FileBody):
                attempt += 1
                time.sleep(3 * attempt)
                continue
            return 0, str(e), {}


def message(data):
    return data.get("message") or data.get("error") or json.dumps(data)[:300] if isinstance(data, dict) else str(data)[:300]


# --- settings ----------------------------------------------------------------------------

COMMON_SETTINGS = [
    ("gitea", "url", "http://localhost:2000", "address of the local Gitea"),
    ("gitea", "token", "", "Gitea token (read access to the repositories; admin for (all))"),
    ("gitea", "public_url", "", "address of the same Gitea for readers, used in links (empty = url)"),
    ("options", "steps", ", ".join(STEPS), "steps that 'Run every step' runs"),
    ("options", "dry_run", "no", "dry run: show what would be done, change nothing"),
    ("options", "include_forks", "yes", "mirror forks"),
    ("options", "include_archived", "yes", "mirror archived repositories"),
    ("options", "wiki", "yes", "mirror the wiki, where the target has a git wiki"),
    ("options", "assets_max_mb", "-1", "largest release file to copy, in MB; -1 = no limit, 0 = no files"),
    ("options", "release_assets_keep", "newest", "releases synced, with their files, outside 'Releases kept in full': "
                                                 "newest | all | N (the N newest); older ones lose their files"),
]


class Config:
    def __init__(self, path, header, settings):
        self.path, self.header, self.settings = path, header, settings
        self.cfg = configparser.ConfigParser(interpolation=None)
        self.cfg.optionxform = str  # owner names keep their case

    def load(self):
        sections = {s for s, *_ in self.settings} | {"sources", "destinations", "releases_all"}
        self.cfg.read_dict({s: {} for s in sections})
        if os.path.exists(self.path):
            self.cfg.read(self.path, encoding="utf-8")
        for section in ("sources", "releases_all"):
            if not self.cfg.has_option(section, "list"):
                self.cfg.set(section, "list", "")
        added = False
        # assets_max_mb was 5000 by default; then 0 meant no limit for a while. Now -1
        # is no limit, the default, as in Gitea's FILE_MAX_SIZE, and 0 is a largest
        # size of 0: no file is copied. A file written with 5000 or 0 gets -1, once
        # (the [migrations] line), so a value chosen after that stays.
        if not self.cfg.has_option("migrations", "assets_max_mb_no_limit"):
            if self.cfg.get("options", "assets_max_mb", fallback="").strip() in ("5000", "0"):
                self.cfg.set("options", "assets_max_mb", "-1")
            if not self.cfg.has_section("migrations"):
                self.cfg.add_section("migrations")
            self.cfg.remove_option("migrations", "assets_max_mb_unlimited")
            self.cfg.set("migrations", "assets_max_mb_no_limit", "done")
            added = True
        for s, k, d, _ in self.settings:
            if not self.cfg.has_option(s, k):
                self.cfg.set(s, k, d)
                added = True
        if added or not os.path.exists(self.path):
            self.save()

    def save(self):
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(self.header + "\n")
            self.cfg.write(f)
        os.replace(tmp, self.path)

    def get(self, key, section="options"):
        default = next((d for s, k, d, _ in self.settings if s == section and k == key), "")
        return self.cfg.get(section, key, fallback=default)

    def set(self, section, key, value):
        self.cfg.set(section, key, value)
        self.save()

    def flag(self, key):
        return self.get(key).strip().lower() in ("yes", "true", "1", "on", "y")

    def sources(self):
        return [s.strip() for s in self.cfg.get("sources", "list", fallback="").replace(",", "\n").split("\n") if s.strip()]

    def set_sources(self, items):
        self.set("sources", "list", "\n" + "\n".join(items) if items else "")

    def destinations(self):
        return dict(self.cfg.items("destinations")) if self.cfg.has_section("destinations") else {}

    def releases_all(self):
        """The menu's "Releases kept in full": the sources whose every release is
        copied, notes and files, and keeps them all."""
        return [s.strip() for s in self.cfg.get("releases_all", "list", fallback="").replace(",", "\n").split("\n")
                if s.strip()]

    def set_releases_all(self, items):
        self.set("releases_all", "list", "\n" + "\n".join(items) if items else "")

    def keeps_all_releases(self, repo):
        """True when the Gitea repository is in "Releases kept in full": (all),
        (allpublic) or (allprivate) by its visibility, its owner or owner/name - in any
        case."""
        full = repo["full_name"].lower()
        owner = full.split("/", 1)[0]
        for item in self.releases_all():
            v = item.lower()
            if v in ("(all)", full, owner):
                return True
            if v in ("(allpublic)", "(allprivate)") and bool(repo.get("private")) == (v == "(allprivate)"):
                return True
        return False

    def assets_limit(self):
        """assets_max_mb in bytes, as Gitea reads FILE_MAX_SIZE: None for a negative
        value (-1, the default: no limit), 0 for 0 (no file is copied)."""
        try:
            mb = int((self.get("assets_max_mb") or "-1").strip())
        except ValueError:
            mb = -1
        return None if mb < 0 else mb * 1048576

    def keep_count(self):
        """How many of the newest releases are synced with their files: None for all."""
        v = self.get("release_assets_keep").strip().lower()
        if v == "all":
            return None
        if v in ("", "newest"):
            return 1
        return max(1, int(v)) if v.isdigit() else 1


# --- the Gitea source -----------------------------------------------------------------------

class Gitea:
    def __init__(self, url, token, public_url=""):
        self.base = url.rstrip("/")
        self.api = self.base + "/api/v1"
        self.token = token
        self.public = (public_url or url).rstrip("/")

    def headers(self):
        return {"Authorization": f"token {self.token}"} if self.token else {}

    def get(self, path, params=None):
        url = self.api + path + ("?" + urllib.parse.urlencode(params) if params else "")
        status, data, _ = http("GET", url, self.headers())
        if status != 200:
            raise LookupError(f"Gitea {path}: {status} {message(data)}")
        return data

    def all(self, path, params=None, key=None):
        """Every page. Stops when a page brings nothing new, so an endpoint that ignores
        paging cannot loop forever."""
        seen, page = set(), 1
        while True:
            data = self.get(path, dict(params or {}, limit=50, page=page))
            items = data.get(key) if key else data
            items = items or []
            new = [i for i in items if json.dumps(i.get("id", i), sort_keys=True) not in seen]
            for i in new:
                seen.add(json.dumps(i.get("id", i), sort_keys=True))
                yield i
            if len(items) < 50 or not new:
                return
            page += 1

    def public_link(self, url):
        return self.public + url[len(self.base):] if url and url.startswith(self.base) else url

    def git_env(self):
        """The token for git, in the environment rather than on the command line."""
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        if self.token:
            env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0=f"http.{self.base}/.extraHeader",
                       GIT_CONFIG_VALUE_0=f"Authorization: token {self.token}")
        return env


def github_origin(repo):
    """https://github.com/<owner>/<name> when Gitea migrated the repository from GitHub."""
    m = re.match(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", repo.get("original_url") or "")
    return f"https://github.com/{m.group(1)}/{m.group(2)}" if m else ""


class Item:
    """An issue, pull request or comment as the destination gets it."""

    def __init__(self, gitea, repo, data, issue=None):
        self.data = data
        body = data.get("body") or ""
        m = ATTRIB.match(body)
        origin = github_origin(repo)
        if m:  # imported through the API by github-to-gitea.py: the line names the original
            self.author, self.created, url, body = m.group(1), m.group(2), m.group(3), body[m.end():]
        else:
            self.author = data.get("original_author") or (data.get("user") or {}).get("login") or "ghost"
            self.created = data.get("created_at") or ""
            url = ""
        if not url:
            if issue is None and origin:
                kind = "pull" if data.get("pull_request") is not None else "issues"
                url = f"{origin}/{kind}/{data['number']}"
            else:
                url = gitea.public_link(data.get("html_url") or "")
        self.url, self.text = url, body
        self.html_url = gitea.public_link(data.get("html_url") or "")

    def body(self, text=None):
        """WeKan's mirror format: the marker, the text, then where it came from."""
        source = f"\nSource: {self.html_url}." if self.html_url and self.html_url != self.url else ""
        return (f"<!-- wekan-mirror:{self.url} -->\n\n{self.text if text is None else text}\n\n---\n"
                f"Mirrored from {self.url}.{source}\nOriginal author: {self.author}; created: {self.created}.")


MARKER = re.compile(r"<!-- wekan-mirror:(\S+?) -->")


def image_extension(data):
    """The file extension of an image by its first bytes: GitLab checks an avatar's
    type by its file name."""
    for magic, ext in ((b"\x89PNG", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"GIF8", ".gif")):
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data.lstrip()[:5] in (b"<?xml", b"<svg ") or b"<svg" in data[:300]:
        return ".svg"
    return ".png"


def marker_of(text):
    m = MARKER.search(text or "")
    return m.group(1) if m else ""


# --- the engine ---------------------------------------------------------------------------

class Engine:
    def __init__(self, target, here):
        self.t = target
        self.here = here
        self.ini = os.path.join(here, f"{target.NAME}.ini")
        self.state_file = os.path.join(here, f"{target.NAME}-state.json")
        self.cfg = Config(self.ini, target.INI_HEADER, COMMON_SETTINGS[:3] + target.SETTINGS + COMMON_SETTINGS[3:])
        self.state = {}
        self.restart = False
        self.counts = {}

    # settings and state
    @property
    def dry(self):
        return self.cfg.flag("dry_run") or self.force_dry

    force_dry = False

    def configure(self):
        self.gitea = Gitea(self.cfg.get("url", "gitea"), self.cfg.get("token", "gitea"), self.cfg.get("public_url", "gitea"))
        missing = self.t.missing_settings(self.cfg)
        if not self.cfg.get("token", "gitea"):
            missing.insert(0, "[gitea] token")
        if missing:
            raise SystemExit(f"Missing {', '.join(missing)}: set it in the menu's Settings or in {self.ini}")
        self.t.configure(self)

    def load_state(self):
        self.state = json.load(open(self.state_file, encoding="utf-8")) if os.path.exists(self.state_file) else {}

    def save_state(self):
        if self.dry:
            return
        tmp = self.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=1)
        os.replace(tmp, self.state_file)

    def record(self, kind, what):
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if kind != "checked":
            log(f"  [{kind}] {what}")

    def destination(self, owner):
        """Where a Gitea owner goes at the target: [destinations] owner = X, else the
        target's default template with {owner}."""
        d = self.cfg.destinations()
        return d.get(owner) or self.t.default_destination(self.cfg).format(owner=owner)

    # sources
    def expand(self, src):
        try:
            if src.lower() in SPECIAL:
                want = SPECIAL[src.lower()]
                repos = [r for r in self.gitea.all("/repos/search", {"sort": "alpha"}, key="data")
                         if want is None or bool(r.get("private")) == want]
            elif "/" in src:
                return [src]
            else:
                try:
                    self.gitea.get(f"/orgs/{src}")
                    repos = self.gitea.all(f"/orgs/{src}/repos")
                except LookupError:
                    repos = self.gitea.all(f"/users/{src}/repos")
                repos = [r for r in repos if r["owner"]["login"].lower() == src.lower()]
            out = []
            for r in repos:
                if r.get("fork") and not self.cfg.flag("include_forks"):
                    continue
                if r.get("archived") and not self.cfg.flag("include_archived"):
                    continue
                out.append(r["full_name"])
            return sorted(out, key=str.lower)
        except LookupError as e:
            raise RepoError(f"{src}: {e}")

    # one repository
    def run_repo(self, full, steps):
        repo = self.gitea.get(f"/repos/{full}")
        dest = self.destination(repo["owner"]["login"])
        state = self.state.setdefault(full, {})
        log(f"\n=== Gitea {self.gitea.public}/{full} -> {self.t.TITLE} {self.t.describe(dest, repo)}")
        if repo.get("empty"):
            self.record("skipped", f"{full}: the Gitea repository is empty")
            return
        handle = self.t.ensure_repo(dest, repo)
        if handle is None:  # dry run, or the target refuses this repository
            return
        if "git" in steps:
            self.sync_git(repo, handle)
        if "issues" in steps and (repo.get("has_issues", True) or repo.get("has_pull_requests", True)):
            self.sync_issues(repo, handle, state)
        if "releases" in steps:
            self.sync_releases(repo, handle)
        if "avatars" in steps:
            self.sync_avatars(repo, handle)

    # git
    def work_repo(self, repo, wiki=False):
        path = os.path.join(self.here, "work", repo["owner"]["login"], repo["name"] + (".wiki" if wiki else "") + ".git")
        url = f"{self.gitea.base}/{repo['full_name']}{'.wiki' if wiki else ''}.git"
        env = self.gitea.git_env()
        if os.path.isdir(path):
            subprocess.run(["git", "-C", path, "fetch", "--quiet", "--prune", "origin"], check=True, env=env)
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            subprocess.run(["git", "clone", "--quiet", "--mirror", url, path], check=True, env=env)
        return path

    def push(self, path, dest_url, what):
        """Branches and tags, never forced and never deleted. A rejected ref is reported;
        the others still go."""
        refs = subprocess.run(["git", "-C", path, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags"],
                              capture_output=True, text=True, check=True).stdout.split()
        if not refs:
            self.record("skipped", f"{what}: no branches or tags")
            return
        cmd = ["git", "-C", path, "push", "--porcelain"]
        if self.dry:
            cmd.append("--dry-run")
        r = subprocess.run(cmd + [dest_url, "refs/heads/*:refs/heads/*", "refs/tags/*:refs/tags/*"],
                           capture_output=True, text=True, env=self.t.git_env())
        changed = rejected = 0
        for line in r.stdout.splitlines():
            if line[:1] in ("*", "+", "-", " ") and "\t" in line:
                changed += line[:1] != " "
                if line[:1] in ("*", "+"):
                    self.record("planned" if self.dry else "copied", f"{what} {line.split(chr(9))[1].split(':')[1]}")
            elif line[:1] == "!" and "\t" in line:
                rejected += 1
                ref = line.split("\t")[1].split(":")[1]
                self.record("failed", f"{what} {ref}: {line.split(chr(9))[2] if line.count(chr(9)) > 1 else 'rejected'} "
                                      f"(the destination has commits the source does not; not forced)")
        if r.returncode and not rejected:
            self.record("failed", f"{what}: git push: {(r.stderr or r.stdout).strip()[-400:]}")
        elif not changed and not rejected:
            self.record("checked", f"{what}: up to date")

    def sync_git(self, repo, handle):
        try:
            path = self.work_repo(repo)
            self.push(path, self.t.git_url(handle, repo), f"git {repo['full_name']}")
        except subprocess.CalledProcessError as e:
            self.record("failed", f"git {repo['full_name']}: {e}")
            return
        self.t.after_git(handle, repo)
        if self.cfg.flag("wiki") and repo.get("has_wiki"):
            # Gitea says has_wiki for every repository with the wiki turned on, written
            # or not: only a wiki with pages is copied, and only then is it turned on at
            # the destination.
            env = self.gitea.git_env()
            ls = subprocess.run(["git", "ls-remote", f"{self.gitea.base}/{repo['full_name']}.wiki.git"],
                                capture_output=True, text=True, env=env)
            if ls.returncode or not ls.stdout.strip():
                self.record("checked", f"wiki {repo['full_name']}: none in Gitea")
                return
            wiki_url = self.t.wiki_url(handle, repo)
            if not wiki_url:
                self.record("skipped", f"wiki {repo['full_name']}: {self.t.TITLE} has no git wiki")
                return
            try:
                self.push(self.work_repo(repo, wiki=True), wiki_url, f"wiki {repo['full_name']}")
            except subprocess.CalledProcessError as e:
                self.record("failed", f"wiki {repo['full_name']}: {e}")

    # issues
    def sync_issues(self, repo, handle, state):
        full = repo["full_name"]
        st = state.setdefault("issues", {})
        mapping = st.setdefault("dest", {})  # identity URL -> destination id
        started = now_iso()
        params = {"state": "all"}
        if st.get("since") and not self.restart:
            params["since"] = st["since"]
        items = sorted(self.gitea.all(f"/repos/{full}/issues", params), key=lambda i: i["number"])
        log(f"Issues and pull requests: {len(items)} {'changed since ' + st['since'] if 'since' in params else 'in all'}.")
        if not items:
            st["since"] = started
            self.save_state()
            return
        labels = {l["name"]: l for l in self.gitea.all(f"/repos/{full}/labels")}
        milestones = {m["title"]: m for m in self.gitea.all(f"/repos/{full}/milestones", {"state": "all"})}
        tracker = self.t.issue_tracker(handle, repo, labels, milestones)
        if tracker is None:
            return
        inventory = None
        failures = 0
        progress = set() if self.restart else set(st.get("progress") or [])
        for data in items:
            num = data["number"]
            if num in progress:
                continue
            item = Item(self.gitea, repo, data)
            try:
                dest = mapping.get(item.url)
                if dest is None:
                    if inventory is None:
                        inventory = self.t.issue_inventory(tracker)  # identity URL -> id
                        mapping.update(inventory)
                    dest = mapping.get(item.url)
                created = False
                if dest is None:
                    if self.dry:
                        self.record("planned", f"issue {item.url}")
                        continue
                    text = self.t.rewrite(tracker, repo, item.text)
                    dest = self.t.create_issue(tracker, item, item.body(text), data)
                    mapping[item.url] = dest
                    created = True
                    self.record("copied", f"issue {item.url}")
                    self.save_state()
                if data.get("comments"):
                    self.sync_comments(repo, tracker, data, dest)
                if data["state"] == "closed" and (created or not self.t.is_closed(tracker, dest)):
                    if self.dry:
                        self.record("planned", f"close {item.url}")
                    else:
                        self.t.close_issue(tracker, dest)
                self.record("checked", f"issue {item.url}")
                progress.add(num)
                st["progress"] = sorted(progress)
                self.save_state()
            except (LookupError, RuntimeError) as e:
                self.record("failed", f"issue {item.url}: {e}")
                failures += 1
        if failures:
            # "since" stays where it was, so the next run reads the failed ones again;
            # "progress" lets it skip the ones that went through.
            log(f"  {failures} issue(s) failed; the next run tries them again.")
        else:
            st.pop("progress", None)
            st["since"] = started
        self.save_state()

    def sync_comments(self, repo, tracker, data, dest):
        comments = list(self.gitea.all(f"/repos/{repo['full_name']}/issues/{data['number']}/comments"))
        if not comments:
            return
        existing = self.t.comments(tracker, dest)  # texts of the destination's comments
        markers = {marker_of(t) for t in existing} - {""}
        texts = [normalize(t) for t in existing]
        for c in comments:
            item = Item(self.gitea, repo, c, issue=data)
            if item.url in markers:
                continue
            probe = normalize(item.text)[:120]
            if len(probe) >= 30 and any(probe in t for t in texts):
                continue  # copied before without this marker (WeKan's mirror, from GitHub)
            if self.dry:
                self.record("planned", f"comment {item.url}")
                continue
            self.t.add_comment(tracker, dest, item.body(self.t.rewrite(tracker, repo, item.text)))
            markers.add(item.url)
            self.record("copied", f"comment {item.url}")

    # releases
    def sync_releases(self, repo, handle):
        full = repo["full_name"]
        releases = [r for r in self.gitea.all(f"/repos/{full}/releases") if not r.get("draft")]
        if not releases:
            return
        # Gitea lists the newest first.
        full_list = self.cfg.keeps_all_releases(repo)
        n = None if full_list else self.cfg.keep_count()
        keep = {r["tag_name"] for r in (releases if n is None else releases[:n])}
        log(f"Releases: {len(releases)}" + (", kept in full." if n is None else
                                             f"; the {'newest' if n == 1 else f'{n} newest'} synced."))
        target = self.t.release_target(handle, repo)
        if target is None:
            return
        limit = self.cfg.assets_limit()
        for r in reversed(releases):  # oldest first
            tag = r["tag_name"]
            if tag not in keep:
                self.drop_release_files(target, r, full)
                continue
            try:
                have = self.t.release_files(target, r)  # None = the release is missing
                if have is None:
                    if self.dry:
                        self.record("planned", f"release {full} {tag}")
                        have = set()
                    else:
                        self.t.create_release(target, r, self.release_notes(repo, r))
                        self.record("copied", f"release {full} {tag}")
                        have = self.t.release_files(target, r) or set()
                assets = r.get("assets") or []
                if not full_list:
                    names = [a["name"] for a in assets]
                    wanted, newest = newest_version_files(names)
                    if newest:
                        log(f"  {tag}: {len(names)} files of several versions; only version {newest}'s, {len(wanted)} files")
                        assets = [a for a in assets if a["name"] in wanted]
                        older = sorted(self.t.asset_name(x) for x in set(names) - wanted
                                       if self.t.asset_name(x) in have)
                        if older:
                            if self.dry:
                                for f in older:
                                    self.record("planned", f"delete {full} {tag}/{f} (an older version)")
                            else:
                                self.t.delete_files(target, r, older)
                                for f in older:
                                    self.record("deleted", f"{full} {tag}/{f} (an older version)")
                for a in assets:
                    if self.t.asset_name(a["name"]) in have:
                        continue
                    if limit is not None and (limit == 0 or a.get("size", 0) > limit):
                        self.record("skipped", f"{tag}/{a['name']}: {a['size'] // 1048576} MB > assets_max_mb")
                        continue
                    if self.dry:
                        self.record("planned", f"file {tag}/{a['name']}")
                        continue
                    path = self.download(a)
                    try:
                        self.t.upload_file(target, r, a["name"], path)
                        self.record("copied", f"file {tag}/{a['name']}")
                    finally:
                        shutil.rmtree(os.path.dirname(path), ignore_errors=True)
                self.record("checked", f"release {tag}")
            except (LookupError, RuntimeError, OSError, subprocess.CalledProcessError) as e:
                self.record("failed", f"release {full} {tag}: {e}")

    def drop_release_files(self, target, r, full):
        """An older release: not copied, and its files at the destination - from an
        earlier run, or from WeKan's mirror - are deleted; its notes stay."""
        tag = r["tag_name"]
        try:
            have = self.t.release_files(target, r)
            if not have:
                return
            files = sorted(f for f in have if f not in getattr(self.t, "NOTES_FILES", ()))
            if not files:
                return
            if self.dry:
                for f in files:
                    self.record("planned", f"delete {full} {tag}/{f} (an older release)")
                return
            self.t.delete_files(target, r, files)
            for f in files:
                self.record("deleted", f"{full} {tag}/{f} (an older release)")
        except (LookupError, RuntimeError, OSError, subprocess.CalledProcessError) as e:
            self.record("failed", f"release {full} {tag}: deleting the older release's files: {e}")

    def release_notes(self, repo, r):
        origin = github_origin(repo)
        url = f"{origin}/releases/tag/{urllib.parse.quote(r['tag_name'])}" if origin else self.gitea.public_link(r.get("html_url"))
        return (f"<!-- wekan-mirror:{url} -->\n\n{r.get('body') or ''}\n\n---\nMirrored from {url}.\n"
                f"Published: {r.get('published_at') or r.get('created_at') or ''}.")

    def download(self, asset):
        d = os.path.join(mirror_tmp_dir(self.t.NAME), uuid.uuid4().hex)
        os.makedirs(d)
        path = os.path.join(d, safe_segment(asset["name"]))
        req = urllib.request.Request(asset["browser_download_url"], headers=dict(self.gitea.headers(), **{"User-Agent": "gitea-mirror"}))
        with urllib.request.urlopen(req, timeout=None) as resp, open(path, "wb") as f:
            while chunk := resp.read(1 << 20):
                f.write(chunk)
        if asset.get("size") and os.path.getsize(path) != asset["size"]:
            raise RuntimeError(f"download of {asset['name']} is incomplete")
        return path

    # avatars
    AVATAR_MAX = 10 * 1048576

    def fetch_avatar(self, url):
        req = urllib.request.Request(url, headers=dict(self.gitea.headers(), **{"User-Agent": "gitea-mirror"}))
        with urllib.request.urlopen(req, timeout=120) as r:
            data = r.read(self.AVATAR_MAX + 1)
        if len(data) > self.AVATAR_MAX:
            raise RuntimeError("larger than 10 MB")
        return data

    def sync_avatars(self, repo, handle):
        owner = repo["owner"]["login"]
        seen = self.__dict__.setdefault("_avatar_owners", set())
        if owner not in seen:
            seen.add(owner)
            url = (repo.get("owner") or {}).get("avatar_url")
            if url:
                dest = self.destination(owner)
                self.put_avatar(f"owner:{owner}", url, f"avatar of {owner}",
                                lambda path: self.t.owner_avatar(dest, path))
        if repo.get("avatar_url"):
            self.put_avatar(f"repo:{repo['full_name']}", repo["avatar_url"], f"avatar of {repo['full_name']}",
                            lambda path: self.t.repo_avatar(handle, repo, path))

    def put_avatar(self, key, url, what, upload):
        """Download a Gitea avatar and upload it with upload(path) - only when it is not
        the image uploaded last time. upload returns False when the target cannot."""
        try:
            data = self.fetch_avatar(self.gitea.base + url[len(self.gitea.public):] if url.startswith(self.gitea.public) and self.gitea.public != self.gitea.base else url)
        except (urllib.error.URLError, OSError, RuntimeError) as e:
            self.record("failed", f"{what}: {e}")
            return
        digest = hashlib.sha256(data).hexdigest()
        done = self.state.setdefault("_avatars", {})
        if done.get(key) == digest:
            return
        if self.dry:
            self.record("planned", what)
            return
        d = os.path.join(mirror_tmp_dir(self.t.NAME), uuid.uuid4().hex)
        os.makedirs(d)
        try:
            path = os.path.join(d, "avatar" + image_extension(data))
            with open(path, "wb") as f:
                f.write(data)
            if upload(path):
                done[key] = digest
                self.save_state()
                self.record("copied", what)
        except (RuntimeError, OSError) as e:
            self.record("failed", f"{what}: {e}")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    # a run over the sources, resumable
    def run(self, steps, only=None):
        self.configure()
        self.load_state()
        self.counts = {}
        todo, failed = [], []
        srcs = [only] if only else self.cfg.sources()
        for src in srcs:
            try:
                todo += self.expand(src)
            except RepoError as e:
                failed.append((src, str(e)))
        if not todo and not failed:
            log("No sources. Add one from the menu, for example 'wekan/wekan', 'wekan', 'xet7' or '(all)'.")
        todo = list(dict.fromkeys(todo))
        key = {"steps": list(steps), "sources": srcs, "dry": self.dry}
        prev = self.state.get("_run") or {}
        done = set()
        if not self.restart and prev.get("key") == key and not prev.get("finished"):
            done = set(prev.get("done") or []) & set(todo)
            if done:
                log(f"Resuming the interrupted run of {prev.get('started')}: {len(done)} of {len(todo)} "
                    f"repositories already done, skipping them (--restart runs them all).")
        self.state["_run"] = {"key": key, "started": prev.get("started") if done else now_iso(), "done": sorted(done)}
        self.save_state()
        interrupted = False
        for full in todo:
            if full in done:
                continue
            try:
                self.run_repo(full, steps)
                done.add(full)
                self.state["_run"]["done"] = sorted(done)
                self.save_state()
            except (RepoError, LookupError, RuntimeError) as e:
                log(f"  ERROR: {e}")
                failed.append((full, str(e)))
            except KeyboardInterrupt:
                log("\nInterrupted. The next run with the same steps and sources continues from here.")
                interrupted = True
                break
            except Exception as e:  # a fault in one repository must not stop the others
                log(f"  ERROR: {type(e).__name__}: {e}")
                for line in traceback.format_exc().rstrip().splitlines():
                    log(f"    {line}")
                failed.append((full, f"{type(e).__name__}: {e}"))
        if not interrupted:
            self.state["_run"]["finished"] = now_iso()
            self.save_state()
        summary = ", ".join(f"{k} {v}" for k, v in sorted(self.counts.items()))
        log(f"\nDone: {len(todo)} {'repository' if len(todo) == 1 else 'repositories'}" + (f" ({summary})" if summary else "")
            + (f", {len(failed)} errors:" if failed else "."))
        for full, msg in failed:
            log(f"  {full}: {msg}")
        bad = failed or self.counts.get("failed")
        return not bad

    def check_online(self):
        """Read-only: can the Gitea and the target be reached with these settings?"""
        self.configure()
        ok = True
        try:
            v = self.gitea.get("/version")["version"]
            who = self.gitea.get("/user")["login"]
            log(f"Gitea {self.gitea.base}: version {v}, token of {who}.")
        except (LookupError, KeyError, TypeError) as e:
            log(f"Gitea {self.gitea.base}: {e}")
            ok = False
        for line, good in self.t.check_online(self):
            log(f"{self.t.TITLE}: {line}")
            ok = ok and good
        return ok

    def check_missing(self, only=None):
        """Read-only: everything a run would copy, without copying it."""
        self.force_dry = True
        try:
            log("Checking what is not mirrored yet (dry run, nothing is changed)...")
            return self.run([s for s in STEPS], only)
        finally:
            self.force_dry = False


VERSION_IN_NAME = re.compile(r"\d+(?:\.\d+)+")


def newest_version_files(names):
    """The names to keep of one release's files. A release like rustdesk's nightly
    holds the same files over and over with different version numbers -
    rustdesk-1.2.0-x86_64.deb ... rustdesk-1.4.3-x86_64.deb, 352 files of 20
    versions. When a file name, its version number taken out, appears with more than
    one version, only the files of the newest version in the release are kept, and
    files without a version number. Any other release keeps every file.
    Returns (names to keep, the newest version or None)."""
    versions = {}
    for n in names:
        m = VERSION_IN_NAME.search(n)
        if m:
            versions[n] = (tuple(int(x) for x in m.group().split(".")), n[:m.start()] + "\0" + n[m.end():])
    seen = {}
    for v, stem in versions.values():
        seen.setdefault(stem, set()).add(v)
    if not any(len(vs) > 1 for vs in seen.values()):
        return set(names), None
    newest = max(v for v, _ in versions.values())
    return {n for n in names if n not in versions or versions[n][0] == newest}, ".".join(map(str, newest))


# --- the menu ------------------------------------------------------------------------------

def ask(prompt, default=""):
    try:
        v = input(f"{prompt}{f' [{default}]' if default else ''}: ").strip()
    except EOFError:
        return default
    return v or default


def source_help(e):
    t = e.t
    return f"""\
  A source is one of:
    owner/name    one Gitea repository, e.g. wekan/wekan
    org           every repository of a Gitea organization, e.g. wekan
    user          every repository a Gitea user OWNS, e.g. xet7
    (all)         {SPECIAL_NAMES['(all)']}
    (allpublic)   {SPECIAL_NAMES['(allpublic)']}
    (allprivate)  {SPECIAL_NAMES['(allprivate)']}
  Each repository goes to {t.DESTINATION_HELP}"""


def menu_sources(e):
    while True:
        items = e.cfg.sources()
        print("\nSources:")
        print(source_help(e) + "\n\n  Configured sources:")
        for i, s in enumerate(items, 1):
            what = SPECIAL_NAMES.get(s.lower()) or ("one repository" if "/" in s else
                                                    "organization or user: every repository it owns")
            print(f" {i:2}) {s}  ({what})")
        if not items:
            print("    (none)")
        print("  a) Add   r) Remove   s) Show what would be mirrored   0) Back")
        c = ask("Choice").lower()
        if c in ("", "0"):
            return
        if c == "a":
            v = ask("Gitea repository, organization or user, or (all) / (allpublic) / (allprivate)").strip().strip("/")
            v = re.sub(r"^https?://[^/]+/", "", v).removesuffix(".git")
            if v.lower() in SPECIAL:
                v = v.lower()
            if v and v not in items and (v in SPECIAL or re.fullmatch(r"[\w.-]+(/[\w.-]+)?", v)):
                e.cfg.set_sources(items + [v])
            elif v:
                print("Not valid, or already in the list.")
        elif c == "r":
            n = ask("Number to remove")
            if n.isdigit() and 1 <= int(n) <= len(items):
                del items[int(n) - 1]
                e.cfg.set_sources(items)
        elif c == "s":
            e.configure()
            for s in items:
                try:
                    repos = e.expand(s)
                    print(f"  {s}: {len(repos)} repositories")
                    for r in repos:
                        print(f"     {r} -> {e.t.describe(e.destination(r.split('/')[0]), {'name': r.split('/')[1]})}")
                except RepoError as err:
                    print(f"  {s}: {err}")


def menu_destinations(e):
    while True:
        d = e.cfg.destinations()
        print(f"\nDestinations: which {e.t.DESTINATION_WORD} each Gitea owner goes to.")
        print(f"  Without a line here, owner X goes to {e.t.default_destination(e.cfg).format(owner='X')}"
              f" (setting {e.t.DEFAULT_KEY}).")
        for i, (k, v) in enumerate(sorted(d.items()), 1):
            print(f" {i:2}) {k} -> {v}")
        if not d:
            print("    (none)")
        print("  a) Add or change   r) Remove   0) Back")
        c = ask("Choice").lower()
        if c in ("", "0"):
            return
        if c == "a":
            owner = ask("Gitea owner").strip()
            if owner:
                v = ask(f"{e.t.DESTINATION_WORD} at {e.t.TITLE}", d.get(owner, owner)).strip()
                if v:
                    e.cfg.set("destinations", owner, v)
        elif c == "r":
            n = ask("Number to remove")
            keys = sorted(d)
            if n.isdigit() and 1 <= int(n) <= len(keys):
                e.cfg.cfg.remove_option("destinations", keys[int(n) - 1])
                e.cfg.save()


SECRET_KEYS = ("token", "password", "client_secret")


def masked(e, section, key):
    v = e.cfg.cfg.get(section, key, fallback="")
    if key in SECRET_KEYS:
        return f"{v[:4]}…{v[-4:]}" if len(v) > 8 else ("(set)" if v else "(missing)")
    return v


def menu_settings(e):
    settings = e.cfg.settings
    while True:
        print(f"\nSettings (saved in {os.path.basename(e.ini)})")
        for i, (s, k, _, desc) in enumerate(settings, 1):
            print(f" {i:2}) [{s}] {k} = {masked(e, s, k)}\n       {desc}")
        print("  0) Back")
        c = ask("Choice")
        if c in ("", "0"):
            return
        if c.isdigit() and 1 <= int(c) <= len(settings):
            s, k, d, desc = settings[int(c) - 1]
            v = ask(f"{s}.{k} ({desc})", "" if k in SECRET_KEYS else e.cfg.get(k, s))
            if k in SECRET_KEYS and not v:
                continue
            if k == "steps":
                bad = [x for x in re.split(r"[,\s]+", v) if x and x not in STEPS]
                if bad:
                    print(f"Unknown step: {', '.join(bad)}. Steps: {', '.join(STEPS)}")
                    continue
            if k == "assets_max_mb" and not re.fullmatch(r"-1|\d+", v.strip()):
                print("The value is -1 (no limit), 0 (no files) or the largest size in MB.")
                continue
            e.cfg.set(s, k, v)


RELEASES_ALL_HELP = """\
  Every release - notes and files - is copied and kept for the repositories listed here:
    (all)         every repository
    (allpublic)   every public repository / (allprivate) every private repository
    org or user   every repository of a Gitea organization or user, e.g. wekan
    owner/name    one repository, e.g. wekan/wekan
  Every other repository gets only its newest release, notes and files, and the files of
  its older releases are deleted from the destination (setting release_assets_keep)."""


def menu_releases_all(e):
    while True:
        items = e.cfg.releases_all()
        print("\nReleases kept in full:")
        print(RELEASES_ALL_HELP + "\n\n  Listed:")
        for i, s in enumerate(items, 1):
            print(f" {i:2}) {s}")
        if not items:
            print("    (none - only the newest release is copied)")
        print("  a) Add   r) Remove   0) Back")
        c = ask("Choice").lower()
        if c in ("", "0"):
            return
        if c == "a":
            v = ask("(all) / (allpublic) / (allprivate), a Gitea organization or user, or owner/name").strip().strip("/")
            v = re.sub(r"^https?://[^/]+/", "", v).removesuffix(".git")
            if v.lower() in SPECIAL:
                v = v.lower()
            if v and v.lower() not in [x.lower() for x in items] and \
                    (v in SPECIAL or re.fullmatch(r"[\w.-]+(/[\w.-]+)?", v)):
                e.cfg.set_releases_all(items + [v])
            elif v:
                print("Not valid, or already in the list.")
        elif c == "r":
            n = ask("Number to remove")
            if n.isdigit() and 1 <= int(n) <= len(items):
                del items[int(n) - 1]
                e.cfg.set_releases_all(items)


def pick_source(e):
    items = e.cfg.sources()
    for i, s in enumerate(items, 1):
        print(f" {i:2}) {s}")
    print("  a) all sources   or type a repository, organization or user, or (all) / (allpublic) / (allprivate)")
    c = ask("Source", "a")
    if c == "a":
        return None
    if c.isdigit() and 1 <= int(c) <= len(items):
        return items[int(c) - 1]
    c = c.strip().strip("/")
    return (c.lower() if c.lower() in SPECIAL else c) or None


def configured_steps(e):
    return [x for x in re.split(r"[,\s]+", e.cfg.get("steps")) if x in STEPS]


def menu(e):
    t = e.t
    while True:
        items = e.cfg.sources()
        print(f"""
Gitea -> {t.TITLE}     Gitea: {e.cfg.get('url', 'gitea')}     {t.TITLE}: {t.where(e.cfg)}     settings: {os.path.basename(e.ini)}
Sources: {', '.join(items) or '(none - add one with 3)'}
Dry run: {'ON' if e.cfg.flag('dry_run') else 'off'}
Releases kept in full: {', '.join(e.cfg.releases_all()) or '(none - only the newest release)'}

 1) Run every step ({', '.join(configured_steps(e))}) for every source
 2) Run chosen steps for a chosen source
 3) Sources: add / remove / show repositories
 4) Settings (Gitea, {t.TITLE}, tokens, steps...)
 5) Destinations: which {t.DESTINATION_WORD} each Gitea owner goes to
 6) Dry run on / off
 7) Check Gitea and {t.TITLE} access (read-only)
 8) Check what is not mirrored yet (read-only)
 9) Releases kept in full: which organizations, users or repositories keep every release""")
        extra = getattr(t, "MENU_EXTRA", ())
        for n, (label, _) in enumerate(extra, 10):
            print(f"{n:2}) {label}")
        print(" 0) Quit")
        c = ask("Choice")
        try:
            if c in ("0", "q", ""):
                return
            if c == "1":
                e.run(configured_steps(e))
            elif c == "2":
                for i, s in enumerate(STEPS, 1):
                    print(f" {i}) {s}: {STEP_NAMES[s]}")
                chosen = ask("Steps as numbers or names (e.g. 2 3, or issues releases)", "1 2 3")
                steps = [STEPS[int(x) - 1] if x.isdigit() and 1 <= int(x) <= len(STEPS) else x
                         for x in re.split(r"[,\s]+", chosen) if x]
                steps = [s for s in STEPS if s in steps]
                if steps:
                    e.run(steps, pick_source(e))
            elif c == "3":
                menu_sources(e)
            elif c == "4":
                menu_settings(e)
            elif c == "5":
                menu_destinations(e)
            elif c == "6":
                e.cfg.set("options", "dry_run", "no" if e.cfg.flag("dry_run") else "yes")
            elif c == "7":
                e.check_online()
            elif c == "8":
                e.check_missing(pick_source(e))
            elif c == "9":
                menu_releases_all(e)
            elif c.isdigit() and 10 <= int(c) < 10 + len(extra):
                getattr(t, extra[int(c) - 10][1])(e)
        except SystemExit as err:
            print(err)
        except KeyboardInterrupt:
            print("\nInterrupted.")


def main(target, here, doc):
    p = argparse.ArgumentParser(description=doc, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("steps", nargs="*", metavar="step", help=f"{', '.join(STEPS)}; with no steps, in a terminal, the menu opens")
    p.add_argument("--all", "--sync", action="store_true", help="run the configured steps for every source, without the menu")
    p.add_argument("--source", help="only this source (owner/name, an organization or user, or (all) / (allpublic) / (allprivate))")
    p.add_argument("--check-online", action="store_true", help="read-only: check Gitea and target access")
    p.add_argument("--check-missing", "--preview", action="store_true", help="read-only: show what is not mirrored yet")
    p.add_argument("--restart", action="store_true", help="do not resume an interrupted run; read every issue again")
    a = p.parse_args()
    e = Engine(target, here)
    e.cfg.load()
    e.restart = a.restart
    bad = set(a.steps) - set(STEPS)
    if bad:
        p.error(f"unknown step: {', '.join(sorted(bad))}")
    if not (a.steps or a.all or a.source or a.check_online or a.check_missing) and not sys.stdin.isatty():
        p.error("no terminal for the menu: give steps, --all, --check-online or --check-missing")
    start_log(here, target.NAME)
    if a.check_online:
        sys.exit(0 if e.check_online() else 1)
    if a.check_missing:
        sys.exit(0 if e.check_missing(a.source) else 1)
    if a.steps or a.all or a.source:
        sys.exit(0 if e.run(a.steps or configured_steps(e), a.source) else 1)
    menu(e)
