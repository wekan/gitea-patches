#!/usr/bin/env python3
"""mirror/common/gitea_backup.py: backing up a Gitea organization or user into its
one backup <dir>/<name>/<name>.db and <name>.zip - updated in place with only what is
new or changed - combining the dated backups of older versions into it, old data
(listed, deleted), moving all of a Gitea into backups a repository at a time,
deleting an owner from Gitea completely, and restoring it - into Gitea with the same
numbers, authors, comments, releases and files, and as WeKan boards.

The pure parts and a fake Gitea run always. The whole round trip runs against a real, DISPOSABLE
Gitea when one is given - never the Gitea you use, because it creates and deletes
organizations:

  python3 -I tests/giteaBackup.test.py
  GITEA_TEST_URL=http://127.0.0.1:3399 GITEA_TEST_TOKEN=<admin token> \\
    GITEA_TEST_USER=alice python3 -I tests/giteaBackup.test.py

GITEA_TEST_USER is a second, ordinary user of that Gitea: the issues are written as
them (Sudo), and the restore must keep them as the author.
"""

import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "mirror", "common"))
import gitea_backup as gb  # noqa: E402

failures = 0


def check(name, cond):
    global failures
    print(("ok   " if cond else "FAIL ") + name)
    failures += 0 if cond else 1


# --- pure ----------------------------------------------------------------------------------

check("a tag or file name is never a path", [gb.safe_part(x) for x in ("v1/beta", "..", ".", "", "a\\b", "x\ny")]
      == ["v1_beta", "_", "_", "_", "a_b", "x_y"])
here = "/opt/mirror/github-to-gitea"
check("the backup directory is beside the script by default",
      gb.backup_root("", here) == f"{here}/github-to-gitea-backup")
check("a relative backup directory is beside the script", gb.backup_root("b", here) == f"{here}/b")
check("an absolute backup directory is another disk", gb.backup_root("/run/media/u/disk/bk", here) == "/run/media/u/disk/bk")
check("~ is the home directory", gb.backup_root("~/bk", here) == os.path.expanduser("~/bk"))
check("WeKan colors: the nearest one", (gb.wekan_color("#eb4646"), gb.wekan_color("0078c0"), gb.wekan_color("bad"))
      == ("red", "blue", "gray"))
check("a WeKan id has no dash (the zip import reads up to one)", "-" not in gb._wid("a/b", "file", "x-y"))
c = gb.Client("http://g:3000/api/v1", "t")
check("the client takes the address with or without /api/v1", (c.root, c.api) == ("http://g:3000", "http://g:3000/api/v1"))
check("a file is downloaded from this Gitea by its uuid, not from ROOT_URL",
      c.file_url({"uuid": "u1", "browser_download_url": "http://elsewhere/attachments/u1"}) == "http://g:3000/attachments/u1")
try:
    c.file_url({"name": "x", "browser_download_url": "http://elsewhere/x"})
    check("negative: a file on another host is refused (the token would go there)", False)
except gb.BackupError:
    check("negative: a file on another host is refused (the token would go there)", True)

with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as d:
    for p in ("wekan/2026-01-01_00-00-00", "wekan/2026-02-01_00-00-00", "wekan/2026-03-01_00-00-00.partial", "x/junk"):
        os.makedirs(os.path.join(d, p))
    for p in ("wekan/2026-01-01_00-00-00", "wekan/2026-02-01_00-00-00", "wekan/2026-03-01_00-00-00.partial"):
        open(os.path.join(d, p, "wekan.db"), "w").close()
    check("backups: newest first, and a .partial one is not a backup",
          [b[1] for b in gb.backups(d)] == ["2026-02-01_00-00-00", "2026-01-01_00-00-00"])

src = open(os.path.join(ROOT, "mirror", "common", "gitea_backup.py"), encoding="utf-8").read()
check("negative: the zip is written STORED, never deflated (Omi reads its entries in place)",
      "ZIP_DEFLATED" not in src.split("# --- restoring as WeKan boards")[0])
check("negative: deleting an owner needs its name typed", "Type {name} to go on\") != name" in src)
gtg = open(os.path.join(ROOT, "mirror", "github-to-gitea", "github-to-gitea.py"), encoding="utf-8").read()
check("github-to-gitea: the menu has Backups and the backup_dir setting",
      "menu_backups()" in gtg and '"backup_dir", gitea_backup.DEFAULT_DIR' in gtg)

# --- dated backups of older versions, combined into one -------------------------------------

OLD_SCHEMA = """
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
"""
TMP = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")


def sh(*args, cwd=None):
    r = subprocess.run(["git"] + list(args), cwd=cwd, env=ENV, capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"git {args}: {r.stdout}{r.stderr}")
    return r.stdout.strip()


def commit(work, text):
    open(os.path.join(work, "f.txt"), "a").write(text + "\n")
    sh("add", ".", cwd=work)
    sh("commit", "-qm", text, cwd=work)
    return sh("rev-parse", "HEAD", cwd=work)


def bundle_of(work, refs):
    b = os.path.join(TMP, "b.bundle")
    sh("bundle", "create", "-q", b, *refs, cwd=work)
    data = open(b, "rb").read()
    os.remove(b)
    return data


def make_dated(root, name, stamp, created, issues, files, bundle=None):
    """A dated backup as older versions wrote it: no seen, no runs."""
    d = os.path.join(root, name, stamp)
    os.makedirs(d)
    db = sqlite3.connect(os.path.join(d, f"{name}.db"))
    db.executescript(OLD_SCHEMA)
    for k, v in {"format": "1", "created": created, "gitea": "http://g", "owner": name, "kind": "organization",
                 "complete": "yes"}.items():
        db.execute("INSERT INTO meta VALUES (?, ?)", (k, v))
    db.execute("INSERT INTO owner VALUES (?, 'organization', '{}')", (name,))
    db.execute("INSERT INTO repos VALUES ('old/proj', 'proj', '{}', '[]', ?, NULL, ?, '[]')",
               ("proj/git.bundle" if bundle else None, 0 if bundle else 1))
    for n, title in issues:
        db.execute("INSERT INTO issues VALUES ('old/proj', ?, 0, ?, 'open', ?)",
                   (n, title, json.dumps({"number": n, "title": title})))
    with zipfile.ZipFile(os.path.join(d, f"{name}.zip"), "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        for path, data in files.items():
            info = zipfile.ZipInfo(path, (2026, 1, 1, 0, 0, 0))
            with z.open(info, "w", force_zip64=True) as out:
                out.write(data)
            db.execute("INSERT INTO files VALUES (?, 'old/proj', 'issues', '1', ?, ?, ?, ?, '', '{}')",
                       (path, hash(path) % 1000, path.rsplit("/", 1)[-1], len(data), hashlib.sha256(data).hexdigest()))
        if bundle:
            info = zipfile.ZipInfo("proj/git.bundle", (2026, 1, 1, 0, 0, 0))
            with z.open(info, "w", force_zip64=True) as out:
                out.write(bundle)
    db.commit()
    db.close()
    return d


said = []
say = said.append
work = os.path.join(TMP, "w")
sh("init", "-q", "-b", "main", work)
c1 = commit(work, "one")
sh("branch", "gone", cwd=work)
b1 = bundle_of(work, ["--all"])
c2 = commit(work, "two")
sh("branch", "-D", "gone", cwd=work)
b2 = bundle_of(work, ["--all"])
c3 = commit(work, "three")
b3 = bundle_of(work, ["--all"])
R = os.path.join(TMP, "combine")
A1, A2 = b"first copy of a\n" * 300, b"second copy of a\n" * 300
s1 = make_dated(R, "old", "2026-01-01_00-00-00", "2026-01-01T00:00:00+00:00", [(1, "One"), (2, "Two")],
                {"proj/issues/1/1-a.txt": A1}, b1)
s2 = make_dated(R, "old", "2026-02-01_00-00-00", "2026-02-01T00:00:00+00:00", [(1, "One, edited"), (2, "Two"), (3, "Three")],
                {"proj/issues/1/1-a.txt": A2, "proj/issues/1/2-b.txt": b"b" * 4000}, b2)
s3 = make_dated(R, "old", "2026-03-01_00-00-00", "2026-03-01T00:00:00+00:00", [(1, "One, edited"), (3, "Three")],
                {"proj/issues/1/2-b.txt": b"b" * 4000, "proj/issues/1/3-c.txt": b"c" * 100}, b3)
os.makedirs(os.path.join(R, "old", "2026-03-02_00-00-00.partial"))
open(os.path.join(R, "old", "2026-03-02_00-00-00.partial", "old.db"), "w").write("broken off")
before_size = sum(gb.size_of(x) for x in (s1, s2, s3))

r = gb.combine(R, "old", say, dry_run=True)
check("combine --dry-run: reports, and deletes and writes nothing",
      r and r["dry_run"] and all(os.path.isdir(x) for x in (s1, s2, s3)) and not gb.is_single(os.path.join(R, "old"))
      and r["freed"] > 0 and any("dry run" in m for m in said))

# A damaged dated zip: nothing combined, nothing deleted.
bad = make_dated(os.path.join(TMP, "bad"), "old", "2026-01-01_00-00-00", "2026-01-01T00:00:00+00:00", [(1, "x")],
                 {"proj/issues/1/1-a.txt": A1})
make_dated(os.path.join(TMP, "bad"), "old", "2026-02-01_00-00-00", "2026-02-01T00:00:00+00:00", [(1, "y")],
           {"proj/issues/1/1-a.txt": A2})
zp = os.path.join(bad, "old.zip")
blob = bytearray(open(zp, "rb").read())
blob[200:210] = b"XXXXXXXXXX"  # inside the entry's bytes: its CRC no longer matches
open(zp, "wb").write(blob)
try:
    gb.combine(os.path.join(TMP, "bad"), "old", say)
    check("negative: a damaged dated zip stops the combining", False)
except gb.BackupError as e:
    check("negative: a damaged dated zip stops the combining, and says why", "damaged" in str(e)
          and "Nothing was deleted" in str(e))
check("negative: ... and deletes nothing, leaves nothing", sorted(os.listdir(os.path.join(TMP, "bad", "old")))
      == ["2026-01-01_00-00-00", "2026-02-01_00-00-00"])

said.clear()
r = gb.combine(R, "old", say)
od = os.path.join(R, "old")
print(f"    combined: {r['before']} -> {r['after']} bytes, {r['freed']} freed")
check("combine: the dated directories are gone, one backup is there, the .partial is left and named",
      sorted(os.listdir(od)) == ["2026-03-02_00-00-00.partial", "old.db", "old.zip"]
      and any(".partial" in m and "broke off" in m for m in said))
check("combine: the disk space freed is reported", r["freed"] > 0 and r["after"] < before_size
      and any("freed" in m for m in said))
b = gb.Backup(od)
check("combine: every row of every dated backup, the newest value for the same key",
      [(x["number"], x["title"]) for x in b.rows("SELECT number, title FROM issues ORDER BY number")]
      == [(1, "One, edited"), (2, "Two"), (3, "Three")])
check("combine: rows only in older backups are old data (seen < runs); runs counts the dated backups",
      b.meta["runs"] == "3" and [x["number"] for x in b.rows("SELECT number FROM issues WHERE seen < 3")] == [2])
check("combine: every file, the newest copy of the same path",
      sorted(b.zip.namelist()) == ["proj/git.bundle", "proj/issues/1/1-a.txt", "proj/issues/1/2-b.txt",
                                   "proj/issues/1/3-c.txt"] and b.zip.read("proj/issues/1/1-a.txt") == A2)
check("combine: the zip is STORED and whole", b.zip.testzip() is None and
      all(i.compress_type == zipfile.ZIP_STORED for i in b.zip.infolist()))
heads = gb.bundle_heads(b.zip, "proj/git.bundle")
check("combine: bundles that differ are merged - a branch deleted later is kept, main is the newest",
      heads.get("refs/heads/main") == c3 and heads.get("refs/heads/gone") == c1 and r["merged"] == 1)
check("combine: meta says what was merged, created is the first",
      json.loads(b.meta["merged"]) == ["2026-01-01_00-00-00", "2026-02-01_00-00-00", "2026-03-01_00-00-00"]
      and b.meta["created"].startswith("2026-01-01") and b.meta["kept"])
b.close()
check("backups(): one line for the owner", [(o, os.path.basename(d)) for o, _, d in gb.backups(R)] == [("old", "old")])
runs, data, paths = gb.old_only(od)
check("old data after combining: issue 2 and the file a, which only older backups had",
      [x["number"] for x in data["issues"]] == [2] and paths == {"proj/issues/1/1-a.txt": len(A2)})
r2 = gb.combine(R, "old", say, delete_partial=True)
check("combine again: nothing to combine; the .partial is deleted when asked",
      r2 is None and sorted(os.listdir(od)) == ["old.db", "old.zip"])

# Backup(<owner directory>) combines first.
R2 = os.path.join(TMP, "combine2")
make_dated(R2, "o2", "2026-01-01_00-00-00", "2026-01-01T00:00:00+00:00", [(1, "x")], {"a/x": b"x"})
make_dated(R2, "o2", "2026-01-02_00-00-00", "2026-01-02T00:00:00+00:00", [(2, "y")], {"a/y": b"y"})
b = gb.Backup(os.path.join(R2, "o2"), say=say)
check("Backup(<NAME>) combines the dated backups first", sorted(b.zip.namelist()) == ["a/x", "a/y"]
      and len(b.rows("SELECT * FROM issues")) == 2 and not gb.dated(R2, "o2")[0])
b.close()

# The command line: --combine-backups, offline - no Gitea, no GitHub, no token.
CLI = os.path.join(TMP, "cli")
os.makedirs(os.path.join(CLI, "github-to-gitea"))
os.symlink(os.path.join(ROOT, "mirror", "common"), os.path.join(CLI, "common"))
shutil.copy(os.path.join(ROOT, "mirror", "github-to-gitea", "github-to-gitea.py"), os.path.join(CLI, "github-to-gitea"))
R3 = os.path.join(TMP, "combine3")
open(os.path.join(CLI, "github-to-gitea", "github-to-gitea.ini"), "w").write(f"[options]\nbackup_dir = {R3}\n")
for n, stamp in enumerate(("2026-01-01_00-00-00", "2026-01-02_00-00-00")):
    make_dated(R3, "cli", stamp, f"2026-01-0{n + 1}T00:00:00+00:00", [(n, "x")], {"a/same": b"s" * 5000, f"a/{n}": b"n"})
script = os.path.join(CLI, "github-to-gitea", "github-to-gitea.py")
env = {k: v for k, v in os.environ.items() if not k.lower().endswith("_proxy")}
dry = subprocess.run([sys.executable, "-I", script, "--combine-backups", "all", "--dry-run"], capture_output=True,
                     text=True, env=env, stdin=subprocess.DEVNULL)
check("--combine-backups all --dry-run: reports, changes nothing, needs no token",
      dry.returncode == 0 and "dry run" in dry.stdout and len(gb.dated(R3, "cli")[0]) == 2)
real = subprocess.run([sys.executable, "-I", script, "--combine-backups", "cli"], capture_output=True, text=True,
                      env=env, stdin=subprocess.DEVNULL)
check("--combine-backups cli: combined, verified, the dated directories deleted",
      real.returncode == 0 and "freed" in real.stdout and not gb.dated(R3, "cli")[0] and gb.is_single(os.path.join(R3, "cli")))

# --- a fake Gitea: backups updated in place ---------------------------------------------------

GIT = os.path.join(TMP, "git")


def bare(full, commits=1):
    w = os.path.join(TMP, "work", full)
    sh("init", "-q", "-b", "main", w)
    for n in range(commits):
        commit(w, f"{full} {n}")
    where = os.path.join(GIT, f"{full}.git")
    sh("clone", "-q", "--bare", w, where)
    return w, where


class FakeGitea:
    """acme (an organization: small, mid, big), carol (a user: notes), admin (the
    token's own user: own). Every list pages; files download by uuid."""

    def __init__(self):
        self.root = "http://fake-gitea"
        self.dry = False
        self.requests = 0
        self.downloads = [0, 0]
        self.blobs = {}
        self.repos, self.issues, self.comments, self.releases, self.packages = {}, {}, {}, {}, {}
        self.orgs = {"acme": {"username": "acme", "avatar_url": "fake://avatar-acme"}}
        self.users = {"carol": {"login": "carol", "username": "carol"}, "admin": {"login": "admin", "username": "admin"}}
        self.extra_issue = None  # (full, issue): appears only after the backup, so verification fails
        self.ids = 1000
        for full, size in (("acme/small", 1), ("acme/mid", 50), ("acme/big", 900), ("carol/notes", 20),
                           ("admin/own", 5)):
            self.add_repo(full, size)

    def asset(self, data, name):
        self.ids += 1
        u = f"u{self.ids}"
        self.blobs[u] = data
        return {"id": self.ids, "name": name, "size": len(data), "uuid": u,
                "browser_download_url": f"{self.root}/attachments/{u}"}

    def add_repo(self, full, size):
        bare(full)
        owner, name = full.split("/")
        self.repos[full] = {"full_name": full, "name": name, "owner": {"login": owner}, "size": size,
                            "empty": False, "has_wiki": False, "default_branch": "main"}
        self.issues[full] = [{"number": 1, "title": f"{full} issue", "state": "open", "updated_at": "2026-01-01",
                              "assets": [self.asset(full.encode() * 50, "shot.png")]}]
        self.comments[full] = [{"id": self.ids + 7, "body": "c", "issue_url": f"{self.root}/repos/{full}/issues/1",
                                "updated_at": "2026-01-01", "assets": []}]
        self.releases[full] = [{"id": self.ids + 9, "tag_name": "v1", "assets": [self.asset(b"r" * (size * 100), "r.zip")]}]

    def file_url(self, a):
        return f"fake://{a['uuid']}"

    def download(self, url, out):
        data = self.blobs[url.split("//", 1)[1]] if url.startswith("fake://u") else b"avatar"
        out.write(data)
        self.downloads[0] += 1
        self.downloads[1] += len(data)
        return len(data), hashlib.sha256(data).hexdigest()

    def git_url(self, full, wiki=False):
        return os.path.join(GIT, f"{full}{'.wiki' if wiki else ''}.git")

    def git_env(self):
        return dict(os.environ)

    def page(self, items, q):
        limit, page = int(q.get("limit", ["50"])[0]), int(q.get("page", ["1"])[0])
        return 200, items[(page - 1) * limit:page * limit]

    def request(self, method, path, body=None, sudo=None):
        self.requests += 1
        u = urllib.parse.urlsplit(path)
        q = urllib.parse.parse_qs(u.query)
        p = u.path
        if method == "DELETE":
            if p.startswith("/repos/"):
                full = p[len("/repos/"):]
                if full not in self.repos:
                    return 404, {}
                del self.repos[full]
                shutil.rmtree(self.git_url(full), ignore_errors=True)
                return 204, None
            if p.startswith("/orgs/"):
                self.orgs.pop(p.split("/")[2], None)
                return 204, None
            if p.startswith("/admin/users/"):
                self.users.pop(p.split("/")[3], None)
                return 204, None
            if p.startswith("/packages/"):
                owner = p.split("/")[2]
                self.packages[owner] = [x for x in self.packages.get(owner, []) if x["name"] != urllib.parse.unquote(p.split("/")[4])]
                return 204, None
            return 404, {}
        if p == "/user":
            return 200, {"login": "admin", "is_admin": True}
        m = re.fullmatch(r"/orgs/([^/]+)", p)
        if m:
            return (200, self.orgs[m.group(1)]) if m.group(1) in self.orgs else (404, {})
        m = re.fullmatch(r"/users/([^/]+)", p)
        if m:
            return (200, self.users[m.group(1)]) if m.group(1) in self.users else (404, {})
        m = re.fullmatch(r"/(?:orgs|users)/([^/]+)/repos", p)
        if m:
            return self.page([r for f, r in sorted(self.repos.items()) if f.split("/")[0] == m.group(1)], q)
        if p == "/repos/search":
            return self.page([r for _, r in sorted(self.repos.items())], q)
        if p == "/admin/orgs":
            return self.page(list(self.orgs.values()), q)
        if p in ("/orgs/acme/labels", "/orgs/acme/members"):
            return self.page([{"id": 1, "login": "alice", "name": "org-label"}], q)
        if p == "/orgs/acme/teams":
            return self.page([{"id": 5, "name": "devs"}], q)
        if p.startswith("/teams/5/"):
            return self.page([{"id": 1, "login": "alice", "name": "small"}], q)
        m = re.fullmatch(r"/packages/([^/]+)", p)
        if m:
            return self.page(self.packages.get(m.group(1), []), q)
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)(/.*)?", p)
        if not m or m.group(1) not in self.repos:
            return 404, {"message": "no"}
        full, rest = m.group(1), m.group(2) or ""
        if rest == "":
            return 200, self.repos[full]
        if rest == "/topics":
            return 200, {"topics": ["t"]}
        if rest == "/issues":
            items = list(self.issues[full])
            if self.extra_issue and self.extra_issue[0] == full and self.extra_issue[2]:
                items.append(self.extra_issue[1])
            return self.page(items, q)
        if rest == "/issues/comments":
            return self.page(self.comments[full], q)
        if rest == "/releases":
            return self.page(self.releases[full], q)
        if rest in ("/labels", "/milestones", "/collaborators"):
            return self.page([], q)
        return 404, {}


fake = FakeGitea()
BK = os.path.join(TMP, "bk")
said.clear()
d1 = gb.backup(fake, "acme", BK, say)
check("first run: the one backup <dir>/acme/acme.db and acme.zip, no dated directory",
      d1 == os.path.join(BK, "acme") and sorted(os.listdir(d1)) == ["acme.db", "acme.zip"])
b = gb.Backup(d1)
check("first run: run 1, every repository, issue and file", b.meta["runs"] == "1" and b.meta["sources"] == "gitea"
      and [r["name"] for r in b.repos()] == ["big", "mid", "small"] and len(b.rows("SELECT * FROM files")) == 7
      and {"big/git.bundle", "small/releases/v1/r.zip", "owner/avatar.png"} <= set(b.zip.namelist()))
b.close()
first_downloads = list(fake.downloads)
zip1 = open(os.path.join(d1, "acme.zip"), "rb").read()
fake.downloads = [0, 0]
fake.requests = 0
said.clear()
gb.backup(fake, "acme", BK, say)
st = gb.backup.stats
print(f"    unchanged second run: {fake.requests} requests, {fake.downloads[0]} downloads")
check("second run, nothing changed: nothing downloaded, every file and bundle kept",
      fake.downloads == [0, 0] and st.copied[0] == 7 and st.git_same == 3 and st.git_fetched == 0)
check("second run, nothing changed: the zip is not written at all (byte-identical)",
      open(os.path.join(d1, "acme.zip"), "rb").read() == zip1)
b = gb.Backup(d1)
check("second run: run 2, every row seen by it", b.meta["runs"] == "2" and
      not b.rows("SELECT 1 FROM issues WHERE seen < 2") and not b.rows("SELECT 1 FROM files WHERE seen < 2"))
b.close()

# Gitea changes: a new issue with a file, a new release file, a deleted issue, a new commit.
new_file = b"N" * 20000
fake.issues["acme/mid"].append({"number": 2, "title": "new", "state": "open", "updated_at": "2026-02-01",
                                "assets": [fake.asset(new_file, "new.bin")]})
gone = fake.issues["acme/small"].pop()  # issue 1 of small, with its file, deleted in Gitea
fake.releases["acme/big"][0]["assets"].append(fake.asset(b"R" * 3000, "r2.zip"))
commit(os.path.join(TMP, "work", "acme/mid"), "more")
sh("push", "-q", fake.git_url("acme/mid"), "main", cwd=os.path.join(TMP, "work", "acme/mid"))
zsize = os.path.getsize(os.path.join(d1, "acme.zip"))
with zipfile.ZipFile(os.path.join(d1, "acme.zip")) as z:
    old_mid = z.getinfo("mid/git.bundle").file_size
fake.downloads = [0, 0]
said.clear()
gb.backup(fake, "acme", BK, say)
st = gb.backup.stats
zsize2 = os.path.getsize(os.path.join(d1, "acme.zip"))
b = gb.Backup(d1)
new_mid = b.zip.getinfo("mid/git.bundle").file_size
print(f"    third run: zip {zsize} -> {zsize2} bytes; {fake.downloads[0]} downloads, {fake.downloads[1]} bytes")
check("third run: only the two new files are downloaded", fake.downloads == [2, len(new_file) + 3000])
check("third run: the zip grew by the new content only (no copy of anything kept)",
      0 < zsize2 - zsize <= len(new_file) + 3000 + (new_mid - old_mid) + 3 * 600)
check("third run: the changed repository's bundle is fetched, the others kept", st.git_fetched == 1 and st.git_same == 2)
check("an issue deleted in Gitea stays in the backup, with its file - as old data",
      b.rows("SELECT seen FROM issues WHERE repo = 'acme/small' AND number = 1")[0]["seen"] == 2
      and b.rows("SELECT seen FROM files WHERE repo = 'acme/small' AND kind = 'issues'")[0]["seen"] == 2
      and b.meta["runs"] == "3")
b.close()
said.clear()
runs, data, paths = gb.old_only_report(d1, say)
small_file = [p for p in paths if p.startswith("small/issues/1/")]
check("old data: the deleted issue and its file, nothing else",
      {t: len(v) for t, v in data.items() if v} == {"issues": 1, "files": 1} and len(small_file) == 1
      and any("acme/small" in m for m in said) and any("issues 1: #1 acme/small issue" in m for m in said))
check("menu: the old-data and delete items", "What older backups had that the newest does not" in open(
      os.path.join(ROOT, "mirror", "common", "gitea_backup.py"), encoding="utf-8").read())

zip3 = open(os.path.join(d1, "acme.zip"), "rb").read()
db3 = open(os.path.join(d1, "acme.db"), "rb").read()
said.clear()
check("delete old data: without the name typed nothing is deleted",
      gb.delete_old_only(BK, "acme", say, ask=lambda *a: "not-acme") is None
      and open(os.path.join(d1, "acme.zip"), "rb").read() == zip3 and open(os.path.join(d1, "acme.db"), "rb").read() == db3)
r = gb.delete_old_only(BK, "acme", say, dry_run=True)
check("delete old data --dry-run: reports the space, deletes nothing", r["would_free"] == paths[small_file[0]]
      and open(os.path.join(d1, "acme.zip"), "rb").read() == zip3)
b = gb.Backup(d1)
keep = {i.filename: i.CRC for i in b.zip.infolist() if i.filename not in paths}
rows_now = {t: len(b.rows(f"SELECT * FROM {t} WHERE seen = 3")) for t in gb.DATA_TABLES}
b.close()
r = gb.delete_old_only(BK, "acme", say, confirm="acme")
b = gb.Backup(d1)
check("delete old data: the old rows and their zip entries are gone, and their bytes freed",
      r["rows"] == 2 and r["files"] == 1 and r["freed"] >= paths[small_file[0]] and
      not b.rows("SELECT 1 FROM issues WHERE seen < 3") and small_file[0] not in b.zip.namelist())
check("negative: data the newest run saw is never deleted - every row and entry, byte for byte",
      {i.filename: i.CRC for i in b.zip.infolist()} == keep and b.zip.testzip() is None and
      {t: len(b.rows(f"SELECT * FROM {t}")) for t in gb.DATA_TABLES} == rows_now and b.meta.get("deleted_old_only"))
b.close()
print(f"    delete old data: freed {r['freed']} bytes")

# Two runs at once: the second refuses.
with gb.Lock(d1):
    try:
        gb.backup(fake, "acme", BK, say)
        check("negative: a second run while another holds the lock refuses", False)
    except gb.BackupError as e:
        check("negative: a second run while another holds the lock refuses", "being updated by another run" in str(e))
open(os.path.join(d1, gb.LOCK), "w").write(f"999999999 {gb.socket.gethostname()} gitea-backup 2026-01-01\n")
gb.backup(fake, "acme", BK, say)
check("a lock whose process is gone (this host) is stale: taken over, and removed after",
      not os.path.exists(os.path.join(d1, gb.LOCK)))

# A run that breaks off leaves the backup as it was.
zip4, db4 = open(os.path.join(d1, "acme.zip"), "rb").read(), open(os.path.join(d1, "acme.db"), "rb").read()
fake.issues["acme/mid"].append({"number": 3, "title": "x", "state": "open", "updated_at": "2026-03-01",
                                "assets": [fake.asset(b"Z" * 5000, "z.bin")]})
real_download = fake.download


def broken(url, out):
    out.write(b"half")
    raise KeyboardInterrupt


fake.download = broken
try:
    gb.backup(fake, "acme", BK, say)
except KeyboardInterrupt:
    pass
fake.download = real_download
check("negative: a run that breaks off leaves the previous backup byte for byte, and nothing else",
      open(os.path.join(d1, "acme.zip"), "rb").read() == zip4 and open(os.path.join(d1, "acme.db"), "rb").read() == db4
      and sorted(os.listdir(d1)) == ["acme.db", "acme.zip"])

# No room for a copy of the zip: the new entries are appended, and a later run with room
# writes it again without the dead bytes.
real_free = gb.disk_free
gb.disk_free = lambda p: 1
fake.issues["acme/mid"].append({"number": 4, "title": "y", "state": "open", "updated_at": "2026-03-02",
                                "assets": [fake.asset(b"Y" * 7000, "y.bin")]})
said.clear()
gb.backup(fake, "acme", BK, say)
gb.disk_free = real_free
with zipfile.ZipFile(os.path.join(d1, "acme.zip")) as z:
    ok = z.testzip() is None and any(n.endswith("-y.bin") for n in z.namelist()) and any(n.endswith("-z.bin") for n in z.namelist())
check("no room for a copy: the new entries are appended, the zip whole", ok and any("appending" in m for m in said)
      and sorted(os.listdir(d1)) == ["acme.db", "acme.zip"])

# Omi's save that broke off is rolled back as Omi does it.
zp = os.path.join(d1, "acme.zip")
whole = open(zp, "rb").read()
open(zp, "ab").write(b"half of an Omi save")
open(zp + gb.OMI_JOURNAL, "w").write(str(len(whole)))
said.clear()
gb.backup(fake, "acme", BK, say)
check("an Omi save that broke off (its .omi-save journal) is rolled back first",
      any("Omi save" in m for m in said) and not os.path.exists(zp + gb.OMI_JOURNAL) and zipfile.ZipFile(zp).testzip() is None)

# --- migrate all of Gitea into backups ------------------------------------------------------------

shutil.rmtree(GIT)
shutil.rmtree(os.path.join(TMP, "work", "acme"))
shutil.rmtree(os.path.join(TMP, "work", "carol"))
shutil.rmtree(os.path.join(TMP, "work", "admin"))
fake = FakeGitea()
fake.packages["acme"] = [{"type": "generic", "name": "pkg", "version": "1", "repository": {"full_name": "acme/mid"}}]
MG = os.path.join(TMP, "migrate")
said.clear()
r = gb.migrate_all(fake, MG, say, dry_run=True, excludes=["carol/notes"])
check("migrate --dry-run: smallest first, nothing changed", r["plan"] == ["acme/small", "admin/own", "acme/mid", "acme/big"]
      and len(fake.repos) == 5 and not os.path.exists(os.path.join(MG, "acme")))
r = gb.migrate_all(fake, MG, say, confirm=("migrate all", fake.root))
check("negative: without the typed phrase nothing is done", r.get("cancelled") and len(fake.repos) == 5)
r = gb.migrate_all(fake, MG, say, confirm=(gb.MIGRATE_PHRASE, "http://another-gitea"))
check("negative: without the Gitea address typed nothing is done", r.get("cancelled") and len(fake.repos) == 5)

# acme/big does not fit; acme/mid fails its verification (an issue appears after its backup).
big_need = [p for p in gb.migration_plan(fake)[0] if p["full"] == "acme/big"][0]["need"]
gb.disk_free = lambda p: big_need - 1
fake.extra_issue = ("acme/mid", {"number": 9, "title": "late", "state": "open", "assets": []}, False)
real_backup = gb.backup


def backup_then_late(*a, **k):
    out = real_backup(*a, **k)
    if fake.extra_issue and k.get("only") == ["acme/mid"]:
        fake.extra_issue = fake.extra_issue[:2] + (True,)
    return out


gb.backup = backup_then_late
said.clear()
r = gb.migrate_all(fake, MG, say, confirm=(gb.MIGRATE_PHRASE, fake.root), excludes=["carol/notes"])
print("\n".join("      " + m for m in said)) if os.environ.get("DEBUG") else None
check("migrate: stops at the first failed verification; the one before it is done",
      r["done"] == ["acme/small", "admin/own"] and r["failed"] == ["acme/mid"])
check("negative: a repository whose verification failed stays in Gitea", "acme/mid" in fake.repos)
check("each repository is deleted only after it was verified", "acme/small" not in fake.repos
      and any("verified:" in m for m in said))
check("the token's own user is backed up and left in Gitea", "admin" in fake.users and "admin" in r["owners_left"]
      and gb.is_single(os.path.join(MG, "admin")))
fake.extra_issue = None
gb.backup = real_backup
said.clear()
r = gb.migrate_all(fake, MG, say, confirm=(gb.MIGRATE_PHRASE, fake.root), excludes=["carol/notes"])
check("migrate again: goes on - done ones are gone, the failed one backed up again, verified, deleted",
      r["done"] == ["acme/mid"] and "acme/mid" not in fake.repos)
check("the disk-space check skips a repository that does not fit, and says so", r["skipped"] == ["acme/big"]
      and "acme/big" in fake.repos and any("does not fit" in m for m in said))
check("negative: an owner that still has repositories is not deleted", "acme" in fake.orgs)
check("the package of a migrated repository is deleted with it", fake.packages["acme"] == [])
gb.disk_free = real_free
r = gb.migrate_all(fake, MG, say, confirm=(gb.MIGRATE_PHRASE, fake.root), excludes=["carol/notes"])
b = gb.Backup(os.path.join(MG, "acme"))
check("after the owner's last repository: its own data is backed up and it is deleted",
      r["done"] == ["acme/big"] and "acme" not in fake.orgs and "acme" in r["owners_deleted"]
      and [x["name"] for x in b.repos()] == ["big", "mid", "small"] and b.rows("SELECT name FROM teams")
      and not b.rows("SELECT 1 FROM repos WHERE seen < ?", int(b.meta["runs"])))
b.close()
check("excludes are kept: carol/notes and carol stay in Gitea", "carol/notes" in fake.repos and "carol" in fake.users)
gtg_src = open(os.path.join(ROOT, "mirror", "github-to-gitea", "github-to-gitea.py"), encoding="utf-8").read()
check("github-to-gitea: --combine-backups, --list-old-only, --delete-old-only, --migrate-all-gitea, --dry-run",
      all(x in gtg_src for x in ("--combine-backups", "--list-old-only", "--delete-old-only", "--migrate-all-gitea",
                                 "--continue-on-error", '"--dry-run"')))
shutil.rmtree(TMP, ignore_errors=True)

# --- the round trip against a disposable Gitea --------------------------------------------

URL, TOKEN, USER = (os.environ.get(k) for k in ("GITEA_TEST_URL", "GITEA_TEST_TOKEN", "GITEA_TEST_USER"))
if not (URL and TOKEN and USER):
    print("skip the round trip: GITEA_TEST_URL, GITEA_TEST_TOKEN and GITEA_TEST_USER are not set")
    sys.exit(1 if failures else 0)

cl = gb.Client(URL, TOKEN)
ORG = "backuptestorg"
say = lambda m: print("   ", m)  # noqa: E731


def req(method, path, body=None, sudo=None, want=(200, 201, 204)):
    s, d = cl.request(method, path, body, sudo=sudo)
    if s not in want:
        raise SystemExit(f"{method} {path}: {s} {d}")
    return d


def upload(path, name, data, sudo=None):
    import io
    s, d = cl.upload(path, name, io.BytesIO(data), len(data), sudo=sudo)
    if s != 201:
        raise SystemExit(f"upload {path}: {s} {d}")
    return d


def gitrun(*args, cwd=None):
    r = subprocess.run(["git"] + list(args), cwd=cwd, env=dict(cl.git_env(), GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                       GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t"), capture_output=True, text=True)
    if r.returncode:
        raise SystemExit(f"git {args}: {r.stdout}{r.stderr}")
    return r.stdout.strip()


cl.request("DELETE", f"/repos/{ORG}/proj")
cl.request("DELETE", f"/repos/{ORG}/empty")
cl.request("DELETE", f"/orgs/{ORG}")
req("POST", "/orgs", {"username": ORG, "full_name": "Backup test", "description": "for the test"})
req("POST", f"/orgs/{ORG}/repos", {"name": "proj", "description": "a project", "default_branch": "main"})
req("POST", f"/orgs/{ORG}/repos", {"name": "empty"})
req("POST", f"/orgs/{ORG}/teams", {"name": "devs", "permission": "write", "units": ["repo.code", "repo.issues"]})
work = tempfile.mkdtemp(dir=os.environ.get("TMPDIR"))
try:
    w = os.path.join(work, "w")
    gitrun("init", "-q", "-b", "main", w)
    open(os.path.join(w, "a.txt"), "w").write("one\n")
    gitrun("add", ".", cwd=w)
    gitrun("commit", "-qm", "one", cwd=w)
    gitrun("checkout", "-qb", "feature", cwd=w)
    open(os.path.join(w, "b.txt"), "w").write("two\n")
    gitrun("add", ".", cwd=w)
    gitrun("commit", "-qm", "two", cwd=w)
    gitrun("checkout", "-qb", "open-feature", cwd=w)
    open(os.path.join(w, "c.txt"), "w").write("three\n")
    gitrun("add", ".", cwd=w)
    gitrun("commit", "-qm", "three", cwd=w)
    gitrun("checkout", "-q", "main", cwd=w)
    gitrun("tag", "v1.0", cwd=w)
    gitrun("push", "-q", cl.git_url(f"{ORG}/proj"), "main", "feature", "open-feature", "v1.0", cwd=w)
    full = f"{ORG}/proj"
    check("the pushed branches are listed once Gitea's push queue has run",
          gb.wait_branches(cl, full, ["main", "feature", "open-feature"]))
    req("PUT", f"/repos/{full}/collaborators/{USER}", {"permission": "write"})
    req("PUT", f"/repos/{full}/topics", {"topics": ["kanban", "backup"]})
    bug = req("POST", f"/repos/{full}/labels", {"name": "bug", "color": "#ee0701"})
    ms = req("POST", f"/repos/{full}/milestones", {"title": "v1", "description": "first"})
    i1 = req("POST", f"/repos/{full}/issues", {"title": "First issue", "body": "Body of one", "labels": [bug["id"]],
                                               "milestone": ms["id"]}, sudo=USER)
    a1 = upload(f"/repos/{full}/issues/1/assets", "shot.png", b"\x89PNG fake image bytes", sudo=USER)
    req("PATCH", f"/repos/{full}/issues/1", {"body": f"Body of one ![shot](/attachments/{a1['uuid']})"})
    c1 = req("POST", f"/repos/{full}/issues/1/comments", {"body": "A comment"}, sudo=USER)
    upload(f"/repos/{full}/issues/comments/{c1['id']}/assets", "log.txt", b"comment file\n" * 100)
    req("POST", f"/repos/{full}/pulls", {"title": "Merged PR", "head": "feature", "base": "main", "body": "merge me"})
    req("POST", f"/repos/{full}/pulls/2/merge", {"Do": "merge"})
    team = [x for x in req("GET", f"/orgs/{ORG}/teams") if x["name"] == "devs"][0]
    req("PUT", f"/teams/{team['id']}/members/{USER}")
    req("PUT", f"/teams/{team['id']}/repos/{ORG}/proj")
    req("POST", f"/repos/{full}/issues", {"title": "Deleted one"})
    req("DELETE", f"/repos/{full}/issues/3")
    req("POST", f"/repos/{full}/pulls", {"title": "Open PR", "head": "open-feature", "base": "main"}, sudo=USER)
    req("POST", f"/repos/{full}/issues", {"title": "Closed issue", "closed": True})
    req("PATCH", f"/repos/{full}/issues/5", {"state": "closed"})
    rel = req("POST", f"/repos/{full}/releases", {"tag_name": "v1.0", "name": "Version 1", "body": "notes"})
    big = bytes(range(256)) * 4096  # 1 MB
    upload(f"/repos/{full}/releases/{rel['id']}/assets", "wekan-1.0.zip", big)
    req("POST", f"/repos/{full}/wiki/new", {"title": "Home", "content_base64": base64.b64encode(b"Wiki home\n").decode()})
    before_refs = gitrun("ls-remote", cl.git_url(full))

    # Backup.
    root = os.path.join(work, "backups")
    d = gb.backup(cl, ORG, root, say)
    check("backup: the one backup <dir>/<name>/<name>.db and <name>.zip",
          d == os.path.join(root, ORG) and sorted(os.listdir(d)) == [f"{ORG}.db", f"{ORG}.zip"])
    db = sqlite3.connect(os.path.join(d, f"{ORG}.db"))
    q = lambda sql, *a: db.execute(sql, a).fetchall()  # noqa: E731
    check("backup: complete, of an organization", dict(q("SELECT key, value FROM meta"))["complete"] == "yes"
          and q("SELECT kind FROM owner")[0][0] == "organization")
    check("backup: both repositories, the empty one marked empty",
          q("SELECT name, empty FROM repos ORDER BY name") == [("empty", 1), ("proj", 0)])
    check("backup: issues and pull requests by number, the deleted number missing",
          q("SELECT number, is_pull, state FROM issues WHERE repo = ? ORDER BY number", full)
          == [(1, 0, "open"), (2, 1, "closed"), (4, 1, "open"), (5, 0, "closed")])
    check("backup: the merged pull request's head and base", json.loads(q(
        "SELECT json FROM pulls WHERE number = 2")[0][0])["merged"] is True)
    check("backup: comments, labels, milestones, releases, teams",
          len(q("SELECT 1 FROM comments")) >= 1 and q("SELECT name FROM labels WHERE repo = ?", full) == [("bug",)]
          and len(q("SELECT 1 FROM milestones")) == 1 and q("SELECT tag FROM releases") == [("v1.0",)]
          and ("devs",) in q("SELECT name FROM teams"))
    z = zipfile.ZipFile(os.path.join(d, f"{ORG}.zip"))
    names = set(z.namelist())
    check("backup zip: git, wiki and every file in folders",
          {"proj/git.bundle", "proj/wiki.bundle", "proj/releases/v1.0/wekan-1.0.zip"} <= names
          and any(n.startswith("proj/issues/1/") and n.endswith("-shot.png") for n in names)
          and any(n.startswith(f"proj/comments/1/{c1['id']}/") and n.endswith("-log.txt") for n in names))
    check("backup zip: every entry STORED", all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist()))
    rows = q("SELECT path, size, sha256 FROM files WHERE kind = 'releases'")
    check("backup: a file's size and sha256 are in the .db, and match the zip",
          rows[0][1] == len(big) and rows[0][2] == hashlib.sha256(big).hexdigest()
          and hashlib.sha256(z.read(rows[0][0])).hexdigest() == rows[0][2])
    db.close()
    z.close()

    # A second run with nothing changed: nothing downloaded, the zip not written.
    zip1 = open(os.path.join(d, f"{ORG}.zip"), "rb").read()
    cl.downloads, cl.requests = [0, 0], 0
    gb.backup(cl, ORG, root, say)
    print(f"    unchanged second run: {cl.requests} requests, {cl.downloads[0]} downloads")
    check("Gitea, second run, nothing changed: no download, every bundle kept, the zip byte-identical",
          cl.downloads == [0, 0] and gb.backup.stats.git_same == 2 and gb.backup.stats.git_fetched == 0
          and open(os.path.join(d, f"{ORG}.zip"), "rb").read() == zip1)
    # Gitea changes: a new comment with a file. Only it is downloaded.
    c2 = req("POST", f"/repos/{full}/issues/1/comments", {"body": "A later comment"}, sudo=USER)
    later = b"later file " * 3000
    upload(f"/repos/{full}/issues/comments/{c2['id']}/assets", "later.txt", later, sudo=USER)
    size1 = os.path.getsize(os.path.join(d, f"{ORG}.zip"))
    cl.downloads = [0, 0]
    gb.backup(cl, ORG, root, say)
    size2 = os.path.getsize(os.path.join(d, f"{ORG}.zip"))
    print(f"    third run: zip {size1} -> {size2} bytes, {cl.downloads[0]} downloads ({cl.downloads[1]} bytes)")
    check("Gitea, third run: only the new file is downloaded, and the zip grew by it only",
          cl.downloads == [1, len(later)] and 0 < size2 - size1 <= len(later) + 1024
          and gb.backup.stats.new["comments"] == 1)
    # The comment is deleted in Gitea: the backup keeps it, as old data.
    req("DELETE", f"/repos/{full}/issues/comments/{c2['id']}")
    gb.backup(cl, ORG, root, say)
    runs, old, paths = gb.old_only(d)
    check("Gitea: a comment deleted in Gitea stays in the backup, with its file - as old data",
          [r["id"] for r in old.get("comments", [])] == [c2["id"]] and len(paths) == 1 and runs == 4)
    check("Gitea: and it is deleted from the backup only when the name is typed",
          gb.delete_old_only(os.path.dirname(d), ORG, say, ask=lambda *a: "no") is None
          and gb.old_only(d)[1].get("comments"))
    gb.delete_old_only(os.path.dirname(d), ORG, say, confirm=ORG)
    check("Gitea: deleted, and every other row kept", not gb.old_only(d)[1].get("comments") and
          len(gb.Backup(d).rows("SELECT 1 FROM comments")) == 1)

    # Deleting needs the name typed (the menu), and then removes everything.
    answers = iter(["2", ORG, "no", "not-the-name", "0"])
    gb.menu(cl, lambda *a: next(answers), say, root)
    check("negative: delete without the name typed deletes nothing", cl.request("GET", f"/orgs/{ORG}")[0] == 200)
    gb.delete_owner(cl, ORG, say)
    check("delete: the organization and its repositories are gone",
          cl.request("GET", f"/orgs/{ORG}")[0] == 404 and cl.request("GET", f"/repos/{full}")[0] == 404)

    # Restore into Gitea.
    gb.restore_gitea(cl, d, ORG, lambda *a: "1", say)
    issues = {i["number"]: i for i in gb.ga.paged(cl.request, f"/repos/{full}/issues?state=all")}
    check("restore: the same numbers, titles and states", {n: (i["title"], i["state"]) for n, i in issues.items()
          if n != 3} == {1: ("First issue", "open"), 2: ("Merged PR", "closed"), 4: ("Open PR", "open"),
                         5: ("Closed issue", "closed")})
    check("restore: a deleted number becomes a closed placeholder", issues[3]["state"] == "closed")
    check("restore: pull requests are pull requests, the merged one merged",
          issues[2].get("pull_request") and req("GET", f"/repos/{full}/pulls/2")["merged"]
          and issues[4].get("pull_request"))
    check("restore: the author is kept (Sudo)", issues[1]["user"]["login"] == USER and issues[4]["user"]["login"] == USER)
    check("restore: labels and milestone", [x["name"] for x in issues[1]["labels"]] == ["bug"]
          and (issues[1].get("milestone") or {}).get("title") == "v1")
    assets = issues[1].get("assets") or []
    check("restore: the issue's file, and its body points at the new one",
          [a["name"] for a in assets] == ["shot.png"] and assets[0]["uuid"] in issues[1]["body"]
          and a1["uuid"] not in issues[1]["body"])
    com = req("GET", f"/repos/{full}/issues/1/comments")
    check("restore: the comment, by its author, with its file", com and com[0]["user"]["login"] == USER
          and "A comment" in com[0]["body"] and [a["name"] for a in com[0].get("assets") or []] == ["log.txt"])
    rels = req("GET", f"/repos/{full}/releases")
    got = None
    if rels and rels[0].get("assets"):
        import io
        out = io.BytesIO()
        cl.download(cl.file_url(rels[0]["assets"][0]), out)
        got = out.getvalue()
    check("restore: the release and its file, byte for byte", rels and rels[0]["tag_name"] == "v1.0" and got == big)
    after = gitrun("ls-remote", cl.git_url(full))
    keep = lambda text: sorted(l for l in text.splitlines() if "refs/heads/" in l or "refs/tags/" in l)  # noqa: E731
    check("restore: every branch and tag at the same commit", set(keep(before_refs)) <= set(keep(after)))
    check("restore: topics and the wiki", req("GET", f"/repos/{full}/topics")["topics"] == ["backup", "kanban"]
          and cl.request("GET", f"/repos/{full}/wiki/page/Home")[0] == 200)
    team = [x for x in req("GET", f"/orgs/{ORG}/teams") if x["name"] == "devs"]
    check("restore: the team, its member and its repository", team and
          [m["login"] for m in req("GET", f"/teams/{team[0]['id']}/members")] == [USER]
          and [r["name"] for r in req("GET", f"/teams/{team[0]['id']}/repos")] == ["proj"])
    check("restore: the collaborator", cl.request("GET", f"/repos/{full}/collaborators/{USER}")[0] == 204)
    check("restore: allow_manual_merge back as it was", req("GET", f"/repos/{full}")["allow_manual_merge"] is False)
    check("restore: the empty repository", cl.request("GET", f"/repos/{ORG}/empty")[0] == 200)
    log = []
    gb.restore_gitea(cl, d, ORG, lambda *a: "1", log.append)
    check("negative: restoring again leaves existing repositories as they are",
          any("already in Gitea" in m for m in log) and len(list(gb.ga.paged(
              cl.request, f"/repos/{full}/issues?state=all"))) == 5)

    # WeKan.
    zips = gb.wekan_zips(d, os.path.join(d, "wekan"), say)
    proj = [p for p in zips if p.endswith("-proj.zip")][0]
    with zipfile.ZipFile(proj) as wz:
        doc = json.loads(wz.read("wekan.json"))
        entries = [n for n in wz.namelist() if n.startswith("attachments/")]
    check("WeKan: a wekan-board-1.0.0 with a card per issue and pull request",
          doc["_format"] == "wekan-board-1.0.0" and sorted(c["title"] for c in doc["cards"])
          == ["#1 First issue", "#2 Merged PR", "#4 Open PR", "#5 Closed issue"])
    lists = {x["_id"]: x["title"] for x in doc["lists"]}
    check("WeKan: Open, Closed and Merged", {c["title"]: lists[c["listId"]] for c in doc["cards"]}
          == {"#1 First issue": "Open", "#2 Merged PR": "Merged", "#4 Open PR": "Open", "#5 Closed issue": "Closed"})
    check("WeKan: the comment comes with an addComment activity, as the import reads it",
          any(a["activityType"] == "addComment" for a in doc["activities"]) and doc["comments"])
    check("WeKan: each file is attachments/<id>-<name>, the id that its row has",
          sorted(e.split("/")[1].split("-")[0] for e in entries) == sorted(r["_id"] for r in doc["attachments"])
          and all(r.get("url") for r in doc["attachments"]))

    # Migrate a Gitea organization into backups, a repository at a time (only that one:
    # this Gitea may have others).
    MORG = "migratetestorg"
    req("POST", "/orgs", {"username": MORG})
    for name, n in (("tiny", 1), ("larger", 3)):
        req("POST", f"/orgs/{MORG}/repos", {"name": name, "default_branch": "main"})
        mw = os.path.join(work, f"m-{name}")
        gitrun("init", "-q", "-b", "main", mw)
        for k in range(n):
            open(os.path.join(mw, "f.txt"), "a").write(("x" * 20000 + "\n") * (k + 1))
            gitrun("add", ".", cwd=mw)
            gitrun("commit", "-qm", f"c{k}", cwd=mw)
        gitrun("push", "-q", cl.git_url(f"{MORG}/{name}"), "main", cwd=mw)
        req("POST", f"/repos/{MORG}/{name}/issues", {"title": f"{name} issue"})
    gb.wait_branches(cl, f"{MORG}/larger", ["main"])
    rel = req("POST", f"/repos/{MORG}/larger/releases", {"tag_name": "v1", "name": "v1", "target_commitish": "main"})
    upload(f"/repos/{MORG}/larger/releases/{rel['id']}/assets", "big.bin", b"B" * 300000)
    mroot = os.path.join(work, "migrated")
    r = gb.migrate_all(cl, mroot, say, dry_run=True, owners=[MORG])
    check("Gitea migrate --dry-run: smallest first, nothing changed", r["plan"] == [f"{MORG}/tiny", f"{MORG}/larger"]
          and cl.request("GET", f"/repos/{MORG}/tiny")[0] == 200)
    r = gb.migrate_all(cl, mroot, say, confirm=(gb.MIGRATE_PHRASE, cl.root), owners=[MORG])
    check("Gitea migrate: each repository backed up, verified and deleted, then the organization",
          r["done"] == [f"{MORG}/tiny", f"{MORG}/larger"] and cl.request("GET", f"/repos/{MORG}/tiny")[0] == 404
          and cl.request("GET", f"/orgs/{MORG}")[0] == 404 and r["owners_deleted"] == [MORG])
    mb = gb.Backup(os.path.join(mroot, MORG))
    check("Gitea migrate: the backup has both repositories, their issues, bundles and the release file",
          [x["name"] for x in mb.repos()] == ["larger", "tiny"] and len(mb.rows("SELECT 1 FROM issues")) == 2
          and {"tiny/git.bundle", "larger/git.bundle", "larger/releases/v1/big.bin"} <= set(mb.zip.namelist())
          and mb.zip.testzip() is None and not gb.old_only(os.path.join(mroot, MORG))[1].get("repos"))
    mb.close()
finally:
    for name in ("tiny", "larger"):
        cl.request("DELETE", f"/repos/migratetestorg/{name}")
    cl.request("DELETE", "/orgs/migratetestorg")
    cl.request("DELETE", f"/repos/{ORG}/proj")
    cl.request("DELETE", f"/repos/{ORG}/empty")
    cl.request("DELETE", f"/orgs/{ORG}")
    shutil.rmtree(work, ignore_errors=True)

sys.exit(1 if failures else 0)
