"""Backing up a Gitea organization or user, deleting it from Gitea, and restoring it -
into Gitea, or as WeKan boards. Shared by github-to-gitea and the mirror scripts.

A backup of NAME is ONE directory, with no date in it:

  <backup_dir>/<NAME>/<NAME>.db    SQLite: every record
  <backup_dir>/<NAME>/<NAME>.zip   every file, in folders

Every backup run - from Gitea (backup()) or straight from GitHub (github_backup.py) -
UPDATES that backup in place, and only with what is new or changed:

  - rows are written by their primary key, the newest wins; a row that is gone at the
    source is KEPT: a backup keeps what was deleted at the source;
  - a file is added only when it is new or changed (asset id, size, uuid / updated_at,
    and its SHA-256); an unchanged one is not downloaded again;
  - a git or wiki bundle is made again only when the refs changed, from a mirror
    seeded from the old bundle, so only the new objects come over the network; a
    branch deleted at the source stays in the bundle;
  - the .zip is written again into <NAME>.zip.tmp: every entry that is kept copied as
    it is (STORED, read from the old .zip on this disk, never downloaded again), the
    new ones added and the ones they replace left out - so the .zip never grows with
    old copies. When the disk has no room for that copy, the new entries are appended
    to the .zip instead (journal <NAME>.zip.append), and a later run rewrites it. The
    .db is updated in a copy, <NAME>.db.tmp. Both are synced and renamed into place
    (marker .backup.commit), so a run that breaks off leaves the previous backup as
    it was. Nothing changed in the .zip: it is not written at all.
  - <NAME>/.backup.lock: one run at a time updates a backup; Omi takes the same lock
    for its edits. A line "<pid> <host> <program> <UTC time>"; when that process is
    gone on this host, the lock is stale and taken over.

Every data table has a column `seen`: the run in which that row was last seen at the
source; meta `runs` is the newest run. A row with seen < runs is OLD DATA - deleted at
the source since, or only in an older backup - listed by old_only() and removed, if
wanted, by delete_old_only().

Older versions wrote one directory per run, <NAME>/YYYY-MM-DD_HH-MM-SS[-n]/ (and
.partial while writing). combine() - which every backup run, the backups list and
Backup(<NAME directory>) do first - merges ALL of them, oldest to newest, into the one
backup: the newest wins for the same key or path, everything else is kept, bundles
that differ are merged into one. The result is verified - every row key and every
zip path of every dated backup is in it, the zip tests whole - and only then are the
dated directories deleted. A .partial one is never merged; the menu offers to delete
it.

The .zip holds each file STORED - not compressed - with ZIP64 records: release files,
attachments and git bundles are compressed already, and a stored entry is the file's
own bytes at an offset, which a reader (Omi, WeKan, unzip) can show or replace
without inflating anything. Its folders:

  owner/avatar.png                              the organization's or user's avatar
  <repo>/git.bundle                             every branch, tag and pull request ref
  <repo>/wiki.bundle                            the wiki, when the repository has one
  <repo>/releases/<tag>/<file>                  a release's files
  <repo>/issues/<number>/<id>-<file>            files of an issue or pull request
  <repo>/comments/<number>/<comment>/<id>-<file>  files of a comment

The .db's tables (FORMAT 1) - every row keeps Gitea's own JSON in `json`, and `seen`:

  meta(key, value)                     format, created (the first run), updated (the
                                       newest run began), runs, sources (gitea,github),
                                       source (the newest run's), gitea, github,
                                       github_updated, owner, kind, complete, kept,
                                       merged (the dated backups combined into it),
                                       missing_files, deleted_old_only
  owner(name, kind, json)              the organization or user
  teams(id, name, json, members, repos) an organization's teams; members and repos as JSON
  members(login, json)                 an organization's members
  repos(full_name, name, json, topics, git_bundle, wiki_bundle, empty, collaborators)
                                       collaborators: [{login, permission}] as JSON
  labels(repo, id, name, json)         repo '' is an organization's label
  milestones(repo, id, title, json)
  issues(repo, number, is_pull, title, state, json)
  pulls(repo, number, json)            head, base, merged, merge commit
  comments(repo, id, issue_number, json)
  releases(repo, id, tag, json)
  files(path, repo, kind, parent, id, name, size, sha256, uuid, json)
                                       every file of the .zip, by its path there

Every call to Gitea goes through a Client: request(method, path, body) for the API,
download(url, out) and upload(path, name, src, size) for files, and git_url(full,
wiki) with git_env() for git. The tests use a fake one.
"""

import base64
import contextlib
import datetime
import hashlib
import json
import mimetypes
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

import gitea_attachments as ga

FORMAT = 1
DEFAULT_DIR = "github-to-gitea-backup"
STAMP = "%Y-%m-%d_%H-%M-%S"
STAMP_RE = re.compile(r"\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d(-\d+)?")
CHUNK = 1 << 20
LOCK = ".backup.lock"          # one run at a time; Omi takes it too
COMMIT = ".backup.commit"      # the renames into place are going on
WORK = ".backup-work"          # git mirrors and downloads of a run
APPEND = ".append"             # <NAME>.zip.append: the zip's length before an append
OMI_JOURNAL = ".omi-save"      # <NAME>.zip.omi-save: Omi's save that broke off
DATA_TABLES = ("owner", "teams", "members", "repos", "labels", "milestones", "issues", "pulls",
               "comments", "releases", "files")
KEYS = {"meta": ("key",), "owner": ("name",), "teams": ("id",), "members": ("login",),
        "repos": ("full_name",), "labels": ("repo", "id"), "milestones": ("repo", "id"),
        "issues": ("repo", "number"), "pulls": ("repo", "number"), "comments": ("repo", "id"),
        "releases": ("repo", "id"), "files": ("path",)}
KEPT_NOTE = ("rows and files that disappeared at the source are kept; seen < runs marks them "
             "(old data: list it, or delete it, in Backups)")
OLD = "(seen IS NULL OR seen < {runs})"


class BackupError(Exception):
    pass


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc)


# --- talking to Gitea -----------------------------------------------------------------------

class _DropAuthOnRedirect(urllib.request.HTTPRedirectHandler):
    """A file in object storage is a redirect to another host: it gets no token."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).netloc != urllib.parse.urlsplit(req.full_url).netloc:
            new.remove_header("Authorization")
        return new


class _Multipart:
    """A multipart body read from a file object in pieces: a release file is gigabytes."""

    def __init__(self, src, name, size, field="attachment"):
        b = uuid.uuid4().hex
        self.ctype = f"multipart/form-data; boundary={b}"
        safe = name.replace('"', "_").replace("\r", "_").replace("\n", "_")
        head = (f"--{b}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{safe}\"\r\n"
                f"Content-Type: {mimetypes.guess_type(name)[0] or 'application/octet-stream'}\r\n\r\n").encode()
        tail = f"\r\n--{b}--\r\n".encode()
        self.parts = [head, src, tail]
        self.length = len(head) + size + len(tail)

    def read(self, n=-1):
        while self.parts:
            p = self.parts[0]
            if isinstance(p, bytes):
                if not p:
                    self.parts.pop(0)
                    continue
                n2 = len(p) if n is None or n < 0 else n
                self.parts[0] = p[n2:]
                return p[:n2]
            chunk = p.read(n if n and n > 0 else CHUNK)
            if chunk:
                return chunk
            self.parts.pop(0)
        return b""


class Client:
    """A Gitea, by its address (http://host:port, with or without /api/v1) and token.
    requests and downloads ([files, bytes]) count what was asked of it."""

    def __init__(self, url, token, dry=False):
        url = url.rstrip("/")
        self.root = url[: -len("/api/v1")] if url.endswith("/api/v1") else url
        self.api = self.root + "/api/v1"
        self.token = token
        self.dry = dry
        self.opener = urllib.request.build_opener(_DropAuthOnRedirect)
        self.requests = 0
        self.downloads = [0, 0]

    def _open(self, method, url, data=None, headers=None, timeout=600):
        h = {"Accept": "application/json", "User-Agent": "gitea-backup", "Authorization": f"token {self.token}"}
        h.update(headers or {})
        return self.opener.open(urllib.request.Request(url, data=data, method=method, headers=h), timeout=timeout)

    def request(self, method, path, body=None, sudo=None):
        if self.dry and method != "GET":
            return 200, {}
        data = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if data is not None else {}
        if sudo:
            headers["Sudo"] = sudo
        for attempt in range(4):
            self.requests += 1
            try:
                with self._open(method, self.api + path, data, headers) as r:
                    text = r.read()
                    return r.status, (json.loads(text) if text.strip() else None)
            except urllib.error.HTTPError as e:
                text = e.read()
                if e.code >= 500 and attempt < 3 and method == "GET":
                    time.sleep(2 * (attempt + 1))
                    continue
                try:
                    return e.code, json.loads(text)
                except ValueError:
                    return e.code, text[:500].decode(errors="replace")
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < 3:
                    time.sleep(2 * (attempt + 1))
                    continue
                return 0, str(e)
        return 0, "no answer"

    def file_url(self, asset):
        """Where an attachment is downloaded from: this Gitea's /attachments/<uuid> - not
        browser_download_url, which carries Gitea's ROOT_URL, maybe another host."""
        if asset.get("uuid"):
            return f"{self.root}/attachments/{asset['uuid']}"
        url = asset.get("browser_download_url") or ""
        if urllib.parse.urlsplit(url).netloc != urllib.parse.urlsplit(self.root).netloc:
            raise BackupError(f"{asset.get('name')}: its address {url} is not on this Gitea")
        return url

    def download(self, url, out):
        """Writes the file to out; returns (bytes, sha256)."""
        digest, size = hashlib.sha256(), 0
        self.downloads[0] += 1
        with self._open("GET", url, headers={"Accept": "*/*"}, timeout=None) as r:
            while chunk := r.read(CHUNK):
                out.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        self.downloads[1] += size
        return size, digest.hexdigest()

    def upload(self, path, name, src, size, sudo=None):
        if self.dry:
            return 201, {"id": 0, "uuid": ""}
        body = _Multipart(src, name, size)
        headers = {"Content-Type": body.ctype, "Content-Length": str(body.length)}
        if sudo:
            headers["Sudo"] = sudo
        url = f"{self.api}{path}?name={urllib.parse.quote(name)}"
        try:
            with self._open("POST", url, body, headers, timeout=None) as r:
                text = r.read()
                return r.status, (json.loads(text) if text.strip() else None)
        except urllib.error.HTTPError as e:
            return e.code, e.read()[:500].decode(errors="replace")

    def git_url(self, full, wiki=False):
        return f"{self.root}/{full}{'.wiki' if wiki else ''}.git"

    def git_env(self):
        """The token as HTTP basic authentication, from the environment - never on the
        command line, where `ps` shows it."""
        basic = base64.b64encode(f"x-access-token:{self.token}".encode()).decode()
        return dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_COUNT="1",
                    GIT_CONFIG_KEY_0="http.extraHeader", GIT_CONFIG_VALUE_0=f"Authorization: Basic {basic}")


def wait_branches(client, full, branches, seconds=60):
    """Gitea takes a push in at once but lists its branches only when its push queue
    has run: a pull request made before that is "not found". True when every branch is
    there."""
    deadline = time.time() + seconds
    while True:
        if all(client.request("GET", f"/repos/{full}/branches/{urllib.parse.quote(b, safe='')}")[0] == 200
               for b in branches):
            return True
        if time.time() > deadline or client.dry:
            return False
        time.sleep(0.5)


def git(args, env=None, cwd=None):
    r = subprocess.run(["git"] + args, env=env, cwd=cwd, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


# --- where backups are --------------------------------------------------------------------

def backup_root(setting, here):
    """The backup directory: the setting, ~ expanded; a relative one is beside the
    script (here). Empty: github-to-gitea-backup beside the script."""
    d = os.path.expanduser((setting or "").strip() or DEFAULT_DIR)
    return d if os.path.isabs(d) else os.path.join(here, d)


def safe_part(text):
    """One folder or file name from Gitea's text (a tag, a file name): never a path,
    never . or .., no control characters."""
    v = re.sub(r"[\x00-\x1f\x7f/\\]", "_", str(text or "")).strip()
    return "_" if v in ("", ".", "..") else v[:200]


def is_single(directory):
    """True for <root>/<NAME> holding <NAME>.db: the one backup of the new layout."""
    d = os.path.normpath(directory)
    return os.path.isfile(os.path.join(d, os.path.basename(d) + ".db"))


def backup_name(directory):
    """NAME of <root>/<NAME> or of an old <root>/<NAME>/<stamp>."""
    d = os.path.normpath(directory)
    return os.path.basename(os.path.dirname(d)) if STAMP_RE.fullmatch(os.path.basename(d)) else os.path.basename(d)


def dated(root, name):
    """(the whole dated backups of NAME, oldest first, and the .partial ones): stamps."""
    d = os.path.join(root, name)
    whole, partial = [], []
    if os.path.isdir(d):
        for s in os.listdir(d):
            full = os.path.join(d, s)
            if not os.path.isdir(full):
                continue
            if STAMP_RE.fullmatch(s) and os.path.isfile(os.path.join(full, f"{name}.db")):
                whole.append(s)
            elif s.endswith(".partial") and STAMP_RE.fullmatch(s[:-len(".partial")]):
                partial.append(s)
    return sorted(whole), sorted(partial)


def _local_stamp(iso):
    try:
        t = datetime.datetime.fromisoformat((iso or "").replace("Z", "+00:00"))
        return t.astimezone().strftime(STAMP)
    except ValueError:
        return ""


def read_meta(db_path):
    """The meta table of a .db, read-only; {} when it cannot be read."""
    try:
        db = sqlite3.connect(f"file:{urllib.parse.quote(db_path)}?mode=ro", uri=True)
        try:
            return dict(db.execute("SELECT key, value FROM meta").fetchall())
        finally:
            db.close()
    except sqlite3.Error:
        return {}


def backups(root, name=None):
    """The backups under root, newest first: (name, when, directory). One a name -
    <root>/<NAME>, `when` its newest run (local time, as a stamp) - and any old dated
    one, <root>/<NAME>/<stamp>, until it is combined. A .partial one is no backup."""
    out = []
    owners = [name] if name else (sorted(os.listdir(root)) if os.path.isdir(root) else [])
    for owner in owners:
        d = os.path.join(root, owner)
        if not os.path.isdir(d) or owner.startswith("."):
            continue
        db = os.path.join(d, f"{owner}.db")
        if os.path.isfile(db):
            when = _local_stamp(read_meta(db).get("updated")) or \
                datetime.datetime.fromtimestamp(os.path.getmtime(db)).strftime(STAMP)
            out.append((owner, when, d))
        for stamp in dated(root, owner)[0]:
            out.append((owner, stamp, os.path.join(d, stamp)))
    return sorted(out, key=lambda b: (b[1], b[0]), reverse=True)


def size_of(path):
    """Bytes of a file, or of everything under a directory."""
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for base, _, names in os.walk(path):
        for n in names:
            try:
                total += os.path.getsize(os.path.join(base, n))
            except OSError:
                pass
    return total


def single_size(directory):
    name = os.path.basename(os.path.normpath(directory))
    return sum(os.path.getsize(p) for p in (os.path.join(directory, f"{name}.db"), os.path.join(directory, f"{name}.zip"))
               if os.path.isfile(p))


# --- the lock, and finishing what a run left ---------------------------------------------

def _holder_alive(line):
    """False only when the lock's process is surely gone: this host, no such pid."""
    parts = line.split()
    if len(parts) < 2 or not parts[0].isdigit():
        return True
    if parts[1] != socket.gethostname():
        return True
    try:
        os.kill(int(parts[0]), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class Lock:
    """<NAME>/.backup.lock, made with O_EXCL: one run at a time updates a backup."""

    def __init__(self, directory, program="gitea-backup"):
        self.dir = directory
        self.path = os.path.join(directory, LOCK)
        self.line = ""
        self.program = program

    def __enter__(self):
        os.makedirs(self.dir, exist_ok=True)
        for attempt in (0, 1):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                try:
                    with open(self.path, encoding="utf-8", errors="replace") as f:
                        holder = f.read().strip()
                except OSError:
                    holder = "?"
                if attempt == 0 and not _holder_alive(holder):
                    with contextlib.suppress(OSError):
                        os.remove(self.path)
                    continue
                raise BackupError(f"{self.dir}: this backup is being updated by another run ({holder}). "
                                  f"If no run is going on, remove {self.path}.")
            self.line = f"{os.getpid()} {socket.gethostname()} {self.program} {now_utc().isoformat()}\n"
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(self.line)
                f.flush()
                os.fsync(f.fileno())
            return self
        raise BackupError(f"{self.path}: cannot take the lock")

    def __exit__(self, *exc):
        try:
            with open(self.path, encoding="utf-8") as f:
                mine = f.read() == self.line
            if mine:
                os.remove(self.path)
        except OSError:
            pass
        return False


def _fsync_file(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_dir(path):
    with contextlib.suppress(OSError):
        _fsync_file(path)


def _install(directory, name, zip_too):
    """<NAME>.zip.tmp and <NAME>.db.tmp into place, behind the commit marker."""
    marker = os.path.join(directory, COMMIT)
    with open(marker, "w", encoding="utf-8") as f:
        f.write(f"gitea-backup {now_utc().isoformat()}\n")
        f.flush()
        os.fsync(f.fileno())
    _fsync_dir(directory)
    db, zp = os.path.join(directory, f"{name}.db"), os.path.join(directory, f"{name}.zip")
    if zip_too and os.path.exists(zp + ".tmp"):
        os.replace(zp + ".tmp", zp)
    if os.path.exists(db + ".tmp"):
        os.replace(db + ".tmp", db)
    os.remove(marker)
    _fsync_dir(directory)


def recover(directory, name, say=print):
    """With the lock held: what a run (or Omi) that broke off left. A commit that was
    renaming is finished; files of a run that had not got so far are removed; an
    append is cut back to the zip's old length; Omi's save that broke off is rolled
    back as Omi does it."""
    db, zp = os.path.join(directory, f"{name}.db"), os.path.join(directory, f"{name}.zip")
    marker = os.path.join(directory, COMMIT)
    if os.path.exists(marker):
        if os.path.exists(zp + ".tmp"):
            os.replace(zp + ".tmp", zp)
        if os.path.exists(db + ".tmp"):
            os.replace(db + ".tmp", db)
        os.remove(marker)
        with contextlib.suppress(OSError):
            os.remove(zp + APPEND)
        say(f"  {name}: finished the commit of a run that broke off")
    for p in (db + ".tmp", zp + ".tmp"):
        if os.path.exists(p):
            os.remove(p)
            say(f"  {name}: removed {os.path.basename(p)} of a run that broke off; the backup is as it was")
    for journal in (zp + APPEND, zp + OMI_JOURNAL):
        if os.path.exists(journal):
            try:
                length = int(open(journal, encoding="utf-8").read().strip() or "-1")
            except ValueError:
                length = -1
            if os.path.exists(zp) and 0 < length <= os.path.getsize(zp):
                with open(zp, "r+b") as f:
                    f.truncate(length)
                    os.fsync(f.fileno())
            os.remove(journal)
            say(f"  {name}: the .zip is back as it was before an "
                f"{'append' if journal.endswith(APPEND) else 'Omi save'} that broke off")
    shutil.rmtree(os.path.join(directory, WORK), ignore_errors=True)


# --- the database -------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS owner(name TEXT PRIMARY KEY, kind TEXT, json TEXT, seen INTEGER);
CREATE TABLE IF NOT EXISTS teams(id INTEGER PRIMARY KEY, name TEXT, json TEXT, members TEXT, repos TEXT, seen INTEGER);
CREATE TABLE IF NOT EXISTS members(login TEXT PRIMARY KEY, json TEXT, seen INTEGER);
CREATE TABLE IF NOT EXISTS repos(full_name TEXT PRIMARY KEY, name TEXT, json TEXT, topics TEXT,
                   git_bundle TEXT, wiki_bundle TEXT, empty INTEGER, collaborators TEXT, seen INTEGER);
CREATE TABLE IF NOT EXISTS labels(repo TEXT, id INTEGER, name TEXT, json TEXT, seen INTEGER, PRIMARY KEY(repo, id));
CREATE TABLE IF NOT EXISTS milestones(repo TEXT, id INTEGER, title TEXT, json TEXT, seen INTEGER, PRIMARY KEY(repo, id));
CREATE TABLE IF NOT EXISTS issues(repo TEXT, number INTEGER, is_pull INTEGER, title TEXT, state TEXT, json TEXT,
                    seen INTEGER, PRIMARY KEY(repo, number));
CREATE TABLE IF NOT EXISTS pulls(repo TEXT, number INTEGER, json TEXT, seen INTEGER, PRIMARY KEY(repo, number));
CREATE TABLE IF NOT EXISTS comments(repo TEXT, id INTEGER, issue_number INTEGER, json TEXT, seen INTEGER,
                    PRIMARY KEY(repo, id));
CREATE TABLE IF NOT EXISTS releases(repo TEXT, id INTEGER, tag TEXT, json TEXT, seen INTEGER, PRIMARY KEY(repo, id));
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, repo TEXT, kind TEXT, parent TEXT, id INTEGER,
                   name TEXT, size INTEGER, sha256 TEXT, uuid TEXT, json TEXT, seen INTEGER);
CREATE INDEX IF NOT EXISTS files_parent ON files(repo, kind, parent);
CREATE INDEX IF NOT EXISTS comments_issue ON comments(repo, issue_number);
"""


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def open_db(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def columns(db, table, schema="main"):
    return [r[1] for r in db.execute(f'PRAGMA {schema}.table_info("{table}")')]


def ensure_schema(db):
    """The tables and columns of SCHEMA, in a .db an older version wrote (no seen)."""
    db.executescript(SCHEMA)
    want = sqlite3.connect(":memory:")
    want.executescript(SCHEMA)
    for t in DATA_TABLES:
        have = set(columns(db, t))
        for r in want.execute(f'PRAGMA table_info("{t}")'):
            if r[1] not in have:
                db.execute(f'ALTER TABLE "{t}" ADD COLUMN "{r[1]}" {r[2]}')
    want.close()
    db.commit()


def number_of(comment):
    m = re.search(r"/(?:issues|pulls)/(\d+)", comment.get("issue_url") or comment.get("html_url") or "")
    return int(m.group(1)) if m else 0


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def copy_entry(src, info, dst):
    """A zip entry into another zip as it is: STORED, its own date, read from this disk."""
    new = zipfile.ZipInfo(info.filename, info.date_time)
    new.compress_type = zipfile.ZIP_STORED
    new.external_attr = info.external_attr
    new.comment = info.comment
    with src.open(info) as s, dst.open(new, "w", force_zip64=True) as d:
        shutil.copyfileobj(s, d, CHUNK)


# --- git bundles ---------------------------------------------------------------------------

def bundle_heads(zf, path):
    """{ref: sha} from a git bundle's header in the zip - read without the pack."""
    heads = {}
    try:
        with zf.open(path) as f:
            if not f.readline().startswith(b"# v"):
                return None
            while True:
                line = f.readline()
                if not line or line == b"\n":
                    return heads
                if line[:1] in (b"@", b"-"):
                    continue
                sha, _, ref = line.decode(errors="replace").strip().partition(" ")
                if ref.startswith("refs/"):
                    heads[ref] = sha
    except (KeyError, OSError, zipfile.BadZipFile):
        return None


def wanted_ref(ref):
    return ref.startswith(("refs/heads/", "refs/tags/")) or re.fullmatch(r"refs/pull/\d+/head", ref) is not None


def remote_heads(url, env):
    """(ok, {ref: sha}) of the branches, tags and pull request heads at url."""
    code, out = git(["ls-remote", url], env=env)
    if code != 0:
        return False, out
    heads = {}
    for line in out.splitlines():
        sha, _, ref = line.partition("\t")
        if wanted_ref(ref) and not ref.endswith("^{}"):
            heads[ref] = sha
    return True, heads


class Stats:
    """What a run did: rows new and changed, files downloaded and kept, git."""
    KEYS = ("issues", "pulls", "comments", "releases")

    def __init__(self):
        self.new = dict.fromkeys(self.KEYS, 0)
        self.changed = dict.fromkeys(self.KEYS, 0)
        self.downloaded = [0, 0]  # files, bytes
        self.copied = [0, 0]      # kept as they were: files, bytes
        self.git_fetched = 0
        self.git_same = 0
        self.missing = []

    def add(self, other):
        for k in self.KEYS:
            self.new[k] += other.new[k]
            self.changed[k] += other.changed[k]
        for a, b in ((self.downloaded, other.downloaded), (self.copied, other.copied)):
            a[0] += b[0]
            a[1] += b[1]
        self.git_fetched += other.git_fetched
        self.git_same += other.git_same
        self.missing += other.missing

    def line(self):
        parts = [f"{k} {self.new[k]} new, {self.changed[k]} changed" for k in self.KEYS]
        return "; ".join(parts) + (f"; files {self.downloaded[0]} downloaded ({ga.human(self.downloaded[1])}), "
                                   f"{self.copied[0]} kept ({ga.human(self.copied[1])})")


def refresh_bundle(w, url, env, old_path, dest, say, st, wiki=False, default=None, seed=None):
    """The repository's (or wiki's) bundle at dest in the zip, or None when there is
    nothing. When the refs at url are the old bundle's, the old one is kept as it is;
    otherwise a mirror seeded from the old bundle (or seed: (zipfile, path)) fetches
    from url - only new objects - and is bundled again. A branch deleted at url stays."""
    kind = "wiki" if wiki else "git"
    ok, heads = remote_heads(url, env)
    if not ok or not heads:
        if old_path and old_path in w.old_names:
            if not ok:
                say(f"    {kind}: cannot read {url}; the old bundle is kept: {str(heads)[-200:]}")
            return old_path
        if not ok and not wiki:
            say(f"    git: cannot read {url}: {str(heads)[-300:]}")
        return None
    if old_path and old_path in w.old_names:
        had = bundle_heads(w.old_zip, old_path) or {}
        if all(had.get(r) == s for r, s in heads.items()):
            st.git_same += 1
            say(f"    {kind}: unchanged")
            return old_path
    mirror = os.path.join(w.tmp, "mirror.git")
    shutil.rmtree(mirror, ignore_errors=True)
    git(["init", "--quiet", "--bare", mirror])
    src = (w.old_zip, old_path) if old_path and old_path in w.old_names else seed
    if src:
        seed_file = os.path.join(w.tmp, "seed.bundle")
        with src[0].open(src[1]) as f, open(seed_file, "wb") as out:
            shutil.copyfileobj(f, out, CHUNK)
        code, out = git(["-C", mirror, "fetch", "--quiet", seed_file, "+refs/*:refs/*"])
        os.remove(seed_file)
        if code != 0:
            say(f"    {kind}: the old bundle could not be read; fetching everything: {out[-200:]}")
    code, out = git(["-C", mirror, "fetch", "--quiet", "--no-tags", url, "+refs/heads/*:refs/heads/*",
                     "+refs/tags/*:refs/tags/*", "+refs/pull/*/head:refs/pull/*/head"], env=env)
    if code != 0:
        shutil.rmtree(mirror, ignore_errors=True)
        if old_path and old_path in w.old_names:
            say(f"    {kind}: git fetch failed; the old bundle is kept: {out[-300:]}")
            return old_path
        raise BackupError(f"{url}: git fetch failed: {out[-500:]}")
    if default:
        git(["-C", mirror, "symbolic-ref", "HEAD", f"refs/heads/{default}"])
    bundle = os.path.join(w.tmp, "repo.bundle")
    code, out = git(["-C", mirror, "bundle", "create", "--quiet", bundle, "--all"])
    shutil.rmtree(mirror, ignore_errors=True)
    if code != 0:
        if "empty bundle" in out.lower():
            return None
        raise BackupError(f"{url}: git bundle failed: {out[-500:]}")
    path, size, _, _ = w.put(old_path or dest, bundle)
    os.remove(bundle)
    st.git_fetched += 1
    say(f"    {kind}: {ga.human(size)}{' (only new objects fetched)' if src else ''}")
    return path


# --- one backup, updated in place ----------------------------------------------------------

class _Writer:
    """The .db and the .zip of one backup being updated - see the top of this file.
    source: 'gitea' or 'github' for a backup run (a new run number; every row written
    gets seen = it); None for a change that is no run (deleting old data).
    only: the repositories this run backs up, when not all of them (a repository at a
    time): the rest stay as current as they were. owner_data: this run reads the
    owner's own rows too (always, when only is None). need: bytes this run may add,
    for choosing between rewriting the zip and appending to it."""

    def __init__(self, directory, name, source=None, only=None, owner_data=True, need=0, say=print):
        self.dir, self.name, self.source, self.say = directory, name, source, say
        self.only = None if only is None else {x.lower() for x in only}
        self.owner_data = owner_data or only is None
        self.need = need
        self.db_path = os.path.join(directory, f"{name}.db")
        self.zip_path = os.path.join(directory, f"{name}.zip")
        self.db_tmp, self.zip_tmp = self.db_path + ".tmp", self.zip_path + ".tmp"
        self.tmp = os.path.join(directory, WORK)
        os.makedirs(self.tmp, exist_ok=True)
        self.existed = os.path.isfile(self.db_path)
        if self.existed:
            shutil.copyfile(self.db_path, self.db_tmp)
        self.db = open_db(self.db_tmp)
        ensure_schema(self.db)
        self.meta = {r["key"]: r["value"] for r in self.db.execute("SELECT key, value FROM meta")}
        runs = int(self.meta.get("runs") or (1 if self.existed else 0))
        self.run = runs + 1 if source else runs
        self.started = now_utc().isoformat()
        self.old_zip = zipfile.ZipFile(self.zip_path) if os.path.isfile(self.zip_path) else None
        self.old_names = set(self.old_zip.namelist()) if self.old_zip else set()
        self.zip = None
        self.mode = None           # 'rewrite' or 'append', at the first new entry
        self.new_names = set()
        self.drop = set()
        self.added = [0, 0]        # entries, bytes written into the zip
        self.readers = []
        self.changes = {}

    # rows ------------------------------------------------------------------------------
    def row(self, table, **values):
        if table in DATA_TABLES and self.source:
            values["seen"] = self.run
            if "json" in values and all(k in values for k in KEYS[table]):
                where = " AND ".join(f'"{k}" = ?' for k in KEYS[table])
                old = self.db.execute(f'SELECT json FROM "{table}" WHERE {where}',
                                      [values[k] for k in KEYS[table]]).fetchone()
                c = self.changes.setdefault(table, [0, 0])
                if old is None:
                    c[0] += 1
                elif old[0] != values["json"]:
                    c[1] += 1
        cols = ", ".join(f'"{c}"' for c in values)
        self.db.execute(f'INSERT OR REPLACE INTO "{table}"({cols}) VALUES ({", ".join("?" * len(values))})',
                        list(values.values()))

    def meta_set(self, **values):
        for k, v in values.items():
            self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (k, str(v)))

    def get(self, table, **key):
        where = " AND ".join(f'"{k}" = ?' for k in key)
        r = self.db.execute(f'SELECT * FROM "{table}" WHERE {where}', list(key.values())).fetchone()
        return dict(r) if r else None

    def file_row(self, repo, kind, parent, fid):
        r = self.db.execute("SELECT * FROM files WHERE repo = ? AND kind = ? AND parent = ? AND id = ? "
                            "ORDER BY path LIMIT 1", (repo, kind, str(parent), fid)).fetchone()
        return dict(r) if r else None

    def keep_file(self, row, **changes):
        """A file the backup has, still at the source: kept as it is, seen now."""
        self.row("files", **dict(row, **changes))

    # the zip ---------------------------------------------------------------------------
    def _free(self, path, ident):
        if path in self.new_names:
            return False
        if ident is None:
            return True
        r = self.db.execute("SELECT repo, kind, parent, id FROM files WHERE path = ?", (path,)).fetchone()
        if r is not None:
            return (r[0], r[1], str(r[2]), r[3]) == (ident[0], ident[1], str(ident[2]), ident[3])
        return path not in self.old_names

    def unique(self, path, ident=None):
        """path, or path-1, -2 ... when this run wrote it already, or another file has it."""
        base, n = path, 1
        while not self._free(path, ident):
            stem, ext = os.path.splitext(base)
            path, n = f"{stem}-{n}{ext}", n + 1
        self.new_names.add(path)
        return path

    def _zip(self):
        if self.zip is None:
            old = os.path.getsize(self.zip_path) if self.old_zip else 0
            free = disk_free(self.dir)
            if self.old_zip is None or self.drop or free - self.need > old + (100 << 20):
                self.mode = "rewrite"
                self.zip = zipfile.ZipFile(self.zip_tmp, "w", zipfile.ZIP_STORED, allowZip64=True)
            else:
                # No room for a copy of the zip: the new entries go after its end, with
                # its old length in a journal until they are whole.
                self.mode = "append"
                self.say(f"  {ga.human(free)} free, the zip is {ga.human(old)}: appending to it this time")
                with open(self.zip_path + APPEND, "w", encoding="utf-8") as f:
                    f.write(str(old))
                    f.flush()
                    os.fsync(f.fileno())
                self.old_zip.close()
                self.zip = zipfile.ZipFile(self.zip_path, "a", zipfile.ZIP_STORED, allowZip64=True)
                self.zip.start_dir = old
                self.zip.fp.seek(old)
                self.old_zip = zipfile.ZipFile(self.zip_path)
        return self.zip

    def put(self, path, src, ident=None, prev=None):
        """The file src into the zip at path (or a free name near it). prev: the files
        row the backup has for it - when its size and SHA-256 are src's, nothing is
        written and its entry is kept. Returns (path, size, sha256, written)."""
        size, digest = os.path.getsize(src), sha256_file(src)
        if prev and prev.get("path") in self.old_names and prev.get("size") == size and prev.get("sha256") == digest:
            return prev["path"], size, digest, False
        if prev and prev.get("path"):
            path = prev["path"]
        path = self.unique(path, ident)
        z = self._zip()
        if self.mode == "append" and path in z.NameToInfo:
            old = z.NameToInfo.pop(path)
            z.filelist.remove(old)
        info = zipfile.ZipInfo(path, time.localtime()[:6])
        info.compress_type = zipfile.ZIP_STORED
        with open(src, "rb") as f, z.open(info, "w", force_zip64=True) as out:
            shutil.copyfileobj(f, out, CHUNK)
        self.added[0] += 1
        self.added[1] += size
        return path, size, digest, True

    def put_stream(self, path, fill, ident=None, prev=None):
        """Like put, from fill(out) -> (size, sha256): written to a file of this run
        first, so a download that breaks off never replaces a whole old entry."""
        tmp = os.path.join(self.tmp, f"download-{uuid.uuid4().hex}")
        try:
            with open(tmp, "wb") as out:
                fill(out)
            return self.put(path, tmp, ident, prev)
        finally:
            with contextlib.suppress(OSError):
                os.remove(tmp)

    def add_stream(self, path, fill, ident=None):
        p, size, digest, _ = self.put_stream(path, fill, ident)
        return p, size, digest

    def add_file(self, path, src, ident=None):
        p, size, digest, _ = self.put(path, src, ident)
        return p, size, digest

    def previous(self):
        """The backup as it was before this run, for reading (None when it is new)."""
        if not self.existed:
            return None
        b = Backup(self.dir, migrate=False)
        self.readers.append(b)
        return b

    # the end ---------------------------------------------------------------------------
    def _carry(self):
        """A run of some repositories only: the rows it did not read stay as current as
        they were."""
        if not self.source or self.only is None or self.run <= 1:
            return
        names = sorted(self.only)
        marks = ",".join("?" * len(names))
        for t in DATA_TABLES:
            if t == "repos":
                cond, args = f"lower(full_name) NOT IN ({marks})", names
            elif t in ("owner", "teams", "members"):
                if self.owner_data:
                    continue
                cond, args = "1", []
            else:
                cond = f"lower(repo) NOT IN ({marks})" + ("" if not self.owner_data else " AND repo != ''")
                args = names
            self.db.execute(f'UPDATE "{t}" SET seen = ? WHERE seen = ? AND {cond}', [self.run, self.run - 1] + args)

    def commit(self):
        for b in self.readers:
            b.close()
        self.readers = []
        if self.source:
            self._carry()
            sources = sorted({s for s in (self.meta.get("sources") or "").split(",") if s} | {self.source})
            self.meta_set(format=FORMAT, created=self.meta.get("created") or self.started, updated=self.started,
                          finished=now_utc().isoformat(), runs=self.run, sources=",".join(sources),
                          source=self.source, complete="yes", kept=KEPT_NOTE)
        self.db.commit()
        zip_too = self.zip is not None or bool(self.drop) or self.old_zip is None
        appended = self.mode == "append"
        if zip_too:
            z = self._zip() if not appended else self.zip
            if self.mode == "rewrite" and self.old_zip:
                for info in self.old_zip.infolist():
                    if info.filename not in self.new_names and info.filename not in self.drop:
                        copy_entry(self.old_zip, info, z)
            z.close()
            _fsync_file(self.zip_tmp if not appended else self.zip_path)
        if self.old_zip:
            self.old_zip.close()
        self.db.close()
        _fsync_file(self.db_tmp)
        if appended:
            os.remove(self.zip_path + APPEND)  # the zip is whole with the new entries
        _install(self.dir, self.name, zip_too and not appended)
        shutil.rmtree(self.tmp, ignore_errors=True)

    close = commit

    def abort(self):
        for b in self.readers:
            with contextlib.suppress(Exception):
                b.close()
        if self.zip is not None:
            if self.mode == "append":
                self.zip._didModify = False  # no new end record: the zip goes back as it was
            with contextlib.suppress(Exception):
                self.zip.close()
        if self.old_zip is not None:
            with contextlib.suppress(Exception):
                self.old_zip.close()
        with contextlib.suppress(Exception):
            self.db.close()
        with contextlib.suppress(OSError):
            os.remove(self.db_tmp)
        with contextlib.suppress(OSError):
            os.remove(self.zip_tmp)
        if self.mode == "append":
            # Back to the zip's old length; the journal goes with it.
            recover(self.dir, self.name, lambda m: None)
        shutil.rmtree(self.tmp, ignore_errors=True)


def disk_free(path):
    """Free bytes on path's file system (tests replace it)."""
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


@contextlib.contextmanager
def updating(root, name, source, say=print, only=None, owner_data=True, need=0):
    """`with updating(root, NAME, 'gitea') as w:` - the lock, what a run that broke off
    left, the dated backups combined, then a _Writer: committed when the block ends,
    and nothing changed when it raises."""
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    with Lock(d):
        recover(d, name, say)
        try:
            _combine_locked(root, name, say)
        except BackupError as e:
            say(f"{e} This run goes on with the one backup.")
        w = _Writer(d, name, source, only=only, owner_data=owner_data, need=need, say=say)
        try:
            yield w
        except BaseException:
            w.abort()
            raise
        w.commit()


# --- combining the dated backups of older versions ------------------------------------------

class _Source:
    def __init__(self, label, db_path, zip_path):
        self.label, self.db_path, self.zip_path = label, db_path, zip_path
        self.meta = read_meta(db_path)
        when = self.meta.get("updated") or self.meta.get("created") or ""
        try:
            self.when = datetime.datetime.fromisoformat(when.replace("Z", "+00:00"))
            if self.when.tzinfo is None:
                self.when = self.when.replace(tzinfo=datetime.timezone.utc)
        except ValueError:
            try:
                self.when = datetime.datetime.strptime(label[:19], STAMP).astimezone()
            except ValueError:
                self.when = datetime.datetime.fromtimestamp(os.path.getmtime(db_path), datetime.timezone.utc)
        self.runs = int(self.meta.get("runs") or 1)
        self.zip = None

    def open(self):
        try:
            self.zip = zipfile.ZipFile(self.zip_path)
        except (OSError, zipfile.BadZipFile) as e:
            raise BackupError(f"{self.label}: {os.path.basename(self.zip_path)} is damaged ({e})")
        return self.zip

    def close(self):
        if self.zip:
            self.zip.close()


def _sources(root, name, stamps):
    d = os.path.join(root, name)
    out = []
    for s in stamps:
        sd = os.path.join(d, s)
        zp = os.path.join(sd, f"{name}.zip")
        if not os.path.isfile(zp):
            raise BackupError(f"{s}: {name}.zip is missing")
        out.append(_Source(s, os.path.join(sd, f"{name}.db"), zp))
    if os.path.isfile(os.path.join(d, f"{name}.db")):
        out.append(_Source("the combined backup", os.path.join(d, f"{name}.db"), os.path.join(d, f"{name}.zip")))
    return sorted(out, key=lambda s: s.when)


def _check_source(src):
    """The source's .db and every zip entry (CRC) are readable, or BackupError."""
    try:
        db = sqlite3.connect(f"file:{urllib.parse.quote(src.db_path)}?mode=ro", uri=True)
        ok = db.execute("PRAGMA quick_check").fetchone()[0]
        db.close()
    except sqlite3.Error as e:
        ok = str(e)
    if ok != "ok":
        raise BackupError(f"{src.label}: its .db is damaged ({ok})")
    if not os.path.isfile(src.zip_path):
        return
    z = src.open()
    try:
        bad = z.testzip()
    except (OSError, zipfile.BadZipFile, EOFError, ValueError) as e:
        bad = f"? ({e})"
    if bad:
        raise BackupError(f"{src.label}: {os.path.basename(src.zip_path)} is damaged at {bad}")


def _merge_bundles(work, versions):
    """versions: [(zipfile, info)] oldest first -> a bundle file with every ref of
    all of them (a ref in several: the newest)."""
    mirror = os.path.join(work, "merge.git")
    shutil.rmtree(mirror, ignore_errors=True)
    git(["init", "--quiet", "--bare", mirror])
    for z, info in versions:
        b = os.path.join(work, "part.bundle")
        with z.open(info) as s, open(b, "wb") as out:
            shutil.copyfileobj(s, out, CHUNK)
        code, out = git(["-C", mirror, "fetch", "--quiet", b, "+refs/*:refs/*"])
        os.remove(b)
        if code != 0:
            raise BackupError(f"{info.filename}: a bundle could not be read: {out[-300:]}")
    merged = os.path.join(work, "merged.bundle")
    code, out = git(["-C", mirror, "bundle", "create", "--quiet", merged, "--all"])
    shutil.rmtree(mirror, ignore_errors=True)
    if code != 0:
        raise BackupError(f"{versions[-1][1].filename}: the bundles could not be merged: {out[-300:]}")
    return merged


def _combined_meta(sources, stamps, runs):
    metas = [s.meta for s in sources]
    meta = dict(metas[-1])
    meta.pop("based_on", None)
    created = [m.get("created") for m in metas if m.get("created")]
    srcs, gh = set(), ""
    merged = []
    for s in sources:
        m = s.meta
        srcs |= {x for x in (m.get("sources") or "").split(",") if x} or \
            {m.get("source") or ("gitea" if m.get("gitea") else "")} - {""}
        g = m.get("github_updated") or (m.get("created") if m.get("source") == "github" else "")
        gh = max(gh, g or "")
        merged += json.loads(m.get("merged") or "[]")
    meta.update(format=str(FORMAT), created=min(created) if created else now_utc().isoformat(),
                updated=metas[-1].get("updated") or metas[-1].get("created") or "", runs=str(runs),
                sources=",".join(sorted(srcs)), complete="yes", kept=KEPT_NOTE,
                merged=json.dumps(sorted(set(merged) | set(stamps))))
    if gh:
        meta["github_updated"] = gh
    for k in ("gitea", "github"):
        v = next((m.get(k) for m in reversed(metas) if m.get(k)), "")
        if v:
            meta[k] = v
    return meta


def _combine_locked(root, name, say, dry_run=False, report=None):
    """combine() with the lock held. Returns a dict of what was done, or None when
    NAME has no dated backups."""
    d = os.path.join(root, name)
    stamps, partials = dated(root, name)
    for p in partials:
        say(f"  {name}/{p}: a backup that broke off ({ga.human(size_of(os.path.join(d, p)))}) - not combined; "
            f"Backups -> Combine dated backups offers to delete it")
    if not stamps:
        return None
    sources = _sources(root, name, stamps)
    before = sum(size_of(os.path.join(d, s)) for s in stamps) + single_size(d)
    say(f"{name}: combining {len(stamps)} dated backups ({', '.join(stamps)}"
        f"{', and the combined one' if len(sources) > len(stamps) else ''}; {ga.human(before)}) "
        f"into {d}/{name}.db and {name}.zip")
    out = {"name": name, "stamps": stamps, "before": before, "dry_run": dry_run}
    try:
        for s in sources:
            _check_source(s)
        latest = {}   # path -> (source, info), the newest wins
        versions = {}
        for s in sources:
            for info in s.zip.infolist():
                latest[info.filename] = (s, info)
                versions.setdefault(info.filename, []).append((s, info))
        estimate = sum(info.file_size for _, info in latest.values()) + max(os.path.getsize(s.db_path) for s in sources)
        out.update(entries=len(latest), estimate=estimate, superseded=sum(len(v) - 1 for v in versions.values()))
        if dry_run:
            say(f"  dry run: {len(latest)} files in the zip ({out['superseded']} older copies left out), "
                f"combined about {ga.human(estimate)}; about {ga.human(max(0, before - estimate))} freed. "
                f"Nothing changed.")
            out["freed"] = max(0, before - estimate)
            return out
        need = estimate + (100 << 20)
        if disk_free(d) < need:
            raise BackupError(f"{ga.human(disk_free(d))} free, the combined backup needs about {ga.human(need)}")
        db_tmp, zip_tmp = os.path.join(d, f"{name}.db.tmp"), os.path.join(d, f"{name}.zip.tmp")
        work = os.path.join(d, WORK)
        os.makedirs(work, exist_ok=True)
        try:
            new = open_db(db_tmp)
            ensure_schema(new)
            offset = 0
            for s in sources:
                new.execute("ATTACH DATABASE ? AS s", (s.db_path,))
                have = {r[0] for r in new.execute("SELECT name FROM s.sqlite_master WHERE type = 'table'")}
                for t in DATA_TABLES:
                    if t not in have:
                        continue
                    scols = columns(new, t, "s")
                    cols = [c for c in scols if c in columns(new, t) and c != "seen"]
                    seen = f"{offset} + COALESCE(seen, {s.runs})" if "seen" in scols else str(offset + s.runs)
                    names = ", ".join(f'"{c}"' for c in cols)
                    new.execute(f'INSERT OR REPLACE INTO main."{t}"({names}, seen) SELECT {names}, {seen} FROM s."{t}"')
                new.commit()
                new.execute("DETACH DATABASE s")
                offset += s.runs
            new.execute("DELETE FROM meta")
            for k, v in _combined_meta(sources, stamps, offset).items():
                new.execute("INSERT INTO meta(key, value) VALUES (?, ?)", (k, v))
            new.commit()
            bundles = {p for r in new.execute("SELECT git_bundle, wiki_bundle FROM repos") for p in r if p}
            merged = 0
            with zipfile.ZipFile(zip_tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
                for path, (s, info) in latest.items():
                    vs = versions[path]
                    if path in bundles and len({v[1].CRC for v in vs}) > 1:
                        newest = bundle_heads(s.zip, path) or {}
                        older = {}
                        for vz, vi in vs[:-1]:
                            older.update(bundle_heads(vz.zip, path) or {})
                        if any(r not in newest for r in older):  # a ref only an older one has
                            b = _merge_bundles(work, [(vz.zip, vi) for vz, vi in vs])
                            info2 = zipfile.ZipInfo(path, info.date_time)
                            info2.compress_type = zipfile.ZIP_STORED
                            with open(b, "rb") as f, z.open(info2, "w", force_zip64=True) as dst:
                                shutil.copyfileobj(f, dst, CHUNK)
                            os.remove(b)
                            merged += 1
                            out.setdefault("merged_bundles", []).append(path)
                            continue
                    copy_entry(s.zip, info, z)
            _fsync_file(zip_tmp)
            # Verify before anything is deleted.
            rows = _verify_combined(new, sources, zip_tmp, latest, set(out.get("merged_bundles", [])))
            new.close()
            _fsync_file(db_tmp)
        except BaseException:
            with contextlib.suppress(Exception):
                new.close()
            for p in (db_tmp, zip_tmp):
                with contextlib.suppress(OSError):
                    os.remove(p)
            shutil.rmtree(work, ignore_errors=True)
            raise
    except BackupError as e:
        raise BackupError(f"{name}: not combined: {e}. Nothing was deleted.")
    finally:
        for s in sources:
            s.close()
    _install(d, name, True)
    shutil.rmtree(os.path.join(d, WORK), ignore_errors=True)
    for s in stamps:
        shutil.rmtree(os.path.join(d, s))
    _fsync_dir(d)
    after = single_size(d)
    out.update(rows=rows, merged=merged, after=after, freed=before - after)
    say(f"  verified: {sum(rows.values())} rows ({', '.join(f'{t} {n}' for t, n in rows.items() if n)}), "
        f"{len(latest)} files ({out['superseded']} older copies left out, {merged} bundles merged); "
        f"zip tested whole")
    say(f"  deleted the {len(stamps)} dated directories: {ga.human(before)} -> {ga.human(after)}, "
        f"{ga.human(before - after)} freed")
    return out


def _verify_combined(db, sources, zip_path, latest, merged):
    """Every row key of every source is in db, every zip path of every source in the
    zip (at the newest copy's CRC, but for merged bundles) and the zip tests whole.
    Returns rows per table, or raises BackupError."""
    rows = {}
    for s in sources:
        db.execute("ATTACH DATABASE ? AS s", (s.db_path,))
        have = {r[0] for r in db.execute("SELECT name FROM s.sqlite_master WHERE type = 'table'")}
        for t in DATA_TABLES:
            if t not in have:
                continue
            key = ", ".join(f'"{k}"' for k in KEYS[t])
            n = db.execute(f'SELECT COUNT(*) FROM (SELECT {key} FROM s."{t}" EXCEPT SELECT {key} FROM main."{t}")').fetchone()[0]
            if n:
                db.execute("DETACH DATABASE s")
                raise BackupError(f"verification: {n} rows of {t} of {s.label} are not in the combined backup")
        db.execute("DETACH DATABASE s")
    for t in DATA_TABLES:
        rows[t] = db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    with zipfile.ZipFile(zip_path) as z:
        infos = {i.filename: i for i in z.infolist()}
        for s in sources:
            for info in s.zip.infolist():
                if info.filename not in infos:
                    raise BackupError(f"verification: {info.filename} of {s.label} is not in the combined zip")
        for path, (s, info) in latest.items():
            if path not in merged and (infos[path].CRC, infos[path].file_size) != (info.CRC, info.file_size):
                raise BackupError(f"verification: {path} is not the newest copy ({s.label})")
        bad = z.testzip()
        if bad:
            raise BackupError(f"verification: the combined zip is damaged at {bad}")
        missing = [r[0] for r in db.execute("SELECT path FROM files") if r[0] not in infos and
                   any(r[0] in v.zip.NameToInfo for v in sources)]
        if missing:
            raise BackupError(f"verification: {len(missing)} files of the .db are not in the combined zip")
    return rows


def combine(root, name, say=print, dry_run=False, delete_partial=False):
    """Combines the dated backups of NAME (<NAME>/YYYY-MM-DD_HH-MM-SS/) into the one
    backup <NAME>/<NAME>.db and .zip, verifies it and deletes them - see the top of
    this file. Needs no Gitea and no GitHub. dry_run: only says what would be done.
    delete_partial: .partial directories (backups that broke off) are deleted too."""
    d = os.path.join(root, name)
    if not os.path.isdir(d):
        raise BackupError(f"{d}: no backups of {name}")
    with Lock(d):
        if not dry_run:
            recover(d, name, say)
        if delete_partial:
            for p in dated(root, name)[1]:
                size = size_of(os.path.join(d, p))
                if dry_run:
                    say(f"  dry run: {name}/{p} would be deleted ({ga.human(size)})")
                else:
                    shutil.rmtree(os.path.join(d, p))
                    say(f"  deleted {name}/{p}, a backup that broke off: {ga.human(size)} freed")
        return _combine_locked(root, name, say, dry_run)


def owners_with_dated(root):
    if not os.path.isdir(root):
        return []
    return [n for n in sorted(os.listdir(root)) if not n.startswith(".") and any(dated(root, n))]


def combine_all(root, say=print, dry_run=False, delete_partial=False, names=None):
    """combine() for every owner under root with dated backups (or names). One that
    fails is reported and the others are still combined. Returns the results."""
    results = []
    for n in names or owners_with_dated(root):
        try:
            r = combine(root, n, say, dry_run, delete_partial)
            if r:
                results.append(r)
        except BackupError as e:
            say(str(e))
            results.append({"name": n, "error": str(e)})
    done = [r for r in results if "freed" in r]
    if len(done) > 1:
        say(f"Together: {ga.human(sum(r['freed'] for r in done))} {'would be ' if dry_run else ''}freed")
    return results


# --- backing up ---------------------------------------------------------------------------

def owner_kind(client, name):
    s, d = client.request("GET", f"/orgs/{name}")
    if s == 200:
        return "organization", d
    s, d = client.request("GET", f"/users/{name}")
    if s == 200:
        return "user", d
    raise BackupError(f"{name}: no such organization or user in Gitea")


def _attachment(client, w, repo, kind, parent, folder, asset, say, st):
    """One Gitea attachment: kept when the backup has it (id, size, uuid), else
    downloaded - and kept still when its bytes are the old ones."""
    prev = w.file_row(repo, kind, parent, asset.get("id"))
    if prev and prev["path"] in w.old_names and prev["size"] == asset.get("size") and \
            (prev["uuid"] or "") == (asset.get("uuid") or ""):
        w.keep_file(prev, json=dumps(asset))
        st.copied[0] += 1
        st.copied[1] += prev["size"] or 0
        return 0
    try:
        path, size, digest, written = w.put_stream(folder, lambda out: client.download(client.file_url(asset), out),
                                                   (repo, kind, str(parent), asset.get("id")), prev)
    except (BackupError, urllib.error.URLError, OSError) as e:
        say(f"    not saved: {folder}: {e}")
        if prev:
            w.keep_file(prev)  # still at the source: the old copy stays current
        return 0
    w.row("files", path=path, repo=repo, kind=kind, parent=str(parent), id=asset.get("id"),
          name=asset.get("name") or "", size=size, sha256=digest, uuid=asset.get("uuid") or "", json=dumps(asset))
    st.downloaded[0] += 1
    st.downloaded[1] += size
    return size


def backup_repo(client, w, repo, say, st=None):
    st = st or Stats()
    full = repo["full_name"]
    short = safe_part(repo["name"])
    say(f"  {full}")
    prev = w.get("repos", full_name=full)
    s, topics = client.request("GET", f"/repos/{full}/topics")
    topics = (topics or {}).get("topics", []) if s == 200 and isinstance(topics, dict) else []
    env = client.git_env()
    if repo.get("empty"):
        git_bundle = prev and prev["git_bundle"]
    else:
        git_bundle = refresh_bundle(w, client.git_url(full), env, prev and prev["git_bundle"],
                                    f"{short}/git.bundle", say, st, default=repo.get("default_branch"))
    if repo.get("has_wiki"):
        wiki_bundle = refresh_bundle(w, client.git_url(full, True), env, prev and prev["wiki_bundle"],
                                     f"{short}/wiki.bundle", say, st, wiki=True)
    else:
        wiki_bundle = prev and prev["wiki_bundle"]
    collaborators = []
    for u in ga.paged(client.request, f"/repos/{full}/collaborators"):
        s, perm = client.request("GET", f"/repos/{full}/collaborators/{u['login']}/permission")
        collaborators.append({"login": u["login"], "permission": (perm or {}).get("permission", "read")
                              if s == 200 and isinstance(perm, dict) else "read"})
    w.row("repos", full_name=full, name=repo["name"], json=dumps(repo), topics=dumps(topics),
          git_bundle=git_bundle, wiki_bundle=wiki_bundle, empty=0 if git_bundle else 1,
          collaborators=dumps(collaborators))
    size = 0
    for label in ga.paged(client.request, f"/repos/{full}/labels"):
        w.row("labels", repo=full, id=label["id"], name=label.get("name"), json=dumps(label))
    for m in ga.paged(client.request, f"/repos/{full}/milestones?state=all"):
        w.row("milestones", repo=full, id=m["id"], title=m.get("title"), json=dumps(m))
    issues = pulls = comments = 0
    for i in ga.paged(client.request, f"/repos/{full}/issues?state=all"):
        is_pull = 1 if i.get("pull_request") else 0
        was = w.get("issues", repo=full, number=i["number"])
        kind = "pulls" if is_pull else "issues"
        if was is None:
            st.new[kind] += 1
        elif json.loads(was["json"] or "{}").get("updated_at") != i.get("updated_at"):
            st.changed[kind] += 1
        w.row("issues", repo=full, number=i["number"], is_pull=is_pull, title=i.get("title"),
              state=i.get("state"), json=dumps(i))
        issues += 1
        for a in i.get("assets") or []:
            size += _attachment(client, w, full, "issues", i["number"], f"{short}/issues/{i['number']}/"
                                f"{a['id']}-{safe_part(a.get('name'))}", a, say, st)
        if is_pull:
            old_pr = w.get("pulls", repo=full, number=i["number"])
            if old_pr and was and json.loads(was["json"] or "{}").get("updated_at") == i.get("updated_at"):
                w.row("pulls", **old_pr)  # unchanged: not asked again
                pulls += 1
                continue
            s, pr = client.request("GET", f"/repos/{full}/pulls/{i['number']}")
            if s == 200:
                w.row("pulls", repo=full, number=i["number"], json=dumps(pr))
                pulls += 1
    for c in ga.paged(client.request, f"/repos/{full}/issues/comments"):
        n = number_of(c)
        was = w.get("comments", repo=full, id=c["id"])
        if was is None:
            st.new["comments"] += 1
        elif json.loads(was["json"] or "{}").get("updated_at") != c.get("updated_at"):
            st.changed["comments"] += 1
        w.row("comments", repo=full, id=c["id"], issue_number=n, json=dumps(c))
        comments += 1
        for a in c.get("assets") or []:
            size += _attachment(client, w, full, "comments", c["id"], f"{short}/comments/{n}/{c['id']}/"
                                f"{a['id']}-{safe_part(a.get('name'))}", a, say, st)
    releases = 0
    for r in ga.paged(client.request, f"/repos/{full}/releases"):
        was = w.get("releases", repo=full, id=r["id"])
        if was is None:
            st.new["releases"] += 1
        elif was["json"] != dumps(r):
            st.changed["releases"] += 1
        w.row("releases", repo=full, id=r["id"], tag=r.get("tag_name"), json=dumps(r))
        releases += 1
        tag = safe_part(r.get("tag_name") or r["id"])
        for a in r.get("assets") or []:
            size += _attachment(client, w, full, "releases", r["id"], f"{short}/releases/{tag}/"
                                f"{safe_part(a.get('name'))}", a, say, st)
    say(f"    {issues - pulls} issues, {pulls} pull requests, {comments} comments, {releases} releases, "
        f"files downloaded {ga.human(size)}")
    return st


def _owner_rows(client, w, name, kind, info, say, st):
    w.row("owner", name=name, kind=kind, json=dumps(info))
    url = info.get("avatar_url")
    prev = w.file_row("", "avatar", "", 0)
    if prev and prev["path"] in w.old_names and json.loads(prev["json"] or "{}").get("avatar_url") == url:
        w.keep_file(prev)
        st.copied[0] += 1
        st.copied[1] += prev["size"] or 0
    elif url:
        try:
            path, size, digest, written = w.put_stream("owner/avatar.png", lambda out: client.download(url, out),
                                                       ("", "avatar", "", 0), prev)
            w.row("files", path=path, repo="", kind="avatar", parent="", id=0, name="avatar.png",
                  size=size, sha256=digest, uuid="", json=dumps({"avatar_url": url}))
            st.downloaded[0] += 1
            st.downloaded[1] += size
        except (urllib.error.URLError, OSError):
            if prev:
                w.keep_file(prev)
    if kind == "organization":
        for label in ga.paged(client.request, f"/orgs/{name}/labels"):
            w.row("labels", repo="", id=label["id"], name=label.get("name"), json=dumps(label))
        for m in ga.paged(client.request, f"/orgs/{name}/members"):
            w.row("members", login=m.get("login"), json=dumps(m))
        for t in ga.paged(client.request, f"/orgs/{name}/teams"):
            people = [m.get("login") for m in ga.paged(client.request, f"/teams/{t['id']}/members")]
            held = [r.get("name") for r in ga.paged(client.request, f"/teams/{t['id']}/repos")]
            w.row("teams", id=t["id"], name=t.get("name"), json=dumps(t), members=dumps(people), repos=dumps(held))


def backup(client, name, root, say, excludes=(), only=None, owner_data=True, need=0):
    """Every record and file of the organization or user NAME, into its one backup
    root/NAME/NAME.db and NAME.zip: made, or updated in place with only what is new
    or changed. only: these repositories (full names) and no others this run - their
    rows are updated, the rest stay as they were; owner_data: with only, the owner's
    own rows (teams, members, labels, avatar) too. Returns root/NAME; backup.stats is
    what was done."""
    name = ga.normalize_scope(name)
    if not name or "/" in name:
        raise BackupError("A backup is of an organization or a user: give its name.")
    kind, info = owner_kind(client, name)
    name = info.get("username") or info.get("login") or name
    if only is None:
        repos = ga.scope_repos(client.request, name)
        repos = [r for r in repos if r["full_name"].lower() not in {e.lower() for e in excludes}]
    else:
        want = {x.lower() for x in only}
        repos = [r for r in ga.scope_repos(client.request, name) if r["full_name"].lower() in want] if want else []
    st = Stats()
    with updating(root, name, "gitea", say, only=only, owner_data=owner_data, need=need) as w:
        say(f"{'Updating the backup of' if w.existed else 'Backing up'} the {kind} {name} (run {w.run}): "
            f"{len(repos)} repositories -> {w.dir}")
        w.meta_set(gitea=client.root, owner=name, kind=kind)
        if only is None or owner_data:
            _owner_rows(client, w, name, kind, info, say, st)
        for repo in repos:
            backup_repo(client, w, repo, say, st)
        d = w.dir
        written = list(w.added)
        mode = w.mode
    db, zp = os.path.join(d, f"{name}.db"), os.path.join(d, f"{name}.zip")
    say(f"Summary: {st.line()}; git {st.git_fetched} fetched, {st.git_same} unchanged; zip: {written[0]} new "
        f"entries ({ga.human(written[1])}){', appended' if mode == 'append' else ''}")
    say(f"Backup ready: {db} ({ga.human(os.path.getsize(db))}), {zp} ({ga.human(os.path.getsize(zp))})")
    backup.stats = st
    return d


# --- deleting an organization or user -----------------------------------------------------

def owner_summary(client, name):
    """What deleting NAME removes: (kind, repositories, issues and PRs, releases,
    attachment bytes, git bytes)."""
    kind, _ = owner_kind(client, name)
    repos = ga.scope_repos(client.request, name)
    issues = releases = 0
    for r in repos:
        issues += r.get("open_issues_count", 0) + r.get("open_pr_counter", 0)
        releases += r.get("release_counter", 0)
    rows = ga.usage(client.request, repos, ("releases",))
    return kind, repos, issues, releases, sum(r["attachments"] for r in rows), sum(r["git"] for r in rows)


def delete_owner(client, name, say):
    """Deletes every repository of NAME - with its issues, pull requests, releases,
    wiki and files - its packages, and then the organization or user itself."""
    kind, _ = owner_kind(client, name)
    s, me = client.request("GET", "/user")
    if s == 200 and (me or {}).get("login", "").lower() == name.lower():
        raise BackupError(f"{name} is the token's own user: Gitea cannot delete it with its own token.")
    failed = 0
    for repo in ga.scope_repos(client.request, name):
        s, d = client.request("DELETE", f"/repos/{repo['full_name']}")
        if s in (200, 204):
            say(f"  deleted {repo['full_name']}")
        else:
            failed += 1
            say(f"  not deleted: {repo['full_name']}: {s} {d}")
    for p in ga.paged(client.request, f"/packages/{name}"):
        s, d = client.request("DELETE", f"/packages/{name}/{p['type']}/{urllib.parse.quote(p['name'], safe='')}/"
                                        f"{urllib.parse.quote(p['version'], safe='')}")
        say(f"  {'deleted' if s in (200, 204) else f'not deleted ({s})'} package {p['type']} {p['name']} {p['version']}")
    if failed:
        raise BackupError(f"{failed} repositories were not deleted, so {name} itself is left.")
    path = f"/orgs/{name}" if kind == "organization" else f"/admin/users/{name}?purge=true"
    s, d = client.request("DELETE", path)
    if s not in (200, 204):
        raise BackupError(f"The {kind} {name} was not deleted: {s} {d}"
                          + (" (deleting a user needs an admin token)" if kind == "user" else ""))
    say(f"Deleted the {kind} {name} from Gitea.")


# --- reading a backup ---------------------------------------------------------------------

class Backup:
    """A backup opened for reading: <root>/<NAME> (the one backup), or an old dated
    <root>/<NAME>/<stamp>. Opening <root>/<NAME> combines its dated backups first,
    unless migrate is False."""

    def __init__(self, directory, migrate=True, say=print):
        d = os.path.normpath(directory)
        base = os.path.basename(d)
        if migrate and not STAMP_RE.fullmatch(base) and dated(os.path.dirname(d), base)[0]:
            try:
                combine(os.path.dirname(d), base, say)
            except BackupError as e:
                say(str(e))
        dbs = [f for f in os.listdir(d) if f.endswith(".db")] if os.path.isdir(d) else []
        if len(dbs) != 1:
            raise BackupError(f"{directory}: not a backup (no single .db)")
        self.dir = d
        self.name = dbs[0][:-3]
        self.db = open_db(os.path.join(d, dbs[0]))
        self.meta = {r["key"]: r["value"] for r in self.db.execute("SELECT key, value FROM meta")}
        if self.meta.get("format") != str(FORMAT):
            raise BackupError(f"{directory}: backup format {self.meta.get('format')}, this reads {FORMAT}")
        self.zip = zipfile.ZipFile(os.path.join(d, f"{self.name}.zip"))

    def rows(self, sql, *args):
        return [dict(r) for r in self.db.execute(sql, args)]

    def owner(self):
        r = self.rows("SELECT * FROM owner")[0]
        return r["kind"], json.loads(r["json"])

    def repos(self):
        out = []
        for r in self.rows("SELECT * FROM repos ORDER BY full_name"):
            r["json"] = json.loads(r["json"])
            r["topics"] = json.loads(r["topics"] or "[]")
            r["collaborators"] = json.loads(r.get("collaborators") or "[]")
            out.append(r)
        return out

    def json_rows(self, table, repo, order):
        return [json.loads(r["json"]) for r in self.rows(f"SELECT json FROM {table} WHERE repo = ? ORDER BY {order}", repo)]

    def files(self, repo, kind, parent):
        return self.rows("SELECT * FROM files WHERE repo = ? AND kind = ? AND parent = ? ORDER BY id", repo, kind, str(parent))

    def extract(self, path, dest):
        with self.zip.open(path) as src, open(dest, "wb") as out:
            shutil.copyfileobj(src, out, CHUNK)

    def close(self):
        self.db.close()
        self.zip.close()


# --- old data: what older backups had that the newest run did not see -------------------------

def old_only(directory):
    """(runs, {table: [rows]}, {zip path: bytes}) of the one backup at directory: the
    rows with seen < runs - deleted at the source since, or only in older backups -
    and the zip entries that go with them: their files, and the bundles of a
    repository that is old as a whole (when no current one shares the path)."""
    name = os.path.basename(os.path.normpath(directory))
    db_path = os.path.join(directory, f"{name}.db")
    if not os.path.isfile(db_path):
        raise BackupError(f"{directory}: no backup {name}.db (dated backups are combined first)")
    db = sqlite3.connect(f"file:{urllib.parse.quote(db_path)}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        meta = dict(db.execute("SELECT key, value FROM meta").fetchall())
        runs = int(meta.get("runs") or 0)
        data, paths = {}, {}
        if not runs:
            return 0, data, paths
        cond = OLD.format(runs=runs)
        for t in DATA_TABLES:
            if "seen" not in columns(db, t):
                continue
            data[t] = [dict(r) for r in db.execute(f'SELECT * FROM "{t}" WHERE {cond}')]
        zp = os.path.join(directory, f"{name}.zip")
        infos = {}
        if os.path.isfile(zp):
            with zipfile.ZipFile(zp) as z:
                infos = {i.filename: i.file_size for i in z.infolist()}
        for f in data.get("files", []):
            if f["path"] in infos:
                paths[f["path"]] = infos[f["path"]]
        current = {p for r in db.execute(f"SELECT git_bundle, wiki_bundle FROM repos WHERE NOT {cond}") for p in r if p}
        for r in data.get("repos", []):
            for p in (r["git_bundle"], r["wiki_bundle"]):
                if p and p in infos and p not in current:
                    paths[p] = infos[p]
        return runs, data, paths
    finally:
        db.close()


def _describe(table, r):
    j = json.loads(r.get("json") or "{}") if r.get("json") else {}
    if table == "issues":
        return f"#{r['number']} {r.get('title') or ''}".strip()
    if table == "pulls":
        return f"#{r['number']} {j.get('title') or ''}".strip()
    if table == "comments":
        return f"{r['id']} on #{r['issue_number']}"
    if table == "releases":
        return r.get("tag") or str(r["id"])
    if table == "labels":
        return r.get("name") or str(r["id"])
    if table == "milestones":
        return r.get("title") or str(r["id"])
    if table == "files":
        return f"{r['path']} ({ga.human(r.get('size') or 0)})"
    if table == "members":
        return r["login"]
    if table == "teams":
        return r.get("name") or str(r["id"])
    if table == "repos":
        return r["full_name"]
    return str(r.get(KEYS[table][0]))


def old_only_report(directory, say=print, limit=50, found=None):
    """Says, per repository, what older backups had that the newest run did not see -
    issues, pull requests, comments, releases, labels, milestones and files, with
    counts and the items (limit a kind) - and returns old_only()'s result."""
    runs, data, paths = found or old_only(directory)
    name = os.path.basename(os.path.normpath(directory))
    total = sum(len(v) for v in data.values())
    if not total:
        say(f"{name}: nothing - every row and file was seen by the newest run" + (f" (run {runs})." if runs else "."))
        return runs, data, paths
    say(f"{name}: what older backups had that the newest run (run {runs}) did not see at the source - "
        f"{total} rows, {len(paths)} files in the zip ({ga.human(sum(paths.values()))}):")
    groups = {}
    for t, rows in data.items():
        for r in rows:
            if t in ("owner", "members", "teams"):
                g = ""
            elif t == "repos":
                g = r["full_name"]
            else:
                g = r.get("repo") or ""
            groups.setdefault(g, {}).setdefault(t, []).append(r)
    order = ("repos", "issues", "pulls", "comments", "releases", "labels", "milestones", "files", "owner",
             "members", "teams")
    for g in sorted(groups):
        say(f"  {g or '(the ' + name + ' itself)'}" + (" - the whole repository: gone at the source"
                                                     if "repos" in groups[g] else ""))
        for t in order:
            rows = groups[g].get(t)
            if not rows or t == "repos":
                continue
            items = [_describe(t, r) for r in rows[:limit]]
            more = f", ... and {len(rows) - limit} more" if len(rows) > limit else ""
            say(f"    {t} {len(rows)}: {', '.join(items)}{more}")
    return runs, data, paths


def delete_old_only(root, name, say=print, ask=None, dry_run=False, confirm=None):
    """Deletes the old data of NAME's backup - old_only() - from the .db and the .zip
    (the zip written again without them, so their bytes are freed), after listing it
    and only when NAME is typed (ask) or given (confirm). Rows the newest run saw are
    never deleted. Returns {'rows', 'files', 'freed'} or None when nothing was."""
    d = os.path.join(root, name)
    if not os.path.isdir(d):
        raise BackupError(f"{d}: no backups of {name}")
    with Lock(d):
        if not dry_run:
            recover(d, name, say)
            try:
                _combine_locked(root, name, say)
            except BackupError as e:
                say(str(e))
        found = old_only_report(d, say)
        runs, data, paths = found
        rows = sum(len(v) for v in data.values())
        if not rows:
            return None
        freed = sum(paths.values())
        say(f"Deleting them frees about {ga.human(freed)} of {name}.zip.")
        if dry_run:
            say("Dry run: nothing deleted.")
            return {"rows": 0, "files": 0, "freed": 0, "would_free": freed}
        typed = confirm if confirm is not None else (ask(f"This deletes them from the backup for good. "
                                                         f"Type {name} to go on") if ask else "")
        if typed != name:
            say("Nothing deleted.")
            return None
        zp = os.path.join(d, f"{name}.zip")
        before = os.path.getsize(zp) if os.path.isfile(zp) else 0
        if paths and disk_free(d) < before - freed + (100 << 20):
            raise BackupError(f"{ga.human(disk_free(d))} free: writing the zip again without them needs about "
                              f"{ga.human(before - freed)}. Nothing deleted.")
        w = _Writer(d, name, None, say=say)
        try:
            if w.run != runs:
                raise BackupError("the backup changed while it was listed; nothing deleted")
            cond = OLD.format(runs=runs)
            deleted = {}
            for t in DATA_TABLES:
                deleted[t] = w.db.execute(f'DELETE FROM "{t}" WHERE {cond}').rowcount
            w.drop = set(paths) & w.old_names
            w.meta_set(deleted_old_only=f"{now_utc().isoformat()}: {sum(deleted.values())} rows, "
                                        f"{len(w.drop)} files, {freed} bytes")
        except BaseException:
            w.abort()
            raise
        w.commit()
    after = os.path.getsize(zp) if os.path.isfile(zp) else 0
    say(f"Deleted {sum(deleted.values())} rows ({', '.join(f'{t} {n}' for t, n in deleted.items() if n)}) and "
        f"{len(paths)} files: {name}.zip {ga.human(before)} -> {ga.human(after)}, {ga.human(before - after)} freed")
    return {"rows": sum(deleted.values()), "files": len(paths), "freed": before - after}


# --- restoring into Gitea -----------------------------------------------------------------

def _attributed(item, kept_author):
    """The body, with a line naming the author and the time when Gitea records the
    token's user as the author - or only the time when the author is kept (Sudo)."""
    user = (item.get("user") or item.get("poster") or {}).get("login") or "ghost"
    when = (item.get("created_at") or "")[:16].replace("T", " ")
    line = f"> *Restored from a backup: {'' if kept_author else '@' + user + ', '}{when}*\n\n"
    return line + (item.get("body") or "")


class _Restore:
    def __init__(self, client, b, target, say):
        self.c, self.b, self.target, self.say = client, b, target, say
        self.users = {}
        s, me = client.request("GET", "/user")
        self.me = (me or {}).get("login", "") if s == 200 else ""
        self.admin = bool((me or {}).get("is_admin")) if s == 200 else False

    def sudo(self, item):
        """The original author, when this Gitea has them and the token may act as them."""
        login = (item.get("user") or item.get("poster") or {}).get("login")
        if not login or not self.admin or login == self.me:
            return None
        return login if self.exists(login) else None

    def exists(self, login):
        if login not in self.users:
            self.users[login] = self.c.request("GET", f"/users/{login}")[0] == 200
        return self.users[login]

    def as_author(self, method, path, item, body):
        """A request made as the item's author when possible, else as the token's user
        with the author named in the body."""
        who = self.sudo(item)
        if who:
            s, d = self.c.request(method, path, dict(body, body=_attributed(item, True)), sudo=who)
            if s in (200, 201):
                return s, d
        return self.c.request(method, path, dict(body, body=_attributed(item, False)))

    def upload_files(self, path, rows, item_body, patch_path):
        """Uploads the backed-up files and points the text at their new addresses."""
        body = item_body
        for f in rows:
            info = self.b.zip.getinfo(f["path"])
            with self.b.zip.open(info) as src:
                s, d = self.c.upload(path, f["name"], src, info.file_size)
            if s != 201:
                self.say(f"    file not restored: {f['name']}: {s} {d}")
                continue
            if f["uuid"] and isinstance(d, dict) and d.get("uuid") and body:
                body = body.replace(f["uuid"], d["uuid"])
        if body != item_body and patch_path:
            self.c.request("PATCH", patch_path, {"body": body})

    def create_owner(self, kind, info, ask):
        s, _ = self.c.request("GET", f"/orgs/{self.target}")
        if s == 200:
            return "organization"
        if self.c.request("GET", f"/users/{self.target}")[0] == 200:
            return "user"
        if kind == "user":
            print(f"Gitea has no user {self.target}. 1) create the organization {self.target}  "
                  f"2) create the user {self.target} (admin token)  0) cancel")
            c = ask("Choice", "1")
            if c == "2":
                import getpass
                email = ask("Email of the new user", info.get("email") or "")
                password = getpass.getpass("Password of the new user (it must be changed at the first login): ")
                s, d = self.c.request("POST", "/admin/users", {"username": self.target, "email": email,
                                                               "password": password, "must_change_password": True,
                                                               "full_name": info.get("full_name") or ""})
                if s != 201:
                    raise BackupError(f"The user {self.target} was not created: {s} {d}")
                return "user"
            if c != "1":
                raise BackupError("Nothing restored.")
        body = {k: info.get(k) for k in ("full_name", "description", "website", "location", "visibility")
                if info.get(k) is not None}
        body["username"] = self.target
        s, d = self.c.request("POST", "/orgs", body)
        if s != 201:
            raise BackupError(f"The organization {self.target} was not created: {s} {d}")
        self.say(f"Created the organization {self.target}")
        for label in self.b.rows("SELECT json FROM labels WHERE repo = ''"):
            label = json.loads(label["json"])
            self.c.request("POST", f"/orgs/{self.target}/labels", {k: label.get(k) for k in
                                                                    ("name", "color", "description", "exclusive")})
        return "organization"

    def push(self, path, url, specs, sub="repo"):
        """The bundle at path (in the zip) into restore-tmp/<sub>.git, pushed to url."""
        tmp = os.path.join(self.b.dir, "restore-tmp")
        os.makedirs(tmp, exist_ok=True)
        bundle, bare = os.path.join(tmp, f"{sub}.bundle"), os.path.join(tmp, f"{sub}.git")
        self.b.extract(path, bundle)
        shutil.rmtree(bare, ignore_errors=True)
        git(["init", "--quiet", "--bare", bare])
        code, out = git(["-C", bare, "fetch", "--quiet", bundle, "+refs/*:refs/*"])
        os.remove(bundle)
        if code != 0:
            raise BackupError(f"git fetch from the bundle failed: {out[-300:]}")
        code, out = git(["-C", bare, "push", "--quiet", url] + specs, env=self.c.git_env())
        if code != 0:
            raise BackupError(f"git push to {url} failed: {out[-500:]}")
        return bare

    def repo(self, r, owner_kind_now):
        old, src = r["full_name"], r["json"]
        full = f"{self.target}/{r['name']}"
        if self.c.request("GET", f"/repos/{full}")[0] == 200:
            self.say(f"  {full}: already in Gitea; left as it is (delete it to restore it again)")
            return
        self.say(f"  {full}")
        body = {"name": r["name"], "description": src.get("description") or "", "private": bool(src.get("private")),
                "default_branch": src.get("default_branch") or "main"}
        path = (f"/orgs/{self.target}/repos" if owner_kind_now == "organization" else
                "/user/repos" if self.target == self.me else f"/admin/users/{self.target}/repos")
        s, d = self.c.request("POST", path, body)
        if s != 201:
            raise BackupError(f"{full}: not created: {s} {d}")
        tmp = os.path.join(self.b.dir, "restore-tmp")
        bare = None
        try:
            if r["git_bundle"]:
                bare = self.push(r["git_bundle"], self.c.git_url(full),
                                 ["+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"])
                code, heads = git(["-C", bare, "for-each-ref", "--format=%(refname:short)", "refs/heads/"])
                wait_branches(self.c, full, heads.split() if code == 0 else [])
                self.say("    git: branches and tags")
            self.c.request("PATCH", f"/repos/{full}", {k: src.get(k) for k in
                           ("default_branch", "website", "has_issues", "has_wiki", "has_pull_requests",
                            "has_projects", "has_releases") if src.get(k) is not None})
            if r["topics"]:
                self.c.request("PUT", f"/repos/{full}/topics", {"topics": r["topics"]})
            for u in r["collaborators"]:
                if self.exists(u["login"]):
                    self.c.request("PUT", f"/repos/{full}/collaborators/{u['login']}",
                                   {"permission": {"owner": "admin"}.get(u["permission"], u["permission"])})
            # A merged pull request is marked merged by "manually merged", which the
            # repository allows only with this on - turned back to the backup's after.
            self.c.request("PATCH", f"/repos/{full}", {"allow_manual_merge": True})
            if r["wiki_bundle"]:
                self.c.request("POST", f"/repos/{full}/wiki/new", {"title": "Home", "content_base64":
                               base64.b64encode(b"Restoring.\n").decode(), "message": "Restore the wiki"})
                self.push(r["wiki_bundle"], self.c.git_url(full, True), ["+refs/heads/*:refs/heads/*"], "wiki")
                self.say("    wiki")
            self.issues(old, full, bare)
            self.releases(old, full)
            self.c.request("PATCH", f"/repos/{full}", {"allow_manual_merge": bool(src.get("allow_manual_merge"))})
            if src.get("archived"):
                self.c.request("PATCH", f"/repos/{full}", {"archived": True})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def issues(self, old, full, bare):
        labels, milestones = {}, {}
        for label in self.b.json_rows("labels", old, "id"):
            s, d = self.c.request("POST", f"/repos/{full}/labels", {k: label.get(k) for k in
                                  ("name", "color", "description", "exclusive", "is_archived") if label.get(k) is not None})
            if s == 201:
                labels[label["name"]] = d["id"]
        s, d = self.c.request("GET", f"/orgs/{self.target}/labels")
        for label in (d if s == 200 and isinstance(d, list) else []):
            labels.setdefault(label["name"], label["id"])
        for m in self.b.json_rows("milestones", old, "id"):
            s, d = self.c.request("POST", f"/repos/{full}/milestones", {"title": m.get("title"),
                                  "description": m.get("description") or "", "due_on": m.get("due_on"),
                                  "state": m.get("state") or "open"})
            if s == 201:
                milestones[m["id"]] = d["id"]
        rows = {r["number"]: r for r in self.b.rows("SELECT * FROM issues WHERE repo = ?", old)}
        pulls = {r["number"]: json.loads(r["json"]) for r in self.b.rows("SELECT * FROM pulls WHERE repo = ?", old)}
        top = max(rows) if rows else 0
        made = {"issues": 0, "pulls": 0, "placeholders": 0}
        for n in range(1, top + 1):
            row = rows.get(n)
            item = json.loads(row["json"]) if row else None
            got = None
            if item and row["is_pull"] and n in pulls:
                got = self.pull(full, item, pulls[n], bare, labels, milestones)
                made["pulls"] += 1 if got else 0
            elif item:
                s, got = self.as_author("POST", f"/repos/{full}/issues", item, {"title": item["title"]})
                got = got if s == 201 else None
                made["issues"] += 1 if got else 0
            if not got:
                why = "was deleted" if not item else "could not be made again"
                s, got = self.c.request("POST", f"/repos/{full}/issues", {
                    "title": f"(#{n} {why})" if not item else item["title"],
                    "body": "" if not item else _attributed(item, False) + f"\n\n*#{n} {why} as a pull request: "
                    f"it is in the backup.*", "closed": True})
                made["placeholders"] += 1
                if s != 201:
                    raise BackupError(f"{full} #{n}: Gitea did not create it: {s} {got}")
            if got.get("number") != n:
                raise BackupError(f"{full}: #{n} became #{got.get('number')} - the repository was not empty")
            if item:
                self.finish_issue(old, full, n, item, labels, milestones, got)
        self.say(f"    {made['issues']} issues, {made['pulls']} pull requests"
                 + (f", {made['placeholders']} placeholders keep the numbers" if made["placeholders"] else ""))

    def pull(self, full, item, pr, bare, labels, milestones):
        n = item["number"]
        head, base = (pr.get("head") or {}), (pr.get("base") or {})
        branch = f"restored-pr/{n}"
        if not bare:
            return None
        sha = head.get("sha")
        if not sha:
            return None
        code, _ = git(["-C", bare, "cat-file", "-e", f"{sha}^{{commit}}"])
        if code != 0:
            return None
        base_ref = base.get("ref") or ""
        specs = [f"{sha}:refs/heads/{branch}"]
        code, _ = git(["-C", bare, "rev-parse", "--verify", "--quiet", f"refs/heads/{base_ref}"])
        if code != 0 and base.get("sha"):
            specs.append(f"{base['sha']}:refs/heads/{base_ref}")
        code, out = git(["-C", bare, "push", "--quiet", self.c.git_url(full)] + specs, env=self.c.git_env())
        if code != 0:
            self.say(f"    #{n}: {out[-200:]}")
            return None
        wait_branches(self.c, full, [branch, base_ref])
        body = {"title": item["title"], "head": branch, "base": base_ref}
        s, d = self.as_author("POST", f"/repos/{full}/pulls", item, body)
        if s != 201 and pr.get("merge_base"):
            # A merged pull request's commits are in its base already, and Gitea makes no
            # pull request without changes: it is made against its merge base, then moved.
            tmp_base = f"restored-pr/{n}-base"
            git(["-C", bare, "push", "--quiet", self.c.git_url(full), f"{pr['merge_base']}:refs/heads/{tmp_base}"],
                env=self.c.git_env())
            wait_branches(self.c, full, [tmp_base])
            s, d = self.as_author("POST", f"/repos/{full}/pulls", item, dict(body, base=tmp_base))
            if s == 201:
                self.c.request("PATCH", f"/repos/{full}/pulls/{n}", {"base": base_ref})
        if s != 201:
            return None
        if pr.get("merged"):
            self.c.request("POST", f"/repos/{full}/pulls/{n}/merge",
                           {"Do": "manually-merged", "merge_commit_id": pr.get("merge_commit_sha") or ""})
        return d

    def finish_issue(self, old, full, n, item, labels, milestones, got):
        patch = {}
        ids = [labels[x["name"]] for x in item.get("labels") or [] if x.get("name") in labels]
        if ids:
            self.c.request("PUT", f"/repos/{full}/issues/{n}/labels", {"labels": ids})
        if item.get("milestone") and item["milestone"].get("id") in milestones:
            patch["milestone"] = milestones[item["milestone"]["id"]]
        assignees = [a["login"] for a in item.get("assignees") or [] if a.get("login") and self.exists(a["login"])]
        if assignees:
            patch["assignees"] = assignees
        if item.get("due_date"):
            patch["due_date"] = item["due_date"]
        if patch:
            self.c.request("PATCH", f"/repos/{full}/issues/{n}", patch)
        files = self.b.files(old, "issues", n)
        if files:
            self.upload_files(f"/repos/{full}/issues/{n}/assets", files, got.get("body") or "",
                              f"/repos/{full}/issues/{n}")
        src = old
        for c in [json.loads(r["json"]) for r in self.b.rows(
                "SELECT json FROM comments WHERE repo = ? AND issue_number = ? ORDER BY id", src, n)]:
            s, d = self.as_author("POST", f"/repos/{full}/issues/{n}/comments", c, {})
            if s != 201:
                self.say(f"    #{n}: a comment was not restored: {s} {d}")
                continue
            cf = self.b.files(src, "comments", c["id"])
            if cf:
                self.upload_files(f"/repos/{full}/issues/comments/{d['id']}/assets", cf, d.get("body") or "",
                                  f"/repos/{full}/issues/comments/{d['id']}")
        if item.get("state") == "closed" and got.get("state") != "closed" and \
                not (item.get("pull_request") or {}).get("merged"):
            self.c.request("PATCH", f"/repos/{full}/issues/{n}", {"state": "closed"})

    def teams(self):
        """The organization's teams, their members and repositories. Owners is
        Gitea's own team, made with the organization: only its members are added."""
        s, have = self.c.request("GET", f"/orgs/{self.target}/teams")
        have = {t["name"]: t for t in (have if s == 200 and isinstance(have, list) else [])}
        for row in self.b.rows("SELECT * FROM teams ORDER BY id"):
            t = json.loads(row["json"])
            team = have.get(t["name"])
            if not team:
                body = {k: t.get(k) for k in ("name", "description", "permission", "units", "units_map",
                                              "can_create_org_repo", "includes_all_repositories")
                        if t.get(k) is not None}
                # A backup made from GitHub has GitHub's words for a team's permission.
                if body.get("permission") in ("pull", "triage", "push", "maintain"):
                    body["permission"] = {"pull": "read", "triage": "read", "push": "write"}.get(
                        body["permission"], "admin")
                if body.get("units_map"):  # Gitea takes one of the two
                    body.pop("units", None)
                s, team = self.c.request("POST", f"/orgs/{self.target}/teams", body)
                if s != 201:
                    self.say(f"  team {t['name']}: not restored: {s} {team}")
                    continue
            for login in json.loads(row["members"] or "[]"):
                if self.exists(login):
                    self.c.request("PUT", f"/teams/{team['id']}/members/{login}")
            if not team.get("includes_all_repositories"):
                for name in json.loads(row.get("repos") or "[]"):
                    self.c.request("PUT", f"/teams/{team['id']}/repos/{self.target}/{name}")
            self.say(f"  team {t['name']}")

    def releases(self, old, full):
        rels = sorted(self.b.json_rows("releases", old, "id"), key=lambda r: r.get("created_at") or "")
        for r in rels:
            s, d = self.c.request("POST", f"/repos/{full}/releases", {
                "tag_name": r.get("tag_name"), "target_commitish": r.get("target_commitish") or "",
                "name": r.get("name") or "", "body": r.get("body") or "", "draft": bool(r.get("draft")),
                "prerelease": bool(r.get("prerelease"))})
            if s != 201:
                self.say(f"    release {r.get('tag_name')}: not restored: {s} {d}")
                continue
            files = self.b.files(old, "releases", r["id"])
            if files:
                self.upload_files(f"/repos/{full}/releases/{d['id']}/assets", files, "", None)
        if rels:
            self.say(f"    {len(rels)} releases")


def restore_gitea(client, directory, target, ask, say, only=None):
    """Restores the backup into the Gitea organization or user TARGET: repositories
    that Gitea already has are left as they are. only: one repository name."""
    b = Backup(directory)
    try:
        kind, info = b.owner()
        target = target or b.name
        r = _Restore(client, b, target, say)
        now = r.create_owner(kind, info, ask)
        repos = [x for x in b.repos() if not only or x["name"] == only]
        say(f"Restoring {len(repos)} repositories of {b.name} ({b.meta.get('created', '')[:19]}) into {target}")
        for repo in repos:
            try:
                r.repo(repo, now)
            except BackupError as e:
                say(f"  {e}")
        if now == "organization":
            r.teams()
    finally:
        b.close()


# --- restoring as WeKan boards ------------------------------------------------------------

WEKAN_COLORS = {
    "white": "ffffff", "green": "3cb500", "yellow": "fad900", "orange": "ff9f19", "red": "eb4646",
    "purple": "a632db", "blue": "0079bf", "sky": "00c2e0", "lime": "51e898", "pink": "ff78cb",
    "black": "4d4d4d", "silver": "c0c0c0", "peachpuff": "ffdab9", "crimson": "dc143c", "plum": "dda0dd",
    "darkgreen": "006400", "slateblue": "6a5acd", "magenta": "ff00ff", "gold": "ffd700", "navy": "000080",
    "gray": "808080", "saddlebrown": "8b4513", "paleturquoise": "afeeee", "mistyrose": "ffe4e1",
    "indigo": "4b0082",
}


def wekan_color(hexcolor):
    """The WeKan label color nearest to Gitea's hex color."""
    h = (hexcolor or "").lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        return "gray"
    rgb = [int(h[i:i + 2], 16) for i in (0, 2, 4)]
    return min(WEKAN_COLORS, key=lambda c: sum((a - int(WEKAN_COLORS[c][i * 2:i * 2 + 2], 16)) ** 2
                                                for i, a in enumerate(rgb)))


def _wid(*parts):
    """A WeKan id (17 characters, no dash: the zip import reads the id up to a dash)."""
    return hashlib.sha256("\0".join(map(str, parts)).encode()).hexdigest()[:17]


def wekan_board(b, repo):
    """The repository's issues and pull requests as one WeKan board document
    (wekan-board-1.0.0), and its files as (zip path in the backup, attachment id, name)."""
    full = repo["full_name"]
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    bid = _wid(full)
    swimlanes = [{"_id": _wid(full, "s", 0), "title": "Issues", "archived": False, "sort": 0},
                 {"_id": _wid(full, "s", 1), "title": "Pull requests", "archived": False, "sort": 1}]
    lists = [{"_id": _wid(full, "l", t), "title": t, "archived": False, "sort": i, "swimlaneId": ""}
             for i, t in enumerate(("Open", "Closed", "Merged"))]
    by_title = {x["title"]: x["_id"] for x in lists}
    labels, label_ids = [], {}
    for label in b.json_rows("labels", full, "id") + [json.loads(r["json"]) for r in b.rows(
            "SELECT json FROM labels WHERE repo = ''")]:
        if label.get("name") in label_ids:
            continue
        lid = _wid(full, "label", label.get("name"))
        label_ids[label.get("name")] = lid
        labels.append({"_id": lid, "name": label.get("name") or "", "color": wekan_color(label.get("color"))})
    pulls = {r["number"]: json.loads(r["json"]) for r in b.rows("SELECT * FROM pulls WHERE repo = ?", full)}
    cards, comments, activities, attachments, files = [], [], [], [], []
    user = "gitea-backup"
    for row in b.rows("SELECT * FROM issues WHERE repo = ? ORDER BY number", full):
        i = json.loads(row["json"])
        n = row["number"]
        cid = _wid(full, "card", n)
        merged = bool((pulls.get(n) or {}).get("merged"))
        column = "Merged" if merged else "Closed" if i.get("state") == "closed" else "Open"
        author = (i.get("user") or {}).get("login") or "ghost"
        description = (i.get("body") or "") + f"\n\n---\n#{n} by @{author}, {(i.get('created_at') or '')[:10]}" \
            + (f" - {i['html_url']}" if i.get("html_url") else "")
        created = i.get("created_at") or now
        cards.append({"_id": cid, "title": f"#{n} {i.get('title') or ''}", "description": description,
                      "archived": False, "swimlaneId": swimlanes[1 if row["is_pull"] else 0]["_id"],
                      "listId": by_title[column], "sort": n, "labelIds": [label_ids[x["name"]] for x in
                      i.get("labels") or [] if x.get("name") in label_ids],
                      "dateLastActivity": i.get("updated_at") or created, "createdAt": created,
                      "userId": user, "requestedBy": author,
                      **({"dueAt": i["due_date"]} if i.get("due_date") else {}),
                      **({"endAt": i["closed_at"]} if i.get("closed_at") else {})})
        activities.append({"activityType": "createCard", "cardId": cid, "createdAt": created, "userId": user})
        for c in [json.loads(r["json"]) for r in b.rows(
                "SELECT json FROM comments WHERE repo = ? AND issue_number = ? ORDER BY id", full, n)]:
            mid = _wid(full, "comment", c["id"])
            who = (c.get("user") or {}).get("login") or "ghost"
            comments.append({"_id": mid, "cardId": cid, "createdAt": c.get("created_at") or created, "userId": user,
                             "text": f"@{who}, {(c.get('created_at') or '')[:16].replace('T', ' ')}:\n\n{c.get('body') or ''}"})
            activities.append({"activityType": "addComment", "commentId": mid, "cardId": cid,
                               "createdAt": c.get("created_at") or created, "userId": user})
            for f in b.files(full, "comments", c["id"]):
                files.append(f)
                attachments.append((cid, f))
        for f in b.files(full, "issues", n):
            files.append(f)
            attachments.append((cid, f))
    rows = []
    for cid, f in attachments:
        aid = _wid(full, "file", f["path"])
        f["wekan_id"] = aid
        rows.append({"_id": aid, "cardId": cid, "meta": {"cardId": cid}, "name": f["name"],
                     "type": mimetypes.guess_type(f["name"])[0] or "application/octet-stream", "size": f["size"],
                     # Read from the zip; this address is only where the file was.
                     "url": json.loads(f["json"] or "{}").get("browser_download_url") or f"attachments/{aid}"})
    src = repo["json"]
    doc = {"_format": "wekan-board-1.0.0", "_id": bid, "title": full, "archived": False, "color": "belize",
           "permission": "private", "description": src.get("description") or "", "createdAt": src.get("created_at") or now,
           "modifiedAt": now, "members": [{"userId": user, "wekanId": user, "isActive": True, "isAdmin": True}],
           "users": [{"_id": user, "username": user, "profile": {"fullname": "Gitea backup"}}],
           "labels": labels, "swimlanes": swimlanes, "lists": lists, "cards": cards, "comments": comments,
           "activities": [{"activityType": "createBoard", "createdAt": src.get("created_at") or now,
                           "userId": user}] + activities,
           "attachments": rows, "checklists": [], "checklistItems": [], "subtaskItems": [], "customFields": [],
           "rules": [], "triggers": [], "actions": []}
    return doc, files


def wekan_zips(directory, out_dir, say):
    """One WeKan import .zip per repository - wekan.json and attachments/<id>-<name> -
    which WeKan's All Boards -> Import, or POST /api/import/zip?newBoard=1, reads."""
    b = Backup(directory)
    made = []
    try:
        os.makedirs(out_dir, exist_ok=True)
        for repo in b.repos():
            doc, files = wekan_board(b, repo)
            path = os.path.join(out_dir, f"{safe_part(repo['full_name'].replace('/', '-'))}.zip")
            with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
                z.writestr("wekan.json", json.dumps(doc, ensure_ascii=False))
                for f in files:
                    info = zipfile.ZipInfo(f"attachments/{f['wekan_id']}-{safe_part(f['name'])}", time.localtime()[:6])
                    info.compress_type = zipfile.ZIP_STORED
                    with b.zip.open(f["path"]) as src, z.open(info, "w", force_zip64=True) as dst:
                        shutil.copyfileobj(src, dst, CHUNK)
            say(f"  {repo['full_name']}: {len(doc['cards'])} cards, {len(files)} files -> {path}")
            made.append(path)
    finally:
        b.close()
    return made


def upload_to_wekan(url, username, password, zips, say):
    """Logs in to WeKan and imports each .zip as a new board."""
    url = url.rstrip("/")
    req = urllib.request.Request(f"{url}/users/login", data=json.dumps({"username": username, "password": password}).encode(),
                                 headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            token = json.loads(r.read()).get("token")
    except urllib.error.HTTPError as e:
        raise BackupError(f"WeKan login failed: {e.code}")
    if not token:
        raise BackupError("WeKan login gave no token")
    for path in zips:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            req = urllib.request.Request(f"{url}/api/import/zip?newBoard=1&membersMode=me&authToken="
                                         f"{urllib.parse.quote(token)}", data=f, method="POST",
                                         headers={"Content-Type": "application/zip", "Content-Length": str(size)})
            try:
                with urllib.request.urlopen(req, timeout=None) as r:
                    say(f"  {os.path.basename(path)}: board {json.loads(r.read()).get('boardId')}")
            except urllib.error.HTTPError as e:
                say(f"  {os.path.basename(path)}: not imported: {e.code} {e.read()[:200].decode(errors='replace')}")


# --- moving all of a Gitea into backups --------------------------------------------------------

MIGRATE_PHRASE = "migrate all gitea"


def estimate(repo, attachments):
    """Bytes a repository's backup needs while it is made: git twice (the mirror and
    its bundle), its files, 10 % and 100 MB more."""
    git_bytes = (repo.get("size") or 0) * 1024
    return int((2 * git_bytes + attachments) * 1.1) + (100 << 20)


def same_disk(a, b):
    """True/False when both paths (or their nearest existing parents) are on one file
    system, None when b is not on this machine."""
    def dev(p):
        p = os.path.abspath(os.path.expanduser(p))
        while not os.path.exists(p):
            parent = os.path.dirname(p)
            if parent == p:
                return None
            p = parent
        return os.stat(p).st_dev
    da, db_ = dev(a), dev(b) if b else None
    return None if da is None or db_ is None else da == db_


def verify_repo(client, root, owner, repo, say=print):
    """Is the repository whole in its owner's backup - by what Gitea has now? Its
    rows of the newest run (issues, pull requests, comments, releases, labels,
    milestones, files: the counts Gitea gives), every file's bytes (CRC and SHA-256),
    and its git and wiki bundles (git bundle verify, every branch, tag and pull
    request head at Gitea's commit). Returns the counts; BackupError when not."""
    full = repo["full_name"]
    b = Backup(os.path.join(root, owner), migrate=False)
    work = os.path.join(root, owner, WORK + "-verify")
    try:
        runs = int(b.meta.get("runs") or 0)
        q = lambda sql, *a: b.db.execute(sql, a).fetchone()[0]  # noqa: E731
        items = list(ga.paged(client.request, f"/repos/{full}/issues?state=all"))
        want = {"issues": len(items), "pulls": sum(1 for i in items if i.get("pull_request")),
                "comments": len(list(ga.paged(client.request, f"/repos/{full}/issues/comments"))),
                "releases": len(list(ga.paged(client.request, f"/repos/{full}/releases"))),
                "labels": len(list(ga.paged(client.request, f"/repos/{full}/labels"))),
                "milestones": len(list(ga.paged(client.request, f"/repos/{full}/milestones?state=all"))),
                "files": len(ga.repo_attachments(client.request, full))}
        got = {t: q(f'SELECT COUNT(*) FROM "{t}" WHERE repo = ? AND seen = ?', full, runs)
               for t in ("issues", "pulls", "comments", "releases", "labels", "milestones")}
        got["files"] = q("SELECT COUNT(*) FROM files WHERE repo = ? AND seen = ? AND kind IN "
                         "('issues', 'comments', 'releases')", full, runs)
        wrong = [f"{t}: Gitea {want[t]}, the backup {got[t]}" for t in want if want[t] != got[t]]
        if wrong:
            raise BackupError(f"{full}: not the same as in Gitea: {'; '.join(wrong)}")
        for f in b.rows("SELECT path, size, sha256 FROM files WHERE repo = ? AND seen = ?", full, runs):
            digest, size = hashlib.sha256(), 0
            try:
                with b.zip.open(f["path"]) as src:  # zipfile checks the CRC at the end
                    while chunk := src.read(CHUNK):
                        digest.update(chunk)
                        size += len(chunk)
            except (KeyError, zipfile.BadZipFile, OSError) as e:
                raise BackupError(f"{full}: {f['path']}: {e}")
            if (size, digest.hexdigest()) != (f["size"], f["sha256"]):
                raise BackupError(f"{full}: {f['path']}: its bytes are not the ones recorded")
        row = (b.rows("SELECT * FROM repos WHERE full_name = ? AND seen = ?", full, runs) or [None])[0]
        if row is None:
            raise BackupError(f"{full}: not in the backup's newest run")
        env = client.git_env()
        bundles = 0
        for wiki, col in ((False, "git_bundle"), (True, "wiki_bundle")):
            if wiki and not repo.get("has_wiki"):
                continue
            ok, heads = remote_heads(client.git_url(full, wiki), env)
            if not ok:
                if wiki:
                    continue
                raise BackupError(f"{full}: git cannot read it now: {str(heads)[-200:]}")
            if not heads:
                continue
            if not row[col]:
                raise BackupError(f"{full}: no {'wiki' if wiki else 'git'} bundle in the backup")
            have = bundle_heads(b.zip, row[col]) or {}
            missing = [r for r, s in heads.items() if have.get(r) != s]
            if missing:
                raise BackupError(f"{full}: the bundle lacks {', '.join(missing[:5])}")
            os.makedirs(work, exist_ok=True)
            bundle, bare = os.path.join(work, "v.bundle"), os.path.join(work, "v.git")
            b.extract(row[col], bundle)
            shutil.rmtree(bare, ignore_errors=True)
            git(["init", "--quiet", "--bare", bare])
            code, out = git(["-C", bare, "bundle", "verify", "--quiet", bundle])
            if code != 0:
                raise BackupError(f"{full}: git bundle verify: {out[-300:]}")
            bundles += 1
        got["bundles"] = bundles
        return got
    finally:
        b.close()
        shutil.rmtree(work, ignore_errors=True)


def _repo_packages(client, owner, full):
    out = []
    for p in ga.paged(client.request, f"/packages/{owner}"):
        if ((p.get("repository") or {}).get("full_name") or "").lower() == full.lower():
            out.append(p)
    return out


def _delete_package(client, owner, p, say):
    s, _ = client.request("DELETE", f"/packages/{owner}/{p['type']}/{urllib.parse.quote(p['name'], safe='')}/"
                                    f"{urllib.parse.quote(p['version'], safe='')}")
    say(f"    {'deleted' if s in (200, 204) else f'not deleted ({s})'} package {p['type']} {p['name']} {p['version']} "
        f"(packages are not in backups)")


def migration_plan(client, excludes=(), say=print, owners=None):
    """(repositories smallest first with their sizes, owners, the token's login, the
    excluded ones) - what migrate_all() moves. owners: only these organizations and
    users (None: every one)."""
    s, me = client.request("GET", "/user")
    me = (me or {}).get("login", "") if s == 200 and isinstance(me, dict) else ""
    gone = {e.lower() for e in excludes}
    repos, kept = [], []
    only = {o.lower() for o in owners} if owners is not None else None
    for r in ga.all_repos(client.request):
        if only is not None and r["full_name"].split("/")[0].lower() not in only:
            continue
        (kept if r["full_name"].lower() in gone else repos).append(r)
    plan = []
    for n, r in enumerate(repos, 1):
        if len(repos) > 20 and n % 20 == 0:
            say(f"  sizes: {n}/{len(repos)}")
        try:
            att = sum(a["size"] for a in ga.repo_attachments(client.request, r["full_name"]))
        except ga.AttachmentError:
            att = 0
        git_bytes = (r.get("size") or 0) * 1024
        plan.append({"repo": r, "full": r["full_name"], "owner": r["full_name"].split("/")[0], "git": git_bytes,
                     "attachments": att, "total": git_bytes + att, "need": estimate(r, att)})
    plan.sort(key=lambda p: (p["total"], p["full"].lower()))
    owners = []
    for p in plan:
        if p["owner"] not in owners:
            owners.append(p["owner"])
    for o in ga.paged(client.request, "/admin/orgs"):
        if o.get("username") and o["username"] not in owners and (only is None or o["username"].lower() in only):
            owners.append(o["username"])
    return plan, owners, me, kept


def migrate_all(client, root, say=print, ask=None, dry_run=False, continue_on_error=False, excludes=(),
                gitea_data=None, confirm=None, owners=None):
    """Moves ALL of the Gitea into backups under root, ONE REPOSITORY at a time,
    smallest first: each is backed up into its owner's one backup, verified
    (verify_repo), synced, and only then deleted from Gitea, with its packages - so
    the space it took is free for the next. After an owner's last repository, its
    own data (teams, members, labels, avatar) is backed up and the organization or
    user deleted - never the token's own user. A repository that does not fit on
    the backup disk now is skipped; one whose verification fails is never deleted
    (and the run stops, unless continue_on_error). Run again, it goes on: what is
    gone from Gitea is done, a repository backed up but not deleted is updated,
    verified and deleted. Excluded repositories stay, and so does their owner.
    confirm: (phrase, Gitea address) instead of asking; owners: only these
    organizations and users. Returns a summary dict."""
    os.makedirs(root, exist_ok=True)
    say(f"Migrate all of {client.root} into backups in {root}, a repository at a time, smallest first")
    if gitea_data:
        same = same_disk(root, gitea_data)
        if same is False:
            say(f"WARNING: {root} is not on the disk of Gitea's data ({gitea_data}): deleting from Gitea "
                f"frees no space for the backups.")
        elif same is None:
            say(f"Gitea's data ({gitea_data}) is not on this machine: whether it is the backups' disk is not known.")
    plan, owners, me, kept = migration_plan(client, excludes, say, owners)
    free = disk_free(root)
    same = same_disk(root, gitea_data) if gitea_data else None
    say(f"{len(plan)} repositories of {len(owners)} organizations and users; {ga.human(sum(p['total'] for p in plan))} "
        f"in Gitea; {ga.human(free)} free in {root}")
    for e in kept:
        say(f"  {e['full_name']}: excluded (Sources excludes): stays in Gitea, and so does {e['full_name'].split('/')[0]}")
    result = {"plan": [p["full"] for p in plan], "done": [], "skipped": [], "failed": [], "owners_deleted": [],
              "owners_left": [], "dry_run": dry_run}
    if dry_run:
        sim = free
        say(f"  {'#':>4}  {'repository':40}  {'git':>10}  {'files':>10}  {'needs':>10}  free after")
        for n, p in enumerate(plan, 1):
            fits = sim >= p["need"]
            if fits:
                sim += (p["total"] if same is not False else 0) - p["total"]
            say(f"  {n:>4}  {p['full'][:40]:40}  {ga.human(p['git']):>10}  {ga.human(p['attachments']):>10}  "
                f"{ga.human(p['need']):>10}  {ga.human(sim) if fits else 'does not fit: skipped'}")
            (result["done"] if fits else result["skipped"]).append(p["full"])
        say(f"Dry run: nothing changed. It would free about {ga.human(sum(p['total'] for p in plan))} of Gitea's disk; "
            f"the backups take about as much.")
        return result
    phrase, url = confirm if confirm else (
        (ask(f'This moves EVERY repository of {client.root} into backups and DELETES it from Gitea. '
             f'Type "{MIGRATE_PHRASE}" to go on') if ask else ""),
        (ask("Type the Gitea address to go on") if ask else ""))
    if phrase != MIGRATE_PHRASE or (url or "").rstrip("/") != client.root:
        say("Nothing done.")
        result["cancelled"] = True
        return result
    left = {}
    for p in plan:
        left[p["owner"]] = left.get(p["owner"], 0) + 1
    blocked = {e["full_name"].split("/")[0].lower() for e in kept}

    def owner_done(owner):
        if owner.lower() in blocked:
            say(f"  {owner}: still has repositories in Gitea (excluded, skipped or failed): not deleted")
            result["owners_left"].append(owner)
            return
        try:
            if ga.scope_repos(client.request, owner):
                say(f"  {owner}: still has repositories in Gitea: not deleted")
                result["owners_left"].append(owner)
                return
            backup(client, owner, root, say, only=[], owner_data=True)
            b = Backup(os.path.join(root, owner), migrate=False)
            try:
                if not b.rows("SELECT 1 FROM owner WHERE seen = ?", int(b.meta.get("runs") or 0)):
                    raise BackupError(f"{owner}: its own data is not in the backup")
            finally:
                b.close()
        except (BackupError, ga.AttachmentError) as e:
            say(f"  {owner}: not deleted: {e}")
            result["owners_left"].append(owner)
            return
        if me and owner.lower() == me.lower():
            say(f"  {owner}: the token's own user - backed up, left in Gitea (it cannot delete itself)")
            result["owners_left"].append(owner)
            return
        try:
            delete_owner(client, owner, say)
            result["owners_deleted"].append(owner)
        except BackupError as e:
            say(f"  {owner}: {e}")
            result["owners_left"].append(owner)

    stopped = False
    for n, p in enumerate(plan, 1):
        full, owner = p["full"], p["owner"]
        free = disk_free(root)
        say(f"[{n}/{len(plan)}] {full}: git {ga.human(p['git'])}, files {ga.human(p['attachments'])}; "
            f"needs {ga.human(p['need'])}, {ga.human(free)} free")
        if free < p["need"]:
            say(f"  {full}: does not fit now ({ga.human(free)} free, needs {ga.human(p['need'])}): skipped, "
                f"left in Gitea")
            result["skipped"].append(full)
            blocked.add(owner.lower())
            continue
        try:
            before = single_size(os.path.join(root, owner)) if is_single(os.path.join(root, owner)) else 0
            backup(client, owner, root, say, only=[full], owner_data=False, need=p["need"])
            counts = verify_repo(client, root, owner, p["repo"], say)
        except (BackupError, ga.AttachmentError, OSError) as e:
            say(f"  {full}: NOT deleted from Gitea - the backup could not be verified: {e}")
            result["failed"].append(full)
            blocked.add(owner.lower())
            if not continue_on_error:
                say("Stopped at the first failure (--continue-on-error goes on with the next).")
                stopped = True
                break
            continue
        say(f"  verified: {', '.join(f'{k} {v}' for k, v in counts.items())}")
        packages = _repo_packages(client, owner, full)
        s, d = client.request("DELETE", f"/repos/{full}")
        if s not in (200, 204):
            say(f"  {full}: backed up and verified, but Gitea did not delete it: {s} {d}")
            result["failed"].append(full)
            blocked.add(owner.lower())
            if not continue_on_error:
                stopped = True
                break
            continue
        for pk in packages:
            _delete_package(client, owner, pk, say)
        grown = single_size(os.path.join(root, owner)) - before
        say(f"  {full}: backed up (+{ga.human(grown)}), deleted from Gitea (about {ga.human(p['total'])} freed); "
            f"{ga.human(disk_free(root))} free now")
        result["done"].append(full)
        left[owner] -= 1
        if left[owner] == 0:
            owner_done(owner)
    if not stopped:
        for owner in owners:
            if owner not in left:  # an organization without repositories
                owner_done(owner)
    say(f"Migrated {len(result['done'])} of {len(plan)} repositories; deleted {len(result['owners_deleted'])} "
        f"organizations and users")
    for x in result["skipped"]:
        say(f"  skipped (no room on the backup disk then; run again when there is): {x}")
    for x in result["failed"]:
        say(f"  not verified, kept in Gitea: {x}")
    return result


# --- the menu -----------------------------------------------------------------------------

def migrate_dated(root, say):
    """The backups list's first step: every owner's dated backups combined."""
    for n in owners_with_dated(root):
        if dated(root, n)[0]:
            try:
                combine(root, n, say)
            except BackupError as e:
                say(str(e))


def pick_backup(root, ask, say=print):
    migrate_dated(root, say)
    found = backups(root)
    if not found:
        print(f"No backups in {root}.")
        return None
    for n, (owner, stamp, d) in enumerate(found[:40], 1):
        print(f" {n:2}) {owner}  {'updated ' if is_single(d) else 'dated '}{stamp}")
    c = ask("Backup", "1")
    return found[int(c) - 1][2] if c.isdigit() and 1 <= int(c) <= min(40, len(found)) else None


def list_backups(root, say):
    migrate_dated(root, say)
    for owner, stamp, d in backups(root):
        if is_single(d):
            m = read_meta(os.path.join(d, f"{owner}.db"))
            say(f"  {owner:30} updated {stamp}  {ga.human(single_size(d)):>10}  runs {m.get('runs', '?'):>3}  "
                f"{m.get('sources', '')}  {d}")
        else:
            say(f"  {owner:30} dated   {stamp}  {ga.human(size_of(d)):>10}  (not combined)  {d}")


def menu_combine(root, ask, say):
    """Combine dated backups into one: an owner or all; a dry run first, then on yes."""
    names = owners_with_dated(root)
    if not names:
        say(f"No dated backups in {root}: nothing to combine.")
        return
    for n in names:
        whole, partial = dated(root, n)
        say(f"  {n}: {len(whole)} dated, {len(partial)} broken off (.partial)")
    which = ask("Which (a name, or all)", "all")
    chosen = names if which == "all" else [which] if which in names else []
    if not chosen:
        return
    combine_all(root, say, dry_run=True, delete_partial=True, names=chosen)
    if ask("Combine them now, verify, and delete the dated directories? (yes/no)", "no").lower() != "yes":
        say("Nothing changed.")
        return
    partial = any(dated(root, n)[1] for n in chosen) and \
        ask("Delete the .partial directories (backups that broke off) too? (yes/no)", "no").lower() == "yes"
    combine_all(root, say, delete_partial=partial, names=chosen)


def menu(client, ask, say, root, dry=False, set_root=None, github=None, excludes=(), gitea_data=None):
    """Backups: make one, delete an organization or user, restore into Gitea or
    WeKan, bring a backup up to date straight from GitHub, combine dated backups,
    old data, moving all of Gitea into backups, and the backup directory.
    set_root(path) saves a new backup directory. client is None without a Gitea
    token: then only what needs no Gitea is done. github: a github_backup.GitHub;
    excludes: owner/name repositories left out."""
    while True:
        print(f"\nBackups of Gitea organizations and users     directory: {root}"
              + ("\n  (dry run: nothing is deleted or restored)" if dry else ""))
        print("  1) Back up an organization or user: every repository, issue, release and file")
        print("     (its one backup is updated in place: only what is new or changed)")
        print("  2) Delete an organization or user from Gitea completely")
        print("  3) Restore a backup into Gitea")
        print("  4) Restore a backup into WeKan: one board a repository")
        print("  5) List the backups")
        print("  6) Backup directory")
        print("  7) Update a backup straight from GitHub (no Gitea): only what is new or changed is downloaded")
        print("  8) Combine dated backups into one (saves disk space)")
        print("  9) What older backups had that the newest does not")
        print(" 10) Delete data that does not exist in the newest backup")
        print(" 11) Migrate all Gitea to backups: a repository at a time, deleted from Gitea once verified")
        print("  0) Back")
        c = ask("Choice")
        try:
            if c in ("", "0"):
                return
            if c in ("1", "2", "3", "11") and client is None:
                say("This needs Gitea: set the [gitea] token in Settings.")
                continue
            if c == "7":
                if github is None:
                    say("No GitHub client.")
                    continue
                import github_backup
                name = ga.normalize_scope(ask("GitHub organization or user"))
                if not name or "/" in name:
                    continue
                if excludes:
                    say(f"Left out (Sources excludes): {', '.join(excludes)}")
                more = ga.parse_excludes(ask("Also leave out (owner/name, comma separated)", ""))
                github_backup.update(github, name, root, say, list(excludes) + more)
            elif c == "1":
                name = ga.normalize_scope(ask("Organization or user"))
                if name and "/" not in name:
                    backup(client, name, root, say, excludes)
            elif c == "2":
                name = ga.normalize_scope(ask("Organization or user to delete"))
                if not name or "/" in name:
                    continue
                kind, repos, issues, releases, att, gitsize = owner_summary(client, name)
                say(f"The {kind} {name}: {len(repos)} repositories (git {ga.human(gitsize)}, release files "
                    f"{ga.human(att)}), {releases} releases; every issue, pull request, comment, wiki, "
                    f"file and package goes with it.")
                for r in repos[:30]:
                    say(f"  {r['full_name']}")
                if len(repos) > 30:
                    say(f"  ... and {len(repos) - 30} more")
                if dry:
                    say("Dry run: nothing is deleted.")
                    continue
                if not backups(root, name) and ask(f"There is no backup of {name} in {root}. Make one first? "
                                                   "(yes/no)", "yes").lower() == "yes":
                    backup(client, name, root, say)
                if ask(f"This deletes {name} from Gitea for good. Type {name} to go on") != name:
                    say("Nothing deleted.")
                    continue
                delete_owner(client, name, say)
            elif c == "3":
                d = pick_backup(root, ask, say)
                if d:
                    target = ga.normalize_scope(ask("Into which Gitea organization or user", backup_name(d)))
                    if target and "/" not in target and not dry:
                        restore_gitea(client, d, target, ask, say)
            elif c == "4":
                d = pick_backup(root, ask, say)
                if d:
                    out = os.path.join(d, "wekan")
                    zips = wekan_zips(d, out, say)
                    say(f"WeKan import files: {out} - import each in WeKan: All Boards -> Import -> WeKan .zip")
                    url = ask("Import them now into WeKan at (address, empty: no)", "")
                    if url and zips and not dry:
                        import getpass
                        user = ask("WeKan username")
                        upload_to_wekan(url, user, getpass.getpass("WeKan password: "), zips, say)
            elif c == "5":
                list_backups(root, say)
            elif c == "6":
                v = ask("Backup directory (another disk too, e.g. /run/media/user/disk/backups)", root)
                if v and set_root:
                    root = set_root(v)
            elif c == "8":
                menu_combine(root, ask, say)
            elif c in ("9", "10"):
                migrate_dated(root, say)
                names = [o for o, _, d in backups(root) if is_single(d)]
                say(f"Backups: {', '.join(names) or 'none'}")
                name = ask("Which backup (organization or user)")
                if name not in names:
                    continue
                if c == "9":
                    old_only_report(os.path.join(root, name), say)
                else:
                    delete_old_only(root, name, say, ask, dry_run=dry)
            elif c == "11":
                migrate_all(client, root, say, ask, dry_run=True, excludes=excludes, gitea_data=gitea_data)
                if dry:
                    continue
                if ask("That was the dry run. Migrate now? (yes/no)", "no").lower() != "yes":
                    continue
                go_on = ask("Go on with the next repository when one fails? (yes/no)", "no").lower() == "yes"
                migrate_all(client, root, say, ask, continue_on_error=go_on, excludes=excludes, gitea_data=gitea_data)
        except (BackupError, ga.AttachmentError) as e:
            say(str(e))
