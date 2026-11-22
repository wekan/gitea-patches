"""Backing up a Gitea organization or user, deleting it from Gitea, and restoring it -
into Gitea, or as WeKan boards. Shared by github-to-gitea and the mirror scripts.

A backup of NAME is one directory:

  <backup_dir>/<NAME>/YYYY-MM-DD_HH-MM-SS/<NAME>.db    SQLite: every record
  <backup_dir>/<NAME>/YYYY-MM-DD_HH-MM-SS/<NAME>.zip   every file, in folders

While it is being written the directory is YYYY-MM-DD_HH-MM-SS.partial, so a backup
that broke off is never taken for a whole one.

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

The .db's tables (FORMAT 1) - every row keeps Gitea's own JSON in `json`:

  meta(key, value)                     format, created, gitea, owner, kind, complete
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
import datetime
import hashlib
import json
import mimetypes
import os
import re
import shutil
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
CHUNK = 1 << 20


class BackupError(Exception):
    pass


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
    """A Gitea, by its address (http://host:port, with or without /api/v1) and token."""

    def __init__(self, url, token, dry=False):
        url = url.rstrip("/")
        self.root = url[: -len("/api/v1")] if url.endswith("/api/v1") else url
        self.api = self.root + "/api/v1"
        self.token = token
        self.dry = dry
        self.opener = urllib.request.build_opener(_DropAuthOnRedirect)

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
        with self._open("GET", url, headers={"Accept": "*/*"}, timeout=None) as r:
            while chunk := r.read(CHUNK):
                out.write(chunk)
                digest.update(chunk)
                size += len(chunk)
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


def backups(root, name=None):
    """The whole backups under root, newest first: (name, stamp, directory)."""
    out = []
    owners = [name] if name else (sorted(os.listdir(root)) if os.path.isdir(root) else [])
    for owner in owners:
        d = os.path.join(root, owner)
        if not os.path.isdir(d):
            continue
        for stamp in os.listdir(d):
            full = os.path.join(d, stamp)
            if re.fullmatch(r"\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d(-\d+)?", stamp) and \
                    os.path.isfile(os.path.join(full, f"{owner}.db")):
                out.append((owner, stamp, full))
    return sorted(out, key=lambda b: (b[1], b[0]), reverse=True)


# --- the database -------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE owner(name TEXT PRIMARY KEY, kind TEXT, json TEXT);
CREATE TABLE teams(id INTEGER PRIMARY KEY, name TEXT, json TEXT, members TEXT, repos TEXT);
CREATE TABLE members(login TEXT PRIMARY KEY, json TEXT);
CREATE TABLE repos(full_name TEXT PRIMARY KEY, name TEXT, json TEXT, topics TEXT,
                   git_bundle TEXT, wiki_bundle TEXT, empty INTEGER, collaborators TEXT);
CREATE TABLE labels(repo TEXT, id INTEGER, name TEXT, json TEXT, PRIMARY KEY(repo, id));
CREATE TABLE milestones(repo TEXT, id INTEGER, title TEXT, json TEXT, PRIMARY KEY(repo, id));
CREATE TABLE issues(repo TEXT, number INTEGER, is_pull INTEGER, title TEXT, state TEXT, json TEXT,
                    PRIMARY KEY(repo, number));
CREATE TABLE pulls(repo TEXT, number INTEGER, json TEXT, PRIMARY KEY(repo, number));
CREATE TABLE comments(repo TEXT, id INTEGER, issue_number INTEGER, json TEXT, PRIMARY KEY(repo, id));
CREATE TABLE releases(repo TEXT, id INTEGER, tag TEXT, json TEXT, PRIMARY KEY(repo, id));
CREATE TABLE files(path TEXT PRIMARY KEY, repo TEXT, kind TEXT, parent TEXT, id INTEGER,
                   name TEXT, size INTEGER, sha256 TEXT, uuid TEXT, json TEXT);
CREATE INDEX files_parent ON files(repo, kind, parent);
CREATE INDEX comments_issue ON comments(repo, issue_number);
"""


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def open_db(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    return db


def number_of(comment):
    m = re.search(r"/(?:issues|pulls)/(\d+)", comment.get("issue_url") or comment.get("html_url") or "")
    return int(m.group(1)) if m else 0


# --- backing up ---------------------------------------------------------------------------

class _Writer:
    """The .db and the .zip of one backup being written."""

    def __init__(self, directory, name):
        self.db = open_db(os.path.join(directory, f"{name}.db"))
        self.db.executescript(SCHEMA)
        self.zip = zipfile.ZipFile(os.path.join(directory, f"{name}.zip"), "w", zipfile.ZIP_STORED, allowZip64=True)
        self.tmp = os.path.join(directory, "tmp")
        os.makedirs(self.tmp, exist_ok=True)
        self.paths = set()

    def unique(self, path):
        base, n = path, 1
        while path in self.paths:
            stem, ext = os.path.splitext(base)
            path, n = f"{stem}-{n}{ext}", n + 1
        self.paths.add(path)
        return path

    def add_stream(self, path, fill):
        """A zip entry written by fill(out) -> (size, sha256), never whole in memory."""
        path = self.unique(path)
        info = zipfile.ZipInfo(path, time.localtime()[:6])
        info.compress_type = zipfile.ZIP_STORED
        with self.zip.open(info, "w", force_zip64=True) as out:
            size, digest = fill(out)
        return path, size, digest

    def add_file(self, path, src):
        def fill(out):
            digest, size = hashlib.sha256(), 0
            with open(src, "rb") as f:
                while chunk := f.read(CHUNK):
                    out.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            return size, digest.hexdigest()
        return self.add_stream(path, fill)

    def row(self, table, **values):
        cols = ", ".join(values)
        self.db.execute(f"INSERT OR REPLACE INTO {table}({cols}) VALUES ({', '.join('?' * len(values))})",
                        list(values.values()))

    def close(self):
        self.db.commit()
        self.db.close()
        self.zip.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


def owner_kind(client, name):
    s, d = client.request("GET", f"/orgs/{name}")
    if s == 200:
        return "organization", d
    s, d = client.request("GET", f"/users/{name}")
    if s == 200:
        return "user", d
    raise BackupError(f"{name}: no such organization or user in Gitea")


def _bundle(client, w, full, wiki, say):
    """The repository's (or its wiki's) every ref as a git bundle in the zip, or None
    when there is nothing - an empty repository, or no wiki."""
    url = client.git_url(full, wiki)
    env = client.git_env()
    code, out = git(["ls-remote", url], env=env)
    if code != 0 or not out.strip():
        if code != 0 and not wiki:
            say(f"    git: cannot read {url}: {out[-300:]}")
        return None
    mirror = os.path.join(w.tmp, "mirror.git")
    shutil.rmtree(mirror, ignore_errors=True)
    code, out = git(["clone", "--quiet", "--mirror", url, mirror], env=env)
    if code != 0:
        raise BackupError(f"{full}{' wiki' if wiki else ''}: git clone failed: {out[-500:]}")
    bundle = os.path.join(w.tmp, "repo.bundle")
    code, out = git(["-C", mirror, "bundle", "create", "--quiet", bundle, "--all"])
    shutil.rmtree(mirror, ignore_errors=True)
    if code != 0:
        if "empty bundle" in out.lower():
            return None
        raise BackupError(f"{full}: git bundle failed: {out[-500:]}")
    path, size, _ = w.add_file(f"{safe_part(full.split('/')[1])}/{'wiki' if wiki else 'git'}.bundle", bundle)
    os.remove(bundle)
    say(f"    {'wiki' if wiki else 'git'}: {ga.human(size)}")
    return path


def _attachment(client, w, repo, kind, parent, folder, asset, say):
    try:
        path, size, digest = w.add_stream(folder, lambda out: client.download(client.file_url(asset), out))
    except (BackupError, urllib.error.URLError, OSError) as e:
        say(f"    not saved: {folder}: {e}")
        return 0
    w.row("files", path=path, repo=repo, kind=kind, parent=str(parent), id=asset.get("id"),
          name=asset.get("name") or "", size=size, sha256=digest, uuid=asset.get("uuid") or "", json=dumps(asset))
    return size


def backup_repo(client, w, repo, say):
    full = repo["full_name"]
    short = safe_part(repo["name"])
    say(f"  {full}")
    s, topics = client.request("GET", f"/repos/{full}/topics")
    topics = (topics or {}).get("topics", []) if s == 200 and isinstance(topics, dict) else []
    git_bundle = None if repo.get("empty") else _bundle(client, w, full, False, say)
    wiki_bundle = _bundle(client, w, full, True, say) if repo.get("has_wiki") else None
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
        w.row("issues", repo=full, number=i["number"], is_pull=is_pull, title=i.get("title"),
              state=i.get("state"), json=dumps(i))
        issues += 1
        for a in i.get("assets") or []:
            size += _attachment(client, w, full, "issues", i["number"], f"{short}/issues/{i['number']}/"
                                f"{a['id']}-{safe_part(a.get('name'))}", a, say)
        if is_pull:
            s, pr = client.request("GET", f"/repos/{full}/pulls/{i['number']}")
            if s == 200:
                w.row("pulls", repo=full, number=i["number"], json=dumps(pr))
                pulls += 1
    for c in ga.paged(client.request, f"/repos/{full}/issues/comments"):
        n = number_of(c)
        w.row("comments", repo=full, id=c["id"], issue_number=n, json=dumps(c))
        comments += 1
        for a in c.get("assets") or []:
            size += _attachment(client, w, full, "comments", c["id"], f"{short}/comments/{n}/{c['id']}/"
                                f"{a['id']}-{safe_part(a.get('name'))}", a, say)
    releases = 0
    for r in ga.paged(client.request, f"/repos/{full}/releases"):
        w.row("releases", repo=full, id=r["id"], tag=r.get("tag_name"), json=dumps(r))
        releases += 1
        tag = safe_part(r.get("tag_name") or r["id"])
        for a in r.get("assets") or []:
            size += _attachment(client, w, full, "releases", r["id"], f"{short}/releases/{tag}/"
                                f"{safe_part(a.get('name'))}", a, say)
    say(f"    {issues - pulls} issues, {pulls} pull requests, {comments} comments, {releases} releases, "
        f"files {ga.human(size)}")
    w.db.commit()


def backup(client, name, root, say, excludes=()):
    """Every record and file of the organization or user NAME, into
    root/NAME/<stamp>/NAME.db and NAME.zip. Returns the backup's directory."""
    name = ga.normalize_scope(name)
    if not name or "/" in name:
        raise BackupError("A backup is of an organization or a user: give its name.")
    kind, info = owner_kind(client, name)
    name = info.get("username") or info.get("login") or name
    repos = ga.scope_repos(client.request, name)
    repos = [r for r in repos if r["full_name"] not in set(excludes)]
    stamp = datetime.datetime.now().strftime(STAMP)
    final = os.path.join(root, name, stamp)
    n = 1
    while os.path.exists(final) or os.path.exists(final + ".partial"):
        final = os.path.join(root, name, f"{stamp}-{n}")
        n += 1
    partial = final + ".partial"
    os.makedirs(partial)
    say(f"Backing up the {kind} {name}: {len(repos)} repositories -> {final}")
    w = _Writer(partial, name)
    try:
        meta = {"format": str(FORMAT), "created": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "gitea": client.root, "owner": name, "kind": kind, "complete": "no"}
        for k, v in meta.items():
            w.row("meta", key=k, value=v)
        w.row("owner", name=name, kind=kind, json=dumps(info))
        if info.get("avatar_url"):
            try:
                path, size, digest = w.add_stream("owner/avatar.png", lambda out: client.download(info["avatar_url"], out))
                w.row("files", path=path, repo="", kind="avatar", parent="", id=0, name="avatar.png",
                      size=size, sha256=digest, uuid="", json="{}")
            except (urllib.error.URLError, OSError):
                pass
        if kind == "organization":
            for label in ga.paged(client.request, f"/orgs/{name}/labels"):
                w.row("labels", repo="", id=label["id"], name=label.get("name"), json=dumps(label))
            for m in ga.paged(client.request, f"/orgs/{name}/members"):
                w.row("members", login=m.get("login"), json=dumps(m))
            for t in ga.paged(client.request, f"/orgs/{name}/teams"):
                people = [m.get("login") for m in ga.paged(client.request, f"/teams/{t['id']}/members")]
                held = [r.get("name") for r in ga.paged(client.request, f"/teams/{t['id']}/repos")]
                w.row("teams", id=t["id"], name=t.get("name"), json=dumps(t), members=dumps(people), repos=dumps(held))
        for repo in repos:
            backup_repo(client, w, repo, say)
        w.row("meta", key="complete", value="yes")
    finally:
        w.close()
    os.rename(partial, final)
    db, zp = os.path.join(final, f"{name}.db"), os.path.join(final, f"{name}.zip")
    say(f"Backup ready: {db} ({ga.human(os.path.getsize(db))}), {zp} ({ga.human(os.path.getsize(zp))})")
    return final


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
    """A backup directory opened for reading."""

    def __init__(self, directory):
        dbs = [f for f in os.listdir(directory) if f.endswith(".db")]
        if len(dbs) != 1:
            raise BackupError(f"{directory}: not a backup (no single .db)")
        self.dir = directory
        self.name = dbs[0][:-3]
        self.db = open_db(os.path.join(directory, dbs[0]))
        self.meta = {r["key"]: r["value"] for r in self.db.execute("SELECT key, value FROM meta")}
        if self.meta.get("format") != str(FORMAT):
            raise BackupError(f"{directory}: backup format {self.meta.get('format')}, this reads {FORMAT}")
        self.zip = zipfile.ZipFile(os.path.join(directory, f"{self.name}.zip"))

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


# --- the menu -----------------------------------------------------------------------------

def pick_backup(root, ask):
    found = backups(root)
    if not found:
        print(f"No backups in {root}.")
        return None
    for n, (owner, stamp, _) in enumerate(found[:40], 1):
        print(f" {n:2}) {owner}  {stamp}")
    c = ask("Backup", "1")
    return found[int(c) - 1][2] if c.isdigit() and 1 <= int(c) <= min(40, len(found)) else None


def menu(client, ask, say, root, dry=False, set_root=None):
    """Backups: make one, delete an organization or user, restore into Gitea or
    WeKan, and the backup directory. set_root(path) saves a new backup directory."""
    while True:
        print(f"\nBackups of Gitea organizations and users     directory: {root}"
              + ("\n  (dry run: nothing is deleted or restored)" if dry else ""))
        print("  1) Back up an organization or user: every repository, issue, release and file")
        print("  2) Delete an organization or user from Gitea completely")
        print("  3) Restore a backup into Gitea")
        print("  4) Restore a backup into WeKan: one board a repository")
        print("  5) List the backups")
        print("  6) Backup directory")
        print("  0) Back")
        c = ask("Choice")
        try:
            if c in ("", "0"):
                return
            if c == "1":
                name = ga.normalize_scope(ask("Organization or user"))
                if name and "/" not in name:
                    backup(client, name, root, say)
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
                d = pick_backup(root, ask)
                if d:
                    target = ga.normalize_scope(ask("Into which Gitea organization or user", os.path.basename(
                        os.path.dirname(d))))
                    if target and "/" not in target and not dry:
                        restore_gitea(client, d, target, ask, say)
            elif c == "4":
                d = pick_backup(root, ask)
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
                for owner, stamp, d in backups(root):
                    size = sum(os.path.getsize(os.path.join(d, f)) for f in os.listdir(d)
                               if os.path.isfile(os.path.join(d, f)))
                    say(f"  {owner:30} {stamp}  {ga.human(size):>10}  {d}")
            elif c == "6":
                v = ask("Backup directory (another disk too, e.g. /run/media/user/disk/backups)", root)
                if v and set_root:
                    root = set_root(v)
        except (BackupError, ga.AttachmentError) as e:
            say(str(e))
